#!/usr/bin/env python3
"""Background download and staging for application-initiated updates.

This module deliberately stops before activating an update.  The running
desktop process may safely download and unpack a verified onedir package, but
the launcher owns switching ``current.json``, validating the new process'
ready signal, and rolling back when that validation fails.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections import deque
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable

from app_version import LAUNCHER_PROTOCOL
from update_protocol import (
    APP_ENTRYPOINT as ENTRYPOINT,
    INSTALL_KIND_ENV,
    INSTALL_KIND_LEGACY,
    INSTALL_ROOT_ENV,
    LAUNCHER_PATH_ENV,
    PACKAGE_FORMAT,
    read_current_version,
    safe_version,
    version_key,
    versioned_app_path,
)


PENDING_SCHEMA = 1
DEFAULT_LAUNCHER_BINARY = "三角洲情报助手.exe"
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
DOWNLOAD_RANGE_WINDOW = 4 * 1024 * 1024
DEFAULT_DOWNLOAD_RETRIES = 4
DEFAULT_RETRY_DELAY = 0.4
DEFAULT_STALL_TIMEOUT = 12.0
DEFAULT_SPEED_WINDOW = 5.0
MAX_DOWNLOAD_URLS = 8
MAX_ARCHIVE_MEMBERS = 30_000
MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024
MAX_RELEASE_HISTORY = 32
MAX_RELEASE_NOTES = 32
MAX_RELEASE_NOTE_LENGTH = 500
_SHA256_HEX = frozenset("0123456789abcdef")
_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)


class UpdateCancelled(RuntimeError):
    """Raised internally when a requested background update is cancelled."""


class UpdateProtocolError(RuntimeError):
    """Raised when the server manifest or package violates the update protocol."""


class DownloadInterrupted(RuntimeError):
    """Raised when a response ends before its declared byte range is complete."""


def default_install_root() -> Path:
    """Resolve the portable install root from legacy and onedir app layouts."""

    configured = os.environ.get(INSTALL_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).resolve()
    executable = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
    for parent in executable.parents:
        if parent.name.casefold() == "app":
            return parent.parent
    return executable.parent


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_release_filename(value: Any, suffix: str, field: str) -> str:
    filename = str(value or "").strip()
    if (
        not filename
        or Path(filename).name != filename
        or PureWindowsPath(filename).name != filename
        or not filename.casefold().endswith(suffix.casefold())
    ):
        raise UpdateProtocolError(f"更新清单中的 {field} 无效")
    return filename


def _positive_int(value: Any, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise UpdateProtocolError(f"更新清单中的 {field} 无效") from exc
    if parsed <= 0:
        raise UpdateProtocolError(f"更新清单中的 {field} 无效")
    return parsed


def _sha256(value: Any, field: str) -> str:
    digest = str(value or "").strip().lower()
    if len(digest) != 64 or any(character not in _SHA256_HEX for character in digest):
        raise UpdateProtocolError(f"更新清单中的 {field} 无效")
    return digest


def _release_notes(value: Any) -> list[str]:
    """Validate optional manifest notes before exposing them to the UI."""

    if value is None:
        # Releases published before notes were introduced remain updateable.
        return []
    if not isinstance(value, list) or len(value) > MAX_RELEASE_NOTES:
        raise UpdateProtocolError("更新清单中的 release_notes 无效")

    notes: list[str] = []
    for note in value:
        if not isinstance(note, str):
            raise UpdateProtocolError("更新清单中的 release_notes 无效")
        normalized = note.strip()
        if not normalized or len(normalized) > MAX_RELEASE_NOTE_LENGTH:
            raise UpdateProtocolError("更新清单中的 release_notes 无效")
        notes.append(normalized)
    return notes


def _release_history(
    value: Any,
    *,
    current_version: str,
    target_version: str,
) -> tuple[list[dict[str, Any]], list[str] | None]:
    """Validate history and return newer records plus the target's notes."""

    if value is None:
        # Manifests published before grouped history remain updateable.
        return [], None
    if not isinstance(value, list) or len(value) > MAX_RELEASE_HISTORY:
        raise UpdateProtocolError("更新清单中的 release_history 无效")

    current_key = version_key(current_version)
    target_key = version_key(target_version)
    previous_key: tuple[int, ...] | None = None
    seen_versions: set[str] = set()
    target_in_history = False
    target_notes: list[str] | None = None
    history: list[dict[str, Any]] = []

    for record in value:
        if not isinstance(record, dict):
            raise UpdateProtocolError("更新清单中的 release_history 无效")
        try:
            version = safe_version(record.get("version"))
        except ValueError as exc:
            raise UpdateProtocolError("更新清单中的 release_history 无效") from exc
        record_key = version_key(version)
        if (
            version in seen_versions
            or (previous_key is not None and record_key <= previous_key)
            or record_key > target_key
        ):
            raise UpdateProtocolError("更新清单中的 release_history 无效")
        notes = _release_notes(record.get("release_notes"))
        if not notes:
            raise UpdateProtocolError("更新清单中的 release_history 无效")

        seen_versions.add(version)
        previous_key = record_key
        if version == target_version:
            target_in_history = True
            target_notes = notes
        if record_key > current_key:
            history.append({"version": version, "release_notes": notes})

    if value and not target_in_history:
        raise UpdateProtocolError("更新清单中的 release_history 无效")
    return history, target_notes


