#!/usr/bin/env python3
"""Tencent AMS client for Delta Force mini-program match records."""

from __future__ import annotations

import argparse
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import requests

from account_storage import raw_dir as account_raw_dir
from auth_registry import get_account_auth
from miniapp_storage import has_required_fields
from secure_store import APP_DIR


ACTIVITY_ID = "21_3LXYAj"
ACTIVITY_URL = f"https://comm.ams.game.qq.com/ide/page/{ACTIVITY_ID}"
MATCH_LIST_TOKEN = "PHq59Y"
MATCH_DETAIL_TOKEN = "ylP3eG"
MATCH_COMMENT_TOKEN = "8NBbh4"
CENTER_TOKEN = "NoOapI"
MATCH_ITEM = "0,0,0,2201,0,0,0,75"
MATCH_PAGE_RETRY_DELAYS = (2.0, 5.0, 10.0)
MATCH_LIST_RETRY_BUDGET_SECONDS = 20.0
AMS_BUSY_MESSAGE_PARTS = (
    "访问人数太多",
    "系统繁忙",
    "服务繁忙",
    "请求过于频繁",
)
AUTH_REJECTED_MESSAGE_PARTS = (
    "请先登录",
    "尚未登录",
    "登录已失效",
    "登录态已失效",
    "登录已过期",
    "登录态已过期",
)
BUSY_AMS_CODES = {"-108"}
BUSY_HTTP_STATUSES = {429, 502, 503, 504}
RAW_DIR = APP_DIR / "cache" / "raw"


class AmsError(RuntimeError):
    def __init__(self, message: str, *, code: Any = None, payload: Any = None):
        super().__init__(message)
        self.code = code
        self.payload = payload


class SavedAuthError(ValueError):
    """The encrypted local credential exists but cannot authenticate an account."""


ErrorKind = Literal["expired", "busy", "network", "unknown"]


@dataclass(frozen=True)
class ErrorClassification:
    kind: ErrorKind
    retryable: bool
    auth_invalid: bool
    http_status: int | None = None


def _http_status(error: Exception) -> int | None:
    response = getattr(error, "response", None)
    if response is None:
        return None
    value = getattr(response, "status_code", None)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def classify_error(error: Exception) -> ErrorClassification:
    """Classify failures once so auth state and API responses cannot disagree."""
    if isinstance(error, SavedAuthError):
        return ErrorClassification("expired", retryable=False, auth_invalid=True)

    if isinstance(error, AmsError):
        message = str(error)
        if str(error.code) == "101" or any(
            part in message for part in AUTH_REJECTED_MESSAGE_PARTS
        ):
            return ErrorClassification("expired", retryable=False, auth_invalid=True)
        if str(error.code) in BUSY_AMS_CODES or any(
            part in message for part in AMS_BUSY_MESSAGE_PARTS
        ):
            return ErrorClassification("busy", retryable=True, auth_invalid=False)

    status = _http_status(error)
    if status in BUSY_HTTP_STATUSES:
        return ErrorClassification(
            "busy", retryable=True, auth_invalid=False, http_status=status
        )
    if isinstance(error, requests.RequestException):
        # An HTTP response is a remote failure, not a local connectivity failure.
        if getattr(error, "response", None) is not None:
            return ErrorClassification(
                "unknown", retryable=False, auth_invalid=False, http_status=status
            )
        return ErrorClassification("network", retryable=True, auth_invalid=False)
    return ErrorClassification("unknown", retryable=False, auth_invalid=False)


def is_auth_rejected_error(error: Exception) -> bool:
    return classify_error(error).kind == "expired" and isinstance(error, AmsError)


def is_ams_busy_error(error: Exception) -> bool:
    return classify_error(error).kind == "busy"


def load_auth(
    account_id: str | None = None, *, app_dir: Path = APP_DIR
) -> dict[str, Any]:
    try:
        auth = get_account_auth(account_id, app_dir=app_dir)
    except ValueError as exc:
        raise SavedAuthError(str(exc)) from exc
    if not has_required_fields(auth):
        raise SavedAuthError("保存的登录态不完整，请重新读取当前账号")
    return auth


def cookie_header(auth: dict[str, Any]) -> str:
    fields = (
        ("openid", "acctype", "appid", "access_token")
        if str(auth.get("acctype", "")).lower() == "qc"
        else (
            "openid",
            "acctype",
            "appid",
            "ieg_ams_session_token",
            "ieg_ams_token",
            "ieg_ams_token_time",
            "ieg_ams_token_v2",
            "unionid",
            "verifysession",
        )
    )
    return "; ".join(
        f"{key}={auth[key]}" for key in fields if auth.get(key) not in (None, "")
    ) + ";"


