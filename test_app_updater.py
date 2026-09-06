from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import URLError

from app_updater import AppUpdater, ENTRYPOINT, PACKAGE_FORMAT, UpdateProtocolError
from update_protocol import LAUNCHER_PROTOCOL_ENV, LAUNCHER_VERSION_ENV


DEFAULT_LAUNCHER = b"protocol two launcher"


class FakeHeaders(dict):
    def get(self, key, default=None):
        return super().get(key, default)


class FakeResponse:
    def __init__(
        self,
        payload: bytes,
        content_type: str,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = io.BytesIO(payload)
        self.status = status
        response_headers = {"Content-Type": content_type}
        response_headers.update(headers or {})
        self.headers = FakeHeaders(response_headers)

    def read(self, size: int = -1) -> bytes:
        return self._payload.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeOpener:
    def __init__(self, payloads: dict[str, tuple[bytes, str]]) -> None:
        self.payloads = payloads
        self.requests: list[str] = []

    def __call__(self, request, *, timeout):
        url = request.full_url
        self.requests.append(url)
        payload, content_type = self.payloads[url]
        return FakeResponse(payload, content_type)


class GatedResponse(FakeResponse):
    def __init__(
        self,
        payload: bytes,
        content_type: str,
        started: threading.Event,
        release: threading.Event,
    ) -> None:
        super().__init__(payload, content_type)
        self.started = started
        self.release = release
        self._gated = False

    def read(self, size: int = -1) -> bytes:
        if not self._gated:
            self._gated = True
            self.started.set()
            self.release.wait(timeout=2)
        return super().read(size)


class GatedPackageOpener(FakeOpener):
    def __init__(self, payloads, package_url: str) -> None:
        super().__init__(payloads)
        self.package_url = package_url
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, request, *, timeout):
        url = request.full_url
        self.requests.append(url)
        payload, content_type = self.payloads[url]
        if url == self.package_url:
            return GatedResponse(payload, content_type, self.started, self.release)
        return FakeResponse(payload, content_type)


class InterruptedResponse(FakeResponse):
    def __init__(self, payload: bytes, content_type: str) -> None:
        super().__init__(payload, content_type)
        self._delivered = False

    def read(self, size: int = -1) -> bytes:
        if not self._delivered:
            self._delivered = True
            return super().read(size)
        raise TimeoutError("download read timed out")


