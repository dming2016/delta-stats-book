#!/usr/bin/env python3
"""DPAPI-encrypted multi-account credential vault."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from account_storage import (
    account_id as derive_account_id,
    account_type,
    cache_file,
    mark_legacy_data_unassigned,
    migrate_legacy_data,
)
from secure_store import APP_DIR, InterProcessFileLock, exists, read_json, write_json


VAULT_FILE = "account_vault.dat"
VAULT_BACKUP_FILE = "account_vault.bak.dat"
VAULT_ENTROPY = "DeltaForceIntelAssistant:account-vault:v2"
VAULT_LOCK_FILE = "account_vault.lock"
LEGACY_AUTH_FILE = "miniapp_auth.dat"
LEGACY_AUTH_ENTROPY = "DeltaForceIntelAssistant:miniapp:v1"
SCHEMA_VERSION = 2
AUTH_STATES = {
    "valid",
    "expired",
    "pending_verification",
    "unverified",
    "incomplete",
    "missing",
}
WECHAT_REQUIRED_AUTH_KEYS = (
    "openid",
    "acctype",
    "appid",
    "ieg_ams_session_token",
    "ieg_ams_token",
    "ieg_ams_token_time",
)
QQ_REQUIRED_AUTH_KEYS = ("openid", "acctype", "appid", "access_token")
AUTH_REVISION_KEYS = (
    "openid",
    "acctype",
    "appid",
    "access_token",
    "ieg_ams_session_token",
    "ieg_ams_token",
    "ieg_ams_token_time",
    "ieg_ams_token_v2",
    "unionid",
    "verifysession",
)


class VaultError(ValueError):
    """The encrypted account vault cannot be used safely."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _credential_complete(payload: dict[str, Any]) -> bool:
    required = (
        QQ_REQUIRED_AUTH_KEYS
        if str(payload.get("acctype", "")).lower() == "qc"
        else WECHAT_REQUIRED_AUTH_KEYS
    )
    return all(payload.get(key) not in (None, "") for key in required)


def _display_name(payload: dict[str, Any]) -> str | None:
    value = str(payload.get("display_name", "")).strip()
    return value[:120] or None


