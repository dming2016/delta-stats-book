"""Best-effort synchronization of Windows installer version metadata."""

from __future__ import annotations

import logging
import os
from pathlib import Path

try:
    import winreg
except ImportError:  # pragma: no cover - the desktop application is Windows-only
    winreg = None

from update_protocol import INNO_UNINSTALL_SUBKEY, INSTALLER_STATE_SUBKEY, safe_version


def _normalized_path(value: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(value))))


def sync_installed_version_metadata(
    install_root: Path,
    version: str,
    logger: logging.Logger,
) -> bool:
    """Update installer metadata only when it belongs to this installation."""

    try:
        version = safe_version(version)
        expected_root = _normalized_path(install_root)
    except Exception:
        logger.exception("Unable to prepare installed version metadata synchronization")
        return False

    if winreg is None:
        logger.info("Windows registry is unavailable; installed version metadata was not synchronized")
        return False

    registry_view = getattr(winreg, "KEY_WOW64_64KEY", 0)
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            INNO_UNINSTALL_SUBKEY,
            0,
            winreg.KEY_QUERY_VALUE | registry_view,
        ) as uninstall_key:
            registered_root, _value_type = winreg.QueryValueEx(
                uninstall_key,
                "InstallLocation",
            )
    except FileNotFoundError:
        logger.info("Installer registry entry was not found; version metadata was not synchronized")
        return False
    except Exception:
        logger.exception("Unable to verify the installer registry entry")
        return False

    try:
        matches_installation = _normalized_path(str(registered_root)) == expected_root
    except Exception:
        logger.exception("Installer registry entry contains an invalid install location")
        return False
    if not matches_installation:
        logger.info(
            "Installer registry entry belongs to another directory; version metadata was not synchronized"
        )
        return False

    updates = (
        (INNO_UNINSTALL_SUBKEY, "DisplayVersion"),
        (INSTALLER_STATE_SUBKEY, "InstalledVersion"),
    )
    updated = True
    for subkey, value_name in updates:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                subkey,
                0,
                winreg.KEY_SET_VALUE | registry_view,
            ) as registry_key:
                winreg.SetValueEx(registry_key, value_name, 0, winreg.REG_SZ, version)
        except Exception:
            updated = False
            logger.exception("Unable to synchronize registry value %s\\%s", subkey, value_name)

    if updated:
        logger.info("Synchronized installed version metadata version=%s", version)
    return updated