class RangeResumeOpener:
    def __init__(self, version_url: str, manifest: dict, package_url: str, package: bytes) -> None:
        self.version_url = version_url
        self.manifest = manifest
        self.package_url = package_url
        self.package = package
        self.package_requests: list[str | None] = []

    def __call__(self, request, *, timeout):
        if request.full_url == self.version_url:
            return FakeResponse(json.dumps(self.manifest).encode(), "application/json")
        if request.full_url != self.package_url:
            raise AssertionError(f"unexpected URL: {request.full_url}")
        range_header = request.get_header("Range")
        self.package_requests.append(range_header)
        if len(self.package_requests) == 1:
            split = max(1, len(self.package) // 2)
            return InterruptedResponse(self.package[:split], "application/zip")
        if not range_header or not range_header.startswith("bytes="):
            raise AssertionError(f"missing resume Range: {range_header}")
        start_text, end_text = range_header.removeprefix("bytes=").split("-", 1)
        start = int(start_text)
        end = min(int(end_text), len(self.package) - 1)
        return FakeResponse(
            self.package[start : end + 1],
            "application/zip",
            status=206,
            headers={"Content-Range": f"bytes {start}-{end}/{len(self.package)}"},
        )


class FallbackUrlOpener:
    def __init__(
        self,
        version_url: str,
        manifest: dict,
        primary_url: str,
        fallback_url: str,
        package: bytes,
    ) -> None:
        self.version_url = version_url
        self.manifest = manifest
        self.primary_url = primary_url
        self.fallback_url = fallback_url
        self.package = package
        self.package_requests: list[str] = []

    def __call__(self, request, *, timeout):
        if request.full_url == self.version_url:
            return FakeResponse(json.dumps(self.manifest).encode(), "application/json")
        self.package_requests.append(request.full_url)
        if request.full_url == self.primary_url:
            raise URLError("primary route unavailable")
        if request.full_url == self.fallback_url:
            return FakeResponse(self.package, "application/zip")
        raise AssertionError(f"unexpected URL: {request.full_url}")


class PartialThenGatedResponse(FakeResponse):
    def __init__(
        self,
        payload: bytes,
        content_type: str,
        blocked: threading.Event,
        release: threading.Event,
    ) -> None:
        super().__init__(payload, content_type)
        self.blocked = blocked
        self.release = release
        self._reads = 0

    def read(self, size: int = -1) -> bytes:
        self._reads += 1
        if self._reads == 1:
            split = max(1, len(self._payload.getbuffer()) // 2)
            return self._payload.read(split)
        if self._reads == 2:
            self.blocked.set()
            self.release.wait(timeout=2)
        return super().read(size)


class PartialGatedOpener(FakeOpener):
    def __init__(self, payloads, package_url: str) -> None:
        super().__init__(payloads)
        self.package_url = package_url
        self.blocked = threading.Event()
        self.release = threading.Event()

    def __call__(self, request, *, timeout):
        url = request.full_url
        self.requests.append(url)
        payload, content_type = self.payloads[url]
        if url == self.package_url:
            return PartialThenGatedResponse(
                payload,
                content_type,
                self.blocked,
                self.release,
            )
        return FakeResponse(payload, content_type)


class MutableClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class ClockedChunkResponse(FakeResponse):
    def __init__(self, payload: bytes, clock: MutableClock) -> None:
        super().__init__(payload, "application/zip")
        split = max(1, len(payload) // 2)
        self._chunks = [payload[:split], payload[split:]]
        self._delays = [1.0, 4.0]
        self.clock = clock

    def read(self, size: int = -1) -> bytes:
        if not self._chunks:
            return b""
        self.clock.advance(self._delays.pop(0))
        return self._chunks.pop(0)


class ClockedPackageOpener(FakeOpener):
    def __init__(self, payloads, package_url: str, clock: MutableClock) -> None:
        super().__init__(payloads)
        self.package_url = package_url
        self.clock = clock

    def __call__(self, request, *, timeout):
        url = request.full_url
        self.requests.append(url)
        payload, content_type = self.payloads[url]
        if url == self.package_url:
            return ClockedChunkResponse(payload, self.clock)
        return FakeResponse(payload, content_type)


def zip_payload(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return output.getvalue()


def manifest_for(
    package: bytes,
    launcher: bytes | None = DEFAULT_LAUNCHER,
    *,
    version: str = "1.5.9",
):
    result = {
        "version": version,
        "package_format": PACKAGE_FORMAT,
        "package_file": f"DeltaStatsApp-{version}.zip",
        "package_size": len(package),
        "package_sha256": hashlib.sha256(package).hexdigest(),
        "entrypoint": ENTRYPOINT,
        "launcher_protocol": 2,
        "launcher_version": version,
    }
    if launcher is not None:
        result.update(
            launcher_file=f"DeltaStatsLauncher-{version}.exe",
            launcher_size=len(launcher),
            launcher_sha256=hashlib.sha256(launcher).hexdigest(),
        )
    return result


def wait_for_phase(updater: AppUpdater, phases: set[str], timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = updater.get_update_status()
        if status["phase"] in phases:
            return status
        time.sleep(0.01)
    raise AssertionError(f"updater did not reach {phases}: {updater.get_update_status()}")


class AppUpdaterTests(unittest.TestCase):
    def make_updater(
        self,
        root: Path,
        manifest: dict,
        package: bytes,
        launcher: bytes | None = None,
        **kwargs,
    ) -> tuple[AppUpdater, FakeOpener]:
        version_url = "https://updates.example/updates/version.json"
        if launcher is None and "launcher_file" in manifest:
            launcher = DEFAULT_LAUNCHER
        payloads = {
            version_url: (json.dumps(manifest).encode(), "application/json"),
            f"https://updates.example/updates/{manifest['package_file']}": (
                package,
                "application/zip",
            ),
        }
        if launcher is not None:
            payloads[f"https://updates.example/updates/{manifest['launcher_file']}"] = (
                launcher,
                "application/octet-stream",
            )
        opener = FakeOpener(payloads)
        updater = AppUpdater(
            version_url,
            current_version=kwargs.pop("current_version", manifest["version"]),
            install_root=root,
            opener=opener,
            **kwargs,
        )
        return updater, opener

    def test_same_version_legacy_install_is_offered_layout_migration(self):
        package = zip_payload({ENTRYPOINT: b"new app"})
        launcher = b"protocol two launcher"
        manifest = manifest_for(package, launcher)
        with TemporaryDirectory() as directory:
            updater, _ = self.make_updater(Path(directory), manifest, package, launcher)

            status = updater.check_for_update(False)

            self.assertTrue(status["ok"])
            self.assertTrue(status["quiet"])
            self.assertEqual(status["phase"], "available")
            self.assertEqual(status["reason"], "layout")
            self.assertTrue(status["needs_launcher"])

    def test_matching_current_layout_is_up_to_date(self):
        package = zip_payload({ENTRYPOINT: b"new app"})
        manifest = manifest_for(package)
        manifest["release_notes"] = {"untrusted": "not relevant to the current version"}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "app" / "versions" / manifest["version"]
            target.mkdir(parents=True)
            (target / ENTRYPOINT).write_bytes(b"current app")
            (root / "app" / "current.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "version": manifest["version"],
                    }
                ),
                encoding="utf-8",
            )
            updater, _ = self.make_updater(
                root,
                manifest,
                package,
            )

            status = updater.check_for_update(False)

            self.assertTrue(status["ok"])
            self.assertEqual(status["phase"], "up_to_date")
            self.assertFalse(status["available"])
            self.assertEqual(status["download_size"], 0)
            self.assertEqual(status["release_notes"], [])

    def test_automatic_check_failure_is_structured_and_quiet(self):
        with TemporaryDirectory() as directory:
            updater = AppUpdater(
                "https://updates.example/updates/version.json",
                current_version="1.5.9",
                install_root=directory,
                opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")),
            )

            status = updater.check_for_update(False)

            self.assertFalse(status["ok"])
            self.assertTrue(status["quiet"])
            self.assertEqual(status["phase"], "error")
            self.assertEqual(status["error"]["code"], "check_failed")
            self.assertIn("offline", status["error"]["message"])

    def test_manual_check_failure_is_not_quiet(self):
        with TemporaryDirectory() as directory:
            updater = AppUpdater(
                "https://updates.example/updates/version.json",
                current_version="1.5.9",
                install_root=directory,
                opener=lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")),
            )

            status = updater.check_for_update(True)

            self.assertFalse(status["quiet"])

    def test_failed_recheck_cannot_start_a_stale_update_plan(self):
        package = zip_payload({ENTRYPOINT: b"new app"})
        manifest = manifest_for(package)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            updater, _ = self.make_updater(root, manifest, package)
            self.assertEqual(updater.check_for_update(False)["phase"], "available")
            updater._opener = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                URLError("offline")
            )

            failed = updater.check_for_update(True)
            start = updater.start_update()

            self.assertEqual(failed["phase"], "error")
            self.assertFalse(failed["available"])
            self.assertEqual(start["error"]["code"], "not_checked")

    def test_older_server_manifest_is_up_to_date_without_parsing_package_fields(self):
        manifest = {
            "version": "1.5.9",
            "package_format": "retired-format",
            "package_file": "../invalid.zip",
            "launcher_protocol": "invalid",
            "release_notes": {"untrusted": "not relevant to a newer installed version"},
        }
        version_url = "https://updates.example/updates/version.json"
        opener = FakeOpener(
            {version_url: (json.dumps(manifest).encode(), "application/json")}
        )
        with TemporaryDirectory() as directory:
            updater = AppUpdater(
                version_url,
                current_version="1.6.0",
                install_root=directory,
                opener=opener,
            )

            status = updater.check_for_update(False)

            self.assertTrue(status["ok"])
            self.assertEqual(status["phase"], "up_to_date")
            self.assertFalse(status["available"])
            self.assertEqual(status["download_size"], 0)
            self.assertIsNone(status["error"])
            self.assertEqual(status["release_notes"], [])

    def test_newer_manifest_release_notes_are_preserved_until_update_is_ready(self):
        package = zip_payload({ENTRYPOINT: b"new app"})
        notes = ["第一项更新", "第二项更新"]
        manifest = manifest_for(package, version="1.6.0")
        manifest["release_notes"] = notes
        manifest["release_history"] = [
            {"version": "1.6.0", "release_notes": notes},
        ]
        with TemporaryDirectory() as directory:
            updater, _ = self.make_updater(
                Path(directory),
                manifest,
                package,
                current_version="1.5.9",
            )

            available = updater.check_for_update(True)
            downloading = updater.start_update()
            ready = wait_for_phase(updater, {"ready", "error"})

            self.assertEqual(available["release_notes"], notes)
            self.assertEqual(downloading["release_notes"], notes)
            self.assertEqual(ready["phase"], "ready", ready)
            self.assertEqual(ready["release_notes"], notes)
            self.assertEqual(
                available["release_history"],
                [{"version": "1.6.0", "release_notes": notes}],
            )
            self.assertEqual(downloading["release_history"], available["release_history"])
            self.assertEqual(ready["release_history"], available["release_history"])

    def test_release_history_includes_only_versions_newer_than_current(self):
        package = zip_payload({ENTRYPOINT: b"new app"})
        manifest = manifest_for(package, version="1.8.0")
        manifest["release_notes"] = [
            "v1.7.3：筛选方式和时段筛选改进。",
            "v1.7.4：支持查看当前版本更新内容。",
            "v1.8.0：新增安装程序。",
        ]
        manifest["release_history"] = [
            {"version": "1.7.3", "release_notes": ["筛选方式和时段筛选改进。"]},
            {"version": "1.7.4", "release_notes": ["支持查看当前版本更新内容。"]},
            {"version": "1.8.0", "release_notes": ["新增安装程序。"]},
        ]
        expected_versions = {
            "1.7.2": ["1.7.3", "1.7.4", "1.8.0"],
            "1.7.3": ["1.7.4", "1.8.0"],
            "1.7.4": ["1.8.0"],
        }

        for current_version, versions in expected_versions.items():
            with self.subTest(current_version=current_version), TemporaryDirectory() as directory:
                updater, _ = self.make_updater(
                    Path(directory),
                    manifest,
                    package,
                    current_version=current_version,
                )

                status = updater.check_for_update(True)

                self.assertEqual(status["phase"], "available")
                self.assertEqual(status["release_notes"], ["新增安装程序。"])
                self.assertEqual(
                    [record["version"] for record in status["release_history"]],
                    versions,
                )

    def test_release_history_is_empty_for_matching_version_layout_migration(self):
        package = zip_payload({ENTRYPOINT: b"new app"})
        manifest = manifest_for(package, version="1.8.0")
        manifest["release_notes"] = ["v1.8.0：新增安装程序。"]
        manifest["release_history"] = [
            {"version": "1.7.4", "release_notes": ["旧版本说明。"]},
            {"version": "1.8.0", "release_notes": ["新增安装程序。"]},
        ]
        with TemporaryDirectory() as directory:
            updater, _ = self.make_updater(
                Path(directory),
                manifest,
                package,
                current_version="1.8.0",
            )

            status = updater.check_for_update(True)

            self.assertEqual(status["phase"], "available")
            self.assertEqual(status["reason"], "layout")
            self.assertEqual(status["release_notes"], ["新增安装程序。"])
            self.assertEqual(status["release_history"], [])

    def test_legacy_newer_manifest_without_release_history_remains_compatible(self):
        package = zip_payload({ENTRYPOINT: b"new app"})
        manifest = manifest_for(package, version="1.6.0")
        manifest["release_notes"] = ["第一项更新"]
        with TemporaryDirectory() as directory:
            updater, _ = self.make_updater(
                Path(directory),
                manifest,
                package,
                current_version="1.5.9",
            )

            status = updater.check_for_update(True)

            self.assertEqual(status["phase"], "available")
            self.assertEqual(status["release_notes"], ["第一项更新"])
            self.assertEqual(status["release_history"], [])

    def test_invalid_release_history_fails_without_leaking_untrusted_value(self):
        package = zip_payload({ENTRYPOINT: b"new app"})
        manifest = manifest_for(package, version="1.6.0")
        manifest["release_history"] = {"not": "a list"}
        with TemporaryDirectory() as directory:
            updater, _ = self.make_updater(
                Path(directory),
                manifest,
                package,
                current_version="1.5.9",
            )

            status = updater.check_for_update(True)

            self.assertFalse(status["ok"])
            self.assertEqual(status["phase"], "error")
            self.assertEqual(status["release_history"], [])

    def test_valid_release_history_takes_precedence_over_legacy_notes(self):
        package = zip_payload({ENTRYPOINT: b"new app"})
        manifest = manifest_for(package, version="1.6.0")
        manifest["release_notes"] = {"legacy": "untrusted and unused"}
        manifest["release_history"] = [
            {"version": "1.6.0", "release_notes": ["安全的分组说明。"]},
        ]
        with TemporaryDirectory() as directory:
            updater, _ = self.make_updater(
                Path(directory),
                manifest,
                package,
                current_version="1.5.9",
            )

            status = updater.check_for_update(True)

            self.assertTrue(status["ok"])
            self.assertEqual(status["release_notes"], ["安全的分组说明。"])
            self.assertEqual(
                status["release_history"],
                [{"version": "1.6.0", "release_notes": ["安全的分组说明。"]}],
            )

    def test_legacy_newer_manifest_without_release_notes_remains_compatible(self):
        package = zip_payload({ENTRYPOINT: b"new app"})
        manifest = manifest_for(package, version="1.6.0")
        with TemporaryDirectory() as directory:
            updater, _ = self.make_updater(
                Path(directory),
                manifest,
                package,
                current_version="1.5.9",
            )

            status = updater.check_for_update(True)

            self.assertEqual(status["phase"], "available")
            self.assertEqual(status["release_notes"], [])

    def test_invalid_release_notes_fail_without_leaking_untrusted_value(self):
        package = zip_payload({ENTRYPOINT: b"new app"})
        manifest = manifest_for(package, version="1.6.0")
        manifest["release_notes"] = {"not": "a list"}
        with TemporaryDirectory() as directory:
            updater, _ = self.make_updater(
                Path(directory),
                manifest,
                package,
                current_version="1.5.9",
            )

            status = updater.check_for_update(True)

            self.assertFalse(status["ok"])
            self.assertEqual(status["phase"], "error")
            self.assertEqual(status["release_notes"], [])

    def test_same_version_legacy_still_requires_onedir_package_fields(self):
        manifest = {"version": "1.6.0"}
        version_url = "https://updates.example/updates/version.json"
        opener = FakeOpener(
            {version_url: (json.dumps(manifest).encode(), "application/json")}
        )
        with TemporaryDirectory() as directory:
            updater = AppUpdater(
                version_url,
                current_version="1.6.0",
                install_root=directory,
                opener=opener,
            )

            status = updater.check_for_update(False)

            self.assertFalse(status["ok"])
            self.assertEqual(status["phase"], "error")
            self.assertIn("快速启动更新包", status["error"]["message"])

    def test_hash_mismatched_root_launcher_is_staged_despite_protocol_environment(self):
        package = zip_payload({ENTRYPOINT: b"new app", "_internal/runtime.dll": b"runtime"})
        launcher = b"protocol two launcher"
        manifest = manifest_for(package, launcher)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "三角洲情报助手.exe").write_bytes(b"old launcher")
            with patch.dict(
                os.environ,
                {LAUNCHER_PROTOCOL_ENV: "2", LAUNCHER_VERSION_ENV: manifest["version"]},
            ):
                updater, opener = self.make_updater(root, manifest, package, launcher)
                self.assertEqual(updater.check_for_update(False)["phase"], "available")

                updater.start_update()
                status = wait_for_phase(updater, {"ready", "error"})

            self.assertEqual(status["phase"], "ready", status)
            target = root / "app" / "versions" / manifest["version"]
            self.assertEqual((target / ENTRYPOINT).read_bytes(), b"new app")
            self.assertEqual((target / "_internal" / "runtime.dll").read_bytes(), b"runtime")
            pending = json.loads((root / "app" / "pending.json").read_text(encoding="utf-8"))
            self.assertEqual(pending["target_dir"], f"versions/{manifest['version']}")
            self.assertEqual(pending["entrypoint"], ENTRYPOINT)
            self.assertEqual(
                pending["launcher_stage"],
                f".updates/{manifest['launcher_file']}",
            )
            self.assertEqual(
                (root / pending["launcher_stage"]).read_bytes(),
                launcher,
            )
            self.assertEqual(status["apply_launcher"], str(root / pending["launcher_stage"]))
            self.assertIn(manifest["launcher_file"], "\n".join(opener.requests))

    def test_missing_root_launcher_is_staged_even_when_environment_claims_current_protocol(self):
        package = zip_payload({ENTRYPOINT: b"new app"})
        launcher = b"protocol two launcher"
        manifest = manifest_for(package, launcher, version="1.6.0")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(
                os.environ,
                {LAUNCHER_PROTOCOL_ENV: "2", LAUNCHER_VERSION_ENV: "1.6.0"},
            ):
                updater, opener = self.make_updater(
                    root,
                    manifest,
                    package,
                    launcher,
                    current_version="1.5.9",
                )
                check = updater.check_for_update(False)

                updater.start_update()
                status = wait_for_phase(updater, {"ready", "error"})

            self.assertEqual(status["phase"], "ready", status)
            self.assertTrue(check["needs_launcher"])
            pending = json.loads((root / "app" / "pending.json").read_text(encoding="utf-8"))
            self.assertEqual(pending["launcher_stage"], f".updates/{manifest['launcher_file']}")
            self.assertIn(manifest["launcher_file"], "\n".join(opener.requests))

    def test_matching_root_launcher_skips_stage_when_old_launcher_set_no_environment(self):
        package = zip_payload({ENTRYPOINT: b"new app"})
        launcher = b"protocol two launcher"
        manifest = manifest_for(package, launcher)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "三角洲情报助手.exe").write_bytes(launcher)
            updater, _ = self.make_updater(root, manifest, package, launcher)

            status = updater.check_for_update(False)

            self.assertEqual(status["phase"], "available")
            self.assertFalse(status["needs_launcher"])
            self.assertEqual(status["download_size"], len(package))

    def test_rejects_zip_traversal_without_writing_outside_versions(self):
        package = zip_payload({ENTRYPOINT: b"new app", "../escaped.txt": b"bad"})
        manifest = manifest_for(package)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            updater, _ = self.make_updater(
                root,
                manifest,
                package,
            )
            updater.check_for_update(False)

            updater.start_update()
            status = wait_for_phase(updater, {"ready", "error"})

            self.assertEqual(status["phase"], "error")
            self.assertFalse((root / "app" / "escaped.txt").exists())
            self.assertFalse((root / "app" / "pending.json").exists())

    def test_stalled_download_resumes_from_partial_file_with_range(self):
        package = zip_payload({ENTRYPOINT: b"new app", "payload.bin": b"x" * 4096})
        manifest = manifest_for(package)
        version_url = "https://updates.example/updates/version.json"
        package_url = f"https://updates.example/updates/{manifest['package_file']}"
        opener = RangeResumeOpener(version_url, manifest, package_url, package)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "三角洲情报助手.exe").write_bytes(DEFAULT_LAUNCHER)
            updater = AppUpdater(
                version_url,
                current_version=manifest["version"],
                install_root=root,
                opener=opener,
                retry_delay=0,
            )
            updater.check_for_update(False)

            updater.start_update()
            status = wait_for_phase(updater, {"ready", "error"})

            split = max(1, len(package) // 2)
            self.assertEqual(status["phase"], "ready", status)
            self.assertEqual(status["retry_count"], 1)
            self.assertEqual(len(opener.package_requests), 2)
            self.assertTrue(opener.package_requests[0].startswith("bytes=0-"))
            self.assertTrue(opener.package_requests[1].startswith(f"bytes={split}-"))

    def test_download_uses_ordered_fallback_url_after_primary_failure(self):
        package = zip_payload({ENTRYPOINT: b"new app"})
        manifest = manifest_for(package)
        version_url = "https://updates.example/updates/version.json"
        primary_url = "https://primary.example/releases/app.zip"
        fallback_url = "https://fallback.example/releases/app.zip"
        manifest["package_urls"] = [primary_url, fallback_url]
        opener = FallbackUrlOpener(
            version_url,
            manifest,
            primary_url,
            fallback_url,
            package,
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "三角洲情报助手.exe").write_bytes(DEFAULT_LAUNCHER)
            updater = AppUpdater(
                version_url,
                current_version=manifest["version"],
                install_root=root,
                opener=opener,
                retry_delay=0,
            )
            updater.check_for_update(False)

            updater.start_update()
            status = wait_for_phase(updater, {"ready", "error"})

            self.assertEqual(status["phase"], "ready", status)
            self.assertEqual(opener.package_requests, [primary_url, fallback_url])
            self.assertEqual(status["download_source_index"], 2)
            self.assertEqual(status["download_source_count"], 2)

    def test_legacy_single_package_url_remains_supported(self):
        package = zip_payload({ENTRYPOINT: b"new app"})
        manifest = manifest_for(package)
        version_url = "https://updates.example/updates/version.json"
        package_url = "https://single.example/releases/app.zip"
        manifest["package_url"] = package_url
        opener = FakeOpener(
            {
                version_url: (json.dumps(manifest).encode(), "application/json"),
                package_url: (package, "application/zip"),
            }
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "三角洲情报助手.exe").write_bytes(DEFAULT_LAUNCHER)
            updater = AppUpdater(
                version_url,
                current_version=manifest["version"],
                install_root=root,
                opener=opener,
            )
            updater.check_for_update(False)

            updater.start_update()
            status = wait_for_phase(updater, {"ready", "error"})

            self.assertEqual(status["phase"], "ready", status)
            self.assertIn(package_url, opener.requests)

    def test_download_status_exposes_recent_and_total_average_speed(self):
        package = zip_payload({ENTRYPOINT: b"new app", "payload.bin": b"x" * 4096})
        manifest = manifest_for(package)
        version_url = "https://updates.example/updates/version.json"
        package_url = f"https://updates.example/updates/{manifest['package_file']}"
        clock = MutableClock()
        opener = ClockedPackageOpener(
            {
                version_url: (json.dumps(manifest).encode(), "application/json"),
                package_url: (package, "application/zip"),
            },
            package_url,
            clock,
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "三角洲情报助手.exe").write_bytes(DEFAULT_LAUNCHER)
            updater = AppUpdater(
                version_url,
                current_version=manifest["version"],
                install_root=root,
                opener=opener,
                clock=clock,
                speed_window=3,
            )
            updater.check_for_update(False)

            updater.start_update()
            status = wait_for_phase(updater, {"ready", "error"})

            second_chunk = len(package) - max(1, len(package) // 2)
            self.assertEqual(status["phase"], "ready", status)
            self.assertAlmostEqual(status["average_speed_bps"], len(package) / 5)
            self.assertAlmostEqual(status["recent_speed_bps"], second_chunk / 4)
            self.assertNotEqual(status["recent_speed_bps"], status["average_speed_bps"])

    def test_checksum_failure_never_writes_pending_or_version_target(self):
        package = zip_payload({ENTRYPOINT: b"new app"})
        manifest = manifest_for(package)
        manifest["package_sha256"] = "0" * 64
        with TemporaryDirectory() as directory:
            root = Path(directory)
            updater, _ = self.make_updater(
                root,
                manifest,
                package,
            )
            updater.check_for_update(False)

            updater.start_update()
            status = wait_for_phase(updater, {"ready", "error"})

            self.assertEqual(status["phase"], "error")
            self.assertIn("SHA-256", status["error"]["message"])
            self.assertFalse((root / "app" / "pending.json").exists())
            self.assertFalse((root / "app" / "versions" / manifest["version"]).exists())

    def test_corrupt_content_from_all_urls_never_becomes_ready(self):
        package = zip_payload({ENTRYPOINT: b"new app"})
        corrupt = package[:-1] + bytes([package[-1] ^ 0xFF])
        manifest = manifest_for(package)
        version_url = "https://updates.example/updates/version.json"
        primary_url = "https://primary.example/releases/app.zip"
        fallback_url = "https://fallback.example/releases/app.zip"
        manifest["package_urls"] = [primary_url, fallback_url]
        opener = FakeOpener(
            {
                version_url: (json.dumps(manifest).encode(), "application/json"),
                primary_url: (corrupt, "application/zip"),
                fallback_url: (corrupt, "application/zip"),
            }
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "三角洲情报助手.exe").write_bytes(DEFAULT_LAUNCHER)
            updater = AppUpdater(
                version_url,
                current_version=manifest["version"],
                install_root=root,
                opener=opener,
                retry_delay=0,
                max_download_retries=1,
            )
            updater.check_for_update(False)

            updater.start_update()
            status = wait_for_phase(updater, {"ready", "error"})

            self.assertEqual(status["phase"], "error")
            self.assertIn("SHA-256", status["error"]["message"])
            self.assertEqual(opener.requests[-2:], [primary_url, fallback_url])
            self.assertFalse((root / "app" / "pending.json").exists())
            self.assertFalse((root / "app" / "versions" / manifest["version"]).exists())
            self.assertFalse((root / ".updates" / f".{manifest['package_file']}.part").exists())

    def test_cancelled_download_never_writes_pending_or_version_target(self):
        package = zip_payload({ENTRYPOINT: b"new app"})
        manifest = manifest_for(package)
        version_url = "https://updates.example/updates/version.json"
        package_url = f"https://updates.example/updates/{manifest['package_file']}"
        opener = GatedPackageOpener(
            {
                version_url: (json.dumps(manifest).encode(), "application/json"),
                package_url: (package, "application/zip"),
            },
            package_url,
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "三角洲情报助手.exe").write_bytes(DEFAULT_LAUNCHER)
            updater = AppUpdater(
                version_url,
                current_version=manifest["version"],
                install_root=root,
                opener=opener,
            )
            updater.check_for_update(False)
            updater.start_update()
            self.assertTrue(opener.started.wait(timeout=2))

            cancelling = updater.cancel_update()
            opener.release.set()
            status = wait_for_phase(updater, {"cancelled", "error"})

            self.assertEqual(cancelling["phase"], "cancelling")
            self.assertEqual(status["phase"], "cancelled", status)
            self.assertFalse((root / "app" / "pending.json").exists())
            self.assertFalse((root / "app" / "versions" / manifest["version"]).exists())

    def test_cancelled_download_keeps_verified_progress_as_a_resumable_part(self):
        package = zip_payload({ENTRYPOINT: b"new app", "payload.bin": b"x" * 4096})
        manifest = manifest_for(package)
        version_url = "https://updates.example/updates/version.json"
        package_url = f"https://updates.example/updates/{manifest['package_file']}"
        opener = PartialGatedOpener(
            {
                version_url: (json.dumps(manifest).encode(), "application/json"),
                package_url: (package, "application/zip"),
            },
            package_url,
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "三角洲情报助手.exe").write_bytes(DEFAULT_LAUNCHER)
            updater = AppUpdater(
                version_url,
                current_version=manifest["version"],
                install_root=root,
                opener=opener,
            )
            updater.check_for_update(False)
            updater.start_update()
            self.assertTrue(opener.blocked.wait(timeout=2))

            updater.cancel_update()
            opener.release.set()
            status = wait_for_phase(updater, {"cancelled", "error"})

            partial = root / ".updates" / f".{manifest['package_file']}.part"
            self.assertEqual(status["phase"], "cancelled", status)
            self.assertTrue(partial.is_file())
            self.assertGreater(partial.stat().st_size, 0)
            self.assertLess(partial.stat().st_size, len(package))
            self.assertFalse((root / "app" / "pending.json").exists())

    def test_start_without_successful_check_returns_structured_error(self):
        package = zip_payload({ENTRYPOINT: b"new app"})
        manifest = manifest_for(package)
        with TemporaryDirectory() as directory:
            updater, _ = self.make_updater(Path(directory), manifest, package)

            status = updater.start_update()

            self.assertFalse(status["ok"])
            self.assertFalse(status["quiet"])
            self.assertEqual(status["error"]["code"], "not_checked")

    def test_concurrent_checks_share_one_in_flight_request(self):
        package = zip_payload({ENTRYPOINT: b"new app"})
        manifest = manifest_for(package)
        version_url = "https://updates.example/updates/version.json"
        opener = GatedPackageOpener(
            {version_url: (json.dumps(manifest).encode(), "application/json")},
            version_url,
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "三角洲情报助手.exe").write_bytes(DEFAULT_LAUNCHER)
            updater = AppUpdater(
                version_url,
                current_version=manifest["version"],
                install_root=root,
                opener=opener,
            )
            result: dict[str, object] = {}
            first = threading.Thread(
                target=lambda: result.update(first=updater.check_for_update(False))
            )
            first.start()
            self.assertTrue(opener.started.wait(timeout=2))

            concurrent = updater.check_for_update(True)
            start_while_checking = updater.start_update()

            self.assertEqual(concurrent["phase"], "checking")
            self.assertTrue(concurrent["quiet"])
            self.assertEqual(start_while_checking["phase"], "checking")
            self.assertEqual(opener.requests, [version_url])
            opener.release.set()
            first.join(timeout=2)
            self.assertFalse(first.is_alive())
            self.assertEqual(result["first"]["phase"], "available")

            self.assertEqual(updater.check_for_update(False)["phase"], "available")
            self.assertEqual(opener.requests.count(version_url), 2)

    def test_launcher_path_must_be_the_controlled_root_binary(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)

            with self.assertRaisesRegex(UpdateProtocolError, "受控安装目录"):
                AppUpdater(
                    "https://updates.example/updates/version.json",
                    current_version="1.6.0",
                    install_root=root,
                    launcher_path=root / ".updates" / "other.exe",
                )


if __name__ == "__main__":
    unittest.main()