def _credential_revision(payload: dict[str, Any]) -> str:
    material = {
        key: payload[key]
        for key in AUTH_REVISION_KEYS
        if payload.get(key) not in (None, "")
    }
    canonical = json.dumps(
        {"schema": 1, "credential": material},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _safe_error(value: Any, credential: dict[str, Any] | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if isinstance(credential, dict):
        for key, secret in credential.items():
            lowered = str(key).lower()
            if not any(
                marker in lowered
                for marker in ("openid", "token", "session", "unionid", "verify")
            ):
                continue
            secret_text = str(secret or "")
            if secret_text:
                text = text.replace(secret_text, "[redacted]")
    return text[:240]


def _validation_result(result: str) -> str:
    value = str(result or "error").strip().lower()
    aliases = {
        "valid": "success",
        "expired": "rejected",
        "network": "network_error",
        "unknown": "error",
    }
    return aliases.get(value, value)


def _empty_vault() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "active_account_id": None,
        "legacy_migrated": False,
        "accounts": {},
    }


def _validate_vault(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise VaultError("账号保险箱版本无法识别")
    revision = payload.get("revision")
    if not isinstance(revision, int) or revision < 0:
        raise VaultError("账号保险箱修订号无效")
    accounts = payload.get("accounts")
    if not isinstance(accounts, dict):
        raise VaultError("账号保险箱账号列表损坏")
    for stored_id, record in accounts.items():
        try:
            normalized_id = derive_account_id({"account_id": stored_id})
        except ValueError as exc:
            raise VaultError("账号保险箱包含无效账号标识") from exc
        if normalized_id != stored_id or not isinstance(record, dict):
            raise VaultError("账号保险箱账号记录损坏")
        identity = record.get("identity")
        if not isinstance(identity, dict) or identity.get("account_id") != stored_id:
            raise VaultError("账号保险箱身份记录损坏")
        if identity.get("account_type") not in ("wechat", "qq"):
            raise VaultError("账号保险箱账号类型无效")
        credential = record.get("credential")
        if credential is not None and not isinstance(credential, dict):
            raise VaultError("账号保险箱凭证记录损坏")
        if isinstance(credential, dict):
            try:
                credential_id = derive_account_id(credential)
            except ValueError as exc:
                raise VaultError("账号保险箱凭证身份损坏") from exc
            if credential_id != stored_id:
                raise VaultError("账号保险箱凭证与账号不匹配")
        if record.get("auth_state") not in AUTH_STATES:
            raise VaultError("账号保险箱登录状态无效")
    active_id = payload.get("active_account_id")
    if active_id is not None and active_id not in accounts:
        raise VaultError("账号保险箱当前账号指针无效")
    return payload


def _record_for_payload(
    payload: dict[str, Any],
    *,
    existing: dict[str, Any] | None,
    activate: bool,
    validation_result: str,
    validation_error: Exception | str | None = None,
) -> dict[str, Any]:
    credential = deepcopy(payload)
    stored_id = derive_account_id(credential)
    now = _utc_now()
    complete = _credential_complete(credential)
    result = _validation_result(validation_result)
    previous_state = existing.get("auth_state") if existing else None
    if not complete:
        auth_state = "incomplete"
    elif result == "success":
        auth_state = "valid"
    elif result == "rejected":
        auth_state = "expired"
    elif result in ("busy", "network_error"):
        auth_state = "pending_verification"
    elif previous_state in AUTH_STATES and previous_state != "missing":
        auth_state = previous_state
    else:
        auth_state = "unverified"

    display_name = _display_name(credential)
    if display_name is None and existing:
        display_name = existing.get("display_name")
    last_validated = existing.get("last_validated_at_utc") if existing else None
    if result in ("success", "rejected"):
        last_validated = now
    return {
        "identity": {
            "account_id": stored_id,
            "account_type": account_type(credential),
            "provider": str(credential.get("provider") or "wechat-miniapp"),
        },
        "credential": credential,
        "display_name": display_name,
        "created_at_utc": existing.get("created_at_utc", now) if existing else now,
        "updated_at_utc": now,
        "last_used_at_utc": now if activate else existing.get("last_used_at_utc") if existing else None,
        "auth_state": auth_state,
        "last_validated_at_utc": last_validated,
        "last_validation": {
            "result": result,
            "at_utc": now,
            "error": str(validation_error)[:500] if validation_error else None,
        },
    }


def _backup_unassigned_legacy(app_dir: Path, reason: str) -> None:
    source = app_dir / LEGACY_AUTH_FILE
    if not source.is_file():
        return
    backup = source.with_name(
        f"miniapp_auth.{reason}-{uuid.uuid4().hex[:12]}.dat"
    )
    shutil.copy2(source, backup)


def _write_vault_locked(vault: dict[str, Any], app_dir: Path) -> Path:
    _validate_vault(vault)
    return write_json(VAULT_FILE, vault, VAULT_ENTROPY, app_dir=app_dir)


def _write_legacy_mirror_locked(vault: dict[str, Any], app_dir: Path) -> None:
    active_id = vault.get("active_account_id")
    record = vault.get("accounts", {}).get(active_id) if active_id else None
    credential = record.get("credential") if isinstance(record, dict) else None
    if isinstance(credential, dict):
        write_json(
            LEGACY_AUTH_FILE,
            credential,
            LEGACY_AUTH_ENTROPY,
            app_dir=app_dir,
        )
    else:
        (app_dir / LEGACY_AUTH_FILE).unlink(missing_ok=True)


def _load_vault_locked(app_dir: Path, *, for_write: bool = False) -> dict[str, Any]:
    if exists(VAULT_FILE, app_dir=app_dir):
        try:
            return _validate_vault(read_json(VAULT_FILE, VAULT_ENTROPY, app_dir=app_dir))
        except Exception as main_error:
            if not exists(VAULT_BACKUP_FILE, app_dir=app_dir):
                raise VaultError(f"账号保险箱无法读取：{main_error}") from main_error
            try:
                backup = _validate_vault(
                    read_json(
                        VAULT_BACKUP_FILE,
                        VAULT_ENTROPY,
                        app_dir=app_dir,
                    )
                )
            except Exception as backup_error:
                raise VaultError(
                    f"账号保险箱及其备份均无法读取：{main_error}; {backup_error}"
                ) from backup_error
            _write_vault_locked(backup, app_dir)
            _write_legacy_mirror_locked(backup, app_dir)
            return backup

    if exists(VAULT_BACKUP_FILE, app_dir=app_dir):
        try:
            backup = _validate_vault(
                read_json(VAULT_BACKUP_FILE, VAULT_ENTROPY, app_dir=app_dir)
            )
        except Exception as backup_error:
            raise VaultError(f"账号保险箱备份无法读取：{backup_error}") from backup_error
        _write_vault_locked(backup, app_dir)
        _write_legacy_mirror_locked(backup, app_dir)
        return backup

    vault = _empty_vault()
    if not exists(LEGACY_AUTH_FILE, app_dir=app_dir):
        if for_write:
            mark_legacy_data_unassigned("no_saved_auth", app_dir=app_dir)
            vault["legacy_migrated"] = True
        return vault

    try:
        legacy = read_json(
            LEGACY_AUTH_FILE,
            LEGACY_AUTH_ENTROPY,
            app_dir=app_dir,
        )
        stored_id = derive_account_id(legacy)
    except Exception as exc:
        if not for_write:
            raise VaultError(f"旧登录态无法迁移：{exc}") from exc
        _backup_unassigned_legacy(app_dir, "unreadable")
        mark_legacy_data_unassigned("saved_auth_unreadable", app_dir=app_dir)
        vault["legacy_migrated"] = True
        return vault

    migrate_legacy_data(legacy, app_dir=app_dir)
    record = _record_for_payload(
        legacy,
        existing=None,
        activate=True,
        validation_result="not_run",
    )
    vault.update(
        revision=1,
        active_account_id=stored_id,
        legacy_migrated=True,
        accounts={stored_id: record},
    )
    _commit_vault_locked(vault, app_dir)
    return vault


def _commit_vault_locked(vault: dict[str, Any], app_dir: Path) -> Path:
    if exists(VAULT_FILE, app_dir=app_dir):
        previous = _validate_vault(
            read_json(VAULT_FILE, VAULT_ENTROPY, app_dir=app_dir)
        )
        write_json(
            VAULT_BACKUP_FILE,
            previous,
            VAULT_ENTROPY,
            app_dir=app_dir,
        )
    else:
        write_json(
            VAULT_BACKUP_FILE,
            vault,
            VAULT_ENTROPY,
            app_dir=app_dir,
        )
    path = _write_vault_locked(vault, app_dir)
    _write_legacy_mirror_locked(vault, app_dir)
    return path


def ensure_migrated(*, app_dir: Path = APP_DIR) -> dict[str, Any]:
    app_dir = Path(app_dir)
    with InterProcessFileLock(VAULT_LOCK_FILE, app_dir=app_dir):
        vault = _load_vault_locked(app_dir)
        if _register_cached_accounts_locked(vault, app_dir):
            vault["revision"] += 1
            _commit_vault_locked(vault, app_dir)
        return deepcopy(vault)


def _register_cached_accounts_locked(vault: dict[str, Any], app_dir: Path) -> bool:
    accounts_root = app_dir / "accounts"
    if not accounts_root.is_dir():
        return False

    added = False
    try:
        account_directories = list(accounts_root.iterdir())
    except OSError:
        return False

    for directory in account_directories:
        if not directory.is_dir():
            continue
        try:
            stored_id = derive_account_id({"account_id": directory.name})
        except ValueError:
            continue
        if stored_id in vault["accounts"]:
            continue

        path = directory / "cache" / "matches.json"
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            cached_id = derive_account_id(
                {"account_id": payload.get("account_id")}
                if isinstance(payload, dict)
                else {}
            )
            cached_type = payload.get("account_type")
            matches = payload.get("matches")
            if (
                cached_id != stored_id
                or cached_type not in ("wechat", "qq")
                or not isinstance(matches, list)
            ):
                continue
            discovered_at = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue

        vault["accounts"][stored_id] = {
            "identity": {
                "account_id": stored_id,
                "account_type": cached_type,
                "provider": "local-cache",
            },
            "credential": None,
            "display_name": None,
            "created_at_utc": discovered_at,
            "updated_at_utc": discovered_at,
            "last_used_at_utc": None,
            "auth_state": "missing",
            "last_validated_at_utc": None,
            "last_validation": {
                "result": "cache_discovered",
                "at_utc": discovered_at,
                "error": None,
            },
        }
        added = True
    return added


def _cache_summary(stored_id: str, account_kind: str, app_dir: Path) -> dict[str, Any]:
    identity = {"account_id": stored_id, "account_type": account_kind}
    path = cache_file(identity, app_dir=app_dir)
    if not path.is_file():
        return {
            "has_cache": False,
            "exists": False,
            "viewable": True,
            "matches": 0,
            "updated_at": None,
            "warning": None,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        matches = payload.get("matches") if isinstance(payload, dict) else None
        if not isinstance(matches, list):
            raise ValueError("cache root is not a match collection")
        return {
            "has_cache": True,
            "exists": True,
            "viewable": True,
            "matches": len(matches),
            "updated_at": payload.get("updated_at"),
            "warning": None,
        }
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {
            "has_cache": True,
            "exists": True,
            "viewable": False,
            "matches": 0,
            "updated_at": None,
            "warning": f"本地战绩缓存无法读取：{exc}",
        }


def _safe_record_summary(
    stored_id: str,
    record: dict[str, Any],
    *,
    active_id: str | None,
    app_dir: Path,
) -> dict[str, Any]:
    identity = record["identity"]
    credential = record.get("credential")
    complete = isinstance(credential, dict) and _credential_complete(credential)
    credential_revision = (
        _credential_revision(credential) if isinstance(credential, dict) else None
    )
    cache = _cache_summary(stored_id, identity["account_type"], app_dir)
    last_validation = record.get("last_validation") or {}
    auth_state = record["auth_state"]
    last_error = _safe_error(last_validation.get("error"), credential)
    is_active = stored_id == active_id
    can_sync = complete and auth_state in (
        "valid",
        "pending_verification",
        "unverified",
    )
    auth_summary = {
        "state": auth_state,
        "has_credential": isinstance(credential, dict),
        "has_required_fields": complete,
        "last_validated_at_utc": record.get("last_validated_at_utc"),
        "last_validation": {
            "result": last_validation.get("result"),
            "at_utc": last_validation.get("at_utc"),
            "error": last_error,
        },
        "last_error": last_error,
    }
    return {
        "id": stored_id,
        "account_id": stored_id,
        "short_id": stored_id[-6:],
        "account_type": identity["account_type"],
        "provider": identity.get("provider"),
        "display_name": record.get("display_name"),
        "active": is_active,
        "is_active": is_active,
        "auth_state": auth_state,
        "auth": auth_summary,
        "has_credential": auth_summary["has_credential"],
        "has_required_fields": complete,
        "credential_revision": credential_revision,
        "created_at_utc": record.get("created_at_utc"),
        "updated_at_utc": record.get("updated_at_utc"),
        "saved_at_utc": credential.get("saved_at_utc") if isinstance(credential, dict) else None,
        "last_used_at_utc": record.get("last_used_at_utc"),
        "last_validated_at_utc": record.get("last_validated_at_utc"),
        "last_validation": {
            "result": last_validation.get("result"),
            "at_utc": last_validation.get("at_utc"),
            "error": last_error,
        },
        "last_error": last_error,
        "cache": cache,
        "capabilities": {
            "activate": True,
            "view_cache": cache["viewable"],
            "sync": can_sync,
            "verify": complete,
            "remove": True,
        },
        "can_sync": can_sync,
    }


def registry_summary(*, app_dir: Path = APP_DIR) -> dict[str, Any]:
    app_dir = Path(app_dir)
    with InterProcessFileLock(VAULT_LOCK_FILE, app_dir=app_dir):
        vault = _load_vault_locked(app_dir)
        if _register_cached_accounts_locked(vault, app_dir):
            vault["revision"] += 1
            _commit_vault_locked(vault, app_dir)
        active_id = vault.get("active_account_id")
        accounts = [
            _safe_record_summary(
                stored_id,
                record,
                active_id=active_id,
                app_dir=app_dir,
            )
            for stored_id, record in vault["accounts"].items()
        ]
    accounts.sort(
        key=lambda item: (
            not item["active"],
            str(item.get("last_used_at_utc") or item.get("updated_at_utc") or ""),
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": vault["revision"],
        "active_account_id": active_id,
        "accounts": accounts,
    }


def _identity_summary(stored_id: str, record: dict[str, Any]) -> dict[str, Any]:
    return {
        "account_id": stored_id,
        "account_type": record["identity"]["account_type"],
        "provider": record["identity"].get("provider"),
        "display_name": record.get("display_name"),
        "auth_state": record.get("auth_state"),
    }


def get_active_identity(*, app_dir: Path = APP_DIR) -> dict[str, Any] | None:
    app_dir = Path(app_dir)
    with InterProcessFileLock(VAULT_LOCK_FILE, app_dir=app_dir):
        vault = _load_vault_locked(app_dir)
        active_id = vault.get("active_account_id")
        if active_id is None:
            return None
        return _identity_summary(active_id, vault["accounts"][active_id])


def get_account_identity(
    account_id: str, *, app_dir: Path = APP_DIR
) -> dict[str, Any]:
    app_dir = Path(app_dir)
    normalized_id = derive_account_id({"account_id": account_id})
    with InterProcessFileLock(VAULT_LOCK_FILE, app_dir=app_dir):
        vault = _load_vault_locked(app_dir)
        record = vault["accounts"].get(normalized_id)
        if record is None:
            raise ValueError("账号不存在")
        return _identity_summary(normalized_id, record)


def get_account_auth(
    account_id: str | None = None,
    *,
    allow_expired: bool = False,
    app_dir: Path = APP_DIR,
) -> dict[str, Any]:
    app_dir = Path(app_dir)
    with InterProcessFileLock(VAULT_LOCK_FILE, app_dir=app_dir):
        vault = _load_vault_locked(app_dir)
        selected_id = account_id or vault.get("active_account_id")
        if selected_id is None:
            raise ValueError("尚未选择账号，请先读取当前账号")
        normalized_id = derive_account_id({"account_id": selected_id})
        record = vault["accounts"].get(normalized_id)
        if record is None:
            raise ValueError("账号不存在")
        credential = record.get("credential")
        if not isinstance(credential, dict) or record.get("auth_state") == "missing":
            raise ValueError("该账号没有保存的登录态，请重新读取")
        if not _credential_complete(credential) or record.get("auth_state") == "incomplete":
            raise ValueError("保存的登录态不完整，请重新读取当前账号")
        if record.get("auth_state") == "expired" and not allow_expired:
            raise ValueError("当前登录态已过期，请重新读取当前账号")
        return deepcopy(credential)


def upsert_account(
    payload: dict[str, Any],
    *,
    activate: bool = True,
    validation_result: str = "success",
    validation_error: Exception | str | None = None,
    app_dir: Path = APP_DIR,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("登录态必须是对象")
    stored_id = derive_account_id(payload)
    app_dir = Path(app_dir)
    with InterProcessFileLock(VAULT_LOCK_FILE, app_dir=app_dir):
        vault = _load_vault_locked(app_dir, for_write=True)
        existing = vault["accounts"].get(stored_id)
        record = _record_for_payload(
            payload,
            existing=existing,
            activate=activate,
            validation_result=validation_result,
            validation_error=validation_error,
        )
        vault["accounts"][stored_id] = record
        if activate:
            vault["active_account_id"] = stored_id
        vault["legacy_migrated"] = True
        vault["revision"] += 1
        _commit_vault_locked(vault, app_dir)
        return _safe_record_summary(
            stored_id,
            record,
            active_id=vault.get("active_account_id"),
            app_dir=app_dir,
        )


def activate_account(
    account_id: str, *, app_dir: Path = APP_DIR
) -> dict[str, Any]:
    app_dir = Path(app_dir)
    normalized_id = derive_account_id({"account_id": account_id})
    with InterProcessFileLock(VAULT_LOCK_FILE, app_dir=app_dir):
        vault = _load_vault_locked(app_dir)
        record = vault["accounts"].get(normalized_id)
        if record is None:
            raise ValueError("账号不存在")
        if vault.get("active_account_id") != normalized_id:
            now = _utc_now()
            record["last_used_at_utc"] = now
            record["updated_at_utc"] = now
            vault["active_account_id"] = normalized_id
            vault["revision"] += 1
            _commit_vault_locked(vault, app_dir)
        else:
            _write_legacy_mirror_locked(vault, app_dir)
        return _safe_record_summary(
            normalized_id,
            record,
            active_id=normalized_id,
            app_dir=app_dir,
        )


def remove_account(
    account_id: str, *, app_dir: Path = APP_DIR
) -> dict[str, Any]:
    app_dir = Path(app_dir)
    normalized_id = derive_account_id({"account_id": account_id})
    with InterProcessFileLock(VAULT_LOCK_FILE, app_dir=app_dir):
        vault = _load_vault_locked(app_dir)
        record = vault["accounts"].get(normalized_id)
        if record is None:
            raise ValueError("账号不存在")
        now = _utc_now()
        record["credential"] = None
        record["auth_state"] = "missing"
        record["updated_at_utc"] = now
        record["last_validation"] = {
            "result": "removed",
            "at_utc": now,
            "error": None,
        }
        if vault.get("active_account_id") == normalized_id:
            vault["active_account_id"] = None
        vault["revision"] += 1
        _commit_vault_locked(vault, app_dir)
        return _safe_record_summary(
            normalized_id,
            record,
            active_id=vault.get("active_account_id"),
            app_dir=app_dir,
        )


def record_validation(
    account_id: str,
    result: str,
    error: Exception | str | None = None,
    *,
    app_dir: Path = APP_DIR,
) -> dict[str, Any]:
    app_dir = Path(app_dir)
    normalized_id = derive_account_id({"account_id": account_id})
    normalized_result = _validation_result(result)
    with InterProcessFileLock(VAULT_LOCK_FILE, app_dir=app_dir):
        vault = _load_vault_locked(app_dir)
        record = vault["accounts"].get(normalized_id)
        if record is None:
            raise ValueError("账号不存在")
        now = _utc_now()
        credential = record.get("credential")
        complete = isinstance(credential, dict) and _credential_complete(credential)
        if credential is None:
            record["auth_state"] = "missing"
        elif not complete:
            record["auth_state"] = "incomplete"
        elif normalized_result == "success":
            record["auth_state"] = "valid"
        elif normalized_result == "rejected":
            record["auth_state"] = "expired"
        if normalized_result in ("success", "rejected"):
            record["last_validated_at_utc"] = now
        record["last_validation"] = {
            "result": normalized_result,
            "at_utc": now,
            "error": str(error)[:500] if error is not None else None,
        }
        record["updated_at_utc"] = now
        vault["revision"] += 1
        _commit_vault_locked(vault, app_dir)
        return _safe_record_summary(
            normalized_id,
            record,
            active_id=vault.get("active_account_id"),
            app_dir=app_dir,
        )


def safe_candidate_summary(payload: dict[str, Any]) -> dict[str, Any]:
    stored_id = derive_account_id(payload)
    return {
        "id": stored_id,
        "account_id": stored_id,
        "short_id": stored_id[-6:],
        "account_type": account_type(payload),
        "provider": payload.get("provider") or "wechat-miniapp",
        "display_name": _display_name(payload),
        "has_required_fields": _credential_complete(payload),
        "credential_revision": _credential_revision(payload),
    }
