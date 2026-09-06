#!/usr/bin/env python3
"""Tests for fail-soft Windows installer metadata synchronization."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import install_metadata
from install_metadata import sync_installed_version_metadata
from update_protocol import (
    INNO_UNINSTALL_SUBKEY,
    INSTALLER_APP_ID,
    INSTALLER_STATE_SUBKEY,
)


class _RegistryKey:
    def __init__(self, subkey: str) -> None:
        self.subkey = subkey

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        return None


class _FakeWinreg:
    HKEY_CURRENT_USER = object()
    KEY_QUERY_VALUE = 0x0001
    KEY_SET_VALUE = 0x0002
    KEY_WOW64_64KEY = 0x0100
    REG_SZ = 1

    def __init__(self, install_root: Path | None) -> None:
        self.values: dict[str, dict[str, str]] = {}
        if install_root is not None:
            self.values[INNO_UNINSTALL_SUBKEY] = {
                "InstallLocation": str(install_root),
                "DisplayVersion": "1.8.2",
            }
            self.values[INSTALLER_STATE_SUBKEY] = {"InstalledVersion": "1.8.2"}
        self.set_calls: list[tuple[str, str, str]] = []
        self.fail_values: set[tuple[str, str]] = set()

    def OpenKey(self, root, subkey: str, _reserved: int, _access: int) -> _RegistryKey:
        if root is not self.HKEY_CURRENT_USER or subkey not in self.values:
            raise FileNotFoundError(subkey)
        return _RegistryKey(subkey)

    def QueryValueEx(self, key: _RegistryKey, value_name: str):
        try:
            return self.values[key.subkey][value_name], self.REG_SZ
        except KeyError as exc:
            raise FileNotFoundError(value_name) from exc

    def SetValueEx(
        self,
        key: _RegistryKey,
        value_name: str,
        _reserved: int,
        _value_type: int,
        value: str,
    ) -> None:
        if (key.subkey, value_name) in self.fail_values:
            raise OSError("registry write failed")
        self.values[key.subkey][value_name] = value
        self.set_calls.append((key.subkey, value_name, value))


class InstallMetadataTests(unittest.TestCase):
    def test_matching_installer_entry_updates_both_version_values(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            registry = _FakeWinreg(root)
            logger = MagicMock()
            with patch.object(install_metadata, "winreg", registry):
                self.assertTrue(sync_installed_version_metadata(root, "1.8.3", logger))

        self.assertEqual(
            registry.set_calls,
            [
                (INNO_UNINSTALL_SUBKEY, "DisplayVersion", "1.8.3"),
                (INSTALLER_STATE_SUBKEY, "InstalledVersion", "1.8.3"),
            ],
        )

    def test_portable_directory_does_not_modify_another_installation(self) -> None:
        with TemporaryDirectory() as installed, TemporaryDirectory() as portable:
            registry = _FakeWinreg(Path(installed).resolve())
            logger = MagicMock()
            with patch.object(install_metadata, "winreg", registry):
                self.assertFalse(
                    sync_installed_version_metadata(Path(portable).resolve(), "1.8.3", logger)
                )

        self.assertEqual(registry.set_calls, [])

    def test_missing_installer_entry_is_fail_soft(self) -> None:
        with TemporaryDirectory() as temporary:
            registry = _FakeWinreg(None)
            logger = MagicMock()
            with patch.object(install_metadata, "winreg", registry):
                self.assertFalse(
                    sync_installed_version_metadata(Path(temporary), "1.8.3", logger)
                )

        logger.info.assert_called()

    def test_one_registry_write_failure_does_not_block_the_other_value(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            registry = _FakeWinreg(root)
            registry.fail_values.add((INNO_UNINSTALL_SUBKEY, "DisplayVersion"))
            logger = MagicMock()
            with patch.object(install_metadata, "winreg", registry):
                self.assertFalse(sync_installed_version_metadata(root, "1.8.3", logger))

        self.assertEqual(
            registry.set_calls,
            [(INSTALLER_STATE_SUBKEY, "InstalledVersion", "1.8.3")],
        )
        logger.exception.assert_called_once()

    def test_registry_identity_matches_the_inno_setup_app_id(self) -> None:
        script = (Path(__file__).parent / "installer" / "DeltaStatsAssistant.iss").read_text(
            encoding="utf-8"
        )
        self.assertIn(INSTALLER_APP_ID.strip("{}"), script)
        self.assertTrue(INNO_UNINSTALL_SUBKEY.endswith(f"{INSTALLER_APP_ID}_is1"))


if __name__ == "__main__":
    unittest.main()
