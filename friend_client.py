#!/usr/bin/env python3
"""Desktop window for one player's Delta Force match records."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
from ctypes import byref, c_int, sizeof, windll
from ctypes.wintypes import POINT
from http.client import HTTPConnection, HTTPException
from pathlib import Path

import webview

from app_updater import AppUpdater
from app_version import APP_DISPLAY_NAME, APP_VERSION, RELEASE_NOTES
from diagnostics import LOG_DIR, configure_logging
from install_metadata import sync_installed_version_metadata
from instance_guard import acquire_app_mutex, release_app_mutex
from secure_store import APP_DIR
from startup_protocol import STARTUP_READY_FILE_ENV, mark_startup_ready
from stats_server import create_server
from update_protocol import (
    INSTALL_KIND_ENV,
    INSTALL_KIND_VERSIONED,
    INSTALL_ROOT_ENV,
    START_TRACE_ID_ENV,
    read_current_version,
)


WM_NCLBUTTONDOWN = 0x00A1
HTCAPTION = 2
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_DONOTROUND = 1
DWMWCP_ROUND = 2
RESIZE_HIT_TESTS = {
    "left": 10,
    "right": 11,
    "top": 12,
    "top_left": 13,
    "top_right": 14,
    "bottom": 15,
    "bottom_left": 16,
    "bottom_right": 17,
}
MB_ICONERROR = 0x00000010
CREATE_NO_WINDOW = 0x08000000
ACTIVATION_METADATA_TIMEOUT_SECONDS = 5.0
LOGGER = configure_logging("desktop")


def resource_path(name: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root / name


def create_app_updater() -> AppUpdater | None:
    try:
        bootstrap = json.loads(resource_path("update-bootstrap.json").read_text(encoding="utf-8"))
        version_url = str(bootstrap["version_url"]).strip()
        return AppUpdater(version_url, current_version=APP_VERSION)
    except (OSError, UnicodeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        LOGGER.exception("Unable to initialize the in-app updater")
        return None


def open_directory(path: Path) -> dict[str, str | bool]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))
        return {"ok": True, "path": str(path)}
    except OSError as exc:
        LOGGER.exception("Unable to open directory: %s", path)
        return {"ok": False, "message": str(exc)}


class WindowControls:
    def __init__(self, *, maximized: bool = False, updater: AppUpdater | None = None) -> None:
        self._window = None
        self._maximized = maximized
        self._updater = updater

    def _bind(self, window) -> None:
        self._window = window
        window.events.before_show += self._prepare_window_before_show
        window.events.maximized += self._mark_maximized
        window.events.restored += self._mark_restored
        window.events.moved += self._update_maximized_bounds

    def _prepare_window_before_show(self) -> None:
        if not self._window or not self._window.native:
            return
        try:
            from System.Drawing import Rectangle
            from System.Windows.Forms import FormWindowState, Screen

            native = self._window.native
            work = Screen.FromHandle(native.Handle).WorkingArea
            width = min(work.Width, max(native.MinimumSize.Width, round(work.Width * 0.82)))
            height = min(work.Height, max(native.MinimumSize.Height, round(work.Height * 0.82)))
            native.Bounds = Rectangle(
                work.Left + (work.Width - width) // 2,
                work.Top + (work.Height - height) // 2,
                width,
                height,
            )
            native.MaximizedBounds = work
            native.WindowState = FormWindowState.Normal
            self._set_maximized(False)
        except Exception:
            LOGGER.exception("Unable to prepare native window bounds")

    def _mark_maximized(self) -> None:
        self._set_maximized(True)

    def _mark_restored(self) -> None:
        self._set_maximized(False)

    def _update_maximized_bounds(self, *_args) -> None:
        if not self._window or not self._window.native:
            return
        try:
            from System import Action
            from System.Windows.Forms import Screen

            native = self._window.native

            def apply_bounds() -> None:
                native.MaximizedBounds = Screen.FromHandle(native.Handle).WorkingArea

            if native.InvokeRequired:
                native.Invoke(Action(apply_bounds))
            else:
                apply_bounds()
        except Exception:
            LOGGER.exception("Unable to update native maximized bounds")

    def _set_maximized(self, maximized: bool) -> None:
        self._maximized = maximized
        self._apply_corner_preference(maximized)
        if self._window and self._window.events.loaded.is_set():
            value = "true" if maximized else "false"
            self._window.run_js(f"window.setDesktopMaximized?.({value})")

    def _apply_corner_preference(self, maximized: bool) -> None:
        handle = self._handle()
        if not handle:
            return
        preference = c_int(DWMWCP_DONOTROUND if maximized else DWMWCP_ROUND)
        result = windll.dwmapi.DwmSetWindowAttribute(
            handle,
            DWMWA_WINDOW_CORNER_PREFERENCE,
            byref(preference),
            sizeof(preference),
        )
        if result:
            LOGGER.warning(
                "Unable to set native window corner preference: HRESULT 0x%08X",
                result & 0xFFFFFFFF,
            )

    def get_window_state(self) -> dict[str, bool]:
        self._maximized = self._is_native_maximized()
        return {"maximized": self._maximized}

    def get_app_info(self) -> dict[str, object]:
        return {
            "name": APP_DISPLAY_NAME,
            "version": APP_VERSION,
            "data_directory": str(APP_DIR),
            "log_directory": str(LOG_DIR),
            "release_notes": list(RELEASE_NOTES),
        }

    def open_data_directory(self) -> dict[str, str | bool]:
        return open_directory(APP_DIR)

    def open_log_directory(self) -> dict[str, str | bool]:
        return open_directory(LOG_DIR)

    @staticmethod
    def _normalize_update_status(status: dict[str, object]) -> dict[str, object]:
        """Expose stable UI field names while retaining updater protocol fields."""
        result = dict(status)
        phase = str(result.get("phase") or "idle").strip().lower()
        result["phase"] = phase
        if not result.get("current"):
            result["current"] = result.get("current_version") or APP_VERSION
        if not result.get("latest"):
            result["latest"] = result.get("latest_version")
        if "update_available" not in result:
            result["update_available"] = bool(
                result.get("available", phase == "available")
            )
        if "completed" not in result:
            result["completed"] = result.get("downloaded", 0)
        if "total" not in result:
            result["total"] = result.get("download_size", 0)
        if "cancelable" not in result:
            result["cancelable"] = phase in {"downloading", "verifying", "extracting"}
        release_notes = result.get("release_notes")
        result["release_notes"] = [
            note.strip()
            for note in release_notes
            if isinstance(note, str) and note.strip()
        ] if isinstance(release_notes, list) else []
        return result

    @classmethod
    def _update_failure(cls, message: str, code: str) -> dict[str, object]:
        return cls._normalize_update_status(
            {
                "ok": False,
                "quiet": False,
                "phase": "error",
                "available": False,
                "current_version": APP_VERSION,
                "message": message,
                "error": {"code": code, "message": message},
            }
        )

    def _update_unavailable(self, manual: bool = False) -> dict[str, object]:
        result = self._update_failure("当前运行方式不支持在线更新", "unavailable")
        result["quiet"] = not manual
        return result

    def check_for_update(self, manual: bool = False) -> dict[str, object]:
        if not self._updater:
            return self._update_unavailable(bool(manual))
        return self._normalize_update_status(self._updater.check_for_update(bool(manual)))

    def get_update_status(self) -> dict[str, object]:
        if not self._updater:
            return self._update_unavailable()
        return self._normalize_update_status(self._updater.get_update_status())

    def start_update(self) -> dict[str, object]:
        if not self._updater:
            return self._update_unavailable(True)
        return self._normalize_update_status(self._updater.start_update())

    def cancel_update(self) -> dict[str, object]:
        if not self._updater:
            return self._update_unavailable(True)
        return self._normalize_update_status(self._updater.cancel_update())

    def restart_to_update(self) -> dict[str, object]:
        if not self._updater:
            return self._update_unavailable(True)
        status = self._normalize_update_status(self._updater.get_update_status())
        if status.get("phase") != "ready":
            return self._update_failure("更新尚未准备完成", "not_ready")
        try:
            pending = Path(str(status["pending_path"])).resolve()
            helper = Path(str(status["apply_launcher"])).resolve()
            root = self._updater.install_root.resolve()
            expected_pending = root / "app" / "pending.json"
            root_launcher = root / "三角洲情报助手.exe"
            if pending != expected_pending or not pending.is_file():
                raise RuntimeError("更新请求文件无效")
            if helper != root_launcher:
                try:
                    helper.relative_to(root / ".updates")
                except ValueError as exc:
                    raise RuntimeError("更新启动器位置无效") from exc
            if not helper.is_file() or helper.suffix.lower() != ".exe":
                raise RuntimeError("更新启动器不存在")
            environment = os.environ.copy()
            environment[INSTALL_ROOT_ENV] = str(root)
            subprocess.Popen(
                [str(helper), "--apply-pending", str(pending), str(os.getpid())],
                cwd=str(root),
                env=environment,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as exc:
            LOGGER.exception("Unable to start pending update activator")
            return self._update_failure(f"无法启动更新：{exc}", "activation_failed")
        if self._window:
            threading.Timer(0.2, self._window.destroy).start()
        return self._normalize_update_status(
            {
                **status,
                "ok": True,
                "quiet": False,
                "phase": "restarting",
                "cancelable": False,
                "message": "正在重启并启用新版",
                "error": None,
            }
        )

    def minimize_window(self) -> bool:
        if self._window:
            self._window.minimize()
            return True
        return False

    def toggle_maximize(self) -> bool:
        if not self._window:
            return self._maximized
        current = self._is_native_maximized()
        maximized = not current
        if maximized:
            self._update_maximized_bounds()
        if not self._set_native_maximized(maximized):
            return current
        self._set_maximized(maximized)
        return maximized

    def _is_native_maximized(self) -> bool:
        handle = self._handle()
        if not handle:
            return self._maximized
        return bool(windll.user32.IsZoomed(handle))

    def _set_native_maximized(self, maximized: bool) -> bool:
        from System.Windows.Forms import FormWindowState

        state = FormWindowState.Maximized if maximized else FormWindowState.Normal

        def apply_state() -> None:
            self._window.native.WindowState = state

        return self._run_on_native_ui_thread(apply_state)

    def close_window(self) -> bool:
        if self._window:
            self._window.destroy()
            return True
        return False

    def start_drag(self) -> bool:
        return self._send_non_client_message(HTCAPTION)

    def start_resize(self, direction: str) -> bool:
        if self._maximized or direction not in RESIZE_HIT_TESTS:
            return False
        return self._send_non_client_message(RESIZE_HIT_TESTS[direction])

    def _send_non_client_message(self, hit_test: int) -> bool:
        sent = False

        def send_message() -> None:
            nonlocal sent
            handle = self._handle()
            cursor = self._cursor_position()
            if not handle or cursor is None:
                return
            cursor_position = (cursor.x & 0xFFFF) | ((cursor.y & 0xFFFF) << 16)
            windll.user32.ReleaseCapture()
            windll.user32.SendMessageW(handle, WM_NCLBUTTONDOWN, hit_test, cursor_position)
            sent = True

        return self._run_on_native_ui_thread(send_message) and sent

    def _run_on_native_ui_thread(self, action) -> bool:
        if not self._window or not self._window.native:
            return False
        try:
            from System import Action

            native = self._window.native
            if native.InvokeRequired:
                native.Invoke(Action(action))
            else:
                action()
            return True
        except Exception:
            LOGGER.exception("Unable to run native window action")
            return False

    def _handle(self) -> int:
        if not self._window or not self._window.native:
            return 0
        try:
            return int(self._window.native.Handle.ToInt64())
        except (AttributeError, TypeError, ValueError):
            LOGGER.exception("Unable to resolve native window handle")
            return 0

    def _cursor_position(self) -> POINT | None:
        cursor = POINT()
        if not windll.user32.GetCursorPos(byref(cursor)):
            return None
        return cursor

def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_until_ready(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        connection = HTTPConnection(
            "127.0.0.1",
            port,
            timeout=min(0.5, max(0.05, deadline - time.monotonic())),
        )
        try:
            connection.request("GET", "/index.html", headers={"Connection": "close"})
            response = connection.getresponse()
            response.read()
            if response.status == 200:
                return
        except (OSError, HTTPException):
            time.sleep(0.1)
        finally:
            connection.close()
    raise RuntimeError("本地战绩服务启动超时")


def serve_local(server) -> None:
    LOGGER.info("Local stats server thread starting on port %s", server.server_address[1])
    try:
        server.serve_forever()
    except BaseException:
        LOGGER.exception("Local stats server thread failed")
        raise


def log_thread_stacks() -> None:
    frames = sys._current_frames()
    for thread in threading.enumerate():
        frame = frames.get(thread.ident)
        if frame is None:
            continue
        LOGGER.error(
            "Thread stack for %s (%s):\n%s",
            thread.name,
            thread.ident,
            "".join(traceback.format_stack(frame)),
        )


def reconcile_installed_version_after_activation(
    root: Path,
    timeout: float = ACTIVATION_METADATA_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        if read_current_version(root / "app") == APP_VERSION:
            sync_installed_version_metadata(root, APP_VERSION, LOGGER)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            LOGGER.info(
                "Current version pointer did not activate version=%s; installer metadata was not synchronized",
                APP_VERSION,
            )
            return
        time.sleep(min(0.05, remaining))


def mark_desktop_ready() -> None:
    trace_id = os.environ.get(START_TRACE_ID_ENV, "-")
    marked = mark_startup_ready()
    LOGGER.info("Desktop page loaded trace=%s startup_signal=%s", trace_id, marked)
    raw_root = os.environ.get(INSTALL_ROOT_ENV, "").strip()
    if marked and raw_root and os.environ.get(INSTALL_KIND_ENV) == INSTALL_KIND_VERSIONED:
        try:
            threading.Thread(
                target=reconcile_installed_version_after_activation,
                args=(Path(raw_root).resolve(),),
                name="installed-version-sync",
                daemon=True,
            ).start()
        except Exception:
            LOGGER.exception("Unable to start installed version metadata reconciliation")


def _run_desktop() -> None:
    trace_id = os.environ.get(START_TRACE_ID_ENV, "-")
    LOGGER.info("Desktop Python entry trace=%s", trace_id)
    port = available_port()
    server = create_server(port=port)
    server_thread = threading.Thread(
        target=serve_local,
        args=(server,),
        name="delta-stats-server",
        daemon=True,
    )
    server_thread.start()
    try:
        wait_until_ready(port)
    except Exception:
        log_thread_stacks()
        raise
    LOGGER.info("Local HTTP ready trace=%s port=%s", trace_id, port)
    url = f"http://127.0.0.1:{port}/delta-stats-page.html"

    controls = WindowControls(updater=create_app_updater())
    window = webview.create_window(
        APP_DISPLAY_NAME,
        url,
        js_api=controls,
        width=1280,
        height=840,
        min_size=(800, 520),
        frameless=True,
        easy_drag=False,
        shadow=True,
        background_color="#f4f5f4",
    )
    controls._bind(window)
    window.events.loaded += mark_desktop_ready
    try:
        webview.start()
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3)


def main() -> None:
    mutex = acquire_app_mutex()
    if mutex is None:
        windll.user32.MessageBoxW(
            None,
            f"{APP_DISPLAY_NAME}已经打开，无需重复启动。",
            APP_DISPLAY_NAME,
            0x00000040,
        )
        return
    try:
        _run_desktop()
    finally:
        release_app_mutex(mutex)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        LOGGER.exception("Desktop application failed")
        if not os.environ.get(STARTUP_READY_FILE_ENV):
            windll.user32.MessageBoxW(
                None,
                f"程序启动失败：\n{exc}\n\n诊断日志：\n{LOGGER.handlers[0].baseFilename}",
                APP_DISPLAY_NAME,
                MB_ICONERROR,
            )
        raise SystemExit(1)
