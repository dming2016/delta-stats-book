#!/usr/bin/env python3
"""Regression tests for the lightweight desktop launcher."""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path
from subprocess import TimeoutExpired
from unittest.mock import call, patch

import launcher
from launcher import (
    CREATE_NO_WINDOW,
    LEGACY_STARTUP_GRACE_SECONDS,
    PROCESS_STOP_TIMEOUT_SECONDS,
    apply_pending,
    launch_app,
    main,
    resolve_active_app,
    schedule_launcher_replacement,
)
from startup_protocol import STARTUP_READY_FILE_ENV, STARTUP_READY_VALUE
from update_protocol import (
    INSTALL_KIND_LEGACY,
    INSTALL_KIND_VERSIONED,
    PACKAGE_FORMAT,
    read_version_pointer,
)


class LauncherTests(unittest.TestCase):
    @patch("launcher.subprocess.Popen")
    def test_launch_app_waits_for_explicit_page_ready_signal(self, popen) -> None:
        process = popen.return_value
        process.poll.return_value = None

        def report_ready(_command, *, cwd, env):
            self.assertEqual(cwd, "app")
            Path(env[STARTUP_READY_FILE_ENV]).write_text(STARTUP_READY_VALUE, encoding="utf-8")
            return process

        popen.side_effect = report_ready
        result = launch_app(Path("DeltaStatsApp.exe"), Path("app"), environment={"TEST": "1"})
        self.assertIs(result, process)
        self.assertEqual(popen.call_args.kwargs["env"]["TEST"], "1")

    @patch("launcher.subprocess.Popen")
    def test_launch_app_reports_immediate_exit(self, popen) -> None:
        popen.return_value.poll.return_value = 7
        with self.assertRaisesRegex(RuntimeError, "退出码 7"):
            launch_app(Path("DeltaStatsApp.exe"), Path("app"))

    @patch("launcher.subprocess.Popen")
    def test_launch_app_legacy_mode_uses_process_survival_check(self, popen) -> None:
        process = popen.return_value
        process.wait.side_effect = TimeoutExpired(
            cmd="DeltaStatsApp.exe",
            timeout=LEGACY_STARTUP_GRACE_SECONDS,
        )
        result = launch_app(Path("DeltaStatsApp.exe"), Path("app"), require_ready=False)
        self.assertIs(result, process)
        popen.assert_called_once_with(["DeltaStatsApp.exe"], cwd="app", env=None)

    @patch("launcher.STARTUP_GRACE_SECONDS", 0.01)
    @patch("launcher.subprocess.run")
    @patch("launcher.subprocess.Popen")
    def test_launch_app_stops_process_tree_that_never_reports_ready(self, popen, run) -> None:
        process = popen.return_value
        process.poll.return_value = None
        process.pid = 4321
        run.return_value.returncode = 0
        with self.assertRaisesRegex(RuntimeError, "未在 0.01 秒内完成页面加载"):
            launch_app(Path("DeltaStatsApp.exe"), Path("app"))
        run.assert_called_once_with(
            ["taskkill", "/PID", "4321", "/T", "/F"],
            stdout=-3,
            stderr=-3,
            creationflags=CREATE_NO_WINDOW,
            timeout=PROCESS_STOP_TIMEOUT_SECONDS,
            check=False,
        )

    def test_self_replacement_supports_chinese_install_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="三角洲更新测试-!%-") as temp_dir:
            root = Path(temp_dir)
            current = root / "三角洲情报助手.exe"
            staged = root / ".updates" / "launcher-1.6.0.exe"
            staged.parent.mkdir()
            current.write_bytes(b"old-launcher")
            staged.write_bytes(b"new-launcher")
            process = schedule_launcher_replacement(staged, current)
            try:
                # Cold PowerShell startup on hosted Windows can exceed five seconds.
                self.assertEqual(process.wait(timeout=30), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
            self.assertEqual(current.read_bytes(), b"new-launcher")
            self.assertFalse(staged.exists())

    @patch("launcher.subprocess.Popen")
    def test_replacement_script_uses_ascii_and_unicode_environment(self, popen) -> None:
        with tempfile.TemporaryDirectory(prefix="路径-!%-") as directory:
            root = Path(directory).resolve()
            staged = root / ".updates" / "launcher.exe"
            staged.parent.mkdir()
            staged.write_bytes(b"new")
            current = root / "三角洲情报助手.exe"
            schedule_launcher_replacement(staged, current)
            text = staged.with_suffix(".ps1").read_text(encoding="ascii")
            self.assertIn("Move-Item -LiteralPath", text)
            self.assertNotIn(str(staged), text)
            self.assertEqual(popen.call_args.kwargs["env"]["DELTA_REPLACE_SOURCE"], str(staged))
            self.assertEqual(popen.call_args.kwargs["env"]["DELTA_REPLACE_TARGET"], str(current))
            self.assertEqual(popen.call_args.args[0][0], "powershell.exe")
            self.assertEqual(popen.call_args.kwargs["creationflags"], CREATE_NO_WINDOW)

    @patch("launcher.subprocess.Popen")
    def test_replacement_rejects_stage_outside_install_update_directory(self, popen) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged = root / "outside.exe"
            with self.assertRaisesRegex(RuntimeError, "更新区"):
                schedule_launcher_replacement(staged, root / "launcher.exe")
            self.assertFalse(staged.with_suffix(".ps1").exists())
            popen.assert_not_called()

    def test_resolve_active_app_prefers_valid_current_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            legacy = root / "app" / "DeltaStatsApp.exe"
            current = root / "app" / "versions" / "1.6.0" / "DeltaStatsApp.exe"
            current.parent.mkdir(parents=True)
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_bytes(b"legacy")
            current.write_bytes(b"onedir")
            (root / "app" / "current.json").write_text(
                '{"schema":1,"version":"1.6.0"}', encoding="utf-8"
            )
            self.assertEqual(
                resolve_active_app(root),
                (current, INSTALL_KIND_VERSIONED, "1.6.0"),
            )

    def test_resolve_active_app_falls_back_when_pointer_target_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            legacy = root / "app" / "DeltaStatsApp.exe"
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b"legacy")
            (root / "app" / "version.txt").write_text("1.5.8", encoding="utf-8")
            (root / "app" / "current.json").write_text(
                '{"schema":1,"version":"1.6.0"}', encoding="utf-8"
            )
            self.assertEqual(
                resolve_active_app(root),
                (legacy, INSTALL_KIND_LEGACY, "1.5.8"),
            )

    def test_resolve_active_app_uses_previous_version_before_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            previous = root / "app" / "versions" / "1.5.9" / "DeltaStatsApp.exe"
            legacy = root / "app" / "DeltaStatsApp.exe"
            previous.parent.mkdir(parents=True)
            legacy.parent.mkdir(parents=True, exist_ok=True)
            previous.write_bytes(b"previous")
            legacy.write_bytes(b"legacy")
            (root / "app" / "current.json").write_text(
                '{"schema":1,"version":"1.6.0"}', encoding="utf-8"
            )
            (root / "app" / "previous.json").write_text(
                '{"schema":1,"version":"1.5.9"}', encoding="utf-8"
            )
            self.assertEqual(
                resolve_active_app(root),
                (previous, INSTALL_KIND_VERSIONED, "1.5.9"),
            )

    def test_non_object_version_pointers_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pointer = Path(temporary).resolve() / "current.json"
            for payload in ("null", "[]", '"1.8.4"'):
                with self.subTest(payload=payload):
                    pointer.write_text(payload, encoding="utf-8")
                    self.assertIsNone(read_version_pointer(pointer))

    @patch("launcher.app_instance_running", return_value=False)
    @patch("launcher.launch_app")
    @patch("launcher.install_root")
    def test_normal_start_never_checks_the_network(self, root_mock, launch_mock, _running) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            app = root / "app" / "DeltaStatsApp.exe"
            app.parent.mkdir(parents=True)
            app.write_bytes(b"app")
            (app.parent / "version.txt").write_text("1.5.8", encoding="utf-8")
            root_mock.return_value = root
            self.assertEqual(main([]), 0)
        launch_mock.assert_called_once()
        source = Path(launcher.__file__).read_text(encoding="utf-8")
        self.assertNotIn("urlopen", source)
        self.assertNotIn("updater_ui", source)

    @patch("launcher.show_error")
    @patch("launcher.sync_installed_version_metadata")
    @patch("launcher.app_instance_running", return_value=False)
    @patch("launcher.launch_app")
    @patch("launcher.install_root")
    def test_normal_start_rolls_back_to_previous_after_current_fails(
        self,
        root_mock,
        launch_mock,
        _running,
        sync_mock,
        show_error_mock,
    ) -> None:
        launch_mock.side_effect = [RuntimeError("current failed"), object()]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root_mock.return_value = root
            current = root / "app" / "versions" / "1.8.4" / "DeltaStatsApp.exe"
            previous = root / "app" / "versions" / "1.8.3" / "DeltaStatsApp.exe"
            current.parent.mkdir(parents=True)
            previous.parent.mkdir(parents=True)
            current.write_bytes(b"current")
            previous.write_bytes(b"previous")
            (root / "app" / "current.json").write_text(
                '{"schema":1,"version":"1.8.4"}', encoding="utf-8"
            )
            (root / "app" / "previous.json").write_text(
                '{"schema":1,"version":"1.8.3"}', encoding="utf-8"
            )

            self.assertEqual(main([]), 0)

            self.assertEqual(launch_mock.call_args_list[0].args[:2], (current, current.parent))
            self.assertEqual(launch_mock.call_args_list[1].args[:2], (previous, previous.parent))
            self.assertEqual(
                json.loads((root / "app" / "current.json").read_text(encoding="utf-8")),
                {"schema": 1, "version": "1.8.3"},
            )
            sync_mock.assert_called_once_with(root, "1.8.3", launcher.LOGGER)
            show_error_mock.assert_not_called()

    @patch("launcher.show_error")
    @patch("launcher.app_instance_running", return_value=False)
    @patch("launcher.launch_app")
    @patch("launcher.install_root")
    def test_normal_start_does_not_fallback_when_failed_process_may_still_run(
        self, root_mock, launch_mock, _running, show_error_mock
    ) -> None:
        launch_mock.side_effect = launcher.ProcessTreeTerminationError("cannot stop")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root_mock.return_value = root
            current = root / "app" / "versions" / "1.8.4" / "DeltaStatsApp.exe"
            previous = root / "app" / "versions" / "1.8.3" / "DeltaStatsApp.exe"
            current.parent.mkdir(parents=True)
            previous.parent.mkdir(parents=True)
            current.write_bytes(b"current")
            previous.write_bytes(b"previous")
            (root / "app" / "current.json").write_text(
                '{"schema":1,"version":"1.8.4"}', encoding="utf-8"
            )
            (root / "app" / "previous.json").write_text(
                '{"schema":1,"version":"1.8.3"}', encoding="utf-8"
            )

            self.assertEqual(main([]), 1)

        launch_mock.assert_called_once()
        show_error_mock.assert_called_once()

    @patch("launcher.show_warning")
    @patch("launcher.app_instance_running", return_value=True)
    def test_normal_start_does_not_open_a_second_app(self, _running, warning) -> None:
        self.assertEqual(main([]), 0)
        warning.assert_called_once()

    @staticmethod
    def _pending(root: Path, version: str, **overrides) -> Path:
        candidate = root / "app" / "versions" / version / "DeltaStatsApp.exe"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"candidate")
        payload = {
            "schema": 1,
            "version": version,
            "package_format": PACKAGE_FORMAT,
            "target_dir": f"versions/{version}",
            "entrypoint": "DeltaStatsApp.exe",
        }
        payload.update(overrides)
        pending = root / "app" / "pending.json"
        pending.write_text(json.dumps(payload), encoding="utf-8")
        return pending

    @patch("launcher.install_root")
    @patch("launcher.app_instance_running", return_value=False)
    @patch("launcher._wait_for_process_exit")
    @patch("launcher.sync_installed_version_metadata")
    @patch("launcher.launch_app")
    def test_pending_candidate_is_verified_before_pointer_switch(
        self, launch_mock, sync_mock, wait_mock, _running, root_mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root_mock.return_value = root
            legacy = root / "app" / "DeltaStatsApp.exe"
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b"legacy")
            (legacy.parent / "version.txt").write_text("1.5.8", encoding="utf-8")
            pending = self._pending(root, "1.6.0")
            self.assertEqual(apply_pending(pending, 1234), 0)
            current = json.loads((root / "app" / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(current, {"schema": 1, "version": "1.6.0"})
            self.assertFalse(pending.exists())
            wait_mock.assert_called_once_with(1234)
            self.assertEqual(launch_mock.call_count, 1)
            sync_mock.assert_called_once_with(root, "1.6.0", launcher.LOGGER)

    @patch("launcher.install_root")
    @patch("launcher.app_instance_running", return_value=False)
    @patch("launcher._wait_for_process_exit")
    @patch("launcher.sync_installed_version_metadata")
    @patch("launcher.launch_app")
    def test_failed_candidate_keeps_current_pointer_and_relaunches_previous(
        self, launch_mock, sync_mock, _wait_mock, _running, root_mock
    ) -> None:
        launch_mock.side_effect = [RuntimeError("candidate failed"), object()]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root_mock.return_value = root
            old = root / "app" / "versions" / "1.5.9" / "DeltaStatsApp.exe"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"old")
            (root / "app" / "current.json").write_text(
                '{"schema":1,"version":"1.5.9"}', encoding="utf-8"
            )
            pending = self._pending(root, "1.6.0")
            with self.assertRaisesRegex(RuntimeError, "candidate failed"):
                apply_pending(pending, 1234)
            self.assertEqual(
                json.loads((root / "app" / "current.json").read_text(encoding="utf-8")),
                {"schema": 1, "version": "1.5.9"},
            )
            self.assertEqual(launch_mock.call_args_list[1].args[:2], (old, old.parent))
            sync_mock.assert_not_called()

    @patch("launcher.install_root")
    @patch("launcher.app_instance_running", return_value=False)
    @patch("launcher._wait_for_process_exit")
    @patch("launcher.sync_installed_version_metadata")
    @patch("launcher.launch_app")
    @patch("launcher.write_pointer", side_effect=OSError("pointer write failed"))
    def test_pending_does_not_sync_installer_metadata_before_pointer_switch(
        self,
        _write_mock,
        _launch_mock,
        sync_mock,
        _wait_mock,
        _running,
        root_mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root_mock.return_value = root
            pending = self._pending(root, "1.6.0")
            with self.assertRaisesRegex(OSError, "pointer write failed"):
                apply_pending(pending, 1234)
            sync_mock.assert_not_called()

    @patch("launcher.install_root")
    @patch("launcher._wait_for_process_exit")
    def test_pending_rejects_remote_controlled_entrypoint(self, _wait_mock, root_mock) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root_mock.return_value = root
            pending = self._pending(root, "1.6.0", entrypoint="other.exe")
            with self.assertRaisesRegex(RuntimeError, "入口"):
                apply_pending(pending, 1234)

    @patch("launcher.install_root")
    @patch("launcher.app_instance_running", return_value=False)
    @patch("launcher._wait_for_process_exit")
    @patch("launcher.sync_installed_version_metadata")
    @patch("launcher.launch_app")
    @patch("launcher.write_pointer")
    def test_pending_cleanup_failure_does_not_roll_back_activated_candidate(
        self, write_mock, launch_mock, sync_mock, _wait_mock, _running, root_mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root_mock.return_value = root
            pending = self._pending(root, "1.6.0")
            with patch.object(Path, "unlink", side_effect=OSError("locked")):
                self.assertEqual(apply_pending(pending, 1234), 0)
            write_mock.assert_called_once_with(root / "app" / "current.json", "1.6.0")
            launch_mock.assert_called_once()
            sync_mock.assert_called_once_with(root, "1.6.0", launcher.LOGGER)

    @patch("launcher.install_root")
    def test_pending_rejects_a_different_install_root(self, root_mock) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            expected_root = Path(first).resolve()
            other_root = Path(second).resolve()
            root_mock.return_value = expected_root
            pending = self._pending(other_root, "1.6.0")
            with self.assertRaisesRegex(RuntimeError, "当前安装目录"):
                apply_pending(pending, 1234)

    @patch("launcher.install_root")
    @patch("launcher.app_instance_running", return_value=False)
    @patch("launcher._wait_for_process_exit")
    @patch("launcher.launch_app")
    def test_unstoppable_candidate_is_never_followed_by_previous_app(
        self, launch_mock, _wait_mock, _running, root_mock
    ) -> None:
        launch_mock.side_effect = launcher.ProcessTreeTerminationError("cannot stop")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root_mock.return_value = root
            old = root / "app" / "versions" / "1.5.9" / "DeltaStatsApp.exe"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"old")
            (root / "app" / "current.json").write_text(
                '{"schema":1,"version":"1.5.9"}', encoding="utf-8"
            )
            pending = self._pending(root, "1.6.0")
            with self.assertRaises(launcher.ProcessTreeTerminationError):
                apply_pending(pending, 1234)
            self.assertTrue(pending.is_file())
            self.assertEqual(launch_mock.call_count, 1)
            self.assertEqual(
                json.loads((root / "app" / "current.json").read_text(encoding="utf-8")),
                {"schema": 1, "version": "1.5.9"},
            )

    @patch("launcher.install_root")
    @patch("launcher._wait_for_process_exit")
    def test_pending_rejects_corrupted_staged_launcher(self, _wait_mock, root_mock) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            root_mock.return_value = root
            stage = root / ".updates" / "launcher-1.6.0.exe"
            stage.parent.mkdir()
            stage.write_bytes(b"corrupted")
            pending = self._pending(
                root,
                "1.6.0",
                launcher_stage=".updates/launcher-1.6.0.exe",
                launcher_size=stage.stat().st_size,
                launcher_sha256=hashlib.sha256(b"expected").hexdigest(),
            )
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                apply_pending(pending, 1234)
            _wait_mock.assert_not_called()

    def test_frozen_launcher_uses_its_physical_install_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            staged = root / ".updates" / "launcher.exe"
            pending = root / "app" / "pending.json"
            pending.parent.mkdir(parents=True)
            pending.write_text("{}", encoding="utf-8")
            with (
                patch.object(launcher.sys, "frozen", True, create=True),
                patch.object(launcher.sys, "executable", str(staged)),
                patch.dict(os.environ, {"DELTA_STATS_INSTALL_ROOT": str(root / "wrong")}),
            ):
                self.assertEqual(launcher.install_root(), root.resolve())

    def test_portable_root_named_updates_is_not_treated_as_staged_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / ".updates"
            executable = root / launcher.LAUNCHER_BINARY
            executable.parent.mkdir()
            with (
                patch.object(launcher.sys, "frozen", True, create=True),
                patch.object(launcher.sys, "executable", str(executable)),
            ):
                self.assertEqual(launcher.install_root(), root.resolve())


if __name__ == "__main__":
    unittest.main()
