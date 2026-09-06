#!/usr/bin/env python3
"""Lightweight desktop launcher and post-exit onedir update activator."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from ctypes import wintypes
from pathlib import Path

from app_version import APP_DISPLAY_NAME, LAUNCHER_PROTOCOL, LAUNCHER_VERSION
from diagnostics import configure_logging
from install_metadata import sync_installed_version_metadata
from instance_guard import app_instance_running
from startup_protocol import STARTUP_READY_FILE_ENV, is_startup_ready
from update_protocol import (
    APP_ENTRYPOINT,
    INSTALL_KIND_ENV,
    INSTALL_KIND_LEGACY,
    INSTALL_KIND_VERSIONED,
    INSTALL_ROOT_ENV,
    LAUNCHER_PATH_ENV,
    LAUNCHER_PROTOCOL_ENV,
    LAUNCHER_VERSION_ENV,
    PACKAGE_FORMAT,
    START_TRACE_ID_ENV,
    read_current_version,
    read_version_pointer,
    safe_version,
    version_key,
    versioned_app_path,
    write_pointer,
)


LAUNCHER_BINARY = "三角洲情报助手.exe"
MB_ICONERROR = 0x00000010
MB_ICONWARNING = 0x00000030
CREATE_NO_WINDOW = 0x08000000
STARTUP_GRACE_SECONDS = 20.0
LEGACY_STARTUP_GRACE_SECONDS = 8.0
STARTUP_HANDSHAKE_VERSION = "1.5.5"
PROCESS_STOP_TIMEOUT_SECONDS = 3.0
APP_EXIT_TIMEOUT_SECONDS = 45.0
LOGGER = configure_logging("launcher")


class ProcessTreeTerminationError(RuntimeError):
    """Raised when a failed app process tree cannot be confirmed stopped."""


def show_error(title: str, message: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, message, title, MB_ICONERROR)


def show_warning(title: str, message: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, message, title, MB_ICONWARNING)


def install_root() -> Path:
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        staged_root = executable.parent.parent
        if (
            executable.parent.name == ".updates"
            and executable.name != LAUNCHER_BINARY
            and (staged_root / "app" / "pending.json").is_file()
        ):
            return staged_root
        return executable.parent
    configured = os.environ.get(INSTALL_ROOT_ENV, "").strip()
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parent


def read_text(path: Path, default: str = "0.0.0") -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return default


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=PROCESS_STOP_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProcessTreeTerminationError("无法结束启动失败的桌面程序进程树") from exc
        if result.returncode != 0:
            raise ProcessTreeTerminationError(
                f"无法结束启动失败的桌面程序进程树，taskkill 退出码 {result.returncode}"
            )
        try:
            process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise ProcessTreeTerminationError("桌面程序进程树结束后父进程仍未退出") from exc
        return
    process.terminate()
    try:
        process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)


def launch_app(
    app_path: Path,
    app_dir: Path,
    *,
    require_ready: bool = True,
    environment: dict[str, str] | None = None,
) -> subprocess.Popen:
    command = [str(app_path)]
    if not require_ready:
        process = subprocess.Popen(command, cwd=str(app_dir), env=environment)
        try:
            return_code = process.wait(timeout=LEGACY_STARTUP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            return process
        raise RuntimeError(f"桌面程序启动后立即退出，退出码 {return_code}")

    ready_path = Path(tempfile.gettempdir()) / f"delta-stats-startup-{uuid.uuid4().hex}.ready"
    ready_path.unlink(missing_ok=True)
    launch_environment = dict(environment or os.environ)
    launch_environment[STARTUP_READY_FILE_ENV] = str(ready_path)
    try:
        process = subprocess.Popen(command, cwd=str(app_dir), env=launch_environment)
        LOGGER.info("App spawned trace=%s pid=%s", launch_environment.get(START_TRACE_ID_ENV, "-"), process.pid)
        deadline = time.monotonic() + STARTUP_GRACE_SECONDS
        while time.monotonic() < deadline:
            return_code = process.poll()
            if return_code is not None:
                raise RuntimeError(f"桌面程序启动后立即退出，退出码 {return_code}")
            if is_startup_ready(ready_path):
                LOGGER.info(
                    "Desktop app reported ready trace=%s pid=%s",
                    launch_environment.get(START_TRACE_ID_ENV, "-"),
                    process.pid,
                )
                return process
            time.sleep(0.05)
        stop_process(process)
        raise RuntimeError(f"桌面程序未在 {STARTUP_GRACE_SECONDS:g} 秒内完成页面加载")
    finally:
        ready_path.unlink(missing_ok=True)


def schedule_launcher_replacement(staged: Path, current: Path) -> subprocess.Popen:
    staged = staged.resolve()
    current = current.resolve()
    if staged.parent != current.parent / ".updates":
        raise RuntimeError("暂存启动器不在当前安装目录的更新区")
    script = staged.with_suffix(".ps1")
    log_path = Path(LOGGER.handlers[0].baseFilename)
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "for ($attempt = 0; $attempt -lt 120; $attempt++) {\n"
        "  try {\n"
        "    Move-Item -LiteralPath $env:DELTA_REPLACE_SOURCE -Destination $env:DELTA_REPLACE_TARGET -Force\n"
        "    Remove-Item -LiteralPath $PSCommandPath -ErrorAction SilentlyContinue\n"
        "    exit 0\n"
        "  } catch { Start-Sleep -Seconds 1 }\n"
        "}\n"
        "Add-Content -LiteralPath $env:DELTA_REPLACE_LOG -Value ((Get-Date -Format o) + ' launcher replacement failed') -Encoding UTF8\n"
        "exit 1\n",
        encoding="ascii",
    )
    environment = {
        **os.environ,
        "DELTA_REPLACE_SOURCE": str(staged),
        "DELTA_REPLACE_TARGET": str(current),
        "DELTA_REPLACE_LOG": str(log_path),
    }
    return subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        cwd=str(current.parent),
        env=environment,
        creationflags=CREATE_NO_WINDOW,
    )


def _wait_for_process_exit(pid: int, timeout: float = APP_EXIT_TIMEOUT_SECONDS) -> None:
    if pid <= 0:
        return
    if os.name != "nt":
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.1)
        raise RuntimeError("等待旧版退出超时")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        return
    try:
        result = kernel32.WaitForSingleObject(handle, round(timeout * 1000))
    finally:
        kernel32.CloseHandle(handle)
    if result != 0:
        raise RuntimeError("等待旧版退出超时")


def _launch_environment(root: Path, install_kind: str, trace_id: str | None = None) -> dict[str, str]:
    environment = os.environ.copy()
    environment[INSTALL_ROOT_ENV] = str(root)
    environment[INSTALL_KIND_ENV] = install_kind
    environment[LAUNCHER_PATH_ENV] = str(root / LAUNCHER_BINARY)
    environment[LAUNCHER_VERSION_ENV] = LAUNCHER_VERSION
    environment[LAUNCHER_PROTOCOL_ENV] = str(LAUNCHER_PROTOCOL)
    environment[START_TRACE_ID_ENV] = trace_id or uuid.uuid4().hex
    return environment


def resolve_active_app(root: Path) -> tuple[Path, str, str] | None:
    app_root = root / "app"
    current = read_current_version(app_root)
    if current:
        candidate = versioned_app_path(app_root, current)
        if candidate.is_file():
            return candidate, INSTALL_KIND_VERSIONED, current
        LOGGER.warning("Current version pointer has no entrypoint: %s", candidate)
    previous = read_version_pointer(app_root / "previous.json")
    if previous and previous != current:
        candidate = versioned_app_path(app_root, previous)
        if candidate.is_file():
            LOGGER.warning("Falling back to previous app version: %s", previous)
            return candidate, INSTALL_KIND_VERSIONED, previous
    legacy = app_root / APP_ENTRYPOINT
    if legacy.is_file():
        return legacy, INSTALL_KIND_LEGACY, read_text(app_root / "version.txt")
    return None


def resolve_fallback_app(
    root: Path,
    failed: tuple[Path, str, str],
) -> tuple[Path, str, str] | None:
    app_root = root / "app"
    failed_path = failed[0].resolve()
    previous = read_version_pointer(app_root / "previous.json")
    if previous:
        candidate = versioned_app_path(app_root, previous)
        if candidate.is_file() and candidate.resolve() != failed_path:
            return candidate, INSTALL_KIND_VERSIONED, previous
    legacy = app_root / APP_ENTRYPOINT
    if legacy.is_file() and legacy.resolve() != failed_path:
        return legacy, INSTALL_KIND_LEGACY, read_text(app_root / "version.txt")
    return None


def _safe_relative_file(root: Path, raw_value: object, expected_parent: Path) -> Path | None:
    if not raw_value:
        return None
    relative = Path(str(raw_value))
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("更新请求包含不安全的启动器路径")
    resolved = (root / relative).resolve()
    if resolved.parent != expected_parent.resolve() or resolved.suffix.lower() != ".exe":
        raise RuntimeError("更新请求中的启动器路径无效")
    return resolved


def _verify_launcher_stage(path: Path, pending: dict) -> None:
    try:
        expected_size = int(pending.get("launcher_size"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("暂存启动器大小无效") from exc
    expected_sha256 = str(pending.get("launcher_sha256", "")).strip().lower()
    if (
        expected_size <= 0
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise RuntimeError("暂存启动器校验信息无效")
    try:
        if path.stat().st_size != expected_size:
            raise RuntimeError("暂存启动器大小校验失败")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise RuntimeError("暂存启动器无法读取") from exc
    if digest.hexdigest().lower() != expected_sha256:
        raise RuntimeError("暂存启动器 SHA-256 校验失败")


def _load_pending(pending_path: Path) -> tuple[Path, dict, Path, Path | None]:
    pending_path = pending_path.resolve()
    if pending_path.name != "pending.json" or pending_path.parent.name != "app":
        raise RuntimeError("更新请求文件位置无效")
    root = pending_path.parent.parent
    if pending_path != root / "app" / "pending.json":
        raise RuntimeError("更新请求文件位置无效")
    if root != install_root():
        raise RuntimeError("更新请求不属于当前安装目录")
    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"更新请求无法读取：{exc}") from exc
    if pending.get("schema") != 1 or pending.get("package_format") != PACKAGE_FORMAT:
        raise RuntimeError("更新请求格式不受支持")
    version = safe_version(pending.get("version"))
    if pending.get("entrypoint") != APP_ENTRYPOINT:
        raise RuntimeError("更新入口无效")
    expected_target = Path("versions") / version
    if Path(str(pending.get("target_dir", ""))) != expected_target:
        raise RuntimeError("更新目标目录无效")
    candidate = versioned_app_path(root / "app", version)
    if not candidate.is_file():
        raise RuntimeError("更新包缺少桌面程序入口")
    launcher_stage = _safe_relative_file(root, pending.get("launcher_stage"), root / ".updates")
    if launcher_stage is not None and not launcher_stage.is_file():
        raise RuntimeError("暂存启动器不存在")
    if launcher_stage is not None:
        _verify_launcher_stage(launcher_stage, pending)
    return root, pending, candidate, launcher_stage


def apply_pending(pending_path: Path, wait_pid: int) -> int:
    root, pending, candidate, launcher_stage = _load_pending(pending_path)
    _wait_for_process_exit(wait_pid)
    deadline = time.monotonic() + 5
    while app_instance_running() and time.monotonic() < deadline:
        time.sleep(0.1)
    if app_instance_running():
        raise RuntimeError(f"仍有{APP_DISPLAY_NAME}窗口在运行，更新未切换")

    previous = resolve_active_app(root)
    candidate_process: subprocess.Popen | None = None
    try:
        report_version = safe_version(pending.get("version"))
        candidate_process = launch_app(
            candidate,
            candidate.parent,
            require_ready=True,
            environment=_launch_environment(root, INSTALL_KIND_VERSIONED),
        )
        if previous and previous[1] == INSTALL_KIND_VERSIONED and previous[2] != report_version:
            write_pointer(root / "app" / "previous.json", previous[2])
        write_pointer(root / "app" / "current.json", report_version)
        LOGGER.info("Activated onedir app version=%s", report_version)
        sync_installed_version_metadata(root, report_version, LOGGER)
    except ProcessTreeTerminationError:
        # The candidate may still be alive. Starting the previous app here can
        # create two desktop processes sharing the same local service/cache.
        # Keep both the old pointer and pending request for a later retry.
        LOGGER.exception("Candidate process tree could not be confirmed stopped")
        raise
    except Exception:
        LOGGER.exception("Candidate activation failed")
        if candidate_process is not None:
            stop_process(candidate_process)
        try:
            pending_path.unlink(missing_ok=True)
        except OSError:
            LOGGER.exception("Unable to remove failed pending update request")
        if previous:
            launch_app(
                previous[0],
                previous[0].parent,
                require_ready=version_key(previous[2]) >= version_key(STARTUP_HANDSHAKE_VERSION),
                environment=_launch_environment(root, previous[1]),
            )
        raise

    try:
        pending_path.unlink(missing_ok=True)
    except OSError:
        # The active pointer has already switched successfully. A stale request
        # is safe to overwrite on the next download and must not roll back a
        # candidate that has already passed the ready handshake.
        LOGGER.exception("Unable to remove activated pending update request")

    root_launcher = root / LAUNCHER_BINARY
    if launcher_stage is not None:
        try:
            schedule_launcher_replacement(launcher_stage, root_launcher)
        except Exception:
            LOGGER.exception("Unable to schedule launcher replacement after app activation")
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    LOGGER.info("Launcher Python entry argv=%s", arguments)
    if arguments and arguments[0] == "--apply-pending":
        if len(arguments) != 3 or not arguments[2].isdigit():
            show_error("更新失败", "更新请求参数无效。")
            return 1
        try:
            return apply_pending(Path(arguments[1]), int(arguments[2]))
        except Exception as exc:
            LOGGER.exception("Unable to activate pending update")
            show_error("更新失败", f"新版没有成功启用，已保留上一版本。\n\n{exc}")
            return 1

    if arguments:
        show_error("启动失败", "启动参数无效。")
        return 1
    if app_instance_running():
        show_warning(APP_DISPLAY_NAME, "程序已经打开，无需重复启动。")
        return 0

    root = install_root()
    active = resolve_active_app(root)
    if not active:
        show_error("无法启动", "没有找到桌面程序，请重新下载并完整解压软件包。")
        return 1
    app_path, install_kind, current = active
    try:
        launch_app(
            app_path,
            app_path.parent,
            require_ready=version_key(current) >= version_key(STARTUP_HANDSHAKE_VERSION),
            environment=_launch_environment(root, install_kind),
        )
    except ProcessTreeTerminationError as exc:
        LOGGER.exception("Desktop app launch failed and its process tree may still be running")
        show_error("无法启动", f"桌面程序没有成功启动。\n\n{exc}")
        return 1
    except Exception as exc:
        LOGGER.exception("Desktop app launch failed")
        fallback = resolve_fallback_app(root, active)
        if fallback:
            fallback_path, fallback_kind, fallback_version = fallback
            try:
                launch_app(
                    fallback_path,
                    fallback_path.parent,
                    require_ready=(
                        version_key(fallback_version) >= version_key(STARTUP_HANDSHAKE_VERSION)
                    ),
                    environment=_launch_environment(root, fallback_kind),
                )
            except Exception as fallback_exc:
                LOGGER.exception("Fallback desktop app launch failed")
                show_error(
                    "无法启动",
                    "当前版本和上一版本都没有成功启动。"
                    f"\n\n当前版本：{exc}\n上一版本：{fallback_exc}",
                )
                return 1
            LOGGER.warning("Recovered startup with fallback app version=%s", fallback_version)
            if fallback_kind == INSTALL_KIND_VERSIONED:
                try:
                    write_pointer(root / "app" / "current.json", fallback_version)
                    sync_installed_version_metadata(root, fallback_version, LOGGER)
                except Exception:
                    LOGGER.exception("Unable to persist recovered app version")
            return 0
        show_error("无法启动", f"桌面程序没有成功启动。\n\n{exc}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        LOGGER.exception("Launcher failed")
        show_error(
            "启动失败",
            f"启动器遇到未处理错误：\n{exc}\n\n诊断日志：\n{LOGGER.handlers[0].baseFilename}",
        )
        raise SystemExit(1)
