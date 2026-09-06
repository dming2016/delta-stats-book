#!/usr/bin/env python3
"""Tests for frameless desktop window behavior."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import friend_client
import instance_guard
from app_version import APP_DISPLAY_NAME, APP_VERSION, RELEASE_NOTES
from diagnostics import LOG_DIR
from friend_client import (
    CREATE_NO_WINDOW,
    DWMWA_WINDOW_CORNER_PREFERENCE,
    DWMWCP_DONOTROUND,
    DWMWCP_ROUND,
    HTCAPTION,
    RESIZE_HIT_TESTS,
    WM_NCLBUTTONDOWN,
    WindowControls,
    open_directory,
    reconcile_installed_version_after_activation,
    wait_until_ready,
)
from secure_store import APP_DIR
from startup_protocol import STARTUP_READY_FILE_ENV, STARTUP_READY_VALUE, mark_startup_ready
from update_protocol import INSTALL_KIND_ENV, INSTALL_KIND_VERSIONED, INSTALL_ROOT_ENV, write_pointer


class FriendClientTests(unittest.TestCase):
    def test_wait_until_ready_requires_an_http_response(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        try:
            with self.assertRaisesRegex(RuntimeError, "本地战绩服务启动超时"):
                wait_until_ready(listener.getsockname()[1], timeout=0.15)
        finally:
            listener.close()

    def test_startup_ready_signal_is_one_shot(self) -> None:
        with TemporaryDirectory() as temporary:
            ready_path = Path(temporary) / "startup.ready"
            with patch.dict(os.environ, {STARTUP_READY_FILE_ENV: str(ready_path)}):
                self.assertTrue(mark_startup_ready())
                self.assertNotIn(STARTUP_READY_FILE_ENV, os.environ)
                self.assertFalse(mark_startup_ready())

            self.assertEqual(ready_path.read_text(encoding="utf-8"), STARTUP_READY_VALUE)

    def test_ready_version_reconciles_installer_metadata_after_pointer_switch(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app").mkdir()
            write_pointer(root / "app" / "current.json", APP_VERSION)
            with patch("friend_client.sync_installed_version_metadata") as sync_mock:
                reconcile_installed_version_after_activation(root, timeout=0)

        sync_mock.assert_called_once_with(root, APP_VERSION, friend_client.LOGGER)

    def test_ready_version_waits_for_the_launcher_pointer_switch(self) -> None:
        root = Path("install-root")
        with (
            patch("friend_client.read_current_version", side_effect=["1.8.2", APP_VERSION]) as read,
            patch("friend_client.time.sleep") as sleep_mock,
            patch("friend_client.sync_installed_version_metadata") as sync_mock,
        ):
            reconcile_installed_version_after_activation(root, timeout=1)

        self.assertEqual(read.call_count, 2)
        sleep_mock.assert_called_once()
        sync_mock.assert_called_once_with(root, APP_VERSION, friend_client.LOGGER)

    def test_unactivated_version_never_reconciles_installer_metadata(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app").mkdir()
            write_pointer(root / "app" / "current.json", "1.8.2")
            with patch("friend_client.sync_installed_version_metadata") as sync_mock:
                reconcile_installed_version_after_activation(root, timeout=0)

        sync_mock.assert_not_called()

    @patch("friend_client.threading.Thread")
    @patch("friend_client.mark_startup_ready", return_value=True)
    def test_desktop_ready_starts_version_reconciliation_in_background(
        self,
        _mark_ready,
        thread_mock,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch.dict(
                os.environ,
                {
                    INSTALL_ROOT_ENV: str(root),
                    INSTALL_KIND_ENV: INSTALL_KIND_VERSIONED,
                },
            ):
                friend_client.mark_desktop_ready()

        thread_mock.assert_called_once_with(
            target=reconcile_installed_version_after_activation,
            args=(root,),
            name="installed-version-sync",
            daemon=True,
        )
        thread_mock.return_value.start.assert_called_once_with()

    def test_drag_uses_native_caption_hit_test(self) -> None:
        controls = WindowControls()
        with patch.object(controls, "_send_non_client_message", return_value=True) as send_message:
            self.assertTrue(controls.start_drag())

        send_message.assert_called_once_with(HTCAPTION)

    def test_resize_uses_native_non_client_hit_test(self) -> None:
        controls = WindowControls()
        with patch.object(controls, "_send_non_client_message", return_value=True) as send_message:
            self.assertTrue(controls.start_resize("bottom_right"))

        send_message.assert_called_once_with(RESIZE_HIT_TESTS["bottom_right"])

    def test_resize_is_disabled_while_maximized(self) -> None:
        controls = WindowControls(maximized=True)
        with patch.object(controls, "_send_non_client_message") as send_message:
            self.assertFalse(controls.start_resize("right"))

        send_message.assert_not_called()

    def test_native_drag_message_runs_on_the_window_ui_thread(self) -> None:
        controls = WindowControls()
        cursor = SimpleNamespace(x=320, y=240)

        with (
            patch.object(
                controls,
                "_run_on_native_ui_thread",
                side_effect=lambda action: action() or True,
            ) as run_on_ui_thread,
            patch.object(controls, "_handle", return_value=123),
            patch.object(controls, "_cursor_position", return_value=cursor),
            patch("friend_client.windll.user32.ReleaseCapture") as release_capture,
            patch("friend_client.windll.user32.SendMessageW") as send_message,
        ):
            self.assertTrue(controls._send_non_client_message(HTCAPTION))

        run_on_ui_thread.assert_called_once()
        release_capture.assert_called_once_with()
        send_message.assert_called_once_with(
            123,
            WM_NCLBUTTONDOWN,
            HTCAPTION,
            (cursor.x & 0xFFFF) | ((cursor.y & 0xFFFF) << 16),
        )

    def test_toggle_maximize_uses_native_window_state(self) -> None:
        controls = WindowControls()
        window = MagicMock()
        window.events.loaded.is_set.return_value = False
        controls._window = window

        with (
            patch.object(controls, "_is_native_maximized", side_effect=[False, True]),
            patch.object(controls, "_set_native_maximized", return_value=True) as set_state,
            patch.object(controls, "_update_maximized_bounds") as update_bounds,
        ):
            self.assertTrue(controls.toggle_maximize())
            self.assertFalse(controls.toggle_maximize())

        update_bounds.assert_called_once_with()
        self.assertEqual(
            set_state.call_args_list,
            [unittest.mock.call(True), unittest.mock.call(False)],
        )

    def test_window_state_reads_the_native_zoom_state(self) -> None:
        controls = WindowControls(maximized=False)
        with patch.object(controls, "_is_native_maximized", return_value=True):
            self.assertEqual(controls.get_window_state(), {"maximized": True})

    def test_window_state_applies_native_corner_preference(self) -> None:
        controls = WindowControls()
        with patch.object(controls, "_apply_corner_preference") as apply_corner:
            controls._set_maximized(False)
            controls._set_maximized(True)

        self.assertEqual(
            apply_corner.call_args_list,
            [unittest.mock.call(False), unittest.mock.call(True)],
        )

    def test_open_directory_creates_and_opens_target(self) -> None:
        with TemporaryDirectory() as temporary:
            target = Path(temporary) / "logs"
            with patch("friend_client.os.startfile") as startfile:
                result = open_directory(target)

            self.assertTrue(target.is_dir())
            startfile.assert_called_once_with(str(target))
            self.assertEqual(result, {"ok": True, "path": str(target)})

    def test_open_directory_reports_operating_system_error(self) -> None:
        with TemporaryDirectory() as temporary:
            target = Path(temporary)
            with (
                patch("friend_client.os.startfile", side_effect=OSError("cannot open")),
                patch("friend_client.LOGGER.exception"),
            ):
                result = open_directory(target)

            self.assertEqual(result, {"ok": False, "message": "cannot open"})

    def test_app_info_exposes_version_and_local_directories(self) -> None:
        app_info = WindowControls().get_app_info()

        self.assertEqual(
            app_info,
            {
                "name": APP_DISPLAY_NAME,
                "version": APP_VERSION,
                "data_directory": str(APP_DIR),
                "log_directory": str(LOG_DIR),
                "release_notes": list(RELEASE_NOTES),
            },
        )
        self.assertIsInstance(app_info["release_notes"], list)
        app_info["release_notes"].append("只影响当前桥接结果")
        self.assertEqual(app_info["release_notes"][:-1], list(RELEASE_NOTES))

    def test_update_bridge_normalizes_app_updater_status(self) -> None:
        updater = MagicMock()
        updater.check_for_update.return_value = {
            "ok": True,
            "phase": "available",
            "available": True,
            "current_version": "1.5.8",
            "latest_version": "1.5.9",
            "downloaded": 1024,
            "download_size": 4096,
            "release_notes": ["好友筛选已改进"],
        }

        status = WindowControls(updater=updater).check_for_update(True)

        updater.check_for_update.assert_called_once_with(True)
        self.assertEqual(status["current"], "1.5.8")
        self.assertEqual(status["latest"], "1.5.9")
        self.assertTrue(status["update_available"])
        self.assertEqual(status["completed"], 1024)
        self.assertEqual(status["total"], 4096)
        self.assertFalse(status["cancelable"])
        self.assertEqual(status["release_notes"], ["好友筛选已改进"])

    def test_update_bridge_normalizes_release_notes_to_a_ui_safe_list(self) -> None:
        updater = MagicMock()
        updater.get_update_status.return_value = {
            "phase": "available",
            "release_notes": {"untrusted": "type"},
        }

        status = WindowControls(updater=updater).get_update_status()

        self.assertEqual(status["release_notes"], [])

    def test_update_bridge_delegates_status_start_and_cancel(self) -> None:
        updater = MagicMock()
        updater.get_update_status.return_value = {
            "phase": "downloading",
            "downloaded": 5,
            "download_size": 10,
        }
        updater.start_update.return_value = {"phase": "verifying", "completed": 10, "total": 10}
        updater.cancel_update.return_value = {"phase": "cancelling", "completed": 5, "total": 10}
        controls = WindowControls(updater=updater)

        downloading = controls.get_update_status()
        verifying = controls.start_update()
        cancelling = controls.cancel_update()

        updater.get_update_status.assert_called_once_with()
        updater.start_update.assert_called_once_with()
        updater.cancel_update.assert_called_once_with()
        self.assertEqual(downloading["completed"], 5)
        self.assertEqual(downloading["total"], 10)
        self.assertTrue(downloading["cancelable"])
        self.assertTrue(verifying["cancelable"])
        self.assertFalse(cancelling["cancelable"])

    def test_unavailable_update_bridge_preserves_manual_quiet_semantics(self) -> None:
        controls = WindowControls()

        automatic = controls.check_for_update(False)
        manual = controls.check_for_update(True)

        self.assertTrue(automatic["quiet"])
        self.assertFalse(manual["quiet"])
        self.assertEqual(manual["phase"], "error")
        self.assertEqual(manual["current"], APP_VERSION)
        self.assertFalse(manual["update_available"])
        self.assertEqual(manual["error"]["code"], "unavailable")

    def test_restart_to_update_closes_only_after_helper_starts(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            pending = root / "app" / "pending.json"
            helper = root / ".updates" / "DeltaStatsLauncher-1.5.9.exe"
            pending.parent.mkdir(parents=True)
            helper.parent.mkdir(parents=True)
            pending.write_text("{}", encoding="utf-8")
            helper.write_bytes(b"launcher")
            updater = MagicMock()
            updater.install_root = root
            updater.get_update_status.return_value = {
                "phase": "ready",
                "available": True,
                "current_version": "1.5.8",
                "latest_version": "1.5.9",
                "pending_path": str(pending),
                "apply_launcher": str(helper),
            }
            controls = WindowControls(updater=updater)
            controls._window = MagicMock()
            timer = MagicMock()
            helper_started = False

            def launch_helper(*_args, **_kwargs):
                nonlocal helper_started
                helper_started = True
                return MagicMock()

            def make_timer(*_args, **_kwargs):
                self.assertTrue(helper_started)
                return timer

            with (
                patch("friend_client.subprocess.Popen", side_effect=launch_helper) as popen,
                patch("friend_client.threading.Timer", side_effect=make_timer) as timer_factory,
            ):
                result = controls.restart_to_update()

            command = popen.call_args.args[0]
            options = popen.call_args.kwargs
            self.assertEqual(
                command,
                [str(helper), "--apply-pending", str(pending), str(os.getpid())],
            )
            self.assertEqual(options["cwd"], str(root))
            self.assertEqual(options["env"][INSTALL_ROOT_ENV], str(root))
            self.assertEqual(options["creationflags"], CREATE_NO_WINDOW)
            timer_factory.assert_called_once_with(0.2, controls._window.destroy)
            timer.start.assert_called_once_with()
            self.assertTrue(result["ok"])
            self.assertEqual(result["phase"], "restarting")

    def test_restart_to_update_failure_keeps_current_window_open(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            pending = root / "app" / "pending.json"
            helper = root / "三角洲情报助手.exe"
            pending.parent.mkdir(parents=True)
            pending.write_text("{}", encoding="utf-8")
            helper.write_bytes(b"launcher")
            updater = MagicMock()
            updater.install_root = root
            updater.get_update_status.return_value = {
                "phase": "ready",
                "pending_path": str(pending),
                "apply_launcher": str(helper),
            }
            controls = WindowControls(updater=updater)
            controls._window = MagicMock()

            with (
                patch("friend_client.subprocess.Popen", side_effect=OSError("cannot start")),
                patch("friend_client.threading.Timer") as timer_factory,
                patch("friend_client.LOGGER.exception"),
            ):
                result = controls.restart_to_update()

            self.assertFalse(result["ok"])
            self.assertEqual(result["phase"], "error")
            self.assertEqual(result["error"]["code"], "activation_failed")
            timer_factory.assert_not_called()
            controls._window.destroy.assert_not_called()

    def test_main_rejects_a_second_instance(self) -> None:
        with (
            patch("friend_client.acquire_app_mutex", return_value=None),
            patch("friend_client.release_app_mutex") as release,
            patch("friend_client._run_desktop") as run_desktop,
            patch("friend_client.windll.user32.MessageBoxW") as message_box,
        ):
            friend_client.main()

        run_desktop.assert_not_called()
        release.assert_not_called()
        message_box.assert_called_once()
        self.assertIn("已经打开", message_box.call_args.args[1])

    def test_main_always_releases_the_single_instance_mutex(self) -> None:
        with (
            patch("friend_client.acquire_app_mutex", return_value=321),
            patch("friend_client.release_app_mutex") as release,
            patch("friend_client._run_desktop", side_effect=RuntimeError("failed")),
        ):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                friend_client.main()

        release.assert_called_once_with(321)

    def test_named_mutex_acquisition_closes_duplicate_handle(self) -> None:
        kernel = MagicMock()
        kernel.CreateMutexW.return_value = 88
        with (
            patch.object(instance_guard, "_kernel32", kernel),
            patch("instance_guard.ctypes.set_last_error"),
            patch(
                "instance_guard.ctypes.get_last_error",
                return_value=instance_guard.ERROR_ALREADY_EXISTS,
            ),
        ):
            handle = instance_guard.acquire_app_mutex()

        self.assertIsNone(handle)
        kernel.CreateMutexW.assert_called_once_with(None, False, instance_guard.APP_MUTEX_NAME)
        kernel.CloseHandle.assert_called_once_with(88)

    def test_named_mutex_probe_and_release_close_their_handles(self) -> None:
        kernel = MagicMock()
        kernel.CreateMutexW.return_value = 77
        kernel.OpenMutexW.return_value = 66
        with (
            patch.object(instance_guard, "_kernel32", kernel),
            patch("instance_guard.ctypes.set_last_error"),
            patch("instance_guard.ctypes.get_last_error", return_value=0),
        ):
            owned = instance_guard.acquire_app_mutex()
            self.assertTrue(instance_guard.app_instance_running())
            instance_guard.release_app_mutex(owned)

        self.assertEqual(owned, 77)
        kernel.OpenMutexW.assert_called_once_with(
            instance_guard.SYNCHRONIZE,
            False,
            instance_guard.APP_MUTEX_NAME,
        )
        self.assertEqual(
            kernel.CloseHandle.call_args_list,
            [unittest.mock.call(66), unittest.mock.call(77)],
        )

    def test_maximized_drag_waits_for_twelve_pixel_movement(self) -> None:
        source = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        mouse_down = source.split("document.addEventListener('mousedown'", 1)[1]
        mouse_down = mouse_down.split("document.addEventListener('dblclick'", 1)[0]
        mouse_move = source.split("function continueDesktopDrag", 1)[1]
        mouse_move = mouse_move.split("document.addEventListener('mousedown'", 1)[0]

        self.assertIn("const DESKTOP_DRAG_THRESHOLD = 12", source)
        self.assertNotIn('id="windowTitlebar"', source)
        self.assertIn("closest('#windowDragRegion')", source)
        self.assertNotIn("pywebview-drag-region", source)
        self.assertIn("desktopDragDistanceReached", mouse_move)
        self.assertIn("callDesktop('start_drag')", mouse_move)
        self.assertNotIn("callDesktop('start_drag')", mouse_down)
        self.assertNotIn("window-maximized", mouse_down)

    def test_desktop_shell_uses_one_integrated_titlebar(self) -> None:
        html = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        source = (Path(__file__).parent / "web" / "assets" / "theme-1.9.1.css").read_text(
            encoding="utf-8"
        )
        topbar_markup = html.split('<header class="topbar"', 1)[1].split("</header>", 1)[0]
        desktop_app = source.split(".desktop-window .app", 1)[1].split("}", 1)[0]
        desktop_topbar = source.split(".desktop-window .topbar", 1)[1].split("}", 1)[0]
        window_controls = source.split(".desktop-window .window-controls", 1)[1].split("}", 1)[0]

        self.assertNotIn("window-titlebar", html)
        self.assertIn('class="window-controls"', topbar_markup)
        self.assertIn("height: 56px", desktop_topbar)
        self.assertIn("width: 100%", desktop_app)
        self.assertIn("padding: 0", desktop_app)
        self.assertIn("display: grid", desktop_app)
        self.assertIn("align-self: stretch", window_controls)
        self.assertIn("font-size: 14px", source)

    def test_narrow_browser_topbar_can_expand_to_two_rows(self) -> None:
        source = (Path(__file__).parent / "web" / "assets" / "theme-1.9.1.css").read_text(
            encoding="utf-8"
        )
        mobile = source.split("@media (max-width: 720px)", 1)[1]
        mobile_topbar = mobile.split(".topbar {", 1)[1].split("}", 1)[0]

        self.assertIn("grid-template-rows: auto 55px minmax(0, 1fr) 26px", mobile)
        self.assertIn("height: auto", mobile_topbar)
        self.assertIn("flex-wrap: wrap", mobile_topbar)
        narrow = mobile.split("@media (max-width: 380px)", 1)[1]
        self.assertIn(".topbar-actions { flex: 1 0 100%", narrow)

    def test_account_ui_distinguishes_game_region_from_wechat_login_host(self) -> None:
        html = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        account_region_source = html.split("function accountRegion", 1)[1]
        account_region_source = account_region_source.split("function accountShortId", 1)[0]
        account_name_source = html.split("function accountName", 1)[1]
        account_name_source = account_name_source.split("function accountHealth", 1)[0]
        connect_source = html.split("async function connectAuth", 1)[1]
        connect_source = connect_source.split("async function saveFavoriteFriends", 1)[0]

        for obsolete in (
            "连接微信战绩",
            "重新连接微信",
            "当前微信战绩",
            "微信战绩已连接",
        ):
            self.assertNotIn(obsolete, html)
        self.assertIn('aria-controls="accountDialog"', html)
        self.assertIn("<h2>账号管理</h2>", html)
        self.assertIn("读取当前小程序账号", html)
        self.assertIn("account?.account_type === 'qq' ? 'QQ区'", account_region_source)
        self.assertIn("account?.account_type === 'wechat' ? '微信区'", account_region_source)
        self.assertIn("const region = accountRegion(account)", account_name_source)
        self.assertIn("正在读取电脑版微信中的三角洲小程序登录态", html)
        self.assertIn("appUrl('api/auth/import')", connect_source)
        self.assertIn("正在读取...", connect_source)
        self.assertNotIn("正在连接...", connect_source)
        self.assertIn("await loadData()", connect_source)

    def test_sync_ui_prevents_duplicate_runs_and_retries_local_fetches(self) -> None:
        html = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        sync_source = html.split("async function syncData", 1)[1]
        sync_source = sync_source.split("async function connectAuth", 1)[0]

        self.assertIn("syncRunning: false", html)
        self.assertIn("if (state.syncRunning) return", sync_source)
        self.assertIn("state.syncRunning = true", sync_source)
        self.assertIn("state.syncRunning = false", sync_source)
        self.assertIn("fetchJsonWithRetry", html)
        self.assertIn("const deadline = Date.now() + timeout", html)
        self.assertIn("const data = await response.json()", html)
        self.assertIn("本地战绩服务连接中断，请稍后重试", html)

    def test_startup_sync_recovers_only_the_expired_active_account_once(self) -> None:
        html = (Path(__file__).parent / "web" / "index.html").read_text(
            encoding="utf-8"
        )
        scheduler_source = html.split("function scheduleStartupSync", 1)[1].split(
            "async function runStartupSync", 1
        )[0]
        startup_source = html.split("async function runStartupSync", 1)[1].split(
            "async function connectAuth", 1
        )[0]
        recovery_source = html.split("async function recoverStartupAuth", 1)[1].split(
            "async function openAccountDialog", 1
        )[0]
        connect_source = html.split("async function connectAuth", 1)[1].split(
            "async function openAccountDialog", 1
        )[0]
        initializer = html.split("async function initialize()", 1)[1].split(
            "void initialize();", 1
        )[0]

        self.assertIn("startupSyncScheduled: false", html)
        self.assertIn("startupRecoveryAttempted: false", html)
        self.assertIn("if (\n        state.startupSyncScheduled", scheduler_source)
        self.assertIn("state.startupDataReady", scheduler_source)
        self.assertIn("desktopWindowInitialized", scheduler_source)
        self.assertIn("account.id !== accountId", startup_source)
        self.assertIn("account.auth_state === 'expired'", startup_source)
        self.assertIn("await syncData({manual: false})", startup_source)
        self.assertIn("result?.authInvalid", startup_source)
        self.assertIn("await recoverStartupAuth(accountId)", startup_source)
        self.assertIn("if (state.startupRecoveryAttempted", recovery_source)
        self.assertIn("candidateId: accountId", recovery_source)
        self.assertIn("state.activeAccountId !== accountId", recovery_source)
        self.assertIn("showStartupRecoveryDialog", recovery_source)
        self.assertIn("candidate_id: candidateId", connect_source)
        self.assertIn("isTransientAuthReadFailure", connect_source)
        self.assertIn("requestAnimationFrame", initializer)
        self.assertIn("scheduleStartupSync", initializer)

    def test_warfare_ui_has_an_independent_view_and_metrics(self) -> None:
        html = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn('data-mode="sol"', html)
        self.assertIn('data-mode="mp"', html)
        self.assertNotIn('data-mode="all"', html)
        self.assertIn('id="firebreakView"', html)
        self.assertIn('id="warfareView" hidden', html)
        self.assertIn('id="warfareRulesetSelect"', html)
        self.assertIn('id="warfareResultSelect"', html)
        self.assertIn('id="warfareSideSelect"', html)
        self.assertIn('id="warfareOperatorSelect"', html)

        warfare_markup = html.split('id="warfareView"', 1)[1].split("</main>", 1)[0]
        for label in ("胜率", "场均得分", "K/D", "KDA", "分均击杀", "段位分", "助攻", "救援"):
            with self.subTest(label=label):
                self.assertIn(label, warfare_markup)
        for firebreak_label in ("净收益", "带出价值", "撤离", "AI 击杀", "难度"):
            with self.subTest(firebreak_label=firebreak_label):
                self.assertNotIn(firebreak_label, warfare_markup)

    def test_firebreak_single_match_detail_omits_redundant_kd_column(self) -> None:
        html = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        theme = (Path(__file__).parent / "web" / "assets" / "theme-1.9.1.css").read_text(
            encoding="utf-8"
        )
        detail_source = html.split("function firebreakMatchHtml", 1)[1].split(
            "function renderFirebreakMatches", 1
        )[0]
        detail_grid = theme.split(".detail-row {", 1)[1].split("}", 1)[0]
        medium = theme.split("@media (max-width: 1380px)", 1)[1].split(
            "@media (max-width: 720px)", 1
        )[0]
        mobile = theme.split("@media (max-width: 720px)", 1)[1]

        self.assertNotIn("kdText", detail_source)
        self.assertNotIn("teammateKd", detail_source)
        self.assertNotIn("<span>KD</span>", detail_source)
        self.assertNotIn('detail-cell-label">KD', detail_source)
        self.assertIn("repeat(6, minmax(70px, 1fr))", detail_grid)
        self.assertIn(".detail-row { grid-template-columns: repeat(4, minmax(0, 1fr))", medium)
        self.assertIn(".detail-cell:last-child { grid-column: 1 / -1", mobile)
        self.assertIn('<div class="metric-label">平均 KD</div>', html)
        self.assertIn('<div class="friend-metric-label">平均 KD</div>', html)
        self.assertIn('<div class="warfare-metric-label">KDA</div>', html)

    def test_filter_controls_keep_semantic_widths_across_breakpoints(self) -> None:
        html = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        content = html.split('<div class="content-scroll"', 1)[1]
        responsive_time = html.split("@media (max-width: 1220px)", 1)[1].split(
            "@media (max-width: 1180px)", 1
        )[0]
        medium = html.split("@media (max-width: 1180px)", 1)[1].split(
            "@media (max-width: 720px)", 1
        )[0]
        mobile = html.split("@media (max-width: 720px)", 1)[1]

        self.assertLess(
            content.index('<header class="mode-switch-row"'),
            content.index('<section class="filter-shell"'),
        )
        self.assertIn("flex: 0 1 320px", html)
        self.assertIn("width: min(320px, 100%)", html)
        self.assertIn(
            "grid-template-columns: minmax(320px, 360px) minmax(240px, 300px)",
            html,
        )
        self.assertIn(".time-group { display: flex", responsive_time)
        self.assertIn(".apply { width: auto; }", responsive_time)
        self.assertNotIn(".mode-switch-row { grid-template-columns: 1fr", medium)
        self.assertIn(".mode-segments { flex-basis: auto; width: 100%; }", mobile)
        self.assertIn(".time-group { display: grid; grid-template-columns: 1fr", mobile)
        mobile_touch_targets = mobile.split(
            ".mode-switch-row .segments button,", 1
        )[1].split("}", 1)[0]
        self.assertIn(".filter-shell .apply", mobile_touch_targets)
        self.assertIn("min-height: 40px", mobile_touch_targets)

        instant = html.split('class="time-group time-instant-group"', 1)[1].split(
            'class="time-group time-custom-group"', 1
        )[0]
        custom = html.split('class="time-group time-custom-group"', 1)[1].split(
            'class="friend-row"', 1
        )[0]
        self.assertLess(instant.index('id="presets"'), instant.index('id="sessionPicker"'))
        self.assertLess(custom.index('id="fromInput"'), custom.index('id="toInput"'))
        self.assertLess(custom.index('id="toInput"'), custom.index('id="applyButton"'))

    def test_friend_and_session_filters_support_mode_scoped_multi_selection(self) -> None:
        html = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        defaults = html.split("function createDefaultModeFilters", 1)[1].split(
            "const state", 1
        )[0]
        snapshot = html.split("function snapshotModeFilters", 1)[1].split(
            "function restoreModeFilters", 1
        )[0]
        restore = html.split("function restoreModeFilters", 1)[1].split(
            "function saveViewPreferences", 1
        )[0]
        load_data = html.split("async function loadData", 1)[1].split(
            "async function waitForSync", 1
        )[0]
        mode_switch = html.split("$('modeSegments').addEventListener", 1)[1].split(
            "$('playerSelect')", 1
        )[0]
        session_events = html.split("$('sessionOptions').addEventListener", 1)[1].split(
            "$('favoriteFilters')", 1
        )[0]
        instant = html.split('class="time-group time-instant-group"', 1)[1].split(
            'class="time-group time-custom-group"', 1
        )[0]

        self.assertIn('id="sessionPicker"', instant)
        self.assertIn('id="sessionOptions"', instant)
        self.assertIn('id="clearSessionsButton"', instant)
        self.assertIn('id="closeSessionsButton"', instant)
        self.assertNotIn('id="sessionSelect"', html)
        self.assertNotIn("<select", instant)
        self.assertIn('id="friendModeSegments"', html)
        self.assertIn('data-friend-mode="any"', html)
        self.assertIn('data-friend-mode="all"', html)
        self.assertIn("friendMode: 'any'", defaults)
        self.assertIn("sessions: []", defaults)
        self.assertIn("activeSessionIds: new Set()", html)
        self.assertIn("friendMode: state.friendMode", snapshot)
        self.assertIn("sessions: [...state.activeSessionIds]", snapshot)
        self.assertIn("state.friendMode = normalizeFriendMode(filters.friendMode);", restore)
        self.assertIn("state.activeSessionIds = savedSessionIds(filters.sessions);", restore)
        self.assertIn("params.set('friend_mode', state.friendMode);", load_data)
        self.assertIn("params.append('session', sessionId)", load_data)
        self.assertIn("data-session-id", html)
        self.assertIn("function commitSessionSelection", html)
        self.assertIn("$('sessionPicker').addEventListener('toggle'", html)
        self.assertIn("$('fromInput').value = ''", session_events)
        self.assertIn("renderFavoriteFilters({preserveSelection: true});", mode_switch)

    def test_session_picker_uses_a_popover_without_expanding_the_filter_panel(self) -> None:
        html = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        filter_shell = html.split(".filter-shell {", 1)[1].split("}", 1)[0]
        picker_panel = html.split(".session-picker-panel {", 1)[1].split("}", 1)[0]
        mobile = html.split("@media (max-width: 720px)", 1)[1]
        mobile_picker = mobile.split(".session-picker-panel {", 1)[1].split("}", 1)[0]

        self.assertIn("overflow: visible", filter_shell)
        self.assertIn("position: absolute", picker_panel)
        self.assertIn("z-index: 20", picker_panel)
        self.assertIn("top: calc(100% + 4px)", picker_panel)
        self.assertIn("right: 0", picker_panel)
        self.assertIn("display: flex", picker_panel)
        self.assertIn("flex-direction: column", picker_panel)
        self.assertIn("width: min(500px, calc(100vw - 24px))", picker_panel)
        self.assertIn("max-height: min(320px", picker_panel)
        self.assertNotIn("margin-top", picker_panel)
        self.assertIn("width: 100%", mobile_picker)
        self.assertIn("max-width: 100%", mobile_picker)
        self.assertIn("max-height: min(300px", mobile_picker)
        self.assertIn(".preset-field, .session-field, .date-field { width: 100%; min-width: 0; }", mobile)

    def test_session_picker_rows_stay_compact_and_scroll_inside_the_popover(self) -> None:
        html = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        theme = (Path(__file__).parent / "web" / "assets" / "theme-1.9.1.css").read_text(
            encoding="utf-8"
        )
        options_css = html.split(".session-options {", 1)[1].split("}", 1)[0]
        option_css = html.split(".field .session-option {", 1)[1].split("}", 1)[0]
        footer_css = html.split(".session-picker-footer {", 1)[1].split("}", 1)[0]
        position_source = html.split("function positionSessionPicker", 1)[1].split(
            "function renderSessionPicker", 1
        )[0]
        session_events = html.split("$('sessionOptions').addEventListener", 1)[1].split(
            "$('fromInput').addEventListener", 1
        )[0]

        self.assertIn(".field > label", html)
        self.assertIn(".field > input", html)
        self.assertIn(".field > label", theme)
        self.assertIn(".field > input", theme)
        self.assertNotIn(".field label,", html)
        self.assertNotIn(".field input,", html)
        self.assertIn("flex: 1 1 auto", options_css)
        self.assertIn("min-height: 0", options_css)
        self.assertIn("overflow-y: auto", options_css)
        self.assertIn("overscroll-behavior: contain", options_css)
        self.assertIn("display: grid", option_css)
        self.assertIn("min-height: 56px", option_css)
        self.assertIn("margin: 0", option_css)
        self.assertIn("flex: 0 0 auto", footer_css)
        self.assertNotIn("position: sticky", footer_css)
        self.assertIn("getBoundingClientRect", position_source)
        self.assertIn("window.visualViewport?.height", position_source)
        self.assertIn("open-upward", position_source)
        self.assertIn("--session-picker-max-height", position_source)
        self.assertIn("renderSessionSelectionSummary()", session_events)
        self.assertNotIn("renderSessionPicker()", session_events)
        self.assertIn("$('contentScroll').addEventListener('scroll'", html)
        self.assertIn("window.visualViewport?.addEventListener('resize'", html)

    def test_session_picker_uses_backend_profit_coverage_and_rich_rows(self) -> None:
        html = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        profit_source = html.split("function sessionProfit", 1)[1].split(
            "function signedNumber", 1
        )[0]
        render_source = html.split("function renderSessionPicker", 1)[1].split(
            "function renderSessions", 1
        )[0]
        selection_source = html.split("function renderSessionSelectionSummary", 1)[1].split(
            "function positionSessionPicker", 1
        )[0]
        footer_css = html.split(".session-picker-footer {", 1)[1].split("}", 1)[0]

        self.assertIn('id="sessionSelectionSummary"', html)
        self.assertIn("flex: 0 0 auto", footer_css)
        self.assertIn("numberOrNull(session?.net_profit)", profit_source)
        self.assertIn("numberOrNull(session?.profit_matches)", profit_source)
        self.assertIn("value !== null && matches !== null && matches > 0", profit_source)
        self.assertNotIn("session.net_profit || 0", profit_source)
        self.assertIn("session-option-title", render_source)
        self.assertIn("session-option-range", render_source)
        self.assertIn("session-option-meta", render_source)
        self.assertIn("session-option-profit", render_source)
        self.assertIn("好友条件", render_source)
        self.assertNotIn("compactName", render_source)
        self.assertIn("function sessionPeriodLabel", html)
        self.assertIn("次日 ${sessionTime(session.to)}", html)
        self.assertIn("selectedSessionSummary", selection_source)
        self.assertIn("收益覆盖", html)

    def test_manual_sync_uses_structured_failure_recovery_dialog(self) -> None:
        html = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        normalizer = html.split("function normalizeFailure", 1)[1].split(
            "function responseFailure", 1
        )[0]
        sync_source = html.split("async function syncData", 1)[1].split(
            "function scheduleStartupSync", 1
        )[0]
        wait_source = html.split("async function waitForSync", 1)[1].split(
            "async function syncData", 1
        )[0]
        auth_source = html.split("async function connectAuth", 1)[1].split(
            "async function openAccountDialog", 1
        )[0]
        cancellation_source = html.split("function cancelAuthRecovery", 1)[1].split(
            "function recoveryActions", 1
        )[0]
        actions_source = html.split("function recoveryActions", 1)[1].split(
            "function showRecoveryDialog", 1
        )[0]
        dialog_source = html.split("function showRecoveryDialog", 1)[1].split(
            "function closeRecoveryDialog", 1
        )[0]
        baseline_source = html.split("async function captureAuthRecoveryRevision", 1)[1].split(
            "function showDetectedRecoveryAccount", 1
        )[0]
        attempt_source = html.split("async function attemptAuthRecovery", 1)[1].split(
            "async function openWechatRecovery", 1
        )[0]
        discovery_source = html.split("async function attemptAuthDiscovery", 1)[1].split(
            "async function openWechatRecovery", 1
        )[0]
        open_source = html.split("async function openWechatRecovery", 1)[1].split(
            "async function useDetectedRecoveryAccount", 1
        )[0]
        detected_source = html.split("async function useDetectedRecoveryAccount", 1)[1].split(
            "function showAccountRecovery", 1
        )[0]
        account_health = html.split("function accountHealth", 1)[1].split(
            "function accountCacheText", 1
        )[0]

        for element_id in (
            "recoveryDialog",
            "recoveryMessage",
            "openWechatButton",
            "rereadAuthButton",
            "retrySyncButton",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn('id="openWechatButton" hidden>刷新登录态</button>', html)
        self.assertNotIn('id="openWechatButton" hidden>打开微信</button>', html)
        self.assertNotIn('id="rereadAuthButton" hidden>重新读取登录态</button>', html)
        for field in (
            "error_kind",
            "retryable",
            "auth_invalid",
            "auth_state",
            "retry_after_seconds",
        ):
            self.assertIn(field, normalizer)
        self.assertIn("const manual = Boolean(options?.manual)", sync_source)
        self.assertIn("if (manual) showRecoveryDialog", sync_source)
        self.assertIn("throw responseFailure(job, '同步失败')", wait_source)
        self.assertIn("appUrl('api/auth/candidates')", baseline_source)
        self.assertIn("target?.credential_revision", baseline_source)
        self.assertIn("account?.credential_revision", open_source)
        self.assertNotIn("await captureAuthRecoveryRevision", open_source)
        self.assertLess(
            open_source.index("appUrl('api/wechat/open')"),
            open_source.index("attemptAuthRecovery"),
        )
        self.assertIn("AUTH_RECOVERY_TIMEOUT_MS = 45000", html)
        self.assertIn("candidateId: accountId", attempt_source)
        self.assertIn("recovery: true", attempt_source)
        self.assertIn("knownRevision,", attempt_source)
        self.assertIn("singleAttempt: true", attempt_source)
        self.assertIn("syncData({manual: true})", attempt_source)
        self.assertNotIn("connectAuth()", attempt_source)
        self.assertIn("readAuthCandidates", discovery_source)
        self.assertIn("candidateId,", discovery_source)
        self.assertIn("allowCandidateSwitch: true", discovery_source)
        self.assertIn("singleAttempt: true", discovery_source)
        self.assertIn("syncData({manual: true})", discovery_source)
        self.assertIn("attemptAuthDiscovery", open_source)
        self.assertLess(
            open_source.index("appUrl('api/wechat/open')"),
            open_source.index("attemptAuthDiscovery"),
        )
        self.assertIn("state.authRecoveryRunId += 1", cancellation_source)
        self.assertIn("state.authRecoveryController.abort()", cancellation_source)
        self.assertIn("source === 'sync' && /busy|rate/.test(info.errorKind)", actions_source)
        self.assertIn("info.errorKind === 'auth_missing'", actions_source)
        self.assertIn("showAuthRecovery: accountDiscovery || authRequired || authOptional", actions_source)
        self.assertIn("!actions.showAuthRecovery", dialog_source)
        self.assertIn("actions.accountDiscovery || source !== 'auth'", dialog_source)
        self.assertIn("actions.authRequired || source !== 'sync'", dialog_source)
        self.assertIn("requestBody.recovery = true", auth_source)
        self.assertIn("requestBody.known_revision", auth_source)
        self.assertIn("'waiting_for_miniapp', 'account_mismatch'", auth_source)
        self.assertIn("status === 'account_mismatch'", attempt_source)
        self.assertIn("data.detected_account", attempt_source)
        self.assertIn("AUTH_RECOVERY_VALIDATION_ATTEMPTS = 2", html)
        self.assertIn("blockedRevision = credentialRevision", attempt_source)
        self.assertIn("!hasValue(currentRevision) || currentRevision === blockedRevision", attempt_source)
        self.assertIn("$('rereadAuthButton').disabled = true", attempt_source)
        self.assertNotIn("knownRevision = credentialRevision", attempt_source)
        self.assertIn("if (saved) await switchAccount(detected.id)", detected_source)
        self.assertIn(
            "else await connectAuth({candidateId: detected.id, allowCandidateSwitch: true})",
            detected_source,
        )
        self.assertIn("retryCurrent", detected_source)
        self.assertIn("const allowCandidateSwitch = Boolean(options?.allowCandidateSwitch)", auth_source)
        self.assertIn("&& !allowCandidateSwitch", auth_source)
        self.assertIn("data?.pending", auth_source)
        self.assertIn("pending_verification", auth_source)
        self.assertIn("maxPendingAttempts", auth_source)
        self.assertIn("retry_max_attempts", auth_source)
        self.assertLess(auth_source.index("data?.pending"), auth_source.index("resetAccountScopedState()"))
        self.assertLess(
            account_health.index("authState === 'pending_verification'"),
            account_health.index("accountCanSync(account)"),
        )

    def test_login_recovery_actions_and_timeout_cancellation_are_distinct(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed")
        checker = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[1], 'utf8');
const timeoutStart = html.indexOf('function fetchTimeoutError');
const timeoutEnd = html.indexOf('async function fetchJsonWithRetry', timeoutStart);
const actionStart = html.indexOf('function recoveryActions');
const actionEnd = html.indexOf('function showRecoveryDialog', actionStart);
if (timeoutStart < 0 || timeoutEnd < 0 || actionStart < 0 || actionEnd < 0) {
  throw new Error('login recovery helpers are missing');
}
const timeoutHelpers = new Function(
  html.slice(timeoutStart, timeoutEnd) +
  'return {fetchJsonWithTimeout};'
)();
const recoveryActions = new Function(
  'isExpiredAuthFailure',
  'isPendingAuthState',
  html.slice(actionStart, actionEnd) + 'return recoveryActions;'
)(
  (info) => Boolean(info.authInvalid) || info.authState === 'expired',
  (value) => value === 'pending_verification'
);
const busy = recoveryActions(
  {errorKind: 'busy', authInvalid: false, authState: ''},
  {source: 'sync', recoveryAccountId: 'account-a', currentAuthState: 'valid'}
);
if (!busy.showAuthRecovery || busy.authRequired) throw new Error(JSON.stringify(busy));
const expired = recoveryActions(
  {errorKind: 'auth_expired', authInvalid: true, authState: 'expired'},
  {source: 'sync', recoveryAccountId: 'account-a', currentAuthState: 'expired'}
);
if (!expired.showAuthRecovery || !expired.authRequired) throw new Error(JSON.stringify(expired));
const noAccount = recoveryActions(
  {errorKind: 'busy', authInvalid: false, authState: ''},
  {source: 'sync', recoveryAccountId: '', currentAuthState: 'valid'}
);
if (noAccount.showAuthRecovery || noAccount.authRequired) throw new Error(JSON.stringify(noAccount));
const firstAccount = recoveryActions(
  {errorKind: 'auth_missing', authInvalid: false, authState: ''},
  {source: 'auth', recoveryAccountId: '', currentAuthState: ''}
);
if (!firstAccount.showAuthRecovery || firstAccount.authRequired || !firstAccount.accountDiscovery) {
  throw new Error(JSON.stringify(firstAccount));
}
const unknownAuthFailure = recoveryActions(
  {errorKind: 'unknown', authInvalid: false, authState: ''},
  {source: 'auth', recoveryAccountId: '', currentAuthState: ''}
);
if (unknownAuthFailure.showAuthRecovery || unknownAuthFailure.accountDiscovery) {
  throw new Error(JSON.stringify(unknownAuthFailure));
}

global.fetch = (_url, options) => new Promise((_resolve, reject) => {
  options.signal.addEventListener('abort', () => {
    const error = new Error('aborted');
    error.name = 'AbortError';
    reject(error);
  }, {once: true});
});
(async () => {
  try {
    await timeoutHelpers.fetchJsonWithTimeout('http://local.test', {}, 5);
    throw new Error('timeout did not reject');
  } catch (error) {
    if (error.name !== 'TimeoutError' || error.errorKind !== 'timeout') throw error;
  }
  const parent = new AbortController();
  const request = timeoutHelpers.fetchJsonWithTimeout(
    'http://local.test',
    {signal: parent.signal},
    1000
  );
  parent.abort();
  try {
    await request;
    throw new Error('parent cancellation did not reject');
  } catch (error) {
    if (error.name !== 'AbortError') throw error;
  }
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""
        result = subprocess.run(
            [node, "-e", checker, str(Path(__file__).parent / "web" / "index.html")],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_first_account_discovery_connects_detected_candidate_and_times_out_cleanly(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed")
        checker = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[1], 'utf8');
const start = html.indexOf('async function attemptAuthDiscovery');
const end = html.indexOf('async function openWechatRecovery', start);
if (start < 0 || end < 0) throw new Error('account discovery helper is missing');
const createAttempt = new Function(
  'state', '$', 'AUTH_RECOVERY_TIMEOUT_MS', 'AUTH_RECOVERY_VALIDATION_ATTEMPTS',
  'recoveryRunIsActive', 'readAuthCandidates', 'hasValue', 'connectAuth',
  'normalizeFailure', 'accountName', 'activeAccount', 'syncData',
  'closeRecoveryDialog', 'isExpiredAuthFailure', 'isTransientFailure',
  'waitForRecovery', 'setRecoveryBusy', 'responseFailure', 'numberOrNull', 'Date',
  html.slice(start, end) + 'return attemptAuthDiscovery;'
);

function harness(timeout, candidateRows, overrides = {}) {
  const calls = [];
  const elements = new Map();
  const clock = {now: 0};
  const state = {
    authRecoveryRunId: 7,
    authRecoveryController: {signal: new AbortController().signal},
    recoveryContext: {accountId: '', timedOut: false},
    activeAccountId: null,
  };
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {textContent: '', disabled: false});
    return elements.get(id);
  };
  let importOptions = null;
  const attempt = createAttempt(
    state,
    element,
    timeout,
    2,
    (runId, accountId) => (
      state.authRecoveryRunId === runId
      && state.recoveryContext?.accountId === accountId
      && !state.authRecoveryController.signal.aborted
    ),
    async () => {
      calls.push('candidates');
      if (overrides.readCandidates) return overrides.readCandidates({state, calls, clock});
      return candidateRows;
    },
    (value) => value !== null && value !== undefined && value !== '',
    async (options) => {
      calls.push(`import:${options.candidateId}`);
      importOptions = options;
      if (overrides.connect) return overrides.connect(options, {state, calls, clock});
      state.activeAccountId = options.candidateId;
      return {ok: true, accountId: options.candidateId};
    },
    (value, fallback) => ({
      message: value?.message || value?.error || fallback,
      errorKind: value?.error_kind || 'unknown',
      authInvalid: Boolean(value?.auth_invalid),
      retryable: Boolean(value?.retryable),
    }),
    (account) => account?.display_name || account?.id || '当前账号',
    () => ({id: state.activeAccountId, display_name: '测试账号'}),
    async () => { calls.push('sync'); return {ok: true}; },
    () => { calls.push('close'); },
    (failure) => failure?.authInvalid || failure?.errorKind === 'expired',
    (failure) => Boolean(failure?.retryable) || ['busy', 'network', 'timeout', 'operation_busy'].includes(failure?.errorKind),
    async (milliseconds) => {
      calls.push('wait');
      clock.now += milliseconds;
      if (overrides.wait) await overrides.wait(milliseconds, {state, calls, clock});
    },
    (busy) => { calls.push(`busy:${busy}`); },
    (data, fallback) => {
      const error = new Error(data?.message || data?.error || fallback);
      error.failurePayload = data;
      return error;
    },
    (value) => {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    },
    {now: () => clock.now},
  );
  return {attempt, calls, elements, state, clock, getImportOptions: () => importOptions};
}

(async () => {
  const success = harness(45000, [{
    id: 'candidate-a',
    credential_revision: 'revision-a',
  }]);
  const outcome = await success.attempt({runId: 7, signal: success.state.authRecoveryController.signal});
  if (!outcome.ok) throw new Error(JSON.stringify(outcome));
  if (success.calls.join('|') !== 'candidates|import:candidate-a|sync|close') {
    throw new Error(success.calls.join('|'));
  }
  const options = success.getImportOptions();
  if (!options.allowCandidateSwitch || !options.singleAttempt || options.recovery) {
    throw new Error(JSON.stringify(options));
  }

  const timeout = harness(0, []);
  const timedOut = await timeout.attempt({runId: 7, signal: timeout.state.authRecoveryController.signal});
  if (!timedOut.timedOut || timeout.calls.includes('sync')) throw new Error(JSON.stringify(timedOut));
  if (!timeout.state.recoveryContext.timedOut) throw new Error('timeout state was not recorded');
  if (timeout.elements.get('openWechatButton')?.textContent !== '重新检测') {
    throw new Error(JSON.stringify(timeout.elements.get('openWechatButton')));
  }

  const busy = harness(45000, [
    {id: 'newest', credential_revision: 'newest-r1'},
    {id: 'older', credential_revision: 'older-r1'},
  ], {
    connect: async () => ({
      ok: false,
      failure: {message: '腾讯繁忙', error_kind: 'busy', retryable: true},
      data: {retry_after_seconds: 2},
    }),
  });
  const busyOutcome = await busy.attempt({runId: 7, signal: busy.state.authRecoveryController.signal});
  if (!busyOutcome.timedOut) throw new Error(JSON.stringify(busyOutcome));
  const imports = busy.calls.filter((call) => call.startsWith('import:'));
  if (imports.join('|') !== 'import:newest|import:newest') throw new Error(imports.join('|'));
  if (busy.calls.includes('import:older')) throw new Error('older candidate was imported');

  let networkAttempts = 0;
  const networkRecovery = harness(45000, [{id: 'candidate-a', credential_revision: 'r1'}], {
    connect: async (options, {state}) => {
      networkAttempts += 1;
      if (networkAttempts <= 2) {
        return {
          ok: false,
          failure: {message: '网络中断', error_kind: 'network', retryable: true},
          data: {},
        };
      }
      state.activeAccountId = options.candidateId;
      return {ok: true, accountId: options.candidateId};
    },
  });
  const networkOutcome = await networkRecovery.attempt({runId: 7, signal: networkRecovery.state.authRecoveryController.signal});
  if (!networkOutcome.ok || networkAttempts !== 3) {
    throw new Error(JSON.stringify({networkOutcome, networkAttempts, calls: networkRecovery.calls}));
  }

  let operationBusyAttempts = 0;
  const operationBusyRecovery = harness(45000, [{id: 'candidate-a', credential_revision: 'r1'}], {
    connect: async (options, {state}) => {
      operationBusyAttempts += 1;
      if (operationBusyAttempts <= 2) {
        return {
          ok: false,
          failure: {message: '另一个操作正在进行', error_kind: 'operation_busy', retryable: true},
          data: {retry_after_seconds: 1},
        };
      }
      state.activeAccountId = options.candidateId;
      return {ok: true, accountId: options.candidateId};
    },
  });
  const operationBusyOutcome = await operationBusyRecovery.attempt({runId: 7, signal: operationBusyRecovery.state.authRecoveryController.signal});
  if (!operationBusyOutcome.ok || operationBusyAttempts !== 3) {
    throw new Error(JSON.stringify({operationBusyOutcome, operationBusyAttempts, calls: operationBusyRecovery.calls}));
  }

  const shortDeadline = harness(500, [{id: 'candidate-a', credential_revision: 'r1'}]);
  const shortOutcome = await shortDeadline.attempt({runId: 7, signal: shortDeadline.state.authRecoveryController.signal});
  if (!shortOutcome.timedOut || shortDeadline.calls.some((call) => call.startsWith('import:'))) {
    throw new Error(JSON.stringify({shortOutcome, calls: shortDeadline.calls}));
  }

  const readFailure = harness(45000, [], {
    readCandidates: async () => { throw new Error('candidate read failed'); },
    wait: async (_milliseconds, {state}) => { state.authRecoveryRunId += 1; },
  });
  const readOutcome = await readFailure.attempt({runId: 7, signal: readFailure.state.authRecoveryController.signal});
  if (!readOutcome.cancelled) throw new Error(JSON.stringify(readOutcome));
  if (readFailure.elements.get('recoveryMessage')?.textContent !== 'candidate read failed') {
    throw new Error(JSON.stringify(readFailure.elements.get('recoveryMessage')));
  }
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
"""
        result = subprocess.run(
            [node, "-e", checker, str(Path(__file__).parent / "web" / "index.html")],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_invalid_account_entries_open_recovery_without_automatic_wechat_launch(self) -> None:
        html = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        trigger_source = html.split("function renderAccountTrigger", 1)[1].split(
            "function renderAccountDialog", 1
        )[0]
        account_dialog_source = html.split("function renderAccountDialog", 1)[1].split(
            "function renderAccounts", 1
        )[0]
        event_source = html.split("$('connectButton').addEventListener", 1)[1].split(
            "function setAppMenuOpen", 1
        )[0]
        startup_source = html.split("async function recoverStartupAuth", 1)[1].split(
            "async function openAccountDialog", 1
        )[0]

        self.assertIn("$('refreshButton').disabled = !account", trigger_source)
        self.assertIn("刷新当前账号登录态", trigger_source)
        self.assertIn('data-account-action="recover"', account_dialog_source)
        self.assertIn(">刷新登录态</button>", account_dialog_source)
        self.assertIn("else showAccountRecovery();", event_source)
        self.assertIn("button.dataset.accountAction === 'recover'", event_source)
        self.assertIn("showAccountRecovery", event_source)
        self.assertNotIn("api/wechat/open", startup_source)
        self.assertIn("!account?.has_credential", html)
        self.assertIn("isPendingAuthState(account?.auth_state)", html)
        self.assertIn("retryTimedOutRun", html)

    def test_update_progress_shows_recent_average_and_resume_retry_state(self) -> None:
        html = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        normalize_source = html.split("function normalizeUpdateStatus", 1)[1].split(
            "function updatePhaseNeedsPolling", 1
        )[0]
        render_source = html.split("function renderUpdateStatus", 1)[1].split(
            "async function callUpdateApi", 1
        )[0]

        for element_id in (
            "updateDownloadMeta",
            "updateRecentSpeed",
            "updateAverageSpeed",
            "updateRetryState",
        ):
            self.assertIn(f'id="{element_id}"', html)
        for backend_field in (
            "recent_speed_bps",
            "average_speed_bps",
            "retrying",
            "retry_count",
            "retry_limit",
        ):
            self.assertIn(backend_field, normalize_source)
        self.assertIn("has_recent_speed: hasRecentSpeed", normalize_source)
        self.assertIn("has_average_speed: hasAverageSpeed", normalize_source)
        self.assertIn("formatUpdateSpeed(status.recent_speed_bps)", render_source)
        self.assertIn("formatUpdateSpeed(status.average_speed_bps)", render_source)
        self.assertIn("`全程平均 ${averageSpeed}`", render_source)
        self.assertIn("$('updateDownloadMeta').hidden = !showDownloadMeta", render_source)
        self.assertIn("$('updateRetryState').hidden = !status.retrying", render_source)
        self.assertIn("正在断点重连（${number.format(status.retry_count)}/${number.format(status.retry_limit)}）", render_source)

    def test_desktop_window_uses_restored_shadow_and_native_rounding(self) -> None:
        source = (Path(__file__).parent / "friend_client.py").read_text(encoding="utf-8")
        prepare_window = source.split("def _prepare_window_before_show", 1)[1]
        prepare_window = prepare_window.split("def _mark_maximized", 1)[0]

        self.assertIn("window.events.before_show += self._prepare_window_before_show", source)
        self.assertIn("native.WindowState = FormWindowState.Normal", prepare_window)
        self.assertNotIn("FormWindowState.Maximized", prepare_window)
        self.assertIn("round(work.Width * 0.82)", source)
        self.assertIn("native.MaximizedBounds = Screen.FromHandle(native.Handle).WorkingArea", source)
        self.assertIn(f"DWMWA_WINDOW_CORNER_PREFERENCE = {DWMWA_WINDOW_CORNER_PREFERENCE}", source)
        self.assertIn(f"DWMWCP_DONOTROUND = {DWMWCP_DONOTROUND}", source)
        self.assertIn(f"DWMWCP_ROUND = {DWMWCP_ROUND}", source)
        self.assertIn("shadow=True", source)
        self.assertIn("windll.user32.IsZoomed(handle)", source)
        self.assertNotIn("maximized=True", source)
        self.assertNotIn("SetWindowPos", source)
        self.assertIn("window.events.loaded += mark_desktop_ready", source)


if __name__ == "__main__":
    unittest.main()
