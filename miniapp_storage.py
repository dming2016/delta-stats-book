#!/usr/bin/env python3
"""Read Delta Force's encrypted WeChat mini-program local Storage."""

from __future__ import annotations

import argparse
import json
import os
import struct
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from Crypto.Cipher import AES

from auth_registry import (
    LEGACY_AUTH_ENTROPY,
    LEGACY_AUTH_FILE,
    VAULT_FILE,
    registry_summary,
    upsert_account,
)
from secure_store import APP_DIR


WXID = "wx1c36464bbea2507a"
AUTH_FILE = LEGACY_AUTH_FILE
AUTH_ENTROPY = LEGACY_AUTH_ENTROPY
WECHAT_STORAGE_REQUIRED_KEYS = (
    "openid",
    "ieg_ams_session_token",
    "ieg_ams_token",
    "ieg_ams_token_time",
)
WECHAT_REQUIRED_AUTH_KEYS = (
    "openid",
    "acctype",
    "appid",
    "ieg_ams_session_token",
    "ieg_ams_token",
    "ieg_ams_token_time",
)
WECHAT_OPTIONAL_AUTH_KEYS = ("ieg_ams_token_v2", "unionid", "verifysession")
QQ_STORAGE_REQUIRED_KEYS = (
    "qq_openid",
    "qq_ieg_ams_session_token",
    "qq_ieg_ams_appid",
)
QQ_REQUIRED_AUTH_KEYS = ("openid", "acctype", "appid", "access_token")


def users_root() -> Path:
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return appdata / "Tencent" / "xwechat" / "radium" / "users"


def storage_candidates() -> list[Path]:
    matches = []
    for storage in users_root().glob(f"*/applet/local/{WXID}/usrmmkvstorage0"):
        main_file = storage / WXID
        crc_file = storage / f"{WXID}.crc"
        if main_file.is_file() and crc_file.is_file():
            matches.append(storage)
    return sorted(
        matches,
        key=lambda storage: max(
            (storage / WXID).stat().st_mtime,
            (storage / f"{WXID}.crc").stat().st_mtime,
        ),
        reverse=True,
    )


def find_storage() -> Path:
    matches = storage_candidates()
    for storage in matches:
        return storage
    raise FileNotFoundError(f"WeChat mini-program Storage not found for {WXID}")


def derive_crypt_key(wxid: str) -> bytes:
    if not (wxid.startswith("wx") and len(wxid) == 18):
        raise ValueError(f"invalid wxid: {wxid!r}")
    return ("w" + wxid[2::2]).encode("ascii")


