#!/usr/bin/env python3
"""Account-scoped local paths and one-time legacy data migration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from secure_store import APP_DIR


MIGRATION_FILE = "account-storage-v1.json"
ACCOUNT_ID_PATTERN = re.compile(r"^[0-9a-f]{24}$")


def account_type(auth: dict[str, Any]) -> str:
    value = str(auth.get("account_type", "")).strip().lower()
    if value in ("wechat", "qq"):
        return value
    return "qq" if str(auth.get("acctype", "")).lower() == "qc" else "wechat"


def account_id(auth: dict[str, Any]) -> str:
    explicit = str(auth.get("account_id", "")).strip().lower()
    openid = str(auth.get("openid", "")).strip()
    if openid:
        identity = f"{account_type(auth)}:{openid}"
        calculated = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        if explicit and explicit != calculated:
            raise ValueError("登录态账号标识不一致")
        return calculated
    if explicit and ACCOUNT_ID_PATTERN.fullmatch(explicit):
        return explicit
    if explicit:
        raise ValueError("账号标识格式无效")
    raise ValueError("登录态缺少账号标识")


def account_dir(auth: dict[str, Any], *, app_dir: Path = APP_DIR) -> Path:
    return app_dir / "accounts" / account_id(auth)


def cache_file(auth: dict[str, Any], *, app_dir: Path = APP_DIR) -> Path:
    return account_dir(auth, app_dir=app_dir) / "cache" / "matches.json"


def raw_dir(auth: dict[str, Any], *, app_dir: Path = APP_DIR) -> Path:
    return account_dir(auth, app_dir=app_dir) / "cache" / "raw"


def preferences_file(auth: dict[str, Any], *, app_dir: Path = APP_DIR) -> Path:
    return account_dir(auth, app_dir=app_dir) / "preferences.json"


def _write_migration_marker(marker: Path, payload: dict[str, Any]) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_name(f".{marker.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)


def mark_legacy_data_unassigned(
    reason: str, *, app_dir: Path = APP_DIR
) -> dict[str, Any]:
    marker = app_dir / MIGRATION_FILE
    if marker.is_file():
        return {"migrated": False, "unassigned": True}
    payload = {
        "version": 1,
        "account_id": None,
        "migrated_at_utc": datetime.now(UTC).isoformat(),
        "copied": [],
        "unassigned": True,
        "reason": reason,
    }
    _write_migration_marker(marker, payload)
    return {"migrated": False, "unassigned": True}


def migrate_legacy_data(
    auth: dict[str, Any], *, app_dir: Path = APP_DIR
) -> dict[str, Any]:
    marker = app_dir / MIGRATION_FILE
    target_id = account_id(auth)
    if marker.is_file():
        return {"migrated": False, "account_id": target_id}

    copied: list[str] = []
    legacy_cache = app_dir / "cache" / "matches.json"
    legacy_raw = app_dir / "cache" / "raw"
    legacy_preferences = app_dir / "preferences.json"
    target_cache = cache_file(auth, app_dir=app_dir)
    target_raw = raw_dir(auth, app_dir=app_dir)
    target_preferences = preferences_file(auth, app_dir=app_dir)

    if legacy_cache.is_file() and not target_cache.exists():
        target_cache.parent.mkdir(parents=True, exist_ok=True)
        temporary_cache = target_cache.with_name(
            f".{target_cache.name}.{uuid.uuid4().hex}.migrating"
        )
        shutil.copy2(legacy_cache, temporary_cache)
        os.replace(temporary_cache, target_cache)
        copied.append("cache")
    if legacy_raw.is_dir():
        target_raw.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(legacy_raw, target_raw, dirs_exist_ok=True)
        copied.append("raw")
    if legacy_preferences.is_file() and not target_preferences.exists():
        target_preferences.parent.mkdir(parents=True, exist_ok=True)
        temporary_preferences = target_preferences.with_name(
            f".{target_preferences.name}.{uuid.uuid4().hex}.migrating"
        )
        shutil.copy2(legacy_preferences, temporary_preferences)
        os.replace(temporary_preferences, target_preferences)
        copied.append("preferences")

    payload = {
        "version": 1,
        "account_id": target_id,
        "migrated_at_utc": datetime.now(UTC).isoformat(),
        "copied": copied,
    }
    _write_migration_marker(marker, payload)
    return {"migrated": True, "account_id": target_id, "copied": copied}
