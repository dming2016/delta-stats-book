#!/usr/bin/env python3
"""Local HTTP service for the Delta Force per-match dashboard."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import threading
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from account_storage import (
    account_id as account_id_for_auth,
    migrate_legacy_data,
    preferences_file as account_preferences_file,
)
from auth_registry import (
    activate_account,
    get_account_identity,
    get_active_identity,
    record_validation,
    registry_summary,
    remove_account,
    safe_candidate_summary,
    upsert_account,
)
from delta_data import (
    LOCAL_TZ,
    as_int,
    detect_sessions,
    filter_matches,
    friend_comparisons,
    friend_options,
    map_options,
    read_cache,
    summary,
    sync_matches,
    warfare_friend_comparisons,
    warfare_summary,
)
from delta_api import (
    AmsError,
    DeltaAmsClient,
    SavedAuthError,
    classify_error,
    is_auth_rejected_error,
    load_auth,
)
from diagnostics import configure_logging
from miniapp_storage import WXID, auth_candidates, auth_status
from secure_store import APP_DIR, InterProcessFileLock


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
WEB_ROOT = ROOT / "web"
PREFERENCES_FILE = APP_DIR / "preferences.json"
SYNC_LOCK = threading.Lock()
AUTH_LOCK = threading.Lock()
SYNC_STATE_LOCK = threading.Lock()
SYNC_JOB: dict = {
    "id": None,
    "account_id": None,
    "status": "idle",
    "progress": 0,
    "message": "",
    "result": None,
    "error": None,
    "error_kind": None,
    "retryable": False,
    "auth_invalid": False,
    "auth_state": None,
}
LOGGER = configure_logging("server")
CLIENT_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)
AUTH_RECOVERY_RETRY_AFTER_SECONDS = 3
AUTH_RECOVERY_MAX_ATTEMPTS = 2
OPERATION_BUSY_RETRY_AFTER_SECONDS = 1
WECHAT_MINIAPP_URI = (
    f"weixin://dl/business/?appid={WXID}"
    "&path=pages/index/index&env_version=release"
)
WECHAT_FALLBACK_URI = "weixin://"


class OperationBusyError(RuntimeError):
    """Another local window is synchronizing or mutating account state."""


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=LOCAL_TZ)


def active_auth() -> dict | None:
    try:
        return get_active_identity()
    except Exception:
        return None


def resolve_account_identity(account_id: str | None) -> dict | None:
    if account_id:
        return get_account_identity(account_id)
    return get_active_identity()


def resolve_preferences_file(
    auth: dict | None = None, *, app_dir: Path = APP_DIR
) -> Path:
    if auth is None:
        return app_dir / "accounts" / "_unassigned" / "preferences.json"
    migrate_legacy_data(auth, app_dir=app_dir)
    return account_preferences_file(auth, app_dir=app_dir)


def read_preferences(
    auth: dict | None = None, *, app_dir: Path = APP_DIR
) -> dict:
    path = resolve_preferences_file(auth, app_dir=app_dir)
    if not path.is_file():
        return {"favorite_friends": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"preferences.corrupt-{stamp}.json")
        try:
            os.replace(path, backup)
            location = f"，原文件已保留为 {backup.name}"
        except OSError:
            location = ""
        return {
            "favorite_friends": [],
            "warning": f"固定好友配置损坏，已恢复为空{location}：{exc}",
        }
    favorites = payload.get("favorite_friends", []) if isinstance(payload, dict) else []
    return {
        "favorite_friends": list(
            dict.fromkeys(name.strip() for name in favorites if isinstance(name, str) and name.strip())
        )
    }


def write_preferences(
    payload: dict, auth: dict | None = None, *, app_dir: Path = APP_DIR
) -> dict:
    if auth is None:
        raise ValueError("请先选择账号")
    if not isinstance(payload, dict):
        raise ValueError("preference payload must be an object")
    favorites = payload.get("favorite_friends", [])
    if not isinstance(favorites, list):
        raise ValueError("favorite_friends must be a list")
    preferences = {
        "favorite_friends": list(
            dict.fromkeys(name.strip() for name in favorites if isinstance(name, str) and name.strip())
        )
    }
    path = resolve_preferences_file(auth, app_dir=app_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(preferences, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return preferences


def migrate_active_account_data(auth: dict | None = None) -> dict | None:
    auth = auth or active_auth()
    if auth is None:
        return None
    result = migrate_legacy_data(auth)
    read_cache(auth)
    read_preferences(auth)
    return result


def session_options(
    all_matches: list[dict], selected_matches: list[dict], friends: list[str]
) -> list[dict]:
    sessions = detect_sessions(all_matches)
    match_rows = [
        (datetime.fromisoformat(match["timestamp"]), match)
        for match in all_matches
        if match.get("timestamp")
    ]
    selected_timestamps = [
        datetime.fromisoformat(match["timestamp"])
        for match in selected_matches
        if match.get("timestamp")
    ]
    output = []
    for session in sessions:
        start = datetime.fromisoformat(session["from"])
        end = datetime.fromisoformat(session["to"])
        session_matches = [
            match for timestamp, match in match_rows if start <= timestamp <= end
        ]
        profit_matches = [
            match for match in session_matches if match.get("mode") == "sol"
        ]
        option = {
            **session,
            "profit_matches": len(profit_matches),
            "net_profit": (
                sum(as_int(match.get("net_profit")) for match in profit_matches)
                if profit_matches
                else None
            ),
        }
        if friends:
            option["friend_matches"] = sum(
                start <= timestamp <= end for timestamp in selected_timestamps
            )
        output.append(option)
    return output


def selected_session_ranges(
    query: dict[str, list[str]], sessions: list[dict]
) -> tuple[list[str], list[tuple[datetime, datetime]] | None]:
    """Resolve requested stable session IDs against the current trusted options."""
    requested_ids = query.get("session")
    if requested_ids is None:
        return [], None

    available = {
        str(session.get("id", session["from"])): session
        for session in sessions
        if session.get("from") and session.get("to")
    }
    accepted_ids: list[str] = []
    ranges: list[tuple[datetime, datetime]] = []
    for raw_id in requested_ids:
        session_id = raw_id.strip() if isinstance(raw_id, str) else ""
        session = available.get(session_id)
        if session is None or session_id in accepted_ids:
            continue
        accepted_ids.append(session_id)
        ranges.append(
            (
                datetime.fromisoformat(session["from"]),
                datetime.fromisoformat(session["to"]),
            )
        )
    return accepted_ids, ranges


def query_value(
    query: dict[str, list[str]], name: str, default: str = "all"
) -> str:
    value = query.get(name, [default])[0]
    return value.strip() if isinstance(value, str) and value.strip() else default


def warfare_dimension_matches(match: dict, name: str, expected: str) -> bool:
    if expected == "all":
        return True
    expected_folded = expected.casefold()
    if name == "result":
        if expected_folded in {"win", "victory", "胜利", "1"}:
            return warfare_result_value(match) == "win"
        if expected_folded in {"loss", "defeat", "失败", "2"}:
            return warfare_result_value(match) == "loss"
        if expected_folded in {"quit", "exit", "中途退出", "0"}:
            return warfare_result_value(match) == "quit"
        result = str(match.get("result", "")).casefold()
        result_code = match.get("result_code")
        return result == expected_folded or (
            result_code is not None
            and str(result_code).casefold() == expected_folded
        )

    aliases = {
        "side": ("side", "side_label"),
        "operator": ("operator", "operator_name", "operator_label"),
        "ruleset": ("ruleset", "ruleset_label"),
    }
    return any(
        value not in (None, "") and str(value).casefold() == expected_folded
        for key in aliases[name]
        for value in (match.get(key),)
    )


def filter_warfare_dimensions(
    matches: list[dict], *, result: str, side: str, operator: str, ruleset: str
) -> list[dict]:
    filters = {
        "result": result,
        "side": side,
        "operator": operator,
        "ruleset": ruleset,
    }
    return [
        match
        for match in matches
        if all(
            warfare_dimension_matches(match, name, expected)
            for name, expected in filters.items()
        )
    ]


def warfare_result_value(match: dict) -> str | None:
    result_code = match.get("result_code")
    if result_code is not None:
        code = str(result_code).strip()
        if code == "1":
            return "win"
        if code == "2":
            return "loss"
        if code in {"0", "3"}:
            return "quit"
    result = str(match.get("result", "")).strip().casefold()
    if result in {"win", "victory", "胜利"}:
        return "win"
    if result in {"loss", "defeat", "失败"}:
        return "loss"
    if result in {"quit", "exit", "中途退出"}:
        return "quit"
    return None


def result_options(matches: list[dict]) -> list[dict[str, str]]:
    labels = {"win": "胜利", "loss": "失败", "quit": "中途退出"}
    available = {
        result
        for match in matches
        if (result := warfare_result_value(match)) is not None
    }
    return [
        {"value": value, "label": labels[value]}
        for value in ("win", "loss", "quit")
        if value in available
    ]


def labeled_options(
    matches: list[dict], value_key: str, *label_keys: str
) -> list[dict[str, str]]:
    options: dict[str, str] = {}
    for match in matches:
        raw_value = match.get(value_key)
        if raw_value in (None, ""):
            continue
        value = str(raw_value).strip()
        if not value:
            continue
        label = next(
            (
                str(match.get(key)).strip()
                for key in label_keys
                if match.get(key) not in (None, "") and str(match.get(key)).strip()
            ),
            value,
        )
        options.setdefault(value, label)
    return [
        {"value": value, "label": label}
        for value, label in sorted(options.items(), key=lambda item: (item[1], item[0]))
    ]


def timeline_summary(matches: list[dict]) -> dict:
    return {
        "matches": len(matches),
        "sol_matches": sum(match.get("mode") == "sol" for match in matches),
        "mp_matches": sum(match.get("mode") == "mp" for match in matches),
    }


def ruleset_options(matches: list[dict]) -> list[str]:
    return sorted(
        {
            str(match["ruleset"]).strip()
            for match in matches
            if match.get("ruleset") not in (None, "")
            and str(match["ruleset"]).strip()
        }
    )


def dataset(query: dict[str, list[str]]) -> dict:
    requested_account_id = query.get("account_id", [None])[0]
    auth = resolve_account_identity(requested_account_id)
    cache = read_cache(auth) if auth is not None else read_cache()
    preferences = read_preferences(auth)
    current_account_id = account_id_for_auth(auth) if auth is not None else None
    account_summary = next(
        (
            item
            for item in registry_summary().get("accounts", [])
            if item.get("id") == current_account_id
        ),
        None,
    )
    all_matches = cache.get("matches", [])
    start = parse_datetime(query.get("from", [None])[0])
    end = parse_datetime(query.get("to", [None])[0])
    mode = query_value(query, "mode", "sol")
    if mode not in {"all", "sol", "mp"}:
        mode = "sol"
    view = {"sol": "firebreak", "mp": "warfare", "all": "timeline"}[mode]
    requested_difficulty = query_value(query, "difficulty")
    difficulty = requested_difficulty if mode == "sol" else "all"
    map_name = query_value(query, "map")
    result = query_value(query, "result") if mode == "mp" else "all"
    side = query_value(query, "side") if mode == "mp" else "all"
    operator = query_value(query, "operator") if mode == "mp" else "all"
    ruleset = query_value(query, "ruleset") if mode == "mp" else "all"
    friends = [name.strip() for name in query.get("friend", []) if name.strip()]
    friend_mode = query_value(query, "friend_mode", "any")
    if friend_mode not in {"any", "all"}:
        raise ValueError("friend_mode must be 'any' or 'all'")

    mode_base = filter_matches(all_matches, mode=mode)
    business_matches = filter_matches(
        mode_base,
        difficulty=difficulty,
        map_name=map_name,
    )
    if mode == "mp":
        business_matches = filter_warfare_dimensions(
            business_matches,
            result=result,
            side=side,
            operator=operator,
            ruleset=ruleset,
        )
    sessions = detect_sessions(mode_base)
    accepted_session_ids, time_ranges = selected_session_ranges(query, sessions)
    session_selected = filter_matches(
        business_matches,
        friends=friends,
        friend_mode=friend_mode,
    )
    base_matches = filter_matches(
        business_matches,
        start=start,
        end=end,
        time_ranges=time_ranges,
    )
    selected = filter_matches(
        base_matches,
        friends=friends,
        friend_mode=friend_mode,
    )
    candidate_friends = friend_options(mode_base)
    candidate_friend_names = {item["name"].casefold() for item in candidate_friends}
    favorite_friends = [
        name
        for name in preferences["favorite_friends"]
        if name.casefold() in candidate_friend_names
    ]
    if mode == "sol":
        current_summary = summary(selected)
        current_warfare_summary = None
        current_timeline_summary = None
        comparison_matches = selected if friend_mode == "all" else base_matches
        comparisons = friend_comparisons(comparison_matches, friends)
    elif mode == "mp":
        current_summary = None
        current_warfare_summary = warfare_summary(selected)
        current_timeline_summary = None
        comparison_matches = selected if friend_mode == "all" else base_matches
        comparisons = warfare_friend_comparisons(comparison_matches, friends)
    else:
        current_summary = None
        current_warfare_summary = None
        current_timeline_summary = timeline_summary(selected)
        comparisons = []
    return {
        "view": view,
        "updated_at": cache.get("updated_at"),
        "account_id": current_account_id,
        "account": account_summary,
        "available": {
            "first": all_matches[-1]["timestamp"] if all_matches else None,
            "last": all_matches[0]["timestamp"] if all_matches else None,
            "total": len(all_matches),
        },
        "filters": {
            "from": start.isoformat() if start else None,
            "to": end.isoformat() if end else None,
            "mode": mode,
            "difficulty": difficulty,
            "map": map_name,
            "result": result,
            "side": side,
            "operator": operator,
            "ruleset": ruleset,
            "friends": friends,
            "friend_mode": friend_mode,
            "sessions": accepted_session_ids,
        },
        "summary": current_summary,
        "warfare_summary": current_warfare_summary,
        "timeline_summary": current_timeline_summary,
        "friend_comparisons": comparisons,
        "maps": map_options(mode_base, mode),
        "friends": candidate_friends,
        "favorite_friends": favorite_friends,
        "sessions": session_options(mode_base, session_selected, friends),
        "rulesets": ruleset_options(mode_base) if mode == "mp" else [],
        "results": result_options(mode_base) if mode == "mp" else [],
        "sides": labeled_options(mode_base, "side", "side_label")
        if mode == "mp"
        else [],
        "operators": labeled_options(
            mode_base, "operator", "operator_name", "operator_label"
        )
        if mode == "mp"
        else [],
        "matches": selected,
        "players": [],
        "sync_enabled": bool(account_summary and account_summary.get("can_sync")),
        "warnings": [
            warning
            for warning in (cache.get("warning"), preferences.get("warning"))
            if warning
        ],
    }


def is_auth_rejected(error: Exception) -> bool:
    return is_auth_rejected_error(error)


def is_auth_invalid(error: Exception) -> bool:
    return classify_error(error).auth_invalid


def sync_failure(error: Exception) -> tuple[str, bool]:
    failure = classify_error(error)
    if failure.kind == "busy":
        return (
            "腾讯战绩接口暂时繁忙，自动重试仍未恢复，请稍后再刷新",
            False,
        )
    if failure.kind == "expired" and isinstance(error, AmsError):
        return (
            "当前登录态已被腾讯拒绝，请在电脑版微信重新打开一次三角洲官方小程序",
            True,
        )
    if failure.kind == "network":
        return "无法连接腾讯战绩接口，请检查网络后重试", False
    return str(error), failure.auth_invalid


def failure_payload(
    error: Exception,
    *,
    message: str | None = None,
    auth_state: str | None = None,
) -> dict:
    failure = classify_error(error)
    if message is None:
        message, _ = sync_failure(error)
    payload = {
        "error": message,
        "error_kind": failure.kind,
        "retryable": failure.retryable,
        "auth_invalid": failure.auth_invalid,
    }
    if auth_state:
        payload["auth_state"] = auth_state
    return payload


def operation_busy_payload(error: Exception) -> dict:
    return {
        "ok": False,
        "pending": True,
        "status": "operation_busy",
        "error": str(error),
        "error_kind": "operation_busy",
        "retryable": True,
        "auth_invalid": False,
        "retry_after_seconds": OPERATION_BUSY_RETRY_AFTER_SECONDS,
    }


def auth_probe_failure_message(failures: list[Exception]) -> str:
    kinds = [classify_error(error).kind for error in failures]
    if "network" in kinds:
        return "无法连接腾讯战绩接口，请检查网络后重试；当前无法判断登录态是否失效"
    if "busy" in kinds:
        return "腾讯战绩接口暂时繁忙，请稍后重新读取当前账号"
    messages = [str(error) for error in failures if str(error)]
    if "expired" in kinds:
        return "当前登录态已被腾讯拒绝，请在电脑版微信重新打开一次三角洲官方小程序"
    detail = messages[-1] if messages else "未知错误"
    return f"读取当前账号时出现异常，当前无法判断登录态是否失效：{detail}"


def sync_result(payload: dict, account_id: str | None = None) -> dict:
    matches = payload.get("matches", [])
    return {
        "ok": True,
        "account_id": account_id,
        "error_kind": None,
        "retryable": False,
        "auth_invalid": False,
        "auth_state": "valid",
        "updated_at": payload.get("updated_at"),
        "matches": len(matches),
        "details_loaded": sum(bool(match.get("details_loaded")) for match in matches),
        "income_loaded": sum(bool(match.get("income_loaded")) for match in matches),
        "detail_failures": len(payload.get("detail_failures", [])),
        "remote": {"enabled": False},
    }


def update_sync_job(job_id: str, **values) -> None:
    with SYNC_STATE_LOCK:
        if SYNC_JOB.get("id") == job_id:
            SYNC_JOB.update(values)


def validation_result(error: Exception) -> str:
    kind = classify_error(error).kind
    if is_auth_rejected(error):
        return "rejected"
    if kind == "busy":
        return "busy"
    if kind == "network":
        return "network_error"
    return "error"


def remember_validation(
    account_id: str, result: str, error: Exception | str | None = None
) -> dict | None:
    try:
        return record_validation(
            account_id, result, error=str(error) if error else None
        )
    except Exception:
        LOGGER.exception("Failed to persist account validation state")
        return None


def acquire_operation_lock() -> InterProcessFileLock:
    lock = InterProcessFileLock("operation.lock", timeout=0)
    try:
        lock.acquire()
    except TimeoutError as exc:
        raise OperationBusyError("另一个窗口正在同步或管理账号，请稍后再试") from exc
    return lock


def operation_is_busy() -> bool:
    try:
        lock = acquire_operation_lock()
    except OperationBusyError:
        return True
    lock.release()
    return False


def accounts_payload(*, busy: bool | None = None) -> dict:
    payload = registry_summary()
    payload["sync_running"] = operation_is_busy() if busy is None else busy
    return payload


def account_summary(
    account_id: str, *, summary: dict | None = None
) -> dict | None:
    registry = summary if summary is not None else registry_summary()
    return next(
        (
            account
            for account in registry.get("accounts", [])
            if account.get("id") == account_id
        ),
        None,
    )


def recovery_pending_payload(
    *,
    account_id: str,
    account: dict,
    status: str,
    message: str,
    error_kind: str = "auth_waiting",
    detected_account: dict | None = None,
    credential_revision: str | None = None,
    probe_error_kind: str | None = None,
) -> dict:
    payload = {
        "ok": False,
        "pending": True,
        "status": status,
        "account_id": account_id,
        "account": account,
        "auth_state": account.get("auth_state"),
        "error": message,
        "error_kind": error_kind,
        "retryable": True,
        "auth_invalid": account.get("auth_state") == "expired",
        "retry_after_seconds": AUTH_RECOVERY_RETRY_AFTER_SECONDS,
        **accounts_payload(busy=False),
    }
    if detected_account is not None:
        payload["detected_account"] = detected_account
    if credential_revision:
        payload["credential_revision"] = credential_revision
    if probe_error_kind:
        payload["probe_error_kind"] = probe_error_kind
    return payload


def wechat_protocol_registered() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"weixin") as protocol_key:
            winreg.QueryValueEx(protocol_key, "URL Protocol")
        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT, r"weixin\shell\open\command"
        ) as command_key:
            command, _ = winreg.QueryValueEx(command_key, None)
    except (ImportError, OSError):
        return False
    return bool(str(command or "").strip())


def open_wechat() -> dict:
    if sys.platform != "win32":
        return {
            "ok": False,
            "direct": False,
            "fallback": False,
            "message": "当前系统不支持自动打开微信，请手动打开微信电脑版",
        }
    if not wechat_protocol_registered():
        return {
            "ok": False,
            "direct": False,
            "fallback": False,
            "message": "未检测到微信协议，请手动打开微信电脑版",
        }
    try:
        os.startfile(WECHAT_MINIAPP_URI)
    except OSError as exc:
        LOGGER.warning("Failed to open Delta Force mini-program URI: %s", exc)
    else:
        return {
            "ok": True,
            "direct": True,
            "fallback": False,
            "message": "已打开三角洲行动小程序，请确认当前账号",
        }
    try:
        os.startfile(WECHAT_FALLBACK_URI)
    except OSError as fallback_error:
        LOGGER.warning("Failed to open registered WeChat protocol: %s", fallback_error)
        return {
            "ok": False,
            "direct": False,
            "fallback": False,
            "message": "微信启动失败，请手动打开微信电脑版",
        }
    return {
        "ok": True,
        "direct": False,
        "fallback": True,
        "message": "未能直达小程序，已打开微信，请手动进入三角洲行动小程序",
    }


def load_sync_auth(requested_account_id: str | None = None) -> tuple[str, dict]:
    auth = load_auth(requested_account_id)
    return account_id_for_auth(auth), auth


def run_sync_job(
    job_id: str,
    account_id: str,
    auth: dict,
    operation_lock: InterProcessFileLock,
) -> None:
    try:
        payload = sync_matches(
            pages=10,
            detail_days=3650,
            assist_days=7,
            workers=4,
            progress=lambda percent, message: update_sync_job(
                job_id, progress=percent, message=message
            ),
            auth=auth,
        )
        update_sync_job(
            job_id,
            status="completed",
            progress=100,
            message="同步完成",
            result=sync_result(payload, account_id),
            error=None,
            error_kind=None,
            retryable=False,
            auth_invalid=False,
            auth_state="valid",
        )
        remember_validation(account_id, "success")
    except Exception as exc:
        if isinstance(exc, AmsError):
            LOGGER.exception("Background sync failed (AMS code=%r)", exc.code)
        else:
            LOGGER.exception("Background sync failed")
        account = remember_validation(account_id, validation_result(exc), exc)
        auth_state = account.get("auth_state") if isinstance(account, dict) else None
        update_sync_job(
            job_id,
            status="failed",
            message="同步失败",
            **failure_payload(exc, auth_state=auth_state),
        )
    finally:
        operation_lock.release()
        SYNC_LOCK.release()


def start_sync_job(requested_account_id: str | None = None) -> tuple[dict, bool]:
    if not SYNC_LOCK.acquire(blocking=False):
        with SYNC_STATE_LOCK:
            return dict(SYNC_JOB), False
    operation_lock = None
    try:
        operation_lock = acquire_operation_lock()
        account_id, auth = load_sync_auth(requested_account_id)
    except Exception:
        if operation_lock is not None:
            operation_lock.release()
        SYNC_LOCK.release()
        raise
    job_id = uuid.uuid4().hex
    with SYNC_STATE_LOCK:
        SYNC_JOB.update(
            id=job_id,
            account_id=account_id,
            status="running",
            progress=0,
            message="准备同步",
            result=None,
            error=None,
            error_kind=None,
            retryable=False,
            auth_invalid=False,
            auth_state=None,
        )
    try:
        threading.Thread(
            target=run_sync_job,
            args=(job_id, account_id, auth, operation_lock),
            name="delta-stats-sync",
            daemon=True,
        ).start()
    except Exception:
        operation_lock.release()
        SYNC_LOCK.release()
        raise
    with SYNC_STATE_LOCK:
        return dict(SYNC_JOB), True


class Handler(BaseHTTPRequestHandler):
    server_version = "DeltaForceIntelAssistant/1.0"

    def log_message(self, format: str, *args) -> None:
        LOGGER.debug(format, *args)

    def send_json(self, payload: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except CLIENT_DISCONNECT_ERRORS:
            self.close_connection = True
            LOGGER.debug("Local client disconnected while sending %s", self.path)

    def send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except CLIENT_DISCONNECT_ERRORS:
            self.close_connection = True
            LOGGER.debug("Local client disconnected while sending %s", self.path)

    def read_json_body(self, *, max_size: int = 65536) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > max_size:
            raise ValueError("请求内容过大")
        body = self.rfile.read(length)
        if not body:
            return {}
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是对象")
        return payload

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/api/matches":
            try:
                payload = dataset(query)
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            else:
                self.send_json(payload)
            return
        if parsed.path == "/api/accounts":
            try:
                self.send_json(accounts_payload())
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/auth/candidates":
            try:
                saved_ids = {
                    item.get("id") for item in registry_summary().get("accounts", [])
                }
                candidates = []
                seen = set()
                for candidate in auth_candidates():
                    summary = safe_candidate_summary(candidate)
                    candidate_id = summary.get("id")
                    if not candidate_id or candidate_id in seen:
                        continue
                    seen.add(candidate_id)
                    summary["saved"] = candidate_id in saved_ids
                    candidates.append(summary)
                self.send_json({"candidates": candidates})
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/status":
            auth = active_auth()
            cache = read_cache(auth) if auth is not None else read_cache()
            matches = cache.get("matches", [])
            self.send_json(
                {
                    "auth": auth_status(),
                    "updated_at": cache.get("updated_at"),
                    "matches": len(matches),
                    "details_loaded": sum(bool(match.get("details_loaded")) for match in matches),
                    "income_loaded": sum(bool(match.get("income_loaded")) for match in matches),
                    "warning": cache.get("warning"),
                }
            )
            return
        if parsed.path == "/api/sync/status":
            requested = query.get("id", [None])[0]
            with SYNC_STATE_LOCK:
                job = dict(SYNC_JOB)
            if not requested or requested != job.get("id"):
                self.send_json({"error": "同步任务不存在"}, HTTPStatus.NOT_FOUND)
            else:
                self.send_json(job)
            return
        if parsed.path == "/api/preferences":
            try:
                auth = resolve_account_identity(query.get("account_id", [None])[0])
                self.send_json(read_preferences(auth))
            except Exception as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path in ("/", "/delta-stats-page.html", "/index.html"):
            self.send_file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/assets/"):
            asset = WEB_ROOT / "assets" / Path(parsed.path).name
            content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
            self.send_file(asset, content_type)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/api/wechat/open":
            try:
                request = self.read_json_body()
                if request:
                    raise ValueError("微信启动接口不接受自定义链接")
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                self.send_json(
                    {
                        "ok": False,
                        "direct": False,
                        "fallback": False,
                        "message": str(exc),
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self.send_json(open_wechat())
            return
        if parsed.path == "/api/auth/import":
            if not AUTH_LOCK.acquire(blocking=False):
                exc = OperationBusyError("正在读取当前账号")
                self.send_json(operation_busy_payload(exc), HTTPStatus.CONFLICT)
                return
            operation_lock = None
            try:
                operation_lock = acquire_operation_lock()
                request = self.read_json_body()
                requested_candidate = str(request.get("candidate_id", "")).strip()
                recovery = request.get("recovery", False)
                if not isinstance(recovery, bool):
                    raise ValueError("recovery 必须是布尔值")
                known_revision_value = request.get("known_revision")
                if known_revision_value is not None and not isinstance(
                    known_revision_value, str
                ):
                    raise ValueError("known_revision 必须是字符串")
                known_revision = str(known_revision_value or "").strip()
                candidates = auth_candidates()
                if recovery:
                    if not requested_candidate:
                        raise ValueError("恢复登录态时缺少账号标识")
                    get_account_identity(requested_candidate)
                    registry = registry_summary()
                    target_account = account_summary(
                        requested_candidate, summary=registry
                    )
                    if target_account is None:
                        raise ValueError("只能恢复已经保存的账号")
                    if not candidates:
                        self.send_json(
                            recovery_pending_payload(
                                account_id=requested_candidate,
                                account=target_account,
                                status="waiting_for_miniapp",
                                message="尚未检测到小程序登录态，请在三角洲行动小程序中确认当前账号",
                            ),
                            HTTPStatus.ACCEPTED,
                        )
                        return

                    candidate = candidates[0]
                    candidate_summary = safe_candidate_summary(candidate)
                    candidate_id = candidate_summary.get("id")
                    candidate_revision = candidate_summary["credential_revision"]
                    if candidate_id != requested_candidate:
                        saved_ids = {
                            account.get("id")
                            for account in registry.get("accounts", [])
                        }
                        detected_account = {
                            **candidate_summary,
                            "saved": candidate_id in saved_ids,
                        }
                        self.send_json(
                            recovery_pending_payload(
                                account_id=requested_candidate,
                                account=target_account,
                                status="account_mismatch",
                                error_kind="account_mismatch",
                                message="小程序当前是其他账号，正在等待切回要刷新的账号",
                                detected_account=detected_account,
                                credential_revision=candidate_revision,
                            ),
                            HTTPStatus.ACCEPTED,
                        )
                        return
                    if known_revision and candidate_revision == known_revision:
                        self.send_json(
                            recovery_pending_payload(
                                account_id=requested_candidate,
                                account=target_account,
                                status="waiting_for_miniapp",
                                message="小程序登录态尚未更新，请在小程序中确认当前账号",
                                credential_revision=candidate_revision,
                            ),
                            HTTPStatus.ACCEPTED,
                        )
                        return

                    try:
                        DeltaAmsClient(candidate).fetch_match_page(4, 1)
                    except Exception as exc:
                        failure = classify_error(exc)
                        if failure.kind == "expired":
                            remembered = remember_validation(
                                requested_candidate, "rejected", exc
                            )
                            expired_account = (
                                remembered
                                if isinstance(remembered, dict)
                                else target_account
                            )
                            expired_account = {
                                **expired_account,
                                "credential_revision": candidate_revision,
                            }
                            self.send_json(
                                recovery_pending_payload(
                                    account_id=requested_candidate,
                                    account=expired_account,
                                    status="waiting_for_miniapp",
                                    message="当前登录态仍被腾讯拒绝，正在等待小程序刷新",
                                    credential_revision=candidate_revision,
                                    probe_error_kind="expired",
                                ),
                                HTTPStatus.ACCEPTED,
                            )
                            return
                        if failure.kind in ("busy", "network"):
                            pending_account = upsert_account(
                                candidate,
                                activate=False,
                                validation_result=validation_result(exc),
                                validation_error=exc,
                            )
                            pending_account = {
                                **pending_account,
                                "credential_revision": candidate_revision,
                            }
                            migrate_active_account_data(candidate)
                            self.send_json(
                                {
                                    "ok": False,
                                    "pending": True,
                                    "status": "pending_verification",
                                    "account_id": requested_candidate,
                                    "account": pending_account,
                                    "credential_revision": candidate_revision,
                                    "auth_state": "pending_verification",
                                    "retry_after_seconds": AUTH_RECOVERY_RETRY_AFTER_SECONDS,
                                    "auth": auth_status(),
                                    **failure_payload(
                                        exc,
                                        message=auth_probe_failure_message([exc]),
                                        auth_state="pending_verification",
                                    ),
                                    **accounts_payload(busy=False),
                                },
                                HTTPStatus.ACCEPTED,
                            )
                            return
                        self.send_json(
                            {
                                "ok": False,
                                **failure_payload(
                                    exc,
                                    message=auth_probe_failure_message([exc]),
                                ),
                            },
                            HTTPStatus.BAD_REQUEST,
                        )
                        return

                    recovered_account = upsert_account(
                        candidate, activate=False, validation_result="success"
                    )
                    recovered_account = {
                        **recovered_account,
                        "credential_revision": candidate_revision,
                    }
                    migrate_active_account_data(candidate)
                    self.send_json(
                        {
                            "ok": True,
                            "pending": False,
                            "status": "recovered",
                            "account_id": requested_candidate,
                            "account": recovered_account,
                            "credential_revision": candidate_revision,
                            "error_kind": None,
                            "retryable": False,
                            "auth_invalid": False,
                            "auth_state": "valid",
                            "auth": auth_status(),
                            **accounts_payload(busy=False),
                        }
                    )
                    return

                if requested_candidate:
                    candidates = [
                        candidate
                        for candidate in candidates
                        if safe_candidate_summary(candidate).get("id")
                        == requested_candidate
                    ]
                if not candidates:
                    message = (
                        "未在电脑版微信找到当前账号的完整登录态，请先在小程序切回该账号"
                        if requested_candidate
                        else "未找到完整的小程序登录态，请先在电脑版微信打开三角洲官方小程序"
                    )
                    self.send_json(
                        {
                            "ok": False,
                            "pending": False,
                            "status": "waiting_for_miniapp",
                            "error": message,
                            "error_kind": "auth_missing",
                            "retryable": True,
                            "auth_invalid": False,
                        },
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                connected = None
                failures = []
                for candidate in candidates:
                    try:
                        DeltaAmsClient(candidate).fetch_match_page(4, 1)
                    except Exception as exc:
                        failures.append(exc)
                        failure = classify_error(exc)
                        if failure.kind == "expired":
                            candidate_id = safe_candidate_summary(candidate).get("id")
                            if candidate_id:
                                remember_validation(candidate_id, "rejected", exc)
                            continue
                        message = auth_probe_failure_message([exc])
                        requested_existing = False
                        if requested_candidate and failure.kind in ("busy", "network"):
                            try:
                                get_account_identity(requested_candidate)
                            except ValueError:
                                pass
                            else:
                                requested_existing = True
                        if requested_existing:
                            pending_account = upsert_account(
                                candidate,
                                activate=True,
                                validation_result=validation_result(exc),
                                validation_error=exc,
                            )
                            migrate_active_account_data(candidate)
                            self.send_json(
                                {
                                    "ok": False,
                                    "pending": True,
                                    "account_id": pending_account["id"],
                                    "auth_state": "pending_verification",
                                    "retry_after_seconds": AUTH_RECOVERY_RETRY_AFTER_SECONDS,
                                    "retry_max_attempts": AUTH_RECOVERY_MAX_ATTEMPTS,
                                    "auth": auth_status(),
                                    **failure_payload(
                                        exc,
                                        message=message,
                                        auth_state="pending_verification",
                                    ),
                                    **accounts_payload(busy=False),
                                },
                                HTTPStatus.ACCEPTED,
                            )
                            return
                        self.send_json(
                            {
                                "ok": False,
                                **failure_payload(exc, message=message),
                            },
                            HTTPStatus.BAD_REQUEST,
                        )
                        return
                    connected = candidate
                    break
                if connected is None:
                    error = failures[-1] if failures else RuntimeError("未知错误")
                    self.send_json(
                        {
                            "ok": False,
                            **failure_payload(
                                error,
                                message=auth_probe_failure_message(failures),
                            ),
                        },
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                upsert_account(
                    connected, activate=True, validation_result="success"
                )
                migrate_active_account_data(connected)
                self.send_json(
                    {
                        "ok": True,
                        "pending": False,
                        "error_kind": None,
                        "retryable": False,
                        "auth_invalid": False,
                        "auth_state": "valid",
                        "auth": auth_status(),
                        **accounts_payload(busy=False),
                    }
                )
            except OperationBusyError as exc:
                self.send_json(operation_busy_payload(exc), HTTPStatus.CONFLICT)
            except Exception as exc:
                self.send_json(
                    {"ok": False, **failure_payload(exc)}, HTTPStatus.BAD_REQUEST
                )
            finally:
                if operation_lock is not None:
                    operation_lock.release()
                AUTH_LOCK.release()
            return
        if parsed.path in ("/api/accounts/switch", "/api/accounts/remove"):
            if not AUTH_LOCK.acquire(blocking=False):
                self.send_json({"error": "正在管理账号"}, HTTPStatus.CONFLICT)
                return
            operation_lock = None
            try:
                operation_lock = acquire_operation_lock()
                request = self.read_json_body()
                account_id = str(request.get("account_id", "")).strip()
                if not account_id:
                    raise ValueError("缺少账号标识")
                if parsed.path.endswith("/switch"):
                    activate_account(account_id)
                else:
                    remove_account(account_id)
                self.send_json({"ok": True, **accounts_payload(busy=False)})
            except OperationBusyError as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            finally:
                if operation_lock is not None:
                    operation_lock.release()
                AUTH_LOCK.release()
            return
        if parsed.path == "/api/preferences":
            try:
                payload = self.read_json_body()
                auth = resolve_account_identity(query.get("account_id", [None])[0])
                self.send_json(write_preferences(payload, auth))
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if parsed.path == "/api/sync/start":
            try:
                job, started = start_sync_job(query.get("account_id", [None])[0])
            except OperationBusyError as exc:
                self.send_json(failure_payload(exc), HTTPStatus.CONFLICT)
                return
            except Exception as exc:
                self.send_json(failure_payload(exc), HTTPStatus.BAD_GATEWAY)
                return
            status = HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT
            self.send_json({"job_id": job.get("id"), **job}, status)
            return
        if parsed.path != "/api/sync":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not SYNC_LOCK.acquire(blocking=False):
            self.send_json(
                failure_payload(OperationBusyError("同步正在进行")),
                HTTPStatus.CONFLICT,
            )
            return
        operation_lock = None
        try:
            operation_lock = acquire_operation_lock()
            account_id, auth = load_sync_auth(query.get("account_id", [None])[0])
            payload = sync_matches(
                pages=10, detail_days=3650, assist_days=7, workers=4, auth=auth
            )
            remember_validation(account_id, "success")
            self.send_json(sync_result(payload, account_id))
        except OperationBusyError as exc:
            self.send_json(failure_payload(exc), HTTPStatus.CONFLICT)
        except Exception as exc:
            account = None
            try:
                if "account_id" in locals():
                    account = remember_validation(
                        account_id, validation_result(exc), exc
                    )
            except Exception:
                LOGGER.exception("Failed to update validation after direct sync")
            auth_state = account.get("auth_state") if isinstance(account, dict) else None
            self.send_json(
                failure_payload(exc, auth_state=auth_state),
                HTTPStatus.BAD_GATEWAY,
            )
        finally:
            if operation_lock is not None:
                operation_lock.release()
            SYNC_LOCK.release()


def create_server(host: str = "127.0.0.1", port: int = 4173) -> ThreadingHTTPServer:
    class LoggedServer(ThreadingHTTPServer):
        def handle_error(self, request, client_address) -> None:
            if isinstance(sys.exc_info()[1], CLIENT_DISCONNECT_ERRORS):
                LOGGER.debug("Local client disconnected: %s", client_address)
                return
            LOGGER.exception("Unhandled local HTTP error from %s", client_address)

    server = LoggedServer((host, port), Handler)
    server.daemon_threads = True
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4173)
    args = parser.parse_args()
    server = create_server(args.host, args.port)
    print(f"Delta Force stats: http://{args.host}:{args.port}/delta-stats-page.html", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
