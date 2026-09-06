#!/usr/bin/env python3
"""Private multi-player server for uploaded Delta Force match records."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
PLAYER_ID_RE = re.compile(r"^[a-f0-9]{24}$")
MAP_VARIANTS = {"常规", "普通", "机密", "绝密", "前夜", "永夜", "终夜", "攻防"}
LOCAL_TZ = timezone(timedelta(hours=8))
LEGACY_DOWNLOAD_ARCHIVE = "DeltaStatsAssistant.zip"
LEGACY_INSTALLER_BINARY = "DeltaStatsAssistant-Setup.exe"
LEGACY_LAUNCHER_BINARY = "DeltaStatsLauncher.exe"
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
SAFE_RELEASE_BASENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
APP_PACKAGE_FORMAT = "pyinstaller-onedir-zip-v1"


def base_map_name(map_name: str) -> str:
    base, separator, variant = map_name.rpartition("-")
    return base if separator and variant in MAP_VARIANTS else map_name


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=LOCAL_TZ)


def query_value(
    query: dict[str, list[str]], name: str, default: str = "all"
) -> str:
    values = query.get(name, [default])
    value = values[0] if values else default
    return value.strip() if isinstance(value, str) and value.strip() else default


def query_friend_mode(query: dict[str, list[str]]) -> str:
    friend_mode = query_value(query, "friend_mode", "any")
    if friend_mode not in {"any", "all"}:
        raise ValueError("friend_mode must be 'any' or 'all'")
    return friend_mode


def filter_matches(
    matches: list[dict[str, Any]],
    query: dict[str, list[str]],
    *,
    time_ranges: list[tuple[datetime, datetime]] | None = None,
) -> list[dict[str, Any]]:
    start = parse_datetime(query.get("from", [None])[0])
    end = parse_datetime(query.get("to", [None])[0])
    mode = query_value(query, "mode")
    difficulty = query_value(query, "difficulty")
    map_name = query_value(query, "map")
    friends = {name.casefold() for name in query.get("friend", []) if name.strip()}
    friend_mode = query_friend_mode(query)
    if time_ranges == []:
        return []

    output = []
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
        if mode != "all" and match.get("mode") != mode:
            continue
        if difficulty != "all" and match.get("difficulty") != difficulty:
            continue
        if map_name != "all" and base_map_name(str(match.get("map_name", ""))) != map_name:
            continue
        if friends:
            teammate_names = {
                str(teammate.get("name", "")).strip().casefold()
                for teammate in match.get("teammates", [])
                if isinstance(teammate, dict)
            }
            if friend_mode == "any" and not teammate_names & friends:
                continue
            if friend_mode == "all" and not friends <= teammate_names:
                continue
        output.append(match)
    return output


def summary(matches: list[dict[str, Any]]) -> dict[str, Any]:
    sol = [match for match in matches if match.get("mode") == "sol"]
    mp = [match for match in matches if match.get("mode") == "mp"]
    assist_matches = [match for match in mp if match.get("assists") is not None]
    assists = sum(int(match.get("assists") or 0) for match in assist_matches)
    income = [match for match in sol if match.get("income_loaded")]
    net_profit = sum(int(match.get("net_profit", 0)) for match in sol)
    return {
        "matches": len(matches),
        "sol_matches": len(sol),
        "mp_matches": len(mp),
        "net_profit": net_profit,
        "average_profit": round(net_profit / len(sol)) if sol else 0,
        "gross_income": sum(int(match.get("gross_income") or 0) for match in income),
        "income_loaded": len(income),
        "extractions": sum(match.get("result_code") == 1 for match in sol),
        "kills": sum(int(match.get("kills", 0)) for match in matches),
        "assists": assists,
        "assist_matches": len(assist_matches),
        "average_assists": (
            round(assists / len(assist_matches), 2) if assist_matches else None
        ),
        "ai_kills": sum(int(match.get("ai_kills", 0)) for match in matches),
    }


def friend_options(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for match in matches:
        seen = set()
        for teammate in match.get("teammates", []):
            name = str(teammate.get("name", "")).strip()
            if name and name not in seen:
                counts[name] = counts.get(name, 0) + 1
                seen.add(name)
    return [
        {"name": name, "matches": count}
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def map_options(matches: list[dict[str, Any]], mode: str) -> list[str]:
    return sorted(
        {
            base_map_name(str(match.get("map_name", "")))
            for match in matches
            if match.get("map_name") and (mode == "all" or match.get("mode") == mode)
        }
    )


def detect_sessions(matches: list[dict[str, Any]], gap_minutes: int = 60) -> list[dict[str, Any]]:
    ordered = sorted(matches, key=lambda match: match.get("timestamp", ""))
    sessions: list[dict[str, Any]] = []
    for match in ordered:
        if not match.get("timestamp"):
            continue
        start = datetime.fromisoformat(match["timestamp"])
        end = start + timedelta(seconds=int(match.get("duration_seconds", 0)))
        if not sessions or start - datetime.fromisoformat(sessions[-1]["to"]) > timedelta(minutes=gap_minutes):
            sessions.append(
                {
                    "id": start.isoformat(),
                    "from": start.isoformat(),
                    "to": end.isoformat(),
                    "matches": 1,
                }
            )
        else:
            sessions[-1]["to"] = max(end, datetime.fromisoformat(sessions[-1]["to"])).isoformat()
            sessions[-1]["matches"] += 1
    return list(reversed(sessions))


def selected_session_ranges(
    query: dict[str, list[str]], sessions: list[dict[str, Any]]
) -> tuple[list[str], list[tuple[datetime, datetime]] | None]:
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


def session_options(
    all_matches: list[dict[str, Any]], selected_matches: list[dict[str, Any]], friends: list[str]
) -> list[dict[str, Any]]:
    sessions = detect_sessions(all_matches)
    if not friends:
        return sessions
    selected_timestamps = [
        datetime.fromisoformat(match["timestamp"])
        for match in selected_matches
        if match.get("timestamp")
    ]
    output = []
    for session in sessions:
        start = datetime.fromisoformat(session["from"])
        end = datetime.fromisoformat(session["to"])
        output.append(
            {
                **session,
                "friend_matches": sum(
                    start <= timestamp <= end for timestamp in selected_timestamps
                ),
            }
        )
    return output


def safe_release_basename(value: Any, suffix: str) -> str:
    if not isinstance(value, str) or not SAFE_RELEASE_BASENAME_RE.fullmatch(value):
        raise ValueError("invalid release filename")
    if value in {".", ".."} or not value.lower().endswith(suffix):
        raise ValueError("invalid release filename")
    return value


def load_release_files(release_dir: Path) -> dict[str, str]:
    manifest = json.loads((release_dir / "version.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("invalid release manifest")

    legacy_manifest = "launcher_file" not in manifest and "download_file" not in manifest
    defaults = {
        "launcher_file": LEGACY_LAUNCHER_BINARY,
        "download_file": LEGACY_DOWNLOAD_ARCHIVE,
    }
    fields = {
        "file": ".exe",
        "launcher_file": ".exe",
        "download_file": ".zip",
    }
    files = {}
    for field, suffix in fields.items():
        value = manifest.get(field, defaults.get(field) if legacy_manifest else None)
        files[field] = safe_release_basename(value, suffix)
    package_file = manifest.get("package_file")
    if package_file is not None:
        if manifest.get("package_format") != APP_PACKAGE_FORMAT:
            raise ValueError("invalid app package format")
        files["package_file"] = safe_release_basename(package_file, ".zip")
    installer_file = manifest.get("installer_file")
    if installer_file is not None:
        files["installer_file"] = safe_release_basename(installer_file, ".exe")
    return files


class SharedStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(self.connect()) as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS players (
                    player_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS matches (
                    player_id TEXT NOT NULL,
                    match_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (player_id, match_id)
                );
                CREATE TABLE IF NOT EXISTS preferences (
                    player_id TEXT PRIMARY KEY,
                    favorite_friends TEXT NOT NULL DEFAULT '[]'
                );
                """
            )
            db.commit()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def upload(self, payload: dict[str, Any]) -> dict[str, Any]:
        player_id = str(payload.get("player_id", ""))
        display_name = str(payload.get("display_name", "")).strip()
        matches = payload.get("matches", [])
        if not PLAYER_ID_RE.fullmatch(player_id):
            raise ValueError("invalid player_id")
        if not display_name or len(display_name) > 32:
            raise ValueError("invalid display_name")
        if not isinstance(matches, list) or len(matches) > 5000:
            raise ValueError("invalid matches")
        rows = []
        for match in matches:
            if not isinstance(match, dict) or not match.get("id") or not match.get("timestamp"):
                raise ValueError("invalid match payload")
            rows.append(
                (player_id, str(match["id"]), str(match["timestamp"]), json.dumps(match, ensure_ascii=False, separators=(",", ":")))
            )
        with closing(self.connect()) as db:
            db.execute(
                "INSERT INTO players(player_id, display_name, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(player_id) DO UPDATE SET display_name=excluded.display_name, updated_at=excluded.updated_at",
                (player_id, display_name, payload.get("updated_at")),
            )
            db.executemany(
                "INSERT INTO matches(player_id, match_id, timestamp, payload) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(player_id, match_id) DO UPDATE SET timestamp=excluded.timestamp, payload=excluded.payload",
                rows,
            )
            db.commit()
        return {"ok": True, "player_id": player_id, "matches": len(rows)}

    def players(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT player_id AS id, display_name AS name, updated_at FROM players ORDER BY display_name"
            ).fetchall()
        return [dict(row) for row in rows]

    def matches(self, player_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute(
                "SELECT payload FROM matches WHERE player_id=? ORDER BY timestamp DESC", (player_id,)
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def favorites(self, player_id: str) -> list[str]:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT favorite_friends FROM preferences WHERE player_id=?", (player_id,)
            ).fetchone()
        return json.loads(row["favorite_friends"]) if row else []

    def save_favorites(self, player_id: str, favorites: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(name.strip() for name in favorites if isinstance(name, str) and name.strip()))
        with closing(self.connect()) as db:
            db.execute(
                "INSERT INTO preferences(player_id, favorite_friends) VALUES(?, ?) "
                "ON CONFLICT(player_id) DO UPDATE SET favorite_friends=excluded.favorite_friends",
                (player_id, json.dumps(cleaned, ensure_ascii=False)),
            )
            db.commit()
        return cleaned


def build_handler(store: SharedStore, upload_token: str, release_dir: Path | None = None):
    release_dir = release_dir or store.path.parent / "releases"

    class Handler(BaseHTTPRequestHandler):
        server_version = "DeltaForceShared/1.0"

        def log_message(self, format: str, *args) -> None:
            return

        def send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_file(
            self,
            path: Path,
            content_type: str,
            cache_control: str | None = None,
            download_name: str | None = None,
            head_only: bool = False,
        ) -> None:
            try:
                handle = path.open("rb")
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            with handle:
                size = os.fstat(handle.fileno()).st_size
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header(
                    "Cache-Control",
                    cache_control or ("no-store" if path.suffix == ".html" else "public, max-age=86400"),
                )
                if download_name:
                    self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
                self.send_header("Content-Length", str(size))
                self.end_headers()
                if not head_only:
                    while chunk := handle.read(1024 * 1024):
                        self.wfile.write(chunk)

        def redirect(self, location: str, head_only: bool = False) -> None:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def serve_release_request(self, request_path: str, head_only: bool = False) -> bool:
            if request_path == "/updates/version.json":
                self.send_file(
                    release_dir / "version.json",
                    "application/json; charset=utf-8",
                    "no-store",
                    head_only=head_only,
                )
                return True

            try:
                release_files = load_release_files(release_dir)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                release_files = None

            if request_path == f"/downloads/{LEGACY_DOWNLOAD_ARCHIVE}":
                if not release_files:
                    self.send_error(HTTPStatus.NOT_FOUND)
                elif release_files["download_file"] == LEGACY_DOWNLOAD_ARCHIVE:
                    self.send_file(
                        release_dir / LEGACY_DOWNLOAD_ARCHIVE,
                        "application/zip",
                        "no-store",
                        LEGACY_DOWNLOAD_ARCHIVE,
                        head_only,
                    )
                else:
                    self.redirect(f'/downloads/{release_files["download_file"]}', head_only)
                return True

            if request_path == f"/downloads/{LEGACY_INSTALLER_BINARY}":
                if not release_files or "installer_file" not in release_files:
                    self.send_error(HTTPStatus.NOT_FOUND)
                else:
                    self.redirect(f'/downloads/{release_files["installer_file"]}', head_only)
                return True

            if not release_files:
                return False
            release_routes = {
                f'/updates/{release_files["file"]}': (
                    release_files["file"],
                    "application/octet-stream",
                    None,
                ),
                f'/updates/{release_files["launcher_file"]}': (
                    release_files["launcher_file"],
                    "application/octet-stream",
                    None,
                ),
                f'/downloads/{release_files["download_file"]}': (
                    release_files["download_file"],
                    "application/zip",
                    release_files["download_file"],
                ),
            }
            installer_file = release_files.get("installer_file")
            if installer_file:
                release_routes[f"/downloads/{installer_file}"] = (
                    installer_file,
                    "application/octet-stream",
                    installer_file,
                )
            package_file = release_files.get("package_file")
            if package_file:
                release_routes[f"/updates/{package_file}"] = (
                    package_file,
                    "application/zip",
                    None,
                )
            route = release_routes.get(request_path)
            if not route:
                return False
            filename, content_type, download_name = route
            self.send_file(
                release_dir / filename,
                content_type,
                IMMUTABLE_CACHE_CONTROL,
                download_name,
                head_only,
            )
            return True

        def selected_player(self, query: dict[str, list[str]]) -> tuple[str | None, list[dict[str, Any]]]:
            players = store.players()
            requested = query.get("player", [None])[0]
            ids = {player["id"] for player in players}
            selected = requested if requested in ids else players[0]["id"] if players else None
            return selected, players

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if self.serve_release_request(parsed.path):
                return
            if parsed.path == "/api/matches":
                try:
                    player_id, players = self.selected_player(query)
                    all_matches = store.matches(player_id) if player_id else []
                    mode = query_value(query, "mode")
                    friend_mode = query_friend_mode(query)
                    mode_base = filter_matches(all_matches, {"mode": [mode]})
                    sessions = detect_sessions(mode_base)
                    accepted_session_ids, time_ranges = selected_session_ranges(
                        query, sessions
                    )
                    non_time_query = {
                        name: values
                        for name, values in query.items()
                        if name not in {"from", "to", "session"}
                    }
                    session_selected = filter_matches(mode_base, non_time_query)
                    selected = filter_matches(
                        all_matches,
                        query,
                        time_ranges=time_ranges,
                    )
                    updated_at = next(
                        (
                            player["updated_at"]
                            for player in players
                            if player["id"] == player_id
                        ),
                        None,
                    )
                    self.send_json(
                        {
                            "updated_at": updated_at,
                            "available": {
                                "first": all_matches[-1]["timestamp"] if all_matches else None,
                                "last": all_matches[0]["timestamp"] if all_matches else None,
                                "total": len(all_matches),
                            },
                            "filters": {
                                "player": player_id,
                                "mode": mode,
                                "friends": [
                                    name.strip()
                                    for name in query.get("friend", [])
                                    if name.strip()
                                ],
                                "friend_mode": friend_mode,
                                "sessions": accepted_session_ids,
                            },
                            "summary": summary(selected),
                            "maps": map_options(mode_base, mode),
                            "friends": friend_options(mode_base),
                            "favorite_friends": store.favorites(player_id)
                            if player_id
                            else [],
                            "sessions": session_options(
                                mode_base,
                                session_selected,
                                query.get("friend", []),
                            ),
                            "matches": selected,
                            "players": players,
                            "sync_enabled": False,
                        }
                    )
                except ValueError as exc:
                    self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if parsed.path in ("/", "/download", "/download/", "/index.html"):
                self.send_file(WEB_ROOT / "download.html", "text/html; charset=utf-8")
                return
            if parsed.path in ("/delta-stats-page.html", "/stats", "/stats/"):
                self.send_file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
                return
            if parsed.path.startswith("/assets/"):
                asset = WEB_ROOT / "assets" / Path(parsed.path).name
                self.send_file(asset, mimetypes.guess_type(asset.name)[0] or "application/octet-stream")
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_HEAD(self) -> None:
            if self.serve_release_request(urlparse(self.path).path, head_only=True):
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            if length > 20 * 1024 * 1024:
                self.send_json({"error": "payload too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                if parsed.path == "/api/upload":
                    if self.headers.get("Authorization") != f"Bearer {upload_token}":
                        self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                        return
                    self.send_json(store.upload(payload))
                    return
                if parsed.path == "/api/preferences":
                    query = parse_qs(parsed.query)
                    player_id, _ = self.selected_player(query)
                    if not player_id:
                        raise ValueError("player is required")
                    favorites = payload.get("favorite_friends", [])
                    if not isinstance(favorites, list):
                        raise ValueError("favorite_friends must be a list")
                    self.send_json({"favorite_friends": store.save_favorites(player_id, favorites)})
                    return
                self.send_error(HTTPStatus.NOT_FOUND)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3012)
    parser.add_argument("--data-dir", default=os.environ.get("DELTA_DATA_DIR", str(ROOT / "data")))
    args = parser.parse_args()
    upload_token = os.environ.get("DELTA_UPLOAD_TOKEN", "")
    if not upload_token:
        raise SystemExit("DELTA_UPLOAD_TOKEN is required")
    data_dir = Path(args.data_dir)
    store = SharedStore(data_dir / "shared.sqlite3")
    server = ThreadingHTTPServer((args.host, args.port), build_handler(store, upload_token, data_dir / "releases"))
    print(f"Delta Force shared server: http://{args.host}:{args.port}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
