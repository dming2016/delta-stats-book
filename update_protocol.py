"""Shared constants for the desktop launcher and in-app updater."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path


APP_ENTRYPOINT = "DeltaStatsApp.exe"
CURRENT_SCHEMA = 1
PACKAGE_FORMAT = "pyinstaller-onedir-zip-v1"
INSTALL_ROOT_ENV = "DELTA_STATS_INSTALL_ROOT"
INSTALL_KIND_ENV = "DELTA_STATS_INSTALL_KIND"
LAUNCHER_PATH_ENV = "DELTA_STATS_LAUNCHER_PATH"
LAUNCHER_VERSION_ENV = "DELTA_STATS_LAUNCHER_VERSION"
LAUNCHER_PROTOCOL_ENV = "DELTA_STATS_LAUNCHER_PROTOCOL"
START_TRACE_ID_ENV = "DELTA_STATS_START_TRACE_ID"
INSTALL_KIND_LEGACY = "legacy-onefile"
INSTALL_KIND_VERSIONED = "versioned-onedir"
INSTALLER_APP_ID = "{F0BB66C0-D6D8-49C2-9C42-8B7CA6AC291B}"
INNO_UNINSTALL_SUBKEY = (
    "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\"
    f"{INSTALLER_APP_ID}_is1"
)
INSTALLER_STATE_SUBKEY = r"Software\DeltaStatsAssistant"
VERSION_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)+(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?")


def safe_version(value: object) -> str:
    version = str(value).strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("版本号格式无效")
    return version


def version_key(value: object) -> tuple[int, ...]:
    try:
        core = str(value).strip().split("-", 1)[0].split("+", 1)[0]
        return tuple(int(part) for part in core.split("."))
    except ValueError:
        return (0,)


def read_version_pointer(path: Path) -> str | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        if payload.get("schema") != CURRENT_SCHEMA:
            return None
        return safe_version(payload.get("version"))
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return None


def read_current_version(app_root: Path) -> str | None:
    return read_version_pointer(app_root / "current.json")


def write_pointer(path: Path, version: str) -> None:
    version = safe_version(version)
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(
        json.dumps(
            {"schema": CURRENT_SCHEMA, "version": version},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    deadline = time.monotonic() + 2.0
    while True:
        try:
            os.replace(temporary, path)
            return
        except PermissionError as error:
            if os.name != "nt" or getattr(error, "winerror", None) not in (5, 32, 33) or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def versioned_app_path(app_root: Path, version: str) -> Path:
    return app_root / "versions" / safe_version(version) / APP_ENTRYPOINT
