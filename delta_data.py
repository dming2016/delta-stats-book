#!/usr/bin/env python3
"""Normalize, cache, and filter Delta Force per-match data."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

from account_storage import (
    account_id,
    account_type,
    cache_file as account_cache_file,
    migrate_legacy_data,
    raw_dir as account_raw_dir,
)
from delta_api import DeltaAmsClient, load_auth
from secure_store import APP_DIR


CACHE_FILE = APP_DIR / "cache" / "matches.json"
TEAMMATE_STATS_VERSION = 2
MP_ASSIST_STATS_VERSION = 1
WARFARE_STATS_VERSION = 1
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
MAP_NAMES = {
    "1901": "长弓溪谷-常规",
    "1902": "长弓溪谷-机密",
    "2201": "零号大坝-常规",
    "2202": "零号大坝-机密",
    "2211": "零号大坝-常规",
    "2212": "零号大坝-机密",
    "2231": "零号大坝-前夜",
    "2232": "零号大坝-永夜",
    "2233": "零号大坝-终夜",
    "3901": "航天基地-机密",
    "3902": "航天基地-绝密",
    "8101": "巴克什-常规",
    "8102": "巴克什-机密",
    "8103": "巴克什-绝密",
    "8901": "AZ3-普通",
    "8902": "AZ3-机密",
    "33": "烬区-攻防",
    "107": "堑壕战-攻防",
    "111": "断轨-攻防",
    "113": "贯穿-攻防",
    "171": "克劳狄斗兽场-攻防",
    "311": "乌姆斯运河-攻防",
}
MAP_IMAGES = {
    "1901": "/assets/cgxg-changgui.png",
    "1902": "/assets/cgxg-jimi.png",
    "2201": "/assets/lhdb-changgui.png",
    "2202": "/assets/lhdb-jimi.png",
    "2211": "/assets/lhdb-changgui.png",
    "2212": "/assets/lhdb-jimi.png",
    "2231": "/assets/lhdb-qianye.png",
    "2232": "/assets/lhdb-yongye.png",
    "2233": "/assets/lhdb-zhongye.png",
    "3901": "/assets/htjd-jimi.png",
    "3902": "/assets/htjd-juemi.png",
    "8101": "/assets/bks-changgui.png",
    "8102": "/assets/bks-jimi.png",
    "8103": "/assets/bks-juemi.png",
    "8901": "/assets/az3-changgui.jpg",
    "8902": "/assets/az3-jimi.jpg",
    "33": "/assets/jq.png",
    "171": "/assets/klddsc.png",
}
WARFARE_RULESETS = {
    "占领",
    "攻防",
    "闪击",
    "围攻",
    "钢铁洪流",
    "夺旗",
    "攻防演练",
    "前夜",
    "永夜",
    "终夜",
    "夜战",
    "团队死斗",
    "龙舞惊雷",
    "游刃有余",
    "空地突袭",
    "胜者为王",
}
MAP_VARIANTS = {
    "常规",
    "普通",
    "机密",
    "绝密",
    *WARFARE_RULESETS,
}
OPERATOR_NAMES = {
    "40005": "露娜",
    "10010": "威龙",
    "40010": "骇爪",
    "20003": "蜂医",
    "30008": "牧羊人",
    "10007": "红狼",
    "30009": "乌鲁鲁",
    "20004": "蛊",
    "30010": "深蓝",
    "10011": "无名",
    "10012": "疾风",
    "40011": "银翼",
    "30011": "比特",
    "20005": "蝶",
    "40012": "回响",
    "30012": "液氮",
}
RETRY_BASE_MINUTES = 30
RETRY_MAX_MINUTES = 24 * 60


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text or None


def optional_bool(value: Any) -> bool | None:
    if value in (True, 1, "1", "true", "True"):
        return True
    if value in (False, 0, "0", "false", "False"):
        return False
    return None


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL_TZ)


def difficulty_for_map(map_name: str) -> str:
    if map_name.endswith("绝密"):
        return "top_secret"
    if map_name.endswith("机密"):
        return "confidential"
    if map_name.endswith(("常规", "普通")):
        return "regular"
    return "other"


def base_map_name(map_name: str) -> str:
    base, separator, variant = map_name.rpartition("-")
    return base if separator and variant in MAP_VARIANTS else map_name


def ruleset_for_map(map_name: str | None) -> str | None:
    if not map_name:
        return None
    _base, separator, variant = str(map_name).rpartition("-")
    return variant if separator and variant in WARFARE_RULESETS else None


def operator_name(operator: Any) -> str | None:
    operator_id = optional_str(operator)
    return OPERATOR_NAMES.get(operator_id) if operator_id else None


def side_label(side: Any) -> str | None:
    side_id = optional_int(side)
    if side_id == 1:
        return "进攻"
    if side_id == 2:
        return "防守"
    return None


def warfare_result(result_code: int | None) -> str:
    if result_code == 1:
        return "胜利"
    if result_code == 2:
        return "失败"
    if result_code is None:
        return "未知"
    return "中途退出"


def net_profit_from_detail(row: dict[str, Any]) -> int:
    final_price = as_int(row.get("FinalPrice"))
    key_out = as_int(row.get("KeyChainCarryOutPrice"))
    safe_box = as_int(row.get("CarryoutSafeBoxPrice"))
    key_in = as_int(row.get("KeyChainCarryInPrice"))
    self_in = as_int(row.get("CarryoutSelfPrice"))
    if final_price < key_out:
        return safe_box + key_out - key_in - self_in
    return final_price - key_in - self_in


def normalize_list_row(row: dict[str, Any], mode: str) -> dict[str, Any]:
    if mode == "sol":
        map_id = str(row.get("MapId"))
        room_id = str(row.get("RoomId", ""))
        event_time = str(row.get("dtEventTime", ""))
        result_code = as_int(row.get("EscapeFailReason"))
        result = "撤离成功" if result_code == 1 else "撤离失败"
        kills = as_int(row.get("KillCount")) + as_int(row.get("KillPlayerAICount"))
        deaths = 0 if result_code == 1 else 1
        ai_kills = as_int(row.get("KillAICount"))
        net_profit = as_int(row.get("flowCalGainedPrice"))
        duration = as_int(row.get("DurationS"))
        assists = None
        total_score = None
        rescues = None
        kill_player = None
        killed_by_player = None
        operator = None
        role_id = None
    else:
        map_id = optional_str(row.get("MapID"))
        room_id = optional_str(row.get("RoomId"))
        event_time = optional_str(row.get("dtEventTime"))
        result_code = optional_int(row.get("MatchResult"))
        result = warfare_result(result_code)
        kills = optional_int(row.get("KillNum"))
        deaths = optional_int(row.get("Death"))
        ai_kills = None
        net_profit = None
        duration = optional_int(row.get("gametime"))
        assists = optional_int(row.get("Assist"))
        total_score = optional_int(row.get("TotalScore"))
        rescues = optional_int(row.get("RescueTeammateCount"))
        kill_player = optional_int(row.get("KillPlayer"))
        killed_by_player = optional_int(row.get("KilledByPlayer"))
        operator = optional_str(row.get("ArmedForceId"))
        role_id = optional_str(row.get("RoleId"))

    map_name = MAP_NAMES.get(map_id, f"地图 {map_id}") if map_id else None
    ruleset = ruleset_for_map(map_name) if mode == "mp" else None
    return {
        "id": f"{mode}:{room_id or ''}:{event_time or ''}",
        "room_id": room_id,
        "mode": mode,
        "mode_label": "烽火地带" if mode == "sol" else "全面战场",
        "time": event_time,
        "timestamp": parse_time(event_time).isoformat() if event_time else None,
        "map_id": map_id,
        "map_name": map_name,
        "map_image": MAP_IMAGES.get(map_id),
        "difficulty": difficulty_for_map(map_name) if mode == "sol" else None,
        "ruleset": ruleset,
        "result": result,
        "result_code": result_code,
        "duration_seconds": duration,
        "game_duration": duration if mode == "mp" else None,
        "kills": kills,
        "deaths": deaths,
        "assists": assists,
        "total_score": total_score,
        "rescues": rescues,
        "rank_points": None,
        "side": None,
        "side_label": None,
        "operator": operator,
        "operator_name": operator_name(operator),
        "role_id": role_id,
        "kill_player": kill_player,
        "killed_by_player": killed_by_player,
        "ai_kills": ai_kills,
        "net_profit": net_profit,
        "gross_income": None,
        "income_loaded": False,
        "teammates": [],
        "warfare_players": [],
        "details_loaded": False,
        "teammate_stats_complete": False,
        "teammate_stats_version": 0,
        "assist_stats_version": 0,
        "warfare_stats_version": 0,
    }


def extract_payload_data(payload: dict[str, Any]) -> Any:
    if not isinstance(payload, dict):
        return None
    jdata = payload.get("jData")
    return jdata.get("data") if isinstance(jdata, dict) else None


def is_current_player(row: dict[str, Any]) -> bool:
    return row.get("vopenid") in (True, 1, "1", "true", "True")


def apply_sol_detail(match: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    match["assists"] = None
    rows = extract_payload_data(payload)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("match detail has an invalid player list")
    current = next((row for row in rows if is_current_player(row)), None)
    if current is None:
        raise ValueError("match detail does not identify the current player")
    team_id = str(current.get("TeamId"))
    teammates = []
    for row in rows:
        if team_id is not None and str(row.get("TeamId")) != team_id:
            continue
        if is_current_player(row):
            match["kills"] = as_int(row.get("KillCount")) + as_int(row.get("KillPlayerAICount"))
            match["ai_kills"] = as_int(row.get("KillAICount"))
            detail_profit = net_profit_from_detail(row)
            if not match["net_profit"]:
                match["net_profit"] = detail_profit
            continue
        name = unquote(str(row.get("nickName", ""))).strip()
        if name:
            result_code = as_int(row.get("EscapeFailReason"))
            teammates.append(
                {
                    "name": name,
                    "kills": as_int(row.get("KillCount")) + as_int(row.get("KillPlayerAICount")),
                    "assists": None,
                    "ai_kills": as_int(row.get("KillAICount")),
                    "deaths": 0 if result_code == 1 else 1,
                    "gross_income": as_int(row.get("FinalPrice")),
                    "net_profit": None,
                    "extracted": result_code == 1,
                }
            )
    match["teammates"] = teammates
    match["details_loaded"] = True
    match["teammate_stats_complete"] = True
    match["teammate_stats_version"] = TEAMMATE_STATS_VERSION
    return match


def enrich_sol(client: DeltaAmsClient, match: dict[str, Any]) -> dict[str, Any]:
    return apply_sol_detail(match, client.fetch_match_detail(match["room_id"]))


def normalize_warfare_player(row: dict[str, Any]) -> dict[str, Any]:
    raw_name = optional_str(row.get("nickName"))
    name = unquote(raw_name).strip() if raw_name else None
    is_current_user = optional_bool(row.get("isCurrentUser"))
    is_team_member = optional_bool(row.get("isTeamMember"))
    if is_current_user is True:
        relationship = "self"
    elif is_team_member is True:
        relationship = "teammate"
    elif is_team_member is False:
        relationship = "other"
    else:
        relationship = None
    map_id = optional_str(row.get("mapID"))
    map_name = MAP_NAMES.get(map_id, f"地图 {map_id}") if map_id else None
    result_code = optional_int(row.get("matchResult"))
    side = optional_int(row.get("color"))
    operator = optional_str(row.get("armedForceType"))
    return {
        "name": name or None,
        "is_current_user": is_current_user,
        "is_team_member": is_team_member,
        "relationship": relationship,
        "kills": optional_int(row.get("killNum")),
        "deaths": optional_int(row.get("death")),
        "assists": optional_int(row.get("assist")),
        "total_score": optional_int(row.get("totalScore")),
        "rescues": optional_int(row.get("rescueTeammateCount")),
        "rank_points": optional_int(row.get("rank")),
        "side": side,
        "side_label": side_label(side),
        "operator": operator,
        "operator_name": operator_name(operator),
        "game_duration": optional_int(row.get("gameTime")),
        "map_id": map_id,
        "map_name": map_name,
        "ruleset": ruleset_for_map(map_name),
        "result_code": result_code,
        "result": warfare_result(result_code),
        "start_time": optional_str(row.get("startTime")),
    }


def apply_mp_detail(match: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    center = extract_payload_data(payload)
    detail = center.get("data") if isinstance(center, dict) else None
    rows = detail.get("mpDetailList") if isinstance(detail, dict) else None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("battlefield detail has an invalid player list")
    players = [normalize_warfare_player(row) for row in rows]
    current = next(
        (player for player in players if player.get("is_current_user") is True),
        None,
    )
    if current is None:
        raise ValueError("battlefield detail does not identify the current player")

    shared_map_id = current.get("map_id") or optional_str(match.get("map_id"))
    shared_map_name = (
        MAP_NAMES.get(shared_map_id)
        or current.get("map_name")
        or optional_str(match.get("map_name"))
        if shared_map_id
        else current.get("map_name") or optional_str(match.get("map_name"))
    )
    shared_ruleset = ruleset_for_map(shared_map_name)
    for player in players:
        if player.get("map_id") is None:
            player["map_id"] = shared_map_id
        if player.get("map_name") is None:
            player["map_name"] = shared_map_name
        if player.get("ruleset") is None:
            player["ruleset"] = shared_ruleset

    for field in (
        "kills",
        "deaths",
        "assists",
        "total_score",
        "rescues",
        "rank_points",
        "side",
        "side_label",
        "operator",
        "operator_name",
        "game_duration",
        "ruleset",
    ):
        value = current.get(field)
        if value is not None or field not in match:
            match[field] = value
    if current.get("game_duration") is not None:
        match["duration_seconds"] = current["game_duration"]
    if not match.get("map_id") and current.get("map_id"):
        match["map_id"] = current["map_id"]
        match["map_name"] = current["map_name"]
        match["map_image"] = MAP_IMAGES.get(current["map_id"])
    if match.get("result_code") is None and current.get("result_code") is not None:
        match["result_code"] = current["result_code"]
        match["result"] = current["result"]

    match["warfare_players"] = players
    teammates = []
    for player in players:
        if player.get("is_current_user") is True:
            continue
        if player.get("is_team_member") is not True:
            continue
        name = optional_str(player.get("name"))
        if name:
            teammates.append(
                {
                    "name": name,
                    "kills": player.get("kills"),
                    "deaths": player.get("deaths"),
                    "assists": player.get("assists"),
                    "total_score": player.get("total_score"),
                    "rescues": player.get("rescues"),
                    "rank_points": player.get("rank_points"),
                    "side": player.get("side"),
                    "side_label": player.get("side_label"),
                    "operator": player.get("operator"),
                    "operator_name": player.get("operator_name"),
                    "game_duration": player.get("game_duration"),
                    "ruleset": player.get("ruleset"),
                    "map_id": player.get("map_id"),
                    "result_code": player.get("result_code"),
                    "start_time": player.get("start_time"),
                    "is_current_user": False,
                    "is_team_member": True,
                    "relationship": "teammate",
                    "ai_kills": None,
                    "gross_income": None,
                    "net_profit": None,
                    "extracted": None,
                }
            )
    match["teammates"] = teammates
    match["details_loaded"] = True
    match["teammate_stats_complete"] = True
    match["teammate_stats_version"] = TEAMMATE_STATS_VERSION
    team_players = [
        player for player in players if player.get("is_team_member") is True
    ]
    assists_complete = current.get("assists") is not None and all(
        player.get("assists") is not None for player in team_players
    )
    match["assist_stats_version"] = (
        MP_ASSIST_STATS_VERSION if assists_complete else 0
    )
    match["warfare_stats_version"] = WARFARE_STATS_VERSION
    return match


def enrich_mp(client: DeltaAmsClient, match: dict[str, Any]) -> dict[str, Any]:
    return apply_mp_detail(match, client.fetch_battlefield_detail(match["room_id"]))


def enrich_income(client: DeltaAmsClient, match: dict[str, Any]) -> dict[str, Any]:
    payload = client.fetch_match_detail(match["room_id"], gain_only=True)
    data = extract_payload_data(payload) or {}
    if not isinstance(data, dict) or "Gainedprice" not in data:
        raise ValueError("official gross income is absent from match detail")
    match["gross_income"] = as_int(data.get("Gainedprice"))
    match["income_loaded"] = True
    return match


def enrich_one(auth: dict[str, Any], activity: dict[str, Any], match: dict[str, Any]) -> dict[str, Any]:
    client = DeltaAmsClient(auth)
    client._activity = activity
    return enrich_sol(client, match) if match["mode"] == "sol" else enrich_mp(client, match)


def enrich_income_one(
    auth: dict[str, Any], activity: dict[str, Any], match: dict[str, Any]
) -> dict[str, Any]:
    client = DeltaAmsClient(auth)
    client._activity = activity
    return enrich_income(client, match)


def needs_detail_enrichment(
    match: dict[str, Any],
    threshold: datetime,
    assist_threshold: datetime | None = None,
) -> bool:
    timestamp = match.get("timestamp")
    if not timestamp:
        return False
    if not retry_due(match, "detail"):
        return False
    if as_int(match.get("teammate_stats_version")) < TEAMMATE_STATS_VERSION:
        return True
    if match.get("mode") == "mp" and match.get("deaths") is None:
        return True
    if (
        match.get("mode") == "mp"
        and as_int(match.get("warfare_stats_version")) < WARFARE_STATS_VERSION
    ):
        return datetime.fromisoformat(timestamp) >= (assist_threshold or threshold)
    if (
        match.get("mode") == "mp"
        and as_int(match.get("assist_stats_version")) < MP_ASSIST_STATS_VERSION
    ):
        return datetime.fromisoformat(timestamp) >= (assist_threshold or threshold)
    return not match.get("details_loaded") and datetime.fromisoformat(timestamp) >= threshold


def retry_due(match: dict[str, Any], kind: str, now: datetime | None = None) -> bool:
    retry_after = match.get(f"{kind}_retry_after")
    if not retry_after:
        return True
    try:
        return datetime.fromisoformat(str(retry_after)) <= (now or datetime.now(UTC))
    except ValueError:
        return True


def record_failure(match: dict[str, Any], kind: str, error: Exception) -> None:
    count = as_int(match.get(f"{kind}_failure_count")) + 1
    delay_minutes = min(RETRY_BASE_MINUTES * (2 ** min(count - 1, 8)), RETRY_MAX_MINUTES)
    match[f"{kind}_failure_count"] = count
    match[f"{kind}_retry_after"] = (datetime.now(UTC) + timedelta(minutes=delay_minutes)).isoformat()
    match[f"{kind}_last_error"] = str(error)[:500]


def clear_failure(match: dict[str, Any], kind: str) -> None:
    for suffix in ("failure_count", "retry_after", "last_error"):
        match.pop(f"{kind}_{suffix}", None)


def enrich_from_raw_cache(match: dict[str, Any], raw_path: Path) -> bool:
    room_id = quote(str(match.get("room_id", "")), safe="")
    if not room_id:
        return False
    prefix = "detail-" if match.get("mode") == "sol" else "battlefield-detail-"
    path = raw_path / f"{prefix}{room_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        code = payload.get("iRet", payload.get("ret")) if isinstance(payload, dict) else None
        if code not in (None, 0, "0"):
            return False
        if match.get("mode") == "sol":
            apply_sol_detail(match, payload)
        else:
            apply_mp_detail(match, payload)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False
    return True


def resolve_cache_file(
    auth: dict[str, Any] | None = None, *, app_dir: Path = APP_DIR
) -> Path:
    if auth is None:
        auth = load_auth(app_dir=app_dir)
    migrate_legacy_data(auth, app_dir=app_dir)
    return account_cache_file(auth, app_dir=app_dir)


def read_cache(
    auth: dict[str, Any] | None = None,
    *,
    app_dir: Path = APP_DIR,
    path: Path | None = None,
) -> dict[str, Any]:
    if path is None and auth is None:
        try:
            auth = load_auth(app_dir=app_dir)
        except Exception:
            return {"updated_at": None, "matches": []}
    cache_path = Path(path) if path is not None else resolve_cache_file(auth, app_dir=app_dir)
    if not cache_path.is_file():
        empty = {"updated_at": None, "matches": []}
        if auth is not None:
            empty["account_id"] = account_id(auth)
            empty["account_type"] = account_type(auth)
        return empty
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("matches"), list):
            raise ValueError("cache root is not a match collection")
        for index, match in enumerate(payload["matches"]):
            if not isinstance(match, dict):
                raise ValueError(f"cache match {index} is not an object")
            if not str(match.get("id", "")).strip():
                raise ValueError(f"cache match {index} has no id")
            if not str(match.get("time", "")).strip():
                raise ValueError(f"cache match {index} has no time")
            if match.get("mode") not in ("sol", "mp"):
                raise ValueError(f"cache match {index} has an invalid mode")
            parse_time(str(match["time"]))
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = cache_path.with_name(f"matches.corrupt-{stamp}.json")
        try:
            os.replace(cache_path, backup)
            location = f"，原文件已保留为 {backup.name}"
        except OSError:
            location = ""
        empty = {
            "updated_at": None,
            "matches": [],
            "warning": f"本地战绩缓存损坏，将在下次同步时重建{location}：{exc}",
        }
        if auth is not None:
            empty["account_id"] = account_id(auth)
            empty["account_type"] = account_type(auth)
        return empty
    if auth is not None:
        expected_id = account_id(auth)
        stored_id = str(payload.get("account_id", "")).strip()
        if stored_id and stored_id != expected_id:
            return {
                "updated_at": None,
                "matches": [],
                "warning": "本地战绩缓存属于其他账号，已停止读取",
            }
        if not stored_id:
            payload["account_id"] = expected_id
            payload["account_type"] = account_type(auth)
            _write_cache_file(cache_path, payload)
    raw_path = account_raw_dir(auth, app_dir=app_dir) if auth is not None else None
    for match in payload.get("matches", []):
        if match.get("time"):
            match["timestamp"] = parse_time(match["time"]).isoformat()
        map_id = optional_str(match.get("map_id"))
        match["map_id"] = map_id
        if map_id:
            match["map_name"] = (
                MAP_NAMES.get(map_id)
                or optional_str(match.get("map_name"))
                or f"地图 {map_id}"
            )
        else:
            match["map_name"] = optional_str(match.get("map_name"))
        match["map_image"] = MAP_IMAGES.get(map_id)
        match["difficulty"] = (
            difficulty_for_map(str(match.get("map_name", "")))
            if match.get("mode") == "sol"
            else None
        )
        if match.get("mode") == "mp":
            # These extraction-only values never belong to Warfare matches.  Old
            # caches used zeroes here, which made missing data look authoritative.
            match["ai_kills"] = None
            match["net_profit"] = None
            match["gross_income"] = None
            match["income_loaded"] = False
        else:
            match.setdefault("gross_income", None)
            match.setdefault("income_loaded", match.get("gross_income") is not None)
        match.setdefault("teammate_stats_complete", False)
        match.setdefault("teammate_stats_version", 0)
        match.setdefault("assists", None)
        match.setdefault("assist_stats_version", 0)
        if match.get("mode") == "mp":
            for field in (
                "result_code",
                "kills",
                "deaths",
                "assists",
                "total_score",
                "rescues",
                "rank_points",
                "side",
                "kill_player",
                "killed_by_player",
            ):
                match[field] = optional_int(match.get(field))
            match["result"] = warfare_result(match.get("result_code"))
            match["side_label"] = side_label(match.get("side"))
            match["operator"] = optional_str(match.get("operator"))
            match["operator_name"] = operator_name(match.get("operator"))
            match["role_id"] = optional_str(match.get("role_id"))
            game_duration = optional_int(match.get("game_duration"))
            duration_seconds = optional_int(match.get("duration_seconds"))
            match["game_duration"] = (
                game_duration if game_duration is not None else duration_seconds
            )
            match["duration_seconds"] = (
                duration_seconds if duration_seconds is not None else game_duration
            )
            match["ruleset"] = ruleset_for_map(match.get("map_name"))
            if not isinstance(match.get("warfare_players"), list):
                match["warfare_players"] = []
            match["warfare_stats_version"] = as_int(
                match.get("warfare_stats_version")
            )
        teammates = match.get("teammates")
        match["teammates"] = (
            [row for row in teammates if isinstance(row, dict)]
            if isinstance(teammates, list)
            else []
        )
        for teammate in match["teammates"]:
            if isinstance(teammate, dict):
                teammate.setdefault("assists", None)
                if match.get("mode") == "mp":
                    for field in (
                        "kills",
                        "deaths",
                        "assists",
                        "total_score",
                        "rescues",
                        "rank_points",
                        "side",
                        "game_duration",
                    ):
                        teammate[field] = optional_int(teammate.get(field))
                    teammate["operator"] = optional_str(teammate.get("operator"))
                    teammate["ruleset"] = (
                        optional_str(teammate.get("ruleset"))
                        or match.get("ruleset")
                    )
                    teammate["side_label"] = side_label(teammate.get("side"))
                    teammate["operator_name"] = operator_name(teammate.get("operator"))
                    teammate["is_current_user"] = False
                    teammate["is_team_member"] = True
                    teammate["relationship"] = "teammate"
                    teammate["ai_kills"] = None
                    teammate["gross_income"] = None
                    teammate["net_profit"] = None
                    teammate["extracted"] = None
        if match.get("mode") == "mp":
            match["warfare_players"] = [
                player
                for player in match["warfare_players"]
                if isinstance(player, dict)
            ]
            for player in match.get("warfare_players", []):
                player["name"] = optional_str(player.get("name"))
                for field in (
                    "kills",
                    "deaths",
                    "assists",
                    "total_score",
                    "rescues",
                    "rank_points",
                    "side",
                    "operator",
                    "game_duration",
                    "result_code",
                ):
                    player[field] = optional_int(player.get(field))
                player["is_current_user"] = optional_bool(
                    player.get("is_current_user")
                )
                player["is_team_member"] = optional_bool(
                    player.get("is_team_member")
                )
                player["side_label"] = side_label(player.get("side"))
                player["operator"] = optional_str(player.get("operator"))
                player["operator_name"] = operator_name(player.get("operator"))
                player_map_id = optional_str(player.get("map_id"))
                player["map_id"] = player_map_id
                player_map_name = (
                    MAP_NAMES.get(player_map_id)
                    or optional_str(player.get("map_name"))
                    or f"地图 {player_map_id}"
                    if player_map_id
                    else optional_str(player.get("map_name"))
                    or optional_str(match.get("map_name"))
                )
                player["map_name"] = player_map_name
                player["ruleset"] = ruleset_for_map(player_map_name)
                player["result"] = warfare_result(player.get("result_code"))
                player["start_time"] = optional_str(player.get("start_time"))
                if player.get("is_current_user") is True:
                    player["relationship"] = "self"
                elif player.get("is_team_member") is True:
                    player["relationship"] = "teammate"
                elif player.get("is_team_member") is False:
                    player["relationship"] = "other"
                elif player.get("relationship") not in ("self", "teammate", "other"):
                    player["relationship"] = None
        if "deaths" not in match:
            match["deaths"] = (
                0 if match.get("result_code") == 1 else 1
            ) if match.get("mode") == "sol" else None
        if (
            raw_path is not None
            and match.get("mode") == "mp"
            and (
                as_int(match.get("assist_stats_version")) < MP_ASSIST_STATS_VERSION
                or as_int(match.get("warfare_stats_version"))
                < WARFARE_STATS_VERSION
            )
        ):
            # Cache reads may race a background sync, so raw compatibility stays in memory.
            enrich_from_raw_cache(match, raw_path)
    return payload


def _write_cache_file(cache_path: Path, payload: dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, cache_path)
    finally:
        temporary.unlink(missing_ok=True)


def write_cache(
    payload: dict[str, Any],
    auth: dict[str, Any] | None = None,
    *,
    app_dir: Path = APP_DIR,
    path: Path | None = None,
) -> None:
    cache_path = Path(path) if path is not None else resolve_cache_file(auth, app_dir=app_dir)
    stored = dict(payload)
    if auth is not None:
        stored["account_id"] = account_id(auth)
        stored["account_type"] = account_type(auth)
    _write_cache_file(cache_path, stored)


def sync_matches(
    pages: int = 10,
    detail_days: int = 7,
    workers: int = 4,
    progress: Callable[[int, str], None] | None = None,
    auth: dict[str, Any] | None = None,
    assist_days: int = 7,
) -> dict[str, Any]:
    def report(percent: int, message: str) -> None:
        if progress:
            progress(max(0, min(100, percent)), message)

    report(2, "正在验证登录态")
    auth = dict(auth) if auth is not None else load_auth()
    migrate_legacy_data(auth)
    client = DeltaAmsClient(auth)
    activity = client.activity()
    cached = read_cache(auth)
    raw_path = account_raw_dir(auth)
    existing = {match["id"]: match for match in cached.get("matches", [])}
    fetched: dict[str, dict[str, Any]] = {
        match_id: dict(match) for match_id, match in existing.items()
    }

    page_steps = max(1, pages * 2)
    completed_pages = 0
    for match_type, mode in ((4, "sol"), (5, "mp")):
        previous_ids: set[str] | None = None
        for page in range(1, pages + 1):
            payload = client.fetch_match_page(match_type, page)
            completed_pages += 1
            report(5 + round(completed_pages / page_steps * 30), "正在读取最新逐局列表")
            rows = extract_payload_data(payload) or []
            current_ids = set()
            for row in rows:
                match = normalize_list_row(row, mode)
                current_ids.add(match["id"])
                if match["id"] in existing:
                    cached_match = existing[match["id"]]
                    match["teammates"] = cached_match.get("teammates", [])
                    match["details_loaded"] = cached_match.get("details_loaded", False)
                    match["teammate_stats_complete"] = cached_match.get(
                        "teammate_stats_complete", False
                    )
                    match["teammate_stats_version"] = cached_match.get(
                        "teammate_stats_version", 0
                    )
                    match["assist_stats_version"] = cached_match.get(
                        "assist_stats_version", 0
                    )
                    if mode == "mp":
                        for field in (
                            "kills",
                            "deaths",
                            "assists",
                            "total_score",
                            "rescues",
                            "rank_points",
                            "side",
                            "side_label",
                            "operator",
                            "operator_name",
                            "role_id",
                            "kill_player",
                            "killed_by_player",
                            "game_duration",
                            "duration_seconds",
                        ):
                            if (
                                match.get(field) is None
                                and cached_match.get(field) is not None
                            ):
                                match[field] = cached_match[field]
                        if (
                            match.get("result_code") is None
                            and cached_match.get("result_code") is not None
                        ):
                            match["result_code"] = cached_match["result_code"]
                            match["result"] = cached_match.get(
                                "result",
                                warfare_result(optional_int(match.get("result_code"))),
                            )
                        if not match.get("map_id") and cached_match.get("map_id"):
                            for field in ("map_id", "map_name", "map_image", "ruleset"):
                                match[field] = cached_match.get(field)
                        elif (
                            match.get("ruleset") is None
                            and optional_str(match.get("map_id"))
                            == optional_str(cached_match.get("map_id"))
                        ):
                            match["ruleset"] = cached_match.get("ruleset")
                        players = cached_match.get("warfare_players", [])
                        match["warfare_players"] = (
                            players if isinstance(players, list) else []
                        )
                        match["warfare_stats_version"] = as_int(
                            cached_match.get("warfare_stats_version")
                        )
                    if (
                        mode == "sol"
                        and match["net_profit"] == 0
                        and cached_match.get("details_loaded")
                        and cached_match.get("net_profit") not in (None, "")
                    ):
                        match["net_profit"] = as_int(cached_match["net_profit"])
                    if mode == "sol":
                        match["gross_income"] = cached_match.get("gross_income")
                        match["income_loaded"] = cached_match.get(
                            "income_loaded", match["gross_income"] is not None
                        )
                    for kind in ("detail", "income"):
                        for suffix in ("failure_count", "retry_after", "last_error"):
                            key = f"{kind}_{suffix}"
                            if key in cached_match:
                                match[key] = cached_match[key]
                fetched[match["id"]] = match
            if not rows or current_ids == previous_ids:
                break
            previous_ids = current_ids

    now = datetime.now(UTC)
    threshold = now - timedelta(days=detail_days)
    assist_threshold = now - timedelta(days=assist_days)
    for match in fetched.values():
        if (
            as_int(match.get("teammate_stats_version")) < TEAMMATE_STATS_VERSION
            or (
                match.get("mode") == "mp"
                and as_int(match.get("assist_stats_version")) < MP_ASSIST_STATS_VERSION
            )
            or (
                match.get("mode") == "mp"
                and as_int(match.get("warfare_stats_version"))
                < WARFARE_STATS_VERSION
            )
        ):
            enrich_from_raw_cache(match, raw_path)
    pending_details = [
        match
        for match in fetched.values()
        if needs_detail_enrichment(match, threshold, assist_threshold)
    ]
    failures = []
    detail_total = max(1, len(pending_details))
    detail_done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(enrich_one, auth, activity, dict(match)): match["id"]
            for match in pending_details
        }
        for future in as_completed(futures):
            match_id = futures[future]
            try:
                completed = future.result()
                clear_failure(completed, "detail")
                fetched[match_id] = completed
            except Exception as exc:  # Keep list data usable when one detail call fails.
                record_failure(fetched[match_id], "detail", exc)
                failures.append({"id": match_id, "kind": "detail", "error": str(exc)})
            detail_done += 1
            report(35 + round(detail_done / detail_total * 35), "正在补齐队友和击杀详情")

    pending_income = [
        match
        for match in fetched.values()
        if match["mode"] == "sol"
        and not match.get("income_loaded")
        and match.get("timestamp")
        and retry_due(match, "income")
        and datetime.fromisoformat(match["timestamp"]) >= threshold
    ]
    income_total = max(1, len(pending_income))
    income_done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(enrich_income_one, auth, activity, dict(match)): match["id"]
            for match in pending_income
        }
        for future in as_completed(futures):
            match_id = futures[future]
            try:
                completed = future.result()
                clear_failure(completed, "income")
                fetched[match_id] = completed
            except Exception as exc:
                record_failure(fetched[match_id], "income", exc)
                failures.append({"id": match_id, "kind": "income", "error": str(exc)})
            income_done += 1
            report(70 + round(income_done / income_total * 25), "正在补齐总带出价值")

    matches = sorted(fetched.values(), key=lambda match: match["time"], reverse=True)
    payload = {
        "account_id": account_id(auth),
        "account_type": account_type(auth),
        "updated_at": datetime.now(UTC).isoformat(),
        "pages": pages,
        "detail_days": detail_days,
        "assist_days": assist_days,
        "detail_failures": failures,
        "matches": matches,
    }
    report(98, "正在保存本地战绩")
    write_cache(payload, auth)
    report(100, "同步完成")
    return payload


def friend_options(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for match in matches:
        seen = set()
        for teammate in match.get("teammates", []):
            name = teammate.get("name", "").strip()
            if name and name not in seen:
                counts[name] = counts.get(name, 0) + 1
                seen.add(name)
    return [
        {"name": name, "matches": count}
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def map_options(
    matches: list[dict[str, Any]], mode: str | None = None
) -> list[str]:
    available = {
        base_map_name(str(match.get("map_name", "")))
        for match in matches
        if match.get("map_name") and (not mode or mode == "all" or match.get("mode") == mode)
    }
    known_order = list(dict.fromkeys(base_map_name(name) for name in MAP_NAMES.values()))
    return [name for name in known_order if name in available] + sorted(available - set(known_order))


def filter_matches(
    matches: list[dict[str, Any]],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    time_ranges: list[tuple[datetime, datetime]] | None = None,
    mode: str | None = None,
    friend: str | None = None,
    friends: list[str] | None = None,
    friend_mode: str = "any",
    difficulty: str | None = None,
    map_name: str | None = None,
) -> list[dict[str, Any]]:
    if friend_mode not in {"any", "all"}:
        raise ValueError("friend_mode must be 'any' or 'all'")
    if time_ranges == []:
        return []

    output = []
    selected_friends = [name for name in (friends or []) if name]
    if friend:
        selected_friends.append(friend)
    friend_folded = {name.casefold() for name in selected_friends}
    for match in matches:
        timestamp = datetime.fromisoformat(match["timestamp"])
        if start and timestamp < start:
            continue
        if end and timestamp > end:
            continue
        if time_ranges is not None and not any(
            range_start <= timestamp <= range_end
            for range_start, range_end in time_ranges
        ):
            continue
        if mode and mode != "all" and match["mode"] != mode:
            continue
        if difficulty and difficulty != "all" and match.get("difficulty") != difficulty:
            continue
        if map_name and map_name != "all" and base_map_name(str(match.get("map_name", ""))) != map_name:
            continue
        if friend_folded:
            teammate_folded = {
                teammate.get("name", "").casefold()
                for teammate in match.get("teammates", [])
            }
            if friend_mode == "any" and not teammate_folded & friend_folded:
                continue
            if friend_mode == "all" and not friend_folded <= teammate_folded:
                continue
        output.append(match)
    return output


def summary(matches: list[dict[str, Any]]) -> dict[str, Any]:
    profit_matches = [match for match in matches if match["mode"] == "sol"]
    mp_matches = [match for match in matches if match["mode"] == "mp"]
    assist_matches = [
        match for match in mp_matches if match.get("assists") is not None
    ]
    assists = sum(as_int(match.get("assists")) for match in assist_matches)
    total_profit = sum(as_int(match.get("net_profit")) for match in profit_matches)
    income_matches = [match for match in profit_matches if match.get("income_loaded")]
    kd_matches = [match for match in matches if match.get("deaths") is not None]
    kd_kills = sum(as_int(match.get("kills")) for match in kd_matches)
    deaths = sum(as_int(match.get("deaths")) for match in kd_matches)
    return {
        "matches": len(matches),
        "sol_matches": len(profit_matches),
        "mp_matches": len(mp_matches),
        "profit_matches": len(profit_matches),
        "net_profit": total_profit,
        "average_profit": round(total_profit / len(profit_matches)) if profit_matches else 0,
        "gross_income": sum(match.get("gross_income") or 0 for match in income_matches),
        "income_loaded": len(income_matches),
        "extractions": sum(match.get("result_code") == 1 for match in profit_matches),
        "kills": sum(as_int(match.get("kills")) for match in matches),
        "assists": assists,
        "assist_matches": len(assist_matches),
        "average_assists": (
            round(assists / len(assist_matches), 2) if assist_matches else None
        ),
        "ai_kills": sum(as_int(match.get("ai_kills")) for match in matches),
        "deaths": deaths,
        "kd_kills": kd_kills,
        "kd_matches": len(kd_matches),
        "average_kd": round(kd_kills / deaths, 2) if deaths else None,
    }


def warfare_summary(matches: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate Warfare-only values without treating missing fields as zero."""

    warfare_matches = [match for match in matches if match.get("mode") == "mp"]
    numeric_fields = (
        "total_score",
        "kills",
        "deaths",
        "assists",
        "rescues",
        "rank_points",
        "game_duration",
    )
    known_values: dict[str, list[int]] = {}
    for field in numeric_fields:
        values = []
        for match in warfare_matches:
            value = optional_int(match.get(field))
            if value is not None:
                values.append(value)
        known_values[field] = values

    coverage = {field: len(values) for field, values in known_values.items()}
    result_codes = [optional_int(match.get("result_code")) for match in warfare_matches]
    coverage["result_code"] = sum(code is not None for code in result_codes)
    coverage["warfare_players"] = sum(
        as_int(match.get("warfare_stats_version")) >= WARFARE_STATS_VERSION
        for match in warfare_matches
    )

    side_counts = {"attack": 0, "defense": 0, "unknown": 0}
    operator_counter: Counter[str] = Counter()
    ruleset_counter: Counter[str] = Counter()
    for match in warfare_matches:
        side = optional_int(match.get("side"))
        if side == 1:
            side_counts["attack"] += 1
        elif side == 2:
            side_counts["defense"] += 1
        else:
            side_counts["unknown"] += 1

        operator = optional_str(match.get("operator"))
        if operator:
            operator_counter[operator] += 1
        ruleset = optional_str(match.get("ruleset")) or ruleset_for_map(
            optional_str(match.get("map_name"))
        )
        if ruleset:
            ruleset_counter[ruleset] += 1

    coverage["side"] = side_counts["attack"] + side_counts["defense"]
    coverage["operator"] = sum(operator_counter.values())
    coverage["ruleset"] = sum(ruleset_counter.values())

    def total(field: str) -> int | None:
        values = known_values[field]
        return sum(values) if values else None

    def average(field: str) -> float | None:
        values = known_values[field]
        return round(sum(values) / len(values), 2) if values else None

    kd_rows = []
    kda_rows = []
    kill_duration_rows = []
    score_duration_rows = []
    for match in warfare_matches:
        kills = optional_int(match.get("kills"))
        deaths = optional_int(match.get("deaths"))
        assists = optional_int(match.get("assists"))
        score = optional_int(match.get("total_score"))
        duration = optional_int(match.get("game_duration"))
        if kills is not None and deaths is not None:
            kd_rows.append((kills, deaths))
        if kills is not None and deaths is not None and assists is not None:
            kda_rows.append((kills, deaths, assists))
        if kills is not None and duration is not None and duration > 0:
            kill_duration_rows.append((kills, duration))
        if score is not None and duration is not None and duration > 0:
            score_duration_rows.append((score, duration))

    kd_deaths = sum(deaths for _, deaths in kd_rows)
    kda_deaths = sum(deaths for _, deaths, _ in kda_rows)
    kd_numerator = sum(kills for kills, _ in kd_rows)
    kda_numerator = sum(kills + assists for kills, _, assists in kda_rows)
    kd = (
        round(kd_numerator / kd_deaths, 2)
        if kd_rows and kd_deaths
        else None
    )
    kda = (
        round(kda_numerator / kda_deaths, 2)
        if kda_rows and kda_deaths
        else None
    )
    kd_infinite = bool(kd_rows and kd_deaths == 0 and kd_numerator > 0)
    kda_infinite = bool(kda_rows and kda_deaths == 0 and kda_numerator > 0)

    kill_duration = sum(duration for _, duration in kill_duration_rows)
    score_duration = sum(duration for _, duration in score_duration_rows)
    kills_per_minute = (
        round(sum(kills for kills, _ in kill_duration_rows) * 60 / kill_duration, 2)
        if kill_duration_rows and kill_duration
        else None
    )
    score_per_minute = (
        round(sum(score for score, _ in score_duration_rows) * 60 / score_duration, 2)
        if score_duration_rows and score_duration
        else None
    )

    rank_rows = []
    for match in warfare_matches:
        points = optional_int(match.get("rank_points"))
        time = optional_str(match.get("timestamp")) or optional_str(match.get("time"))
        if points is not None and time:
            rank_rows.append((time, points))
    rank_rows.sort(key=lambda row: row[0])
    rank_points_earliest = rank_rows[0][1] if rank_rows else None
    rank_points_latest = rank_rows[-1][1] if rank_rows else None
    rank_points_change = (
        rank_points_latest - rank_points_earliest
        if len(rank_rows) >= 2
        else None
    )

    wins = sum(code == 1 for code in result_codes)
    losses = sum(code == 2 for code in result_codes)
    known_results = wins + losses
    game_duration = total("game_duration")
    output = {
        "matches": len(warfare_matches),
        "wins": wins,
        "losses": losses,
        "unknown_results": len(warfare_matches) - known_results,
        "known_results": known_results,
        "win_rate": round(wins * 100 / known_results, 2) if known_results else None,
        "total_score": total("total_score"),
        "average_score": average("total_score"),
        "kills": total("kills"),
        "average_kills": average("kills"),
        "deaths": total("deaths"),
        "average_deaths": average("deaths"),
        "assists": total("assists"),
        "average_assists": average("assists"),
        "rescues": total("rescues"),
        "average_rescues": average("rescues"),
        "kd": kd,
        "kda": kda,
        "kd_infinite": kd_infinite,
        "kda_infinite": kda_infinite,
        "game_duration": game_duration,
        "average_game_duration": average("game_duration"),
        "kills_per_minute": kills_per_minute,
        "score_per_minute": score_per_minute,
        "rank_points_latest": rank_points_latest,
        "rank_points_earliest": rank_points_earliest,
        "rank_points_change": rank_points_change,
        "rank_points_trend": [
            {"time": time, "rank_points": points} for time, points in rank_rows
        ],
        "side_counts": side_counts,
        "operator_counts": [
            {
                "operator": operator,
                "operator_name": operator_name(operator),
                "matches": count,
            }
            for operator, count in sorted(
                operator_counter.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "ruleset_counts": [
            {"ruleset": ruleset, "matches": count}
            for ruleset, count in sorted(
                ruleset_counter.items(), key=lambda item: (-item[1], item[0])
            )
        ],
        "coverage": coverage,
    }

    # Flat coverage aliases keep consumers simple while ``coverage`` remains the
    # canonical per-field completeness map.
    output.update(
        {
            "score_matches": coverage["total_score"],
            "kill_matches": coverage["kills"],
            "death_matches": coverage["deaths"],
            "assist_matches": coverage["assists"],
            "rescue_matches": coverage["rescues"],
            "duration_matches": coverage["game_duration"],
            "rank_matches": coverage["rank_points"],
            "kd_matches": len(kd_rows),
            "kda_matches": len(kda_rows),
            "total_duration_seconds": game_duration,
            "rank_points": rank_points_latest,
            "rank_change": rank_points_change,
        }
    )
    return output


def warfare_friend_comparisons(
    matches: list[dict[str, Any]], friends: list[str]
) -> list[dict[str, Any]]:
    comparisons = []
    for friend in dict.fromkeys(name for name in friends if optional_str(name)):
        target = friend.casefold()
        shared_matches = []
        friend_matches = []
        for match in matches:
            if match.get("mode") != "mp":
                continue
            players = match.get("warfare_players")
            players = players if isinstance(players, list) else []
            players_are_authoritative = (
                as_int(match.get("warfare_stats_version"))
                >= WARFARE_STATS_VERSION
                or bool(players)
            )
            candidates = players if players_are_authoritative else match.get("teammates", [])
            if not isinstance(candidates, list):
                candidates = []
            friend_row = None
            for row in candidates:
                if not isinstance(row, dict):
                    continue
                name = optional_str(row.get("name"))
                if not name or name.casefold() != target:
                    continue
                if players_are_authoritative and not (
                    row.get("relationship") == "teammate"
                    or optional_bool(row.get("is_team_member")) is True
                ):
                    continue
                friend_row = row
                break
            if friend_row is None:
                continue

            shared_matches.append(match)
            friend_match = {
                "mode": "mp",
                "time": match.get("time"),
                "timestamp": match.get("timestamp"),
                "map_id": match.get("map_id"),
                "map_name": match.get("map_name"),
                "ruleset": friend_row.get("ruleset") or match.get("ruleset"),
                "result_code": friend_row.get("result_code")
                if friend_row.get("result_code") is not None
                else match.get("result_code"),
                "game_duration": friend_row.get("game_duration")
                if friend_row.get("game_duration") is not None
                else match.get("game_duration"),
                "total_score": friend_row.get("total_score"),
                "kills": friend_row.get("kills"),
                "deaths": friend_row.get("deaths"),
                "assists": friend_row.get("assists"),
                "rescues": friend_row.get("rescues"),
                "rank_points": friend_row.get("rank_points"),
                "side": friend_row.get("side"),
                "operator": friend_row.get("operator"),
                "warfare_players": [],
                "warfare_stats_version": (
                    WARFARE_STATS_VERSION if players_are_authoritative else 0
                ),
            }
            friend_matches.append(friend_match)

        comparisons.append(
            {
                "name": friend,
                "shared_matches": len(shared_matches),
                "self": warfare_summary(shared_matches),
                "friend": warfare_summary(friend_matches),
            }
        )
    return comparisons


def teammate_summary(matches: list[dict[str, Any]], friend: str) -> dict[str, Any]:
    target = friend.casefold()
    teammate_matches = []
    for match in matches:
        teammate = next(
            (
                row
                for row in match.get("teammates", [])
                if str(row.get("name", "")).casefold() == target
            ),
            None,
        )
        if teammate is not None:
            teammate_matches.append((match, teammate))

    teammates = [teammate for _, teammate in teammate_matches]
    mp_rows = [
        teammate
        for match, teammate in teammate_matches
        if match.get("mode") == "mp"
    ]
    assist_rows = [row for row in mp_rows if row.get("assists") is not None]
    assists = sum(as_int(row.get("assists")) for row in assist_rows)

    sol_rows = [row for row in teammates if row.get("extracted") is not None]
    profit_rows = [row for row in teammates if row.get("net_profit") is not None]
    income_rows = [row for row in teammates if row.get("gross_income") is not None]
    kd_rows = [row for row in teammates if row.get("deaths") is not None]
    total_profit = sum(as_int(row.get("net_profit")) for row in profit_rows) if profit_rows else None
    kills = sum(as_int(row.get("kills")) for row in teammates)
    kd_kills = sum(as_int(row.get("kills")) for row in kd_rows)
    deaths = sum(as_int(row.get("deaths")) for row in kd_rows)
    return {
        "matches": len(teammates),
        "sol_matches": len(sol_rows),
        "mp_matches": len(mp_rows),
        "profit_matches": len(profit_rows),
        "net_profit": total_profit,
        "average_profit": round(total_profit / len(profit_rows)) if profit_rows else None,
        "gross_income": sum(as_int(row.get("gross_income")) for row in income_rows),
        "income_loaded": len(income_rows),
        "extractions": sum(row.get("extracted") is True for row in sol_rows),
        "kills": kills,
        "assists": assists,
        "assist_matches": len(assist_rows),
        "average_assists": (
            round(assists / len(assist_rows), 2) if assist_rows else None
        ),
        "ai_kills": sum(as_int(row.get("ai_kills")) for row in teammates),
        "deaths": deaths,
        "kd_kills": kd_kills,
        "kd_matches": len(kd_rows),
        "average_kd": round(kd_kills / deaths, 2) if deaths else None,
    }


def friend_comparisons(
    matches: list[dict[str, Any]], friends: list[str]
) -> list[dict[str, Any]]:
    comparisons = []
    for friend in dict.fromkeys(name for name in friends if name):
        shared_matches = filter_matches(matches, friend=friend)
        comparisons.append(
            {
                "name": friend,
                "self": summary(shared_matches),
                "friend": teammate_summary(shared_matches, friend),
            }
        )
    return comparisons


def detect_sessions(
    matches: list[dict[str, Any]], gap_minutes: int = 60
) -> list[dict[str, Any]]:
    ordered = sorted(
        (match for match in matches if match.get("timestamp")),
        key=lambda match: match["timestamp"],
    )
    if not ordered:
        return []

    gap = timedelta(minutes=gap_minutes)
    sessions: list[dict[str, Any]] = []
    start = datetime.fromisoformat(ordered[0]["timestamp"])
    end = start + timedelta(seconds=as_int(ordered[0].get("duration_seconds")))
    count = 1
    for match in ordered[1:]:
        match_start = datetime.fromisoformat(match["timestamp"])
        match_end = match_start + timedelta(seconds=as_int(match.get("duration_seconds")))
        if match_start - end > gap:
            sessions.append(
                {
                    "id": start.isoformat(),
                    "from": start.isoformat(),
                    "to": end.isoformat(),
                    "matches": count,
                }
            )
            start, end, count = match_start, match_end, 1
            continue
        end = max(end, match_end)
        count += 1
    sessions.append(
        {
            "id": start.isoformat(),
            "from": start.isoformat(),
            "to": end.isoformat(),
            "matches": count,
        }
    )
    sessions.reverse()
    return sessions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("sync", "status"))
    parser.add_argument("--pages", type=int, default=10)
    parser.add_argument("--detail-days", type=int, default=7)
    args = parser.parse_args()

    payload = sync_matches(args.pages, args.detail_days) if args.action == "sync" else read_cache()
    matches = payload.get("matches", [])
    print(
        json.dumps(
            {
                "updated_at": payload.get("updated_at"),
                "matches": len(matches),
                "details_loaded": sum(bool(match.get("details_loaded")) for match in matches),
                "income_loaded": sum(bool(match.get("income_loaded")) for match in matches),
                "friends": len(friend_options(matches)),
                "detail_failures": len(payload.get("detail_failures", [])),
                "summary": summary(matches),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