def _file_matches(path: Path, expected_size: int, expected_sha256: str) -> bool:
    try:
        if not path.is_file() or path.stat().st_size != expected_size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(DOWNLOAD_CHUNK_SIZE):
                digest.update(chunk)
        return digest.hexdigest().lower() == expected_sha256
    except OSError:
        return False


class AppUpdater:
    """Stateful bridge service for checking, downloading, and staging updates."""

    def __init__(
        self,
        version_url: str,
        *,
        current_version: str,
        install_root: Path | str | None = None,
        launcher_path: Path | str | None = None,
        request_timeout: float = 8.0,
        download_timeout: float = 180.0,
        stall_timeout: float = DEFAULT_STALL_TIMEOUT,
        max_download_retries: int = DEFAULT_DOWNLOAD_RETRIES,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        speed_window: float = DEFAULT_SPEED_WINDOW,
        opener: Callable[..., Any] = urllib.request.urlopen,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.version_url = str(version_url).strip()
        if not self.version_url:
            raise ValueError("version_url is required")
        try:
            self.current_version = safe_version(current_version)
        except ValueError as exc:
            raise UpdateProtocolError(str(exc)) from exc
        self.install_root = Path(install_root or default_install_root()).resolve()
        self.app_root = self.install_root / "app"
        self.versions_root = self.app_root / "versions"
        self.current_path = self.app_root / "current.json"
        self.pending_path = self.app_root / "pending.json"
        self.updates_root = self.install_root / ".updates"
        configured_launcher = os.environ.get(LAUNCHER_PATH_ENV, "").strip()
        expected_launcher = (self.install_root / DEFAULT_LAUNCHER_BINARY).resolve()
        resolved_launcher = Path(
            launcher_path or configured_launcher or self.install_root / DEFAULT_LAUNCHER_BINARY
        ).resolve()
        if resolved_launcher != expected_launcher:
            raise UpdateProtocolError("启动器路径不在受控安装目录中")
        self.launcher_path = expected_launcher
        self.request_timeout = request_timeout
        self.download_timeout = download_timeout
        self.stall_timeout = max(0.1, min(float(stall_timeout), float(download_timeout)))
        self.max_download_retries = max(0, int(max_download_retries))
        self.retry_delay = max(0.0, float(retry_delay))
        self.speed_window = max(0.1, float(speed_window))
        self._opener = opener
        self._clock = clock
        self._lock = threading.RLock()
        self._cancel_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._checking = False
        self._manifest: dict[str, Any] | None = None
        self._plan: dict[str, Any] | None = None
        self._download_started_at: float | None = None
        self._download_received = 0
        self._last_progress_at: float | None = None
        self._speed_samples: deque[tuple[float, int]] = deque()
        self._state: dict[str, Any] = {
            "ok": True,
            "quiet": True,
            "phase": "idle",
            "available": False,
            "current_version": self.current_version,
            "latest_version": None,
            "reason": None,
            "downloaded": 0,
            "download_size": 0,
            "completed": 0,
            "total": 0,
            "progress": 0.0,
            "recent_speed_bps": 0.0,
            "average_speed_bps": 0.0,
            "speed_window_seconds": self.speed_window,
            "retry_count": 0,
            "retry_limit": self.max_download_retries,
            "retrying": False,
            "stalled": False,
            "download_source_index": 0,
            "download_source_count": 0,
            "message": "",
            "error": None,
            "release_notes": [],
            "release_history": [],
        }

    def get_update_status(self) -> dict[str, Any]:
        with self._lock:
            self._refresh_speed_locked()
            return copy.deepcopy(self._state)

    def check_for_update(self, manual: bool = False) -> dict[str, Any]:
        """Check once; ``manual=False`` keeps automatic failures quiet."""

        quiet = not bool(manual)
        with self._lock:
            if self._checking or (self._worker and self._worker.is_alive()):
                return copy.deepcopy(self._state)
            self._checking = True
            self._manifest = None
            self._plan = None
            self._download_started_at = None
            self._download_received = 0
            self._last_progress_at = None
            self._speed_samples.clear()
            self._state.update(
                ok=True,
                quiet=quiet,
                phase="checking",
                available=False,
                reason=None,
                download_size=0,
                downloaded=0,
                total=0,
                completed=0,
                progress=0.0,
                recent_speed_bps=0.0,
                average_speed_bps=0.0,
                retry_count=0,
                retrying=False,
                stalled=False,
                download_source_index=0,
                download_source_count=0,
                message="正在检查更新…",
                error=None,
                release_notes=[],
                release_history=[],
            )
        try:
            manifest = self._fetch_manifest()
            plan = self._build_plan(manifest)
        except Exception as exc:
            with self._lock:
                self._checking = False
                return self._record_error_locked(exc, quiet=quiet, code="check_failed")

        with self._lock:
            self._checking = False
            self._manifest = manifest
            self._plan = plan
            available = bool(plan["available"])
            self._state.update(
                ok=True,
                quiet=quiet,
                phase="available" if available else "up_to_date",
                available=available,
                latest_version=plan["version"],
                reason=plan["reason"],
                download_size=plan["download_size"],
                downloaded=0,
                total=plan["download_size"],
                completed=0,
                progress=0.0,
                package_format=plan["package_format"],
                needs_launcher=plan["needs_launcher"],
                release_notes=list(plan["release_notes"]),
                release_history=copy.deepcopy(plan["release_history"]),
                message=(
                    f"发现新版本 v{plan['version']}"
                    if plan["reason"] == "version"
                    else "可完成快速启动组件升级"
                    if available
                    else "已是最新版本"
                ),
                error=None,
            )
            return copy.deepcopy(self._state)

    def start_update(self) -> dict[str, Any]:
        """Start background staging and return immediately."""

        with self._lock:
            if self._checking or (self._worker and self._worker.is_alive()):
                return copy.deepcopy(self._state)
            if self._state.get("phase") == "ready":
                return copy.deepcopy(self._state)
            if not self._manifest or not self._plan or not self._plan.get("available"):
                return self._record_error_locked(
                    UpdateProtocolError("请先检查更新"),
                    quiet=False,
                    code="not_checked",
                )
            self._cancel_event = threading.Event()
            now = self._clock()
            self._download_started_at = now
            self._download_received = 0
            self._last_progress_at = None
            self._speed_samples.clear()
            self._speed_samples.append((now, 0))
            self._state.update(
                ok=True,
                quiet=False,
                phase="downloading",
                downloaded=0,
                completed=0,
                progress=0.0,
                recent_speed_bps=0.0,
                average_speed_bps=0.0,
                retry_count=0,
                retry_limit=self.max_download_retries,
                retrying=False,
                stalled=False,
                download_source_index=0,
                download_source_count=0,
                message="正在下载更新…",
                error=None,
            )
            self._worker = threading.Thread(
                target=self._stage_worker,
                args=(copy.deepcopy(self._manifest), copy.deepcopy(self._plan)),
                name="delta-app-updater",
                daemon=True,
            )
            self._worker.start()
            return copy.deepcopy(self._state)

    def cancel_update(self) -> dict[str, Any]:
        with self._lock:
            if self._worker and self._worker.is_alive() and self._state.get("phase") in {
                "downloading",
                "verifying",
                "extracting",
            }:
                self._cancel_event.set()
                self._state.update(
                    phase="cancelling",
                    message="正在取消更新…",
                )
            return copy.deepcopy(self._state)

    def _fetch_manifest(self) -> dict[str, Any]:
        request = urllib.request.Request(
            self.version_url,
            headers={
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "User-Agent": "DeltaStatsAssistant-Updater/2",
            },
        )
        with self._opener(request, timeout=self.request_timeout) as response:
            body = response.read()
        try:
            manifest = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise UpdateProtocolError("更新清单不是有效的 JSON") from exc
        if not isinstance(manifest, dict):
            raise UpdateProtocolError("更新清单格式无效")
        return manifest

    def _build_plan(self, manifest: dict[str, Any]) -> dict[str, Any]:
        try:
            version = safe_version(manifest.get("version"))
        except ValueError as exc:
            raise UpdateProtocolError(str(exc)) from exc
        latest_key = version_key(version)
        current_key = version_key(self.current_version)
        if latest_key < current_key:
            # A production rollback must never make a newer installed client
            # parse or download an older package.  Only the manifest version is
            # relevant on this path; legacy package fields may be absent.
            return {
                "available": False,
                "reason": None,
                "version": version,
                "package_format": None,
                "package_file": None,
                "package_size": 0,
                "package_sha256": None,
                "package_urls": [],
                "entrypoint": ENTRYPOINT,
                "launcher_protocol": None,
                "needs_launcher": False,
                "launcher": None,
                "download_size": 0,
                "release_notes": [],
                "release_history": [],
            }
        package_format = str(manifest.get("package_format", "")).strip()
        if package_format != PACKAGE_FORMAT:
            raise UpdateProtocolError("服务器未提供可用的快速启动更新包")
        entrypoint = str(manifest.get("entrypoint", ENTRYPOINT)).strip()
        if entrypoint != ENTRYPOINT:
            raise UpdateProtocolError("更新包入口程序无效")
        package_file = _safe_release_filename(manifest.get("package_file"), ".zip", "package_file")
        package_size = _positive_int(manifest.get("package_size"), "package_size")
        package_sha256 = _sha256(manifest.get("package_sha256"), "package_sha256")
        package_urls = self._artifact_urls(manifest, "package", package_file)
        try:
            desired_launcher_protocol = int(manifest.get("launcher_protocol", 0))
        except (TypeError, ValueError) as exc:
            raise UpdateProtocolError("更新清单中的 launcher_protocol 无效") from exc
        if desired_launcher_protocol < LAUNCHER_PROTOCOL:
            raise UpdateProtocolError("启动器协议版本过低")

        layout_matches = self._layout_matches(version, package_format)
        if latest_key > current_key:
            available = True
            reason = "version"
        else:
            available = not layout_matches
            reason = "layout" if available else None

        needs_launcher = available and self._needs_launcher_stage(manifest, desired_launcher_protocol)
        if available:
            release_history, target_release_notes = _release_history(
                manifest.get("release_history"),
                current_version=self.current_version,
                target_version=version,
            )
            release_notes = (
                target_release_notes
                if target_release_notes is not None
                else _release_notes(manifest.get("release_notes"))
            )
        else:
            release_notes = []
            release_history = []
        launcher: dict[str, Any] | None = None
        if needs_launcher:
            launcher_file = _safe_release_filename(
                manifest.get("launcher_file"),
                ".exe",
                "launcher_file",
            )
            launcher = {
                "file": launcher_file,
                "size": _positive_int(manifest.get("launcher_size"), "launcher_size"),
                "sha256": _sha256(manifest.get("launcher_sha256"), "launcher_sha256"),
                "version": str(manifest.get("launcher_version", version)).strip(),
                "urls": self._artifact_urls(manifest, "launcher", launcher_file),
            }
            try:
                launcher["version"] = safe_version(launcher["version"])
            except ValueError as exc:
                raise UpdateProtocolError(str(exc)) from exc

        return {
            "available": available,
            "reason": reason,
            "version": version,
            "package_format": package_format,
            "package_file": package_file,
            "package_size": package_size,
            "package_sha256": package_sha256,
            "package_urls": package_urls,
            "entrypoint": entrypoint,
            "launcher_protocol": desired_launcher_protocol,
            "needs_launcher": needs_launcher,
            "launcher": launcher,
            "download_size": package_size + (launcher["size"] if launcher else 0) if available else 0,
            "release_notes": release_notes,
            "release_history": release_history,
        }

    def _artifact_urls(
        self,
        manifest: dict[str, Any],
        artifact: str,
        fallback_file: str,
    ) -> list[str]:
        list_field = f"{artifact}_urls"
        single_field = f"{artifact}_url"
        raw_urls = manifest.get(list_field)
        if raw_urls is None:
            single_url = manifest.get(single_field)
            raw_urls = [single_url] if single_url is not None else [fallback_file]
        if (
            not isinstance(raw_urls, list)
            or not raw_urls
            or len(raw_urls) > MAX_DOWNLOAD_URLS
        ):
            raise UpdateProtocolError(f"更新清单中的 {list_field} 无效")

        manifest_scheme = urllib.parse.urlparse(self.version_url).scheme.casefold()
        resolved_urls: list[str] = []
        for raw_url in raw_urls:
            if not isinstance(raw_url, str) or not raw_url.strip() or len(raw_url) > 2048:
                raise UpdateProtocolError(f"更新清单中的 {list_field} 无效")
            resolved = urllib.parse.urljoin(self.version_url, raw_url.strip())
            parsed = urllib.parse.urlparse(resolved)
            if (
                parsed.scheme.casefold() not in {"http", "https"}
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.fragment
                or (manifest_scheme == "https" and parsed.scheme.casefold() != "https")
            ):
                raise UpdateProtocolError(f"更新清单中的 {list_field} 无效")
            if resolved not in resolved_urls:
                resolved_urls.append(resolved)
        if not resolved_urls:
            raise UpdateProtocolError(f"更新清单中的 {list_field} 无效")
        return resolved_urls

    def _layout_matches(self, version: str, package_format: str) -> bool:
        if os.environ.get(INSTALL_KIND_ENV, "").strip() == INSTALL_KIND_LEGACY:
            return False
        current = read_current_version(self.app_root)
        return bool(
            current == version
            and package_format == PACKAGE_FORMAT
            and versioned_app_path(self.app_root, version).is_file()
        )

    def _needs_launcher_stage(self, manifest: dict[str, Any], desired_protocol: int) -> bool:
        if desired_protocol < LAUNCHER_PROTOCOL:
            raise UpdateProtocolError("启动器协议版本过低")
        launcher_size = _positive_int(manifest.get("launcher_size"), "launcher_size")
        launcher_sha = _sha256(manifest.get("launcher_sha256"), "launcher_sha256")
        return not _file_matches(self.launcher_path, launcher_size, launcher_sha)

    def _stage_worker(self, manifest: dict[str, Any], plan: dict[str, Any]) -> None:
        package_download: Path | None = None
        staging: Path | None = None
        try:
            total = int(plan["download_size"])
            downloaded = 0
            launcher_stage: Path | None = None
            launcher = plan.get("launcher")
            if launcher:
                launcher_stage = self.updates_root / launcher["file"]
                self._download_verified(
                    launcher["urls"],
                    launcher_stage,
                    expected_size=launcher["size"],
                    expected_sha256=launcher["sha256"],
                    offset=downloaded,
                    total=total,
                )
                downloaded += launcher["size"]

            package_download = self.updates_root / plan["package_file"]
            self._download_verified(
                plan["package_urls"],
                package_download,
                expected_size=plan["package_size"],
                expected_sha256=plan["package_sha256"],
                offset=downloaded,
                total=total,
            )

            self._raise_if_cancelled()
            with self._lock:
                self._state.update(
                    phase="extracting",
                    message="正在解压更新…",
                    progress=1.0,
                    downloaded=total,
                    completed=total,
                    retrying=False,
                    stalled=False,
                )
            staging = self.versions_root / f".staging-{plan['version']}-{uuid.uuid4().hex}"
            self._extract_package(package_download, staging)
            with self._lock:
                # Cancellation is linearized before promotion.  Once the version
                # directory and pending pointer are committed, the result is ready
                # for the launcher and is no longer a cancellable download.
                self._raise_if_cancelled()
                target = self.versions_root / plan["version"]
                self._promote_staging(staging, target)
                staging = None

                pending: dict[str, Any] = {
                    "schema": PENDING_SCHEMA,
                    "version": plan["version"],
                    "package_format": plan["package_format"],
                    "target_dir": f"versions/{plan['version']}",
                    "entrypoint": ENTRYPOINT,
                    "package_sha256": plan["package_sha256"],
                    "launcher_protocol": plan["launcher_protocol"],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                launcher_relative: str | None = None
                if launcher_stage:
                    launcher_relative = launcher_stage.relative_to(self.install_root).as_posix()
                    if not launcher_relative.startswith(".updates/"):
                        raise UpdateProtocolError("启动器暂存路径无效")
                    pending["launcher_stage"] = launcher_relative
                    pending["launcher_version"] = launcher["version"]
                    pending["launcher_sha256"] = launcher["sha256"]
                    pending["launcher_size"] = launcher["size"]
                _write_json_atomic(self.pending_path, pending)
                self._state.update(
                    ok=True,
                    quiet=False,
                    phase="ready",
                    available=True,
                    downloaded=total,
                    completed=total,
                    progress=1.0,
                    retrying=False,
                    stalled=False,
                    message="更新已准备完成，重启后生效",
                    pending_path=str(self.pending_path),
                    launcher_stage=launcher_relative,
                    apply_launcher=str(launcher_stage or self.launcher_path),
                    error=None,
                )
        except UpdateCancelled:
            with self._lock:
                self._state.update(
                    ok=True,
                    quiet=False,
                    phase="cancelled",
                    message="更新已取消",
                    retrying=False,
                    stalled=False,
                    error=None,
                )
        except Exception as exc:
            self._record_error(exc, quiet=False, code="stage_failed")
        finally:
            if staging and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            if package_download:
                package_download.unlink(missing_ok=True)

    def _download_verified(
        self,
        urls: list[str],
        destination: Path,
        *,
        expected_size: int,
        expected_sha256: str,
        offset: int,
        total: int,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if self._matching_file_with_cancellation(destination, expected_size, expected_sha256):
            self._set_download_progress(offset + expected_size, total)
            return
        if not urls:
            raise UpdateProtocolError("更新清单没有提供下载地址")

        temporary = destination.with_name(f".{destination.name}.part")
        if temporary.is_file() and temporary.stat().st_size > expected_size:
            temporary.unlink(missing_ok=True)
        retries = 0
        source_index = 0

        while True:
            self._raise_if_cancelled()
            downloaded = temporary.stat().st_size if temporary.is_file() else 0
            if downloaded == expected_size:
                with self._lock:
                    self._state.update(phase="verifying", message="正在校验更新…")
                self._raise_if_cancelled()
                if self._matching_file_with_cancellation(
                    temporary,
                    expected_size,
                    expected_sha256,
                ):
                    os.replace(temporary, destination)
                    return
                temporary.unlink(missing_ok=True)
                error = UpdateProtocolError("下载文件 SHA-256 校验失败")
                integrity_retry_limit = min(
                    self.max_download_retries,
                    max(0, len(urls) - 1),
                )
                if retries >= integrity_retry_limit:
                    raise error
                retries += 1
                source_index = retries % len(urls)
                self._record_download_retry(
                    error,
                    offset=offset,
                    completed=0,
                    total=total,
                    retry_count=retries,
                    source_index=source_index,
                    source_count=len(urls),
                )
                self._wait_before_retry()
                continue

            range_end = min(expected_size - 1, downloaded + DOWNLOAD_RANGE_WINDOW - 1)
            request = urllib.request.Request(
                urls[source_index],
                headers={
                    "Accept": "application/octet-stream",
                    "Range": f"bytes={downloaded}-{range_end}",
                    "User-Agent": "DeltaStatsAssistant-Updater/3",
                },
            )
            try:
                self._download_range(
                    request,
                    temporary,
                    expected_size=expected_size,
                    range_start=downloaded,
                    range_end=range_end,
                    offset=offset,
                    total=total,
                    source_index=source_index,
                    source_count=len(urls),
                )
            except UpdateCancelled:
                raise
            except Exception as exc:
                retries += 1
                if retries > self.max_download_retries:
                    raise UpdateProtocolError(
                        f"更新下载失败，已重试 {self.max_download_retries} 次：{exc}"
                    ) from exc
                source_index = retries % len(urls)
                current_size = temporary.stat().st_size if temporary.is_file() else 0
                self._record_download_retry(
                    exc,
                    offset=offset,
                    completed=current_size,
                    total=total,
                    retry_count=retries,
                    source_index=source_index,
                    source_count=len(urls),
                )
                self._wait_before_retry()

    def _download_range(
        self,
        request: urllib.request.Request,
        temporary: Path,
        *,
        expected_size: int,
        range_start: int,
        range_end: int,
        offset: int,
        total: int,
        source_index: int,
        source_count: int,
    ) -> None:
        with self._opener(request, timeout=self.stall_timeout) as response:
            content_type = str(response.headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
            if content_type == "text/html":
                raise UpdateProtocolError("下载地址返回了网页，而不是更新文件")

            status = getattr(response, "status", None)
            if status is None:
                getcode = getattr(response, "getcode", None)
                status = getcode() if callable(getcode) else 200
            mode = "ab"
            response_end = expected_size - 1
            completed = range_start
            if int(status) == 206:
                content_range = str(response.headers.get("Content-Range", "")).strip()
                match = _CONTENT_RANGE_RE.fullmatch(content_range)
                if not match:
                    raise UpdateProtocolError("下载线路返回了无效的 Content-Range")
                actual_start = int(match.group(1))
                actual_end = int(match.group(2))
                actual_total = match.group(3)
                if (
                    actual_start != range_start
                    or actual_end < actual_start
                    or actual_end > range_end
                    or (actual_total != "*" and int(actual_total) != expected_size)
                ):
                    raise UpdateProtocolError("下载线路返回的字节范围与请求不一致")
                response_end = actual_end
            elif int(status) == 200:
                # Never append a full response to an existing partial file when
                # an origin ignores Range; restart safely from byte zero.
                mode = "wb"
                completed = 0
                self._set_download_progress(
                    offset,
                    total,
                    source_index=source_index,
                    source_count=source_count,
                )
            else:
                raise UpdateProtocolError(f"下载线路返回了 HTTP {status}")

            with temporary.open(mode) as output:
                while chunk := response.read(DOWNLOAD_CHUNK_SIZE):
                    self._raise_if_cancelled()
                    remaining = response_end + 1 - completed
                    if remaining <= 0 or len(chunk) > remaining:
                        raise UpdateProtocolError("下载线路返回的数据超过声明范围")
                    output.write(chunk)
                    completed += len(chunk)
                    self._set_download_progress(
                        offset + completed,
                        total,
                        received_bytes=len(chunk),
                        source_index=source_index,
                        source_count=source_count,
                    )
            self._raise_if_cancelled()
            if completed != response_end + 1:
                raise DownloadInterrupted(
                    f"下载连接提前结束：期望到字节 {response_end}，实际到 {completed - 1}"
                )

    def _wait_before_retry(self) -> None:
        if self.retry_delay and self._cancel_event.wait(self.retry_delay):
            raise UpdateCancelled("用户取消更新")
        self._raise_if_cancelled()

    @staticmethod
    def _is_stall_error(error: Exception) -> bool:
        if isinstance(error, TimeoutError):
            return True
        if isinstance(error, urllib.error.URLError):
            return isinstance(error.reason, TimeoutError) or "timed out" in str(error.reason).casefold()
        return "timed out" in str(error).casefold()

    def _record_download_retry(
        self,
        error: Exception,
        *,
        offset: int,
        completed: int,
        total: int,
        retry_count: int,
        source_index: int,
        source_count: int,
    ) -> None:
        stalled = self._is_stall_error(error)
        with self._lock:
            self._refresh_speed_locked(force_zero_recent=stalled)
            self._state.update(
                phase="downloading",
                message=(
                    "下载长时间无进展，正在断点重试…"
                    if stalled
                    else "下载连接中断，正在断点重试…"
                ),
                downloaded=min(max(0, offset + completed), max(1, total)),
                download_size=total,
                completed=min(max(0, offset + completed), max(1, total)),
                total=total,
                progress=min(1.0, max(0.0, (offset + completed) / max(1, total))),
                retry_count=retry_count,
                retry_limit=self.max_download_retries,
                retrying=True,
                stalled=stalled,
                download_source_index=source_index + 1,
                download_source_count=source_count,
            )

    def _matching_file_with_cancellation(
        self,
        path: Path,
        expected_size: int,
        expected_sha256: str,
    ) -> bool:
        try:
            if not path.is_file() or path.stat().st_size != expected_size:
                return False
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(DOWNLOAD_CHUNK_SIZE):
                    self._raise_if_cancelled()
                    digest.update(chunk)
            return digest.hexdigest().lower() == expected_sha256
        except OSError:
            return False

    def _set_download_progress(
        self,
        completed: int,
        total: int,
        *,
        received_bytes: int = 0,
        source_index: int = 0,
        source_count: int = 1,
    ) -> None:
        with self._lock:
            now = self._clock()
            if received_bytes > 0:
                self._download_received += received_bytes
                self._last_progress_at = now
                self._speed_samples.append((now, self._download_received))
            self._refresh_speed_locked(now=now)
            if self._state.get("phase") != "cancelling":
                self._state.update(
                    phase="downloading",
                    message="正在下载更新…",
                    downloaded=min(max(0, completed), max(1, total)),
                    download_size=total,
                    completed=min(max(0, completed), max(1, total)),
                    total=total,
                    progress=min(1.0, max(0.0, completed / max(1, total))),
                    retrying=False,
                    stalled=False,
                    download_source_index=source_index + 1,
                    download_source_count=source_count,
                )

    def _refresh_speed_locked(
        self,
        *,
        now: float | None = None,
        force_zero_recent: bool = False,
    ) -> None:
        if self._download_started_at is None:
            self._state["recent_speed_bps"] = 0.0
            self._state["average_speed_bps"] = 0.0
            return
        now = self._clock() if now is None else now
        elapsed = max(0.0, now - self._download_started_at)
        average = self._download_received / elapsed if elapsed > 0 else 0.0
        cutoff = now - self.speed_window
        while len(self._speed_samples) > 1 and self._speed_samples[1][0] <= cutoff:
            self._speed_samples.popleft()
        recent = 0.0
        if (
            not force_zero_recent
            and self._last_progress_at is not None
            and now - self._last_progress_at < self.speed_window
            and self._speed_samples
        ):
            sample_time, sample_bytes = self._speed_samples[0]
            sample_elapsed = now - sample_time
            if sample_elapsed > 0:
                recent = max(0.0, (self._download_received - sample_bytes) / sample_elapsed)
        self._state["recent_speed_bps"] = recent
        self._state["average_speed_bps"] = average

    def _extract_package(self, archive: Path, staging: Path) -> None:
        staging.mkdir(parents=True, exist_ok=False)
        root = staging.resolve()
        seen: set[str] = set()
        extracted = 0
        with zipfile.ZipFile(archive) as package:
            members = package.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise UpdateProtocolError("更新包文件数量异常")
            total_uncompressed = sum(max(0, member.file_size) for member in members)
            if total_uncompressed > MAX_EXTRACTED_BYTES:
                raise UpdateProtocolError("更新包解压后过大")
            for member in members:
                self._raise_if_cancelled()
                relative = self._safe_archive_member(member, seen)
                destination = (staging / Path(*relative.parts)).resolve()
                try:
                    destination.relative_to(root)
                except ValueError as exc:
                    raise UpdateProtocolError("更新包包含越界路径") from exc
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with package.open(member) as source, destination.open("xb") as output:
                    while chunk := source.read(DOWNLOAD_CHUNK_SIZE):
                        self._raise_if_cancelled()
                        output.write(chunk)
                        extracted += len(chunk)
                        with self._lock:
                            self._state["extract_progress"] = (
                                min(1.0, extracted / total_uncompressed)
                                if total_uncompressed
                                else 1.0
                            )
        entrypoint = staging / ENTRYPOINT
        if not entrypoint.is_file() or entrypoint.stat().st_size <= 0:
            raise UpdateProtocolError(f"更新包缺少 {ENTRYPOINT}")

    @staticmethod
    def _safe_archive_member(member: zipfile.ZipInfo, seen: set[str]) -> PurePosixPath:
        name = member.filename.replace("\\", "/")
        path = PurePosixPath(name)
        windows = PureWindowsPath(name)
        if (
            not name
            or name.startswith("/")
            or path.is_absolute()
            or windows.is_absolute()
            or windows.drive
            or any(part in {"", ".", ".."} or ":" in part for part in path.parts)
        ):
            raise UpdateProtocolError("更新包包含不安全路径")
        key = "/".join(path.parts).casefold().rstrip("/")
        if key in seen:
            raise UpdateProtocolError("更新包包含重复路径")
        seen.add(key)
        mode = (member.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            raise UpdateProtocolError("更新包不允许符号链接")
        if member.flag_bits & 0x1:
            raise UpdateProtocolError("更新包不允许加密文件")
        return path

    def _promote_staging(self, staging: Path, target: Path) -> None:
        self.versions_root.mkdir(parents=True, exist_ok=True)
        current_version = read_current_version(self.app_root)
        current_target = (
            versioned_app_path(self.app_root, current_version)
            if current_version
            else None
        )
        if current_target is not None:
            current_target = current_target.parent
        if target.exists() and current_target and target.resolve() == current_target.resolve():
            raise UpdateProtocolError("不能覆盖正在运行的应用版本")
        stale: Path | None = None
        if target.exists():
            stale = self.versions_root / f".stale-{target.name}-{uuid.uuid4().hex}"
            os.replace(target, stale)
        try:
            os.replace(staging, target)
        except Exception:
            if stale and stale.exists() and not target.exists():
                os.replace(stale, target)
            raise
        if stale and stale.exists():
            shutil.rmtree(stale, ignore_errors=True)

    def _raise_if_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise UpdateCancelled("用户取消更新")

    def _record_error(
        self,
        error: Exception,
        *,
        quiet: bool,
        code: str,
    ) -> dict[str, Any]:
        with self._lock:
            return self._record_error_locked(error, quiet=quiet, code=code)

    def _record_error_locked(
        self,
        error: Exception,
        *,
        quiet: bool,
        code: str,
    ) -> dict[str, Any]:
        self._state.update(
            ok=False,
            quiet=quiet,
            phase="error",
            message=str(error),
            error={"code": code, "message": str(error)},
            release_notes=[],
            release_history=[],
        )
        return copy.deepcopy(self._state)


__all__ = [
    "AppUpdater",
    "ENTRYPOINT",
    "LAUNCHER_PROTOCOL",
    "PACKAGE_FORMAT",
    "UpdateCancelled",
    "UpdateProtocolError",
    "default_install_root",
    "version_key",
]