def decode_json_strings(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return decode_json_strings(json.loads(stripped), depth + 1)
            except json.JSONDecodeError:
                return value
        return value
    if isinstance(value, list):
        return [decode_json_strings(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {key: decode_json_strings(item, depth + 1) for key, item in value.items()}
    return value


class DeltaAmsClient:
    def __init__(self, auth: dict[str, Any] | None = None):
        self.auth = auth if auth is not None else load_auth()
        self.raw_dir = account_raw_dir(self.auth)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Cookie": cookie_header(self.auth),
                "User-Agent": "Mozilla/5.0 MicroMessenger MiniProgram DeltaForceIntelAssistant/1.0",
            }
        )
        self._activity: dict[str, Any] | None = None
        self._match_retry_deadline: float | None = None

    def activity(self) -> dict[str, Any]:
        if self._activity is None:
            response = self.session.get(ACTIVITY_URL, timeout=20)
            response.raise_for_status()
            payload = response.json()
            if payload.get("iRet") not in (None, 0, "0"):
                raise AmsError(payload.get("sMsg", "activity config failed"), payload=payload)
            self._activity = payload
        return self._activity

    def flow_config(self, token: str) -> tuple[str, dict[str, Any]]:
        activity = self.activity()
        chart_id = str(activity.get("tokens", {}).get(token, ""))
        if not chart_id:
            raise AmsError(f"flow token is absent from activity config: {token}")
        flow = activity.get("flows", {}).get(chart_id, {})
        host = flow.get("sIdeUrl") or activity.get("sIdeUrl") or "comm.ams.game.qq.com/ide/"
        endpoint = host if host.startswith("http") else f"https://{host}"
        return chart_id, {**flow, "endpoint": endpoint}

    def emit(self, token: str, data: dict[str, Any], *, save_as: str | None = None) -> Any:
        chart_id, flow = self.flow_config(token)
        form = {
            "iChartId": chart_id,
            "iSubChartId": chart_id,
            "sIdeToken": token,
            **data,
        }
        response = self.session.post(flow["endpoint"], data=form, timeout=30)
        response.raise_for_status()
        try:
            payload = decode_json_strings(response.json())
        except requests.JSONDecodeError as exc:
            raise AmsError("AMS returned non-JSON data", payload=response.text[:500]) from exc

        code = payload.get("iRet", payload.get("ret")) if isinstance(payload, dict) else None
        if code not in (None, 0, "0"):
            message = payload.get("sMsg", payload.get("msg", "AMS request failed"))
            raise AmsError(str(message), code=code, payload=payload)
        if save_as:
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            (self.raw_dir / save_as).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return payload

    def fetch_match_page(self, match_type: int, page: int = 1) -> Any:
        for attempt in range(len(MATCH_PAGE_RETRY_DELAYS) + 1):
            try:
                return self.emit(
                    MATCH_LIST_TOKEN,
                    {"type": match_type, "item": MATCH_ITEM, "page": page},
                    save_as=f"matches-{match_type}-{page}.json",
                )
            except (AmsError, requests.HTTPError) as exc:
                if not is_ams_busy_error(exc) or attempt == len(MATCH_PAGE_RETRY_DELAYS):
                    raise
                now = time.monotonic()
                if self._match_retry_deadline is None:
                    self._match_retry_deadline = now + MATCH_LIST_RETRY_BUDGET_SECONDS
                remaining = self._match_retry_deadline - now
                if remaining <= 0:
                    raise
                base_delay = MATCH_PAGE_RETRY_DELAYS[attempt]
                delay = min(random.uniform(base_delay * 0.8, base_delay * 1.2), remaining)
                time.sleep(delay)
        raise AssertionError("unreachable")

    def fetch_match_detail(self, room_id: str, *, gain_only: bool = False) -> Any:
        data: dict[str, Any] = {"roomId": room_id}
        if gain_only:
            data["typeId"] = 3
        return self.emit(
            MATCH_DETAIL_TOKEN,
            data,
            save_as=f"detail-{'gain-' if gain_only else ''}{quote(str(room_id), safe='')}.json",
        )

    def fetch_battlefield_detail(self, room_id: str) -> Any:
        parameter = json.dumps(
            {"roomID": room_id, "needUserDetail": True}, separators=(",", ":")
        )
        return self.emit(
            CENTER_TOKEN,
            {"method": "dfm/center.game.detail", "source": 2, "param": parameter},
            save_as=f"battlefield-detail-{quote(str(room_id), safe='')}.json",
        )

    def fetch_match_comments(self, room_id: str, event_time: str, match_type: str) -> Any:
        return self.emit(
            MATCH_COMMENT_TOKEN,
            {"roomid": room_id, "eventTime": event_time, "type": match_type},
            save_as=f"comments-{quote(str(room_id), safe='')}.json",
        )


def payload_shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: payload_shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return {"type": "list", "length": len(value), "item": payload_shape(value[0]) if value else None}
    return type(value).__name__


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("config", "matches"))
    parser.add_argument("--type", type=int, default=4, choices=(4, 5))
    parser.add_argument("--page", type=int, default=1)
    args = parser.parse_args()

    client = DeltaAmsClient()
    try:
        if args.action == "config":
            chart_id, flow = client.flow_config(MATCH_LIST_TOKEN)
            result = {"chart_id": chart_id, "token": MATCH_LIST_TOKEN, "endpoint": flow["endpoint"]}
        else:
            payload = client.fetch_match_page(args.type, args.page)
            result = {"ok": True, "shape": payload_shape(payload)}
    except AmsError as exc:
        result = {"ok": False, "code": exc.code, "message": str(exc)}
        print(json.dumps(result, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
