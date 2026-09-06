"""Isolated UI preview: synthetic matches only, no credentials or Tencent calls."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app_version import APP_VERSION
from delta_data import (
    detect_sessions, filter_matches, friend_comparisons, friend_options,
    map_options, summary, warfare_summary,
)

def parse_datetime(value):
    return datetime.fromisoformat(value) if value else None


def demo_matches():
    rows = []
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    maps = [
        ("巴克什", "assets/bks-jimi.png", "confidential"),
        ("航天基地", "assets/htjd-juemi.png", "top_secret"),
        ("长弓溪谷", "assets/cgxg-jimi.png", "confidential"),
        ("零号大坝", "assets/lhdb-changgui.png", "regular"),
    ]
    profits = [1268000, -286000, 752400, 384500, -198000, 625000, 98000, 420600, -175000, 563200, 340000, 197000]
    for index, profit in enumerate(profits):
        stamp = today - timedelta(days=1 + index // 4) + timedelta(hours=22, minutes=-(index % 4) * 32)
        name, image, difficulty = maps[index % 4]
        row = {
            "id": f"demo-sol-{index}", "mode": "sol", "mode_label": "烽火",
            "timestamp": stamp.isoformat(), "time": stamp.strftime("%m-%d %H:%M"),
            "map_name": name, "map_image": image, "difficulty": difficulty,
            "result": "撤离成功" if profit > 0 else "撤离失败",
            "result_code": 1 if profit > 0 else 2,
            "duration_seconds": 1150 + index * 23, "kills": [4, 2, 3, 1][index % 4],
            "deaths": int(profit < 0), "ai_kills": 5 + index % 5,
            "net_profit": profit, "gross_income": profit + 350000 if profit > 0 else 0,
            "income_loaded": True,
            "teammates": [{
                "name": "队友 Alpha", "kills": 2 + index % 3, "deaths": int(profit < 0),
                "ai_kills": 4, "gross_income": 630000 if profit > 0 else 0,
                "net_profit": None, "result_code": 1 if profit > 0 else 2,
            }, {
                "name": "队友 Bravo", "kills": 1 + index % 2, "deaths": int(profit < 0),
                "ai_kills": 3, "gross_income": 420000 if profit > 0 else 0,
                "net_profit": None, "result_code": 1 if profit > 0 else 2,
            }],
        }
        rows.append(row)
    for index in range(8):
        stamp = today - timedelta(days=1 + index // 3) + timedelta(hours=17, minutes=-index % 3 * 30)
        rows.append({
            "id": f"demo-mp-{index}", "mode": "mp", "mode_label": "全面战场",
            "timestamp": stamp.isoformat(), "time": stamp.strftime("%m-%d %H:%M"),
            "map_name": "攀升" if index % 2 else "烬区",
            "map_image": "assets/klddsc.png" if index % 2 else "assets/jq.png",
            "result": "胜利" if index % 3 else "失败", "result_code": 1 if index % 3 else 2,
            "duration_seconds": 1380, "game_duration": 1380, "kills": 24 + index * 2,
            "deaths": 10 + index, "assists": 12, "rescues": 7,
            "total_score": 12650 + index * 550, "rank_points": 2320 - index * 25,
            "rank_delta": 25, "side": "attack", "side_label": "进攻",
            "operator": "蜂医", "operator_name": "蜂医", "ruleset": "攻防",
            "teammates": [], "warfare_players": [], "warfare_stats_version": 0,
        })
    return sorted(rows, key=lambda item: item["timestamp"], reverse=True)


MATCHES = demo_matches()
ACCOUNT_ID = "demo-account"


def account_payload():
    return {"active_account_id": ACCOUNT_ID, "accounts": [{
        "id": ACCOUNT_ID, "display_name": "演示账号", "account_type": "wechat",
        "auth_state": "missing", "has_credential": False, "can_sync": False,
        "cache": {"exists": True, "viewable": True, "matches": len(MATCHES)},
    }]}


def dataset(query):
    mode = query.get("mode", ["sol"])[0]
    base = [row for row in MATCHES if row["mode"] == mode]
    sessions = detect_sessions(base)
    selected_sessions = query.get("session", [])
    ranges = [(parse_datetime(s["from"]), parse_datetime(s["to"])) for s in sessions if s["id"] in selected_sessions]
    friends = query.get("friend", [])
    selected = filter_matches(
        base, mode=mode, difficulty=query.get("difficulty", ["all"])[0],
        map_name=query.get("map", ["all"])[0], friends=friends,
        friend_mode=query.get("friend_mode", ["any"])[0],
        start=parse_datetime(query.get("from", [None])[0]),
        end=parse_datetime(query.get("to", [None])[0]),
        time_ranges=ranges or None,
    )
    for session in sessions:
        rows = filter_matches(base, start=parse_datetime(session["from"]), end=parse_datetime(session["to"]))
        session["profit_matches"] = len(rows) if mode == "sol" else 0
        session["net_profit"] = sum(row["net_profit"] for row in rows) if mode == "sol" else None
        session["friend_matches"] = len(rows) if friends else None
    return {
        "account_id": ACCOUNT_ID, "matches": selected, "available": {"total": len(MATCHES)},
        "summary": summary(selected) if mode == "sol" else None,
        "warfare_summary": warfare_summary(selected) if mode == "mp" else None,
        "maps": map_options(base, mode), "friends": friend_options(base),
        "favorite_friends": ["队友 Alpha", "队友 Bravo"] if mode == "sol" else [],
        "friend_comparisons": friend_comparisons(selected, friends) if mode == "sol" else [],
        "sessions": sessions, "filters": {"mode": mode, "sessions": selected_sessions},
        "updated_at": MATCHES[0]["timestamp"], "players": [],
    }


class PreviewHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "web"), **kwargs)

    def log_message(self, *_args):
        pass

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/api/preview":
            return self.send_json({"synthetic_data_only": True, "writes_enabled": False})
        if parsed.path == "/api/accounts":
            return self.send_json(account_payload())
        if parsed.path == "/api/matches":
            return self.send_json(dataset(parse_qs(parsed.query)))
        if parsed.path == "/updates/version.json":
            return self.send_json({"version": APP_VERSION})
        if parsed.path.startswith("/api/"):
            return self.send_json({"error": "Isolated preview; operation unavailable"}, 405)
        if parsed.path in ("/", "/download", "/download/"):
            self.path = "/download.html"
        if parsed.path == "/delta-stats-page.html":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):
        self.send_json({"error": "Isolated preview; writes disabled"}, 405)

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

class PreviewServer(ThreadingHTTPServer):
    # Browser asset bursts exceed the standard library's five-connection queue.
    request_queue_size = 64
    daemon_threads = True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=4178)
    args = parser.parse_args()
    print(f"Isolated demo: http://127.0.0.1:{args.port}", flush=True)
    PreviewServer(("127.0.0.1", args.port), PreviewHandler).serve_forever()