def read_varint(data: bytes, offset: int) -> tuple[int | None, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 64:
            break
    return None, offset


def unwrap_value(raw: bytes) -> Any:
    if not raw:
        return None
    json_offset = 0
    while json_offset < len(raw):
        byte = raw[json_offset]
        json_offset += 1
        if not byte & 0x80:
            break
    try:
        wrapper = json.loads(raw[json_offset:].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(wrapper, dict) or "data" not in wrapper:
        return wrapper
    data = wrapper["data"]
    data_type = wrapper.get("dataType")
    if data_type == "Boolean":
        return data is True or data == "true"
    if data_type == "Number" and isinstance(data, str):
        try:
            return float(data) if "." in data else int(data)
        except ValueError:
            return data
    if data_type == "Object" and isinstance(data, str):
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return data
    return data


def decrypt_storage(storage: Path | None = None) -> dict[str, Any]:
    storage = storage or find_storage()
    main_data = (storage / WXID).read_bytes()
    crc_data = (storage / f"{WXID}.crc").read_bytes()
    if len(main_data) < 4 or len(crc_data) < 40:
        raise ValueError("MMKV storage files are truncated")

    iv = crc_data[12:28]
    actual_size = struct.unpack_from("<I", crc_data, 28)[0]
    ciphertext = main_data[4 : 4 + actual_size]
    if len(ciphertext) != actual_size:
        raise ValueError("MMKV payload is shorter than its metadata")

    key = derive_crypt_key(WXID)[:16].ljust(16, b"\x00")
    plain = AES.new(key, AES.MODE_CFB, iv=iv, segment_size=128).decrypt(ciphertext)

    values: dict[str, Any] = {}
    offset = 4
    while offset < len(plain):
        key_length, key_offset = read_varint(plain, offset)
        if not key_length or key_length > 1024 or key_offset + key_length > len(plain):
            break
        try:
            name = plain[key_offset : key_offset + key_length].decode("utf-8")
        except UnicodeDecodeError:
            break
        value_length, value_offset = read_varint(plain, key_offset + key_length)
        if value_length is None or value_offset + value_length > len(plain):
            break
        values[name] = unwrap_value(plain[value_offset : value_offset + value_length])
        offset = value_offset + value_length

    if not values:
        raise ValueError("MMKV decryption produced no keys; derived key may be invalid")
    return values


def selected_account_type(storage: dict[str, Any]) -> str:
    login_type = str(storage.get("currentLoginType", "")).strip().lower()
    acctype = str(storage.get("acctype", "")).strip().lower()
    if login_type == "qq":
        return "qq"
    if login_type in ("wx", "wechat"):
        return "wechat"
    return "qq" if acctype == "qc" else "wechat"


def has_required_fields(payload: dict[str, Any]) -> bool:
    required = (
        QQ_REQUIRED_AUTH_KEYS
        if str(payload.get("acctype", "")).lower() == "qc"
        else WECHAT_REQUIRED_AUTH_KEYS
    )
    return all(payload.get(key) not in (None, "") for key in required)


def display_name_from_storage(storage: dict[str, Any]) -> str | None:
    role = storage.get("role")
    if isinstance(role, str):
        try:
            role = json.loads(role)
        except json.JSONDecodeError:
            role = None
    candidates = []
    if isinstance(role, dict):
        candidates.extend((role.get("nickname"), role.get("userName")))
    candidates.extend((storage.get("nickname"), storage.get("userName")))
    for candidate in candidates:
        value = unquote(str(candidate or "").strip())
        if value:
            return value[:120]
    return None


def auth_payload_from_storage(storage: dict[str, Any]) -> dict[str, Any]:
    account_type = selected_account_type(storage)
    payload = {
        "provider": "wechat-miniapp",
        "account_type": account_type,
        "saved_at_utc": datetime.now(UTC).isoformat(),
    }
    display_name = display_name_from_storage(storage)
    if display_name:
        payload["display_name"] = display_name
    if account_type == "qq":
        missing = [key for key in QQ_STORAGE_REQUIRED_KEYS if not storage.get(key)]
        if missing:
            raise ValueError(
                f"mini-program QQ login is incomplete; missing keys: {', '.join(missing)}"
            )
        payload.update(
            {
                "acctype": "qc",
                "openid": storage["qq_openid"],
                "appid": storage["qq_ieg_ams_appid"],
                "access_token": storage["qq_ieg_ams_session_token"],
            }
        )
        return payload

    missing = [key for key in WECHAT_STORAGE_REQUIRED_KEYS if not storage.get(key)]
    if missing:
        raise ValueError(
            f"mini-program login is incomplete; missing keys: {', '.join(missing)}"
        )
    payload.update({"appid": WXID, "acctype": "mini"})
    for key in (*WECHAT_STORAGE_REQUIRED_KEYS, *WECHAT_OPTIONAL_AUTH_KEYS):
        if storage.get(key) not in (None, ""):
            payload[key] = storage[key]
    return payload


def auth_payload(storage_path: Path | None = None) -> dict[str, Any]:
    return auth_payload_from_storage(decrypt_storage(storage_path))


def auth_candidates() -> list[dict[str, Any]]:
    payloads = []
    for storage in storage_candidates():
        try:
            payloads.append(auth_payload(storage))
        except (OSError, ValueError):
            continue
    return payloads


def save_auth(payload: dict[str, Any], *, app_dir: Path = APP_DIR) -> Path:
    upsert_account(
        payload,
        activate=True,
        validation_result="success",
        app_dir=app_dir,
    )
    return Path(app_dir) / AUTH_FILE


def import_auth(*, app_dir: Path = APP_DIR) -> Path:
    return save_auth(auth_payload(), app_dir=app_dir)


def auth_status(*, app_dir: Path = APP_DIR) -> dict[str, Any]:
    try:
        summary = registry_summary(app_dir=app_dir)
    except Exception as exc:
        return {
            "exists": False,
            "corrupted": True,
            "error": f"账号保险箱无法读取：{exc}",
            "path": str(Path(app_dir) / VAULT_FILE),
        }
    active_id = summary.get("active_account_id")
    active = next(
        (account for account in summary["accounts"] if account["id"] == active_id),
        None,
    )
    if active is None:
        return {
            "exists": False,
            "active_account_id": None,
            "accounts": len(summary["accounts"]),
            "path": str(Path(app_dir) / VAULT_FILE),
        }
    return {
        "exists": active["has_credential"],
        "active_account_id": active["id"],
        "provider": active.get("provider"),
        "account_type": active["account_type"],
        "display_name": active.get("display_name"),
        "saved_at_utc": active.get("saved_at_utc"),
        "has_required_fields": active["has_required_fields"],
        "auth_state": active["auth_state"],
        "last_validated_at_utc": active.get("last_validated_at_utc"),
        "last_validation": active.get("last_validation"),
        "accounts": len(summary["accounts"]),
        "path": str(Path(app_dir) / VAULT_FILE),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("scan", "import", "status"))
    args = parser.parse_args()

    if args.action == "scan":
        storage = decrypt_storage()
        try:
            payload = auth_payload_from_storage(storage)
            account_type = payload["account_type"]
            complete = has_required_fields(payload)
        except ValueError:
            account_type = selected_account_type(storage)
            complete = False
        safe = {
            "wxid": WXID,
            "key_count": len(storage),
            "keys": sorted(storage),
            "account_type": account_type,
            "has_required_fields": complete,
        }
        print(json.dumps(safe, ensure_ascii=False))
        return 0
    if args.action == "import":
        path = import_auth()
        print(json.dumps({"imported": True, "path": str(path)}, ensure_ascii=False))
        return 0
    print(json.dumps(auth_status(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
