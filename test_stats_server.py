#!/usr/bin/env python3
"""HTTP contract tests for the local desktop service."""

from __future__ import annotations

import json
import time
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import requests

from account_storage import account_id
from auth_registry import safe_candidate_summary
from delta_api import AmsError, SavedAuthError
from friend_client import wait_until_ready
from shared_server import SharedStore, build_handler
from secure_store import InterProcessFileLock
from stats_server import (
    Handler,
    OperationBusyError,
    auth_probe_failure_message,
    create_server,
    dataset,
    open_wechat,
    read_preferences,
    session_options,
    sync_failure,
    write_preferences,
)


class DatasetViewContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sol_match = {
            "id": "sol:one",
            "timestamp": "2026-08-08T08:00:00+08:00",
            "time": "2026-08-08 08:00:00",
            "mode": "sol",
            "map_name": "零号大坝-机密",
            "difficulty": "confidential",
            "duration_seconds": 600,
            "result": "撤离成功",
            "result_code": 1,
            "kills": 3,
            "deaths": 0,
            "assists": None,
            "ai_kills": 2,
            "net_profit": 120000,
            "gross_income": 180000,
            "income_loaded": True,
            "teammates": [
                {
                    "name": "烽火好友",
                    "kills": 2,
                    "deaths": 0,
                    "ai_kills": 1,
                    "gross_income": 90000,
                    "net_profit": None,
                    "extracted": True,
                }
            ],
        }
        self.mp_win = {
            "id": "mp:win",
            "timestamp": "2026-08-08T12:00:00+08:00",
            "time": "2026-08-08 12:00:00",
            "mode": "mp",
            "map_name": "烬区-攻防",
            "difficulty": "other",
            "ruleset": "攻防",
            "duration_seconds": 900,
            "game_duration": 900,
            "result": "胜利",
            "result_code": 1,
            "side": 1,
            "side_label": "进攻方",
            "operator": "medic",
            "operator_name": "蜂医",
            "total_score": 1200,
            "kills": 10,
            "deaths": 2,
            "assists": 5,
            "rescues": 2,
            "rank_points": 5200,
            "ai_kills": 0,
            "net_profit": 0,
            "gross_income": None,
            "income_loaded": False,
            "teammates": [
                {
                    "name": "战场好友",
                    "total_score": 700,
                    "kills": 4,
                    "deaths": 2,
                    "assists": 3,
                    "rescues": 1,
                    "rank_points": 4800,
                    "side": 1,
                    "operator": "support",
                    "operator_name": "骇爪",
                    "game_duration": 900,
                }
            ],
            "warfare_players": [
                {
                    "name": "我",
                    "is_current_user": True,
                    "is_team_member": True,
                    "total_score": 1200,
                    "kills": 10,
                    "deaths": 2,
                    "assists": 5,
                    "rescues": 2,
                    "rank_points": 5200,
                    "side": 1,
                    "operator": "medic",
                    "operator_name": "蜂医",
                    "game_duration": 900,
                },
                {
                    "name": "战场好友",
                    "is_current_user": False,
                    "is_team_member": True,
                    "total_score": 700,
                    "kills": 4,
                    "deaths": 2,
                    "assists": 3,
                    "rescues": 1,
                    "rank_points": 4800,
                    "side": 1,
                    "operator": "support",
                    "operator_name": "骇爪",
                    "game_duration": 900,
                },
            ],
        }
        self.mp_loss = {
            "id": "mp:loss",
            "timestamp": "2026-08-08T12:25:00+08:00",
            "time": "2026-08-08 12:25:00",
            "mode": "mp",
            "map_name": "烬区-攻防",
            "difficulty": "other",
            "ruleset": "攻防",
            "duration_seconds": 780,
            "game_duration": 780,
            "result": "失败",
            "result_code": 2,
            "side": 2,
            "side_label": "防守方",
            "operator": "assault",
            "operator_name": "红狼",
            "total_score": 600,
            "kills": 4,
            "deaths": 5,
            "assists": 2,
            "rescues": 0,
            "rank_points": 5180,
            "ai_kills": 0,
            "net_profit": 0,
            "gross_income": None,
            "income_loaded": False,
            "teammates": [
                {
                    "name": "战场好友",
                    "total_score": 300,
                    "kills": 2,
                    "deaths": 4,
                    "assists": 1,
                    "rescues": 0,
                    "rank_points": 4780,
                    "side": 2,
                    "operator": "engineer",
                    "operator_name": "牧羊人",
                    "game_duration": 780,
                },
                {
                    "name": "另一战友",
                    "total_score": 250,
                    "kills": 1,
                    "deaths": 3,
                    "assists": 2,
                    "rescues": 1,
                    "rank_points": 4300,
                    "side": 2,
                    "operator": "recon",
                    "operator_name": "露娜",
                    "game_duration": 780,
                },
            ],
            "warfare_players": [
                {
                    "name": "我",
                    "is_current_user": True,
                    "is_team_member": True,
                    "total_score": 600,
                    "kills": 4,
                    "deaths": 5,
                    "assists": 2,
                    "rescues": 0,
                    "rank_points": 5180,
                    "side": 2,
                    "operator": "assault",
                    "operator_name": "红狼",
                    "game_duration": 780,
                },
                {
                    "name": "战场好友",
                    "is_current_user": False,
                    "is_team_member": True,
                    "total_score": 300,
                    "kills": 2,
                    "deaths": 4,
                    "assists": 1,
                    "rescues": 0,
                    "rank_points": 4780,
                    "side": 2,
                    "operator": "engineer",
                    "operator_name": "牧羊人",
                    "game_duration": 780,
                },
                {
                    "name": "另一战友",
                    "is_current_user": False,
                    "is_team_member": True,
                    "total_score": 250,
                    "kills": 1,
                    "deaths": 3,
                    "assists": 2,
                    "rescues": 1,
                    "rank_points": 4300,
                    "side": 2,
                    "operator": "recon",
                    "operator_name": "露娜",
                    "game_duration": 780,
                },
            ],
        }
        self.matches = [self.mp_loss, self.mp_win, self.sol_match]

    def load_dataset(self, query: dict[str, list[str]]) -> dict:
        with (
            patch("stats_server.resolve_account_identity", return_value=None),
            patch(
                "stats_server.read_cache",
                return_value={
                    "updated_at": "2026-08-08T13:00:00+08:00",
                    "matches": self.matches,
                },
            ),
            patch(
                "stats_server.read_preferences",
                return_value={
                    "favorite_friends": ["战场好友", "烽火好友", "不存在好友"]
                },
            ),
            patch("stats_server.registry_summary", return_value={"accounts": []}),
        ):
            return dataset(query)

    def test_default_view_is_firebreak_and_keeps_firebreak_summary(self) -> None:
        payload = self.load_dataset({})

        self.assertEqual(payload["view"], "firebreak")
        self.assertEqual([match["id"] for match in payload["matches"]], ["sol:one"])
        self.assertEqual(payload["summary"]["net_profit"], 120000)
        self.assertIsNone(payload["warfare_summary"])
        self.assertIsNone(payload["timeline_summary"])
        self.assertEqual(payload["maps"], ["零号大坝"])
        self.assertEqual(payload["friends"], [{"name": "烽火好友", "matches": 1}])
        self.assertEqual(payload["favorite_friends"], ["烽火好友"])
        self.assertEqual(len(payload["sessions"]), 1)

    def test_warfare_view_scopes_candidates_and_ignores_stale_difficulty(self) -> None:
        payload = self.load_dataset(
            {"mode": ["mp"], "difficulty": ["confidential"]}
        )

        self.assertEqual(payload["view"], "warfare")
        self.assertEqual(payload["filters"]["difficulty"], "all")
        self.assertIsNone(payload["summary"])
        self.assertIsNone(payload["timeline_summary"])
        self.assertEqual(payload["warfare_summary"]["matches"], 2)
        self.assertEqual(payload["warfare_summary"]["wins"], 1)
        self.assertEqual(
            [match["id"] for match in payload["matches"]], ["mp:loss", "mp:win"]
        )
        self.assertEqual(payload["maps"], ["烬区"])
        self.assertEqual(
            payload["friends"],
            [
                {"name": "战场好友", "matches": 2},
                {"name": "另一战友", "matches": 1},
            ],
        )
        self.assertEqual(payload["favorite_friends"], ["战场好友"])
        self.assertEqual(payload["rulesets"], ["攻防"])
        self.assertEqual(
            {item["value"] for item in payload["results"]}, {"win", "loss"}
        )
        self.assertEqual(
            {item["value"] for item in payload["sides"]}, {"1", "2"}
        )
        self.assertEqual(
            {item["value"] for item in payload["operators"]},
            {"medic", "assault"},
        )
        self.assertEqual(len(payload["sessions"]), 1)
        self.assertEqual(payload["sessions"][0]["matches"], 2)

    def test_warfare_view_applies_warfare_dimensions_after_mode_scope(self) -> None:
        payload = self.load_dataset(
            {
                "mode": ["mp"],
                "difficulty": ["top_secret"],
                "result": ["win"],
                "side": ["1"],
                "operator": ["medic"],
                "ruleset": ["攻防"],
                "friend": ["战场好友"],
            }
        )

        self.assertEqual([match["id"] for match in payload["matches"]], ["mp:win"])
        self.assertEqual(payload["warfare_summary"]["matches"], 1)
        self.assertEqual(payload["warfare_summary"]["wins"], 1)
        self.assertEqual(payload["filters"]["difficulty"], "all")
        self.assertEqual(payload["filters"]["result"], "win")
        self.assertEqual(payload["filters"]["side"], "1")
        self.assertEqual(payload["filters"]["operator"], "medic")
        self.assertEqual(payload["filters"]["ruleset"], "攻防")
        self.assertEqual(payload["sessions"][0]["matches"], 2)
        self.assertEqual(payload["sessions"][0]["friend_matches"], 1)

    def test_quit_filter_does_not_treat_an_unknown_result_as_quit(self) -> None:
        unknown = {
            **self.mp_win,
            "id": "mp:unknown",
            "timestamp": "2026-08-08T13:00:00+08:00",
            "result": "结果未知",
            "result_code": None,
            "teammates": [],
            "warfare_players": [],
        }
        quit_match = {
            **self.mp_win,
            "id": "mp:quit",
            "timestamp": "2026-08-08T13:20:00+08:00",
            "result": "中途退出",
            "result_code": 0,
            "side": None,
            "side_label": None,
            "operator": None,
            "operator_name": None,
            "teammates": [],
            "warfare_players": [],
        }
        self.matches = [quit_match, unknown, *self.matches]

        payload = self.load_dataset({"mode": ["mp"], "result": ["quit"]})

        self.assertEqual([match["id"] for match in payload["matches"]], ["mp:quit"])
        self.assertEqual(
            {item["value"] for item in payload["results"]},
            {"win", "loss", "quit"},
        )

    def test_timeline_view_does_not_return_a_mixed_business_summary(self) -> None:
        payload = self.load_dataset({"mode": ["all"]})

        self.assertEqual(payload["view"], "timeline")
        self.assertIsNone(payload["summary"])
        self.assertIsNone(payload["warfare_summary"])
        self.assertEqual(
            payload["timeline_summary"],
            {"matches": 3, "sol_matches": 1, "mp_matches": 2},
        )
        self.assertEqual(len(payload["matches"]), 3)
        self.assertEqual(payload["friend_comparisons"], [])

    def test_warfare_friend_comparison_uses_only_shared_warfare_matches(self) -> None:
        payload = self.load_dataset({"mode": ["mp"], "friend": ["战场好友"]})

        self.assertEqual(len(payload["matches"]), 2)
        self.assertEqual(len(payload["friend_comparisons"]), 1)
        comparison = payload["friend_comparisons"][0]
        self.assertEqual(comparison["name"], "战场好友")
        self.assertEqual(comparison["shared_matches"], 2)
        self.assertEqual(comparison["self"]["matches"], 2)
        self.assertEqual(comparison["self"]["total_score"], 1800)
        self.assertEqual(comparison["friend"]["matches"], 2)
        self.assertEqual(comparison["friend"]["total_score"], 1000)

    def test_friend_mode_all_requires_every_selected_friend(self) -> None:
        query = {
            "mode": ["mp"],
            "friend": ["战场好友", "另一战友"],
        }

        any_payload = self.load_dataset(query)

        self.assertEqual(
            [match["id"] for match in any_payload["matches"]],
            ["mp:loss", "mp:win"],
        )
        self.assertEqual(any_payload["filters"]["friend_mode"], "any")
        any_comparisons = {
            item["name"]: item for item in any_payload["friend_comparisons"]
        }
        self.assertEqual(any_comparisons["战场好友"]["shared_matches"], 2)
        self.assertEqual(any_comparisons["另一战友"]["shared_matches"], 1)
        self.assertEqual(any_payload["sessions"][0]["friend_matches"], 2)

        all_payload = self.load_dataset({**query, "friend_mode": ["all"]})

        self.assertEqual([match["id"] for match in all_payload["matches"]], ["mp:loss"])
        self.assertEqual(all_payload["filters"]["friend_mode"], "all")
        all_comparisons = {
            item["name"]: item for item in all_payload["friend_comparisons"]
        }
        self.assertEqual(all_comparisons["战场好友"]["shared_matches"], 1)
        self.assertEqual(all_comparisons["另一战友"]["shared_matches"], 1)
        self.assertEqual(all_payload["sessions"][0]["friend_matches"], 1)

    def test_session_ids_form_discrete_ranges_and_intersect_manual_time(self) -> None:
        middle_match = {
            **self.mp_win,
            "id": "mp:middle",
            "timestamp": "2026-08-08T10:00:00+08:00",
            "time": "2026-08-08 10:00:00",
        }
        self.matches = [self.mp_loss, middle_match, self.mp_win, self.sol_match]
        initial = self.load_dataset({"mode": ["all"]})
        session_ids = {
            session["from"]: session["id"] for session in initial["sessions"]
        }
        selected_ids = [
            session_ids["2026-08-08T08:00:00+08:00"],
            session_ids["2026-08-08T12:00:00+08:00"],
        ]

        payload = self.load_dataset({"mode": ["all"], "session": selected_ids})

        self.assertEqual(
            [match["id"] for match in payload["matches"]],
            ["mp:loss", "mp:win", "sol:one"],
        )
        self.assertEqual(payload["filters"]["sessions"], selected_ids)

        intersected = self.load_dataset(
            {
                "mode": ["all"],
                "session": selected_ids,
                "from": ["2026-08-08T11:00:00+08:00"],
            }
        )

        self.assertEqual(
            [match["id"] for match in intersected["matches"]],
            ["mp:loss", "mp:win"],
        )

    def test_invalid_session_id_is_an_explicit_empty_filter(self) -> None:
        payload = self.load_dataset(
            {"mode": ["mp"], "session": ["expired-session-id"]}
        )

        self.assertEqual(payload["matches"], [])
        self.assertEqual(payload["filters"]["sessions"], [])
        self.assertEqual(payload["warfare_summary"]["matches"], 0)

    def test_dataset_rejects_unknown_friend_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "friend_mode"):
            self.load_dataset({"friend_mode": ["either"]})


class SharedServerFilterContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        data_dir = Path(self.temporary_directory.name)
        self.store = SharedStore(data_dir / "shared.sqlite3")
        self.player_id = "a" * 24
        self.store.upload(
            {
                "player_id": self.player_id,
                "display_name": "Shared filter test",
                "updated_at": "2026-08-08T13:00:00+08:00",
                "matches": [
                    {
                        "id": "shared:one",
                        "timestamp": "2026-08-08T08:00:00+08:00",
                        "mode": "sol",
                        "map_name": "零号大坝-机密",
                        "difficulty": "confidential",
                        "duration_seconds": 600,
                        "teammates": [{"name": "A"}],
                    },
                    {
                        "id": "shared:middle",
                        "timestamp": "2026-08-08T10:00:00+08:00",
                        "mode": "sol",
                        "map_name": "零号大坝-机密",
                        "difficulty": "confidential",
                        "duration_seconds": 600,
                        "teammates": [{"name": "B"}],
                    },
                    {
                        "id": "shared:both",
                        "timestamp": "2026-08-08T12:00:00+08:00",
                        "mode": "sol",
                        "map_name": "零号大坝-机密",
                        "difficulty": "confidential",
                        "duration_seconds": 600,
                        "teammates": [{"name": "A"}, {"name": "B"}],
                    },
                ],
            }
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            build_handler(self.store, "test-upload-token", data_dir / "releases"),
        )
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        wait_until_ready(self.port)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def request_matches(
        self, parameters: list[tuple[str, str]]
    ) -> tuple[int, dict]:
        path = f"/api/matches?{urlencode(parameters)}" if parameters else "/api/matches"
        request = Request(f"http://127.0.0.1:{self.port}{path}")
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))
        with response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_shared_endpoint_uses_the_same_friend_and_session_contract(self) -> None:
        status, initial = self.request_matches([("mode", "sol")])

        self.assertEqual(status, 200)
        self.assertTrue(
            all(session["id"] == session["from"] for session in initial["sessions"])
        )
        session_ids = {
            session["from"]: session["id"] for session in initial["sessions"]
        }
        selected_ids = [
            session_ids["2026-08-08T08:00:00+08:00"],
            session_ids["2026-08-08T12:00:00+08:00"],
        ]

        status, any_payload = self.request_matches(
            [("mode", "sol"), ("friend", "A"), ("friend", "B")]
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            [match["id"] for match in any_payload["matches"]],
            ["shared:both", "shared:middle", "shared:one"],
        )
        self.assertEqual(any_payload["filters"]["friend_mode"], "any")
        self.assertEqual(
            [session["friend_matches"] for session in any_payload["sessions"]],
            [1, 1, 1],
        )

        status, all_payload = self.request_matches(
            [
                ("mode", "sol"),
                ("friend", "A"),
                ("friend", "B"),
                ("friend_mode", "all"),
            ]
        )

        self.assertEqual(status, 200)
        self.assertEqual([match["id"] for match in all_payload["matches"]], ["shared:both"])
        self.assertEqual(all_payload["filters"]["friend_mode"], "all")
        self.assertEqual(
            [session["friend_matches"] for session in all_payload["sessions"]],
            [1, 0, 0],
        )

        status, multi_session_payload = self.request_matches(
            [("mode", "sol"), *(('session', session_id) for session_id in selected_ids)]
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            [match["id"] for match in multi_session_payload["matches"]],
            ["shared:both", "shared:one"],
        )
        self.assertEqual(multi_session_payload["filters"]["sessions"], selected_ids)

        status, intersected_payload = self.request_matches(
            [
                ("mode", "sol"),
                *(('session', session_id) for session_id in selected_ids),
                ("from", "2026-08-08T11:00:00+08:00"),
            ]
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            [match["id"] for match in intersected_payload["matches"]],
            ["shared:both"],
        )

        status, invalid_session_payload = self.request_matches(
            [("mode", "sol"), ("session", "expired-session-id")]
        )

        self.assertEqual(status, 200)
        self.assertEqual(invalid_session_payload["matches"], [])
        self.assertEqual(invalid_session_payload["filters"]["sessions"], [])

        status, invalid_mode_payload = self.request_matches(
            [("friend_mode", "either")]
        )

        self.assertEqual(status, 400)
        self.assertIn("friend_mode", invalid_mode_payload["error"])


class StatsServerTests(unittest.TestCase):
    def test_friend_session_options_keep_all_sessions_and_add_count(self) -> None:
        matches = [
            {"timestamp": "2026-08-08T08:00:00+08:00", "duration_seconds": 600},
            {"timestamp": "2026-08-08T08:20:00+08:00", "duration_seconds": 600},
            {"timestamp": "2026-08-08T12:00:00+08:00", "duration_seconds": 600},
            {"timestamp": "2026-08-08T12:20:00+08:00", "duration_seconds": 600},
        ]
        options = session_options(matches, [matches[0], matches[1]], ["好友甲"])
        self.assertEqual(len(options), 2)
        self.assertEqual([option["matches"] for option in options], [2, 2])
        self.assertEqual([option["friend_matches"] for option in options], [0, 2])

    def test_session_options_remain_complete_without_friend_filter(self) -> None:
        matches = [
            {"timestamp": "2026-08-08T08:00:00+08:00", "duration_seconds": 600},
            {"timestamp": "2026-08-08T12:00:00+08:00", "duration_seconds": 600},
        ]
        options = session_options(matches, [], [])
        self.assertEqual(len(options), 2)
        self.assertTrue(all("friend_matches" not in option for option in options))

    def test_session_options_aggregate_sol_profit_and_mark_mp_unavailable(self) -> None:
        matches = [
            {
                "timestamp": "2026-08-08T08:00:00+08:00",
                "duration_seconds": 600,
                "mode": "sol",
                "net_profit": 120000,
            },
            {
                "timestamp": "2026-08-08T08:20:00+08:00",
                "duration_seconds": 600,
                "mode": "sol",
                "net_profit": 0,
            },
            {
                "timestamp": "2026-08-08T12:00:00+08:00",
                "duration_seconds": 600,
                "mode": "mp",
                "net_profit": 999999,
            },
        ]

        options = session_options(matches, [], [])

        self.assertEqual(options[0]["profit_matches"], 0)
        self.assertIsNone(options[0]["net_profit"])
        self.assertEqual(options[1]["profit_matches"], 2)
        self.assertEqual(options[1]["net_profit"], 120000)

    def test_preferences_are_isolated_by_account(self) -> None:
        wechat = {"openid": "wechat-user", "acctype": "mini"}
        qq = {"openid": "qq-user", "acctype": "qc"}
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            write_preferences(
                {"favorite_friends": ["微信好友"]}, wechat, app_dir=app_dir
            )
            write_preferences({"favorite_friends": ["QQ好友"]}, qq, app_dir=app_dir)

            self.assertEqual(
                read_preferences(wechat, app_dir=app_dir)["favorite_friends"],
                ["微信好友"],
            )
            self.assertEqual(
                read_preferences(qq, app_dir=app_dir)["favorite_friends"],
                ["QQ好友"],
            )

    def setUp(self) -> None:
        self.operation_directory = TemporaryDirectory()
        operation_directory = Path(self.operation_directory.name)
        self.operation_lock_patcher = patch(
            "stats_server.InterProcessFileLock",
            side_effect=lambda name, timeout=None: InterProcessFileLock(
                name,
                app_dir=operation_directory,
                timeout=timeout,
            ),
        )
        self.operation_lock_patcher.start()
        self.server = create_server(port=0)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        wait_until_ready(self.port)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.operation_lock_patcher.stop()
        self.operation_directory.cleanup()

    def request_json(
        self, path: str, *, method: str = "GET", payload: dict | None = None
    ) -> tuple[int, dict]:
        body = (
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if payload is not None
            else None
        )
        request = Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body,
            headers={"Content-Type": "application/json"} if body is not None else {},
            method=method,
        )
        for attempt in range(2):
            try:
                response = urlopen(request, timeout=3)
            except HTTPError as exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            except URLError:
                if attempt == 1:
                    raise
                time.sleep(0.05)
                continue
            with response:
                return response.status, json.loads(response.read().decode("utf-8"))
        raise AssertionError("unreachable")

    def test_matches_endpoint_rejects_unknown_friend_mode(self) -> None:
        with (
            patch("stats_server.resolve_account_identity", return_value=None),
            patch("stats_server.read_cache", return_value={"matches": []}),
            patch("stats_server.read_preferences", return_value={"favorite_friends": []}),
            patch("stats_server.registry_summary", return_value={"accounts": []}),
        ):
            status, payload = self.request_json("/api/matches?friend_mode=either")

        self.assertEqual(status, 400)
        self.assertIn("friend_mode", payload["error"])

    @patch("stats_server.read_cache", return_value={"matches": []})
    @patch("stats_server.active_auth", return_value=None)
    @patch("stats_server.auth_status")
    def test_status_returns_safe_auth_summary(
        self, auth_status_mock, _active_auth_mock, _read_cache_mock
    ) -> None:
        auth_status_mock.return_value = {
            "exists": True,
            "provider": "wechat-miniapp",
            "account_type": "qq",
            "appid": "wx1c36464bbea2507a",
            "saved_at_utc": "2026-08-08T00:00:00+00:00",
            "has_required_fields": True,
            "path": "redacted",
        }
        status, payload = self.request_json("/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(payload["auth"]["exists"])
        self.assertEqual(payload["auth"]["account_type"], "qq")
        self.assertNotIn("openid", payload["auth"])
        self.assertNotIn("ieg_ams_token", payload["auth"])
        self.assertNotIn("access_token", payload["auth"])

    @patch("stats_server.accounts_payload")
    def test_accounts_endpoint_never_exposes_credentials(
        self, accounts_payload_mock
    ) -> None:
        accounts_payload_mock.return_value = {
            "active_account_id": "account-one",
            "revision": 3,
            "sync_running": False,
            "accounts": [
                {
                    "id": "account-one",
                    "account_type": "wechat",
                    "display_name": "测试账号",
                    "auth_state": "valid",
                    "has_credential": True,
                    "can_sync": True,
                    "cache": {"exists": True, "matches": 12, "updated_at": None},
                }
            ],
        }

        status, payload = self.request_json("/api/accounts")

        self.assertEqual(status, 200)
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("openid", serialized)
        self.assertNotIn("access_token", serialized)
        self.assertNotIn("ieg_ams_token", serialized)

    @patch("stats_server.os.startfile")
    @patch("stats_server.wechat_protocol_registered", return_value=True)
    @patch("stats_server.sys.platform", "win32")
    def test_open_wechat_uses_fixed_delta_force_miniapp_protocol(
        self, _registered_mock, startfile_mock
    ) -> None:
        result = open_wechat()

        self.assertTrue(result["ok"])
        self.assertTrue(result["direct"])
        self.assertFalse(result["fallback"])
        startfile_mock.assert_called_once_with(
            "weixin://dl/business/?appid=wx1c36464bbea2507a"
            "&path=pages/index/index&env_version=release"
        )

    @patch("stats_server.os.startfile")
    @patch("stats_server.wechat_protocol_registered", return_value=True)
    @patch("stats_server.sys.platform", "win32")
    def test_open_wechat_falls_back_only_after_direct_open_fails(
        self, _registered_mock, startfile_mock
    ) -> None:
        startfile_mock.side_effect = [OSError("direct failed"), None]

        result = open_wechat()

        self.assertTrue(result["ok"])
        self.assertFalse(result["direct"])
        self.assertTrue(result["fallback"])
        self.assertEqual(startfile_mock.call_count, 2)
        self.assertEqual(startfile_mock.call_args_list[1].args, ("weixin://",))

    @patch("stats_server.os.startfile", side_effect=OSError("failed"))
    @patch("stats_server.wechat_protocol_registered", return_value=True)
    @patch("stats_server.sys.platform", "win32")
    def test_open_wechat_reports_failure_when_direct_and_fallback_fail(
        self, _registered_mock, startfile_mock
    ) -> None:
        result = open_wechat()

        self.assertFalse(result["ok"])
        self.assertFalse(result["direct"])
        self.assertFalse(result["fallback"])
        self.assertEqual(startfile_mock.call_count, 2)

    @patch("stats_server.open_wechat")
    def test_wechat_open_endpoint_rejects_custom_urls(self, open_wechat_mock) -> None:
        status, payload = self.request_json(
            "/api/wechat/open",
            method="POST",
            payload={"url": "https://example.invalid/"},
        )

        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["direct"])
        self.assertFalse(payload["fallback"])
        open_wechat_mock.assert_not_called()

    @patch("stats_server.open_wechat")
    def test_wechat_open_endpoint_returns_structured_result(
        self, open_wechat_mock
    ) -> None:
        open_wechat_mock.return_value = {
            "ok": True,
            "direct": True,
            "fallback": False,
            "message": "已打开小程序",
        }

        status, payload = self.request_json("/api/wechat/open", method="POST")

        self.assertEqual(status, 200)
        self.assertEqual(
            payload,
            {
                "ok": True,
                "direct": True,
                "fallback": False,
                "message": "已打开小程序",
            },
        )

    @patch("stats_server.safe_candidate_summary")
    @patch("stats_server.auth_candidates")
    @patch("stats_server.registry_summary")
    def test_auth_candidates_endpoint_returns_only_safe_summaries(
        self,
        registry_summary_mock,
        auth_candidates_mock,
        safe_candidate_summary_mock,
    ) -> None:
        registry_summary_mock.return_value = {
            "accounts": [{"id": "candidate-id"}]
        }
        auth_candidates_mock.return_value = [
            {"openid": "secret", "access_token": "token"}
        ]
        safe_candidate_summary_mock.return_value = {
            "id": "candidate-id",
            "account_type": "qq",
            "display_name": "QQ账号",
        }

        status, payload = self.request_json("/api/auth/candidates")

        self.assertEqual(status, 200)
        self.assertEqual(payload["candidates"][0]["id"], "candidate-id")
        self.assertTrue(payload["candidates"][0]["saved"])
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("token", serialized)

    @patch("stats_server.accounts_payload")
    @patch("stats_server.activate_account")
    def test_account_switch_is_local_and_returns_registry(
        self, activate_account_mock, accounts_payload_mock
    ) -> None:
        accounts_payload_mock.return_value = {
            "active_account_id": "target-account",
            "revision": 4,
            "accounts": [],
            "sync_running": False,
        }

        status, payload = self.request_json(
            "/api/accounts/switch",
            method="POST",
            payload={"account_id": "target-account"},
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        activate_account_mock.assert_called_once_with("target-account")

    @patch("stats_server.accounts_payload")
    @patch("stats_server.remove_account")
    def test_account_remove_keeps_cache_policy_in_registry_layer(
        self, remove_account_mock, accounts_payload_mock
    ) -> None:
        accounts_payload_mock.return_value = {
            "active_account_id": None,
            "revision": 5,
            "accounts": [],
            "sync_running": False,
        }

        status, payload = self.request_json(
            "/api/accounts/remove",
            method="POST",
            payload={"account_id": "target-account"},
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        remove_account_mock.assert_called_once_with("target-account")

    @patch("stats_server.activate_account")
    @patch(
        "stats_server.acquire_operation_lock",
        side_effect=OperationBusyError("operation busy"),
    )
    def test_account_switch_rejects_when_another_operation_is_running(
        self, _operation_lock_mock, activate_account_mock
    ) -> None:
        status, payload = self.request_json(
            "/api/accounts/switch",
            method="POST",
            payload={"account_id": "target-account"},
        )

        self.assertEqual(status, 409)
        self.assertIn("operation busy", payload["error"])
        activate_account_mock.assert_not_called()

    @patch("stats_server.registry_summary")
    @patch("stats_server.read_preferences")
    @patch("stats_server.read_cache")
    @patch("stats_server.get_account_identity")
    def test_dataset_uses_explicit_account_and_keeps_expired_cache_viewable(
        self,
        get_account_identity_mock,
        read_cache_mock,
        read_preferences_mock,
        registry_summary_mock,
    ) -> None:
        auth = {"openid": "expired-user", "acctype": "qc"}
        target_id = account_id(auth)
        get_account_identity_mock.return_value = auth
        read_cache_mock.return_value = {"updated_at": None, "matches": []}
        read_preferences_mock.return_value = {"favorite_friends": []}
        registry_summary_mock.return_value = {
            "accounts": [
                {
                    "id": target_id,
                    "auth_state": "expired",
                    "can_sync": False,
                }
            ]
        }

        payload = dataset({"account_id": [target_id]})

        get_account_identity_mock.assert_called_once_with(target_id)
        read_cache_mock.assert_called_once_with(auth)
        self.assertEqual(payload["account_id"], target_id)
        self.assertFalse(payload["sync_enabled"])

    def test_recovery_waits_when_no_miniapp_candidate_exists(self) -> None:
        target = {
            "openid": "saved-target",
            "acctype": "qc",
            "appid": "qq-appid",
            "access_token": "old-token",
        }
        target_id = account_id(target)
        account = {
            "id": target_id,
            "account_type": "qq",
            "auth_state": "expired",
            "has_credential": True,
        }
        registry = {"active_account_id": target_id, "accounts": [account]}
        accounts = {**registry, "sync_running": False}

        with (
            patch("stats_server.get_account_identity", return_value=account),
            patch("stats_server.registry_summary", return_value=registry),
            patch("stats_server.accounts_payload", return_value=accounts),
            patch("stats_server.auth_candidates", return_value=[]),
            patch("stats_server.DeltaAmsClient") as client_mock,
            patch("stats_server.upsert_account") as upsert_mock,
        ):
            status, payload = self.request_json(
                "/api/auth/import",
                method="POST",
                payload={
                    "candidate_id": target_id,
                    "recovery": True,
                    "known_revision": "baseline",
                },
            )

        self.assertEqual(status, 202)
        self.assertEqual(payload["status"], "waiting_for_miniapp")
        self.assertEqual(payload["error_kind"], "auth_waiting")
        self.assertTrue(payload["pending"])
        self.assertTrue(payload["retryable"])
        client_mock.assert_not_called()
        upsert_mock.assert_not_called()

    def test_recovery_reports_latest_other_account_without_mutating_state(
        self,
    ) -> None:
        target = {
            "openid": "shared-openid",
            "acctype": "qc",
            "appid": "qq-appid",
            "access_token": "target-token",
        }
        other = {
            "openid": "shared-openid",
            "acctype": "mini",
            "appid": "wx1c36464bbea2507a",
            "ieg_ams_session_token": "other-session-secret",
            "ieg_ams_token": "other-token-secret",
            "ieg_ams_token_time": "1",
        }
        target_id = account_id(target)
        account = {
            "id": target_id,
            "account_type": "qq",
            "auth_state": "expired",
            "has_credential": True,
        }
        registry = {"active_account_id": target_id, "accounts": [account]}
        accounts = {**registry, "sync_running": False}

        with (
            patch("stats_server.get_account_identity", return_value=account),
            patch("stats_server.registry_summary", return_value=registry),
            patch("stats_server.accounts_payload", return_value=accounts),
            patch("stats_server.auth_candidates", return_value=[other, target]),
            patch("stats_server.DeltaAmsClient") as client_mock,
            patch("stats_server.upsert_account") as upsert_mock,
            patch("stats_server.activate_account") as activate_mock,
            patch("stats_server.remember_validation") as validation_mock,
        ):
            status, payload = self.request_json(
                "/api/auth/import",
                method="POST",
                payload={
                    "candidate_id": target_id,
                    "recovery": True,
                    "known_revision": safe_candidate_summary(target)[
                        "credential_revision"
                    ],
                },
            )

        self.assertEqual(status, 202)
        self.assertEqual(payload["status"], "account_mismatch")
        self.assertEqual(payload["error_kind"], "account_mismatch")
        self.assertEqual(payload["detected_account"]["id"], account_id(other))
        self.assertFalse(payload["detected_account"]["saved"])
        encoded = json.dumps(payload["detected_account"], ensure_ascii=False)
        self.assertNotIn("other-session-secret", encoded)
        self.assertNotIn("other-token-secret", encoded)
        client_mock.assert_not_called()
        upsert_mock.assert_not_called()
        activate_mock.assert_not_called()
        validation_mock.assert_not_called()

    def test_recovery_does_not_probe_unchanged_target_revision(self) -> None:
        target = {
            "openid": "saved-target",
            "acctype": "qc",
            "appid": "qq-appid",
            "access_token": "same-token",
        }
        target_id = account_id(target)
        revision = safe_candidate_summary(target)["credential_revision"]
        account = {
            "id": target_id,
            "account_type": "qq",
            "auth_state": "expired",
            "has_credential": True,
        }
        registry = {"active_account_id": target_id, "accounts": [account]}

        with (
            patch("stats_server.get_account_identity", return_value=account),
            patch("stats_server.registry_summary", return_value=registry),
            patch(
                "stats_server.accounts_payload",
                return_value={**registry, "sync_running": False},
            ),
            patch("stats_server.auth_candidates", return_value=[target]),
            patch("stats_server.DeltaAmsClient") as client_mock,
            patch("stats_server.upsert_account") as upsert_mock,
        ):
            status, payload = self.request_json(
                "/api/auth/import",
                method="POST",
                payload={
                    "candidate_id": target_id,
                    "recovery": True,
                    "known_revision": revision,
                },
            )

        self.assertEqual(status, 202)
        self.assertEqual(payload["status"], "waiting_for_miniapp")
        self.assertEqual(payload["credential_revision"], revision)
        client_mock.assert_not_called()
        upsert_mock.assert_not_called()

    def test_recovery_saves_new_matching_credential_without_switching(self) -> None:
        old_target = {
            "openid": "saved-target",
            "acctype": "qc",
            "appid": "qq-appid",
            "access_token": "old-token",
        }
        new_target = {**old_target, "access_token": "new-token"}
        target_id = account_id(old_target)
        old_revision = safe_candidate_summary(old_target)["credential_revision"]
        new_revision = safe_candidate_summary(new_target)["credential_revision"]
        account = {
            "id": target_id,
            "account_type": "qq",
            "auth_state": "expired",
            "has_credential": True,
        }
        recovered = {**account, "auth_state": "valid"}
        registry = {"active_account_id": target_id, "accounts": [account]}

        with (
            patch("stats_server.get_account_identity", return_value=account),
            patch("stats_server.registry_summary", return_value=registry),
            patch(
                "stats_server.accounts_payload",
                return_value={**registry, "sync_running": False},
            ),
            patch("stats_server.auth_candidates", return_value=[new_target]),
            patch("stats_server.DeltaAmsClient") as client_mock,
            patch("stats_server.upsert_account", return_value=recovered) as upsert_mock,
            patch("stats_server.migrate_active_account_data") as migrate_mock,
            patch("stats_server.auth_status", return_value={"exists": True}),
        ):
            status, payload = self.request_json(
                "/api/auth/import",
                method="POST",
                payload={
                    "candidate_id": target_id,
                    "recovery": True,
                    "known_revision": old_revision,
                },
            )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "recovered")
        self.assertEqual(payload["credential_revision"], new_revision)
        self.assertEqual(payload["account"]["credential_revision"], new_revision)
        client_mock.assert_called_once_with(new_target)
        client_mock.return_value.fetch_match_page.assert_called_once_with(4, 1)
        upsert_mock.assert_called_once_with(
            new_target, activate=False, validation_result="success"
        )
        migrate_mock.assert_called_once_with(new_target)

    def test_recovery_restores_a_cache_only_account_without_saved_credential(
        self,
    ) -> None:
        target = {
            "openid": "cache-only-target",
            "acctype": "qc",
            "appid": "qq-appid",
            "access_token": "fresh-token",
        }
        target_id = account_id(target)
        account = {
            "id": target_id,
            "account_type": "qq",
            "auth_state": "missing",
            "has_credential": False,
        }
        recovered = {**account, "auth_state": "valid", "has_credential": True}
        registry = {"active_account_id": target_id, "accounts": [account]}

        with (
            patch("stats_server.get_account_identity", return_value=account),
            patch("stats_server.registry_summary", return_value=registry),
            patch(
                "stats_server.accounts_payload",
                return_value={**registry, "sync_running": False},
            ),
            patch("stats_server.auth_candidates", return_value=[target]),
            patch("stats_server.DeltaAmsClient") as client_mock,
            patch("stats_server.upsert_account", return_value=recovered) as upsert_mock,
            patch("stats_server.migrate_active_account_data"),
            patch("stats_server.auth_status", return_value={"exists": True}),
        ):
            status, payload = self.request_json(
                "/api/auth/import",
                method="POST",
                payload={"candidate_id": target_id, "recovery": True},
            )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        client_mock.assert_called_once_with(target)
        upsert_mock.assert_called_once_with(
            target, activate=False, validation_result="success"
        )

    def test_recovery_keeps_new_matching_credential_pending_when_busy(self) -> None:
        old_target = {
            "openid": "saved-target",
            "acctype": "qc",
            "appid": "qq-appid",
            "access_token": "old-token",
        }
        new_target = {**old_target, "access_token": "new-token"}
        target_id = account_id(old_target)
        account = {
            "id": target_id,
            "account_type": "qq",
            "auth_state": "expired",
            "has_credential": True,
        }
        pending = {**account, "auth_state": "pending_verification"}
        registry = {"active_account_id": target_id, "accounts": [account]}
        busy = AmsError("访问人数太多", code=-108)

        with (
            patch("stats_server.get_account_identity", return_value=account),
            patch("stats_server.registry_summary", return_value=registry),
            patch(
                "stats_server.accounts_payload",
                return_value={**registry, "sync_running": False},
            ),
            patch("stats_server.auth_candidates", return_value=[new_target]),
            patch("stats_server.DeltaAmsClient") as client_mock,
            patch("stats_server.upsert_account", return_value=pending) as upsert_mock,
            patch("stats_server.migrate_active_account_data"),
            patch("stats_server.auth_status", return_value={"exists": True}),
        ):
            client_mock.return_value.fetch_match_page.side_effect = busy
            status, payload = self.request_json(
                "/api/auth/import",
                method="POST",
                payload={
                    "candidate_id": target_id,
                    "recovery": True,
                    "known_revision": safe_candidate_summary(old_target)[
                        "credential_revision"
                    ],
                },
            )

        self.assertEqual(status, 202)
        self.assertEqual(payload["status"], "pending_verification")
        self.assertEqual(payload["error_kind"], "busy")
        self.assertEqual(payload["auth_state"], "pending_verification")
        upsert_mock.assert_called_once_with(
            new_target,
            activate=False,
            validation_result="busy",
            validation_error=busy,
        )

    def test_recovery_waits_after_explicit_rejection_of_new_candidate(self) -> None:
        old_target = {
            "openid": "saved-target",
            "acctype": "qc",
            "appid": "qq-appid",
            "access_token": "old-token",
        }
        new_target = {**old_target, "access_token": "new-token"}
        target_id = account_id(old_target)
        account = {
            "id": target_id,
            "account_type": "qq",
            "auth_state": "expired",
            "has_credential": True,
        }
        registry = {"active_account_id": target_id, "accounts": [account]}
        rejected = AmsError("请先登录", code=101)

        with (
            patch("stats_server.get_account_identity", return_value=account),
            patch("stats_server.registry_summary", return_value=registry),
            patch(
                "stats_server.accounts_payload",
                return_value={**registry, "sync_running": False},
            ),
            patch("stats_server.auth_candidates", return_value=[new_target]),
            patch("stats_server.DeltaAmsClient") as client_mock,
            patch("stats_server.remember_validation", return_value=account) as remember_mock,
            patch("stats_server.upsert_account") as upsert_mock,
        ):
            client_mock.return_value.fetch_match_page.side_effect = rejected
            status, payload = self.request_json(
                "/api/auth/import",
                method="POST",
                payload={
                    "candidate_id": target_id,
                    "recovery": True,
                    "known_revision": safe_candidate_summary(old_target)[
                        "credential_revision"
                    ],
                },
            )

        self.assertEqual(status, 202)
        self.assertEqual(payload["status"], "waiting_for_miniapp")
        self.assertEqual(payload["error_kind"], "auth_waiting")
        self.assertEqual(payload["probe_error_kind"], "expired")
        remember_mock.assert_called_once_with(target_id, "rejected", rejected)
        upsert_mock.assert_not_called()

    @patch(
        "stats_server.acquire_operation_lock",
        side_effect=OperationBusyError("operation busy"),
    )
    def test_auth_import_lock_conflict_is_structured_and_retryable(
        self, _operation_lock_mock
    ) -> None:
        status, payload = self.request_json(
            "/api/auth/import",
            method="POST",
            payload={"candidate_id": "0" * 24, "recovery": True},
        )

        self.assertEqual(status, 409)
        self.assertEqual(payload["status"], "operation_busy")
        self.assertEqual(payload["error_kind"], "operation_busy")
        self.assertTrue(payload["pending"])
        self.assertTrue(payload["retryable"])
        self.assertEqual(payload["retry_after_seconds"], 1)

    @patch("stats_server.upsert_account")
    @patch("stats_server.DeltaAmsClient")
    @patch("stats_server.auth_candidates", return_value=[])
    def test_first_import_without_candidate_reports_missing_login_without_mutation(
        self, _auth_candidates_mock, client_mock, upsert_mock
    ) -> None:
        status, payload = self.request_json("/api/auth/import", method="POST")

        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status"], "waiting_for_miniapp")
        self.assertEqual(payload["error_kind"], "auth_missing")
        self.assertTrue(payload["retryable"])
        self.assertFalse(payload["auth_invalid"])
        client_mock.assert_not_called()
        upsert_mock.assert_not_called()

    @patch("stats_server.accounts_payload")
    @patch("stats_server.migrate_active_account_data")
    @patch("stats_server.upsert_account")
    @patch("stats_server.DeltaAmsClient")
    @patch("stats_server.auth_candidates")
    @patch("stats_server.auth_status")
    def test_import_auth_returns_updated_status(
        self,
        auth_status_mock,
        auth_candidates_mock,
        client_mock,
        upsert_mock,
        migrate_mock,
        accounts_payload_mock,
    ) -> None:
        auth_status_mock.return_value = {"exists": True, "has_required_fields": True}
        accounts_payload_mock.return_value = {
            "active_account_id": "qq-id",
            "revision": 1,
            "accounts": [],
            "sync_running": False,
        }
        candidate = {
            "openid": "qq-openid",
            "acctype": "qc",
            "appid": "qq-appid",
            "access_token": "qq-session",
        }
        auth_candidates_mock.return_value = [candidate]
        status, payload = self.request_json("/api/auth/import", method="POST")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["error_kind"])
        self.assertFalse(payload["retryable"])
        self.assertFalse(payload["auth_invalid"])
        self.assertEqual(payload["auth_state"], "valid")
        self.assertTrue(payload["auth"]["exists"])
        client_mock.return_value.fetch_match_page.assert_called_once_with(4, 1)
        upsert_mock.assert_called_once_with(
            candidate, activate=True, validation_result="success"
        )
        migrate_mock.assert_called_once_with(candidate)

    @patch("stats_server.migrate_active_account_data")
    @patch("stats_server.upsert_account")
    @patch("stats_server.DeltaAmsClient")
    @patch("stats_server.auth_candidates")
    def test_import_auth_with_candidate_id_never_falls_back_to_another_account(
        self,
        auth_candidates_mock,
        client_mock,
        upsert_mock,
        migrate_mock,
    ) -> None:
        expired = {
            "openid": "expired-user",
            "acctype": "qc",
            "appid": "qq-appid",
            "access_token": "old-session",
        }
        different_current = {
            "openid": "different-user",
            "acctype": "mini",
            "appid": "wx1c36464bbea2507a",
            "ieg_ams_session_token": "session",
            "ieg_ams_token": "token",
            "ieg_ams_token_time": "1",
        }
        auth_candidates_mock.return_value = [different_current]

        status, payload = self.request_json(
            "/api/auth/import",
            method="POST",
            payload={"candidate_id": account_id(expired)},
        )

        self.assertEqual(status, 400)
        self.assertIn("未在电脑版微信找到当前账号", payload["error"])
        self.assertEqual(payload["error_kind"], "auth_missing")
        self.assertFalse(payload["auth_invalid"])
        client_mock.assert_not_called()
        upsert_mock.assert_not_called()
        migrate_mock.assert_not_called()

    @patch("stats_server.accounts_payload")
    @patch("stats_server.migrate_active_account_data")
    @patch("stats_server.upsert_account")
    @patch("stats_server.DeltaAmsClient")
    @patch("stats_server.auth_candidates")
    @patch("stats_server.auth_status")
    def test_import_auth_with_candidate_id_only_updates_that_account(
        self,
        auth_status_mock,
        auth_candidates_mock,
        client_mock,
        upsert_mock,
        migrate_mock,
        accounts_payload_mock,
    ) -> None:
        expired = {
            "openid": "expired-user",
            "acctype": "qc",
            "appid": "qq-appid",
            "access_token": "new-session",
        }
        different_current = {
            "openid": "different-user",
            "acctype": "mini",
            "appid": "wx1c36464bbea2507a",
            "ieg_ams_session_token": "session",
            "ieg_ams_token": "token",
            "ieg_ams_token_time": "1",
        }
        auth_candidates_mock.return_value = [different_current, expired]
        auth_status_mock.return_value = {"exists": True, "has_required_fields": True}
        accounts_payload_mock.return_value = {
            "active_account_id": account_id(expired),
            "revision": 2,
            "accounts": [],
            "sync_running": False,
        }

        status, payload = self.request_json(
            "/api/auth/import",
            method="POST",
            payload={"candidate_id": account_id(expired)},
        )

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        client_mock.assert_called_once_with(expired)
        client_mock.return_value.fetch_match_page.assert_called_once_with(4, 1)
        upsert_mock.assert_called_once_with(
            expired, activate=True, validation_result="success"
        )
        migrate_mock.assert_called_once_with(expired)

    @patch("stats_server.remember_validation")
    @patch("stats_server.accounts_payload")
    @patch("stats_server.migrate_active_account_data")
    @patch("stats_server.upsert_account")
    @patch("stats_server.DeltaAmsClient")
    @patch("stats_server.auth_candidates")
    @patch("stats_server.auth_status")
    def test_import_auth_uses_later_qq_candidate_when_first_is_stale(
        self,
        auth_status_mock,
        auth_candidates_mock,
        client_mock,
        upsert_mock,
        migrate_mock,
        accounts_payload_mock,
        remember_validation_mock,
    ) -> None:
        stale = {"openid": "stale", "acctype": "mini"}
        qq = {
            "openid": "qq-openid",
            "acctype": "qc",
            "appid": "qq-appid",
            "access_token": "qq-session",
        }
        auth_status_mock.return_value = {
            "exists": True,
            "account_type": "qq",
            "has_required_fields": True,
        }
        accounts_payload_mock.return_value = {
            "active_account_id": "qq-id",
            "revision": 2,
            "accounts": [],
            "sync_running": False,
        }
        auth_candidates_mock.return_value = [stale, qq]
        client_mock.return_value.fetch_match_page.side_effect = [
            AmsError("请先登录", code=101),
            {},
        ]

        status, payload = self.request_json("/api/auth/import", method="POST")

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(client_mock.call_count, 2)
        self.assertEqual(client_mock.return_value.fetch_match_page.call_count, 2)
        remember_validation_mock.assert_called_once()
        upsert_mock.assert_called_once_with(
            qq, activate=True, validation_result="success"
        )
        migrate_mock.assert_called_once_with(qq)

    @patch("stats_server.migrate_active_account_data")
    @patch("stats_server.upsert_account")
    @patch("stats_server.DeltaAmsClient")
    @patch("stats_server.auth_candidates")
    def test_import_auth_does_not_fallback_to_old_account_when_newest_is_busy(
        self,
        auth_candidates_mock,
        client_mock,
        upsert_mock,
        migrate_mock,
    ) -> None:
        qq = {"openid": "qq-openid", "acctype": "qc"}
        old_wechat = {"openid": "wechat-openid", "acctype": "mini"}
        auth_candidates_mock.return_value = [qq, old_wechat]
        client_mock.return_value.fetch_match_page.side_effect = AmsError(
            "抱歉，目前访问人数太多！请稍后再试！谢谢！", code=999
        )

        status, payload = self.request_json("/api/auth/import", method="POST")

        self.assertEqual(status, 400)
        self.assertIn("腾讯战绩接口暂时繁忙", payload["error"])
        self.assertEqual(payload["error_kind"], "busy")
        self.assertTrue(payload["retryable"])
        self.assertFalse(payload["auth_invalid"])
        client_mock.assert_called_once_with(qq)
        client_mock.return_value.fetch_match_page.assert_called_once_with(4, 1)
        upsert_mock.assert_not_called()
        migrate_mock.assert_not_called()

    @patch("stats_server.get_account_identity")
    @patch("stats_server.accounts_payload")
    @patch("stats_server.auth_status")
    @patch("stats_server.migrate_active_account_data")
    @patch("stats_server.upsert_account")
    @patch("stats_server.DeltaAmsClient")
    @patch("stats_server.auth_candidates")
    def test_requested_auth_reread_is_retained_when_probe_is_busy(
        self,
        auth_candidates_mock,
        client_mock,
        upsert_mock,
        migrate_mock,
        auth_status_mock,
        accounts_payload_mock,
        get_account_identity_mock,
    ) -> None:
        candidate = {
            "openid": "qq-openid",
            "acctype": "qc",
            "appid": "qq-appid",
            "access_token": "fresh-session",
        }
        candidate_id = account_id(candidate)
        get_account_identity_mock.return_value = {
            "account_id": candidate_id,
            "auth_state": "expired",
        }
        auth_candidates_mock.return_value = [candidate]
        client_mock.return_value.fetch_match_page.side_effect = AmsError(
            "访问人数太多", code=-108
        )
        upsert_mock.return_value = {
            "id": candidate_id,
            "auth_state": "pending_verification",
        }
        auth_status_mock.return_value = {"exists": True}
        accounts_payload_mock.return_value = {
            "active_account_id": candidate_id,
            "accounts": [
                {"id": candidate_id, "auth_state": "pending_verification"}
            ],
            "sync_running": False,
        }

        status, payload = self.request_json(
            "/api/auth/import",
            method="POST",
            payload={"candidate_id": candidate_id},
        )

        self.assertEqual(status, 202)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["pending"])
        self.assertEqual(payload["auth_state"], "pending_verification")
        self.assertEqual(payload["error_kind"], "busy")
        self.assertTrue(payload["retryable"])
        self.assertFalse(payload["auth_invalid"])
        self.assertEqual(payload["retry_after_seconds"], 3)
        self.assertEqual(payload["retry_max_attempts"], 2)
        self.assertEqual(payload["accounts"][0]["auth_state"], "pending_verification")
        upsert_mock.assert_called_once_with(
            candidate,
            activate=True,
            validation_result="busy",
            validation_error=client_mock.return_value.fetch_match_page.side_effect,
        )
        migrate_mock.assert_called_once_with(candidate)

    @patch("stats_server.migrate_active_account_data")
    @patch("stats_server.upsert_account")
    @patch("stats_server.DeltaAmsClient")
    @patch("stats_server.auth_candidates")
    def test_import_auth_does_not_fallback_to_old_account_on_network_failure(
        self,
        auth_candidates_mock,
        client_mock,
        upsert_mock,
        migrate_mock,
    ) -> None:
        qq = {"openid": "qq-openid", "acctype": "qc"}
        old_wechat = {"openid": "wechat-openid", "acctype": "mini"}
        auth_candidates_mock.return_value = [qq, old_wechat]
        client_mock.return_value.fetch_match_page.side_effect = requests.ConnectionError(
            "offline"
        )

        status, payload = self.request_json("/api/auth/import", method="POST")

        self.assertEqual(status, 400)
        self.assertIn("网络", payload["error"])
        self.assertIn("无法判断登录态是否失效", payload["error"])
        client_mock.assert_called_once_with(qq)
        client_mock.return_value.fetch_match_page.assert_called_once_with(4, 1)
        upsert_mock.assert_not_called()
        migrate_mock.assert_not_called()

    @patch("stats_server.remember_validation")
    @patch("stats_server.DeltaAmsClient")
    @patch("stats_server.auth_candidates")
    def test_import_auth_reports_readable_error(
        self, auth_candidates_mock, client_mock, _remember_validation_mock
    ) -> None:
        auth_candidates_mock.return_value = [{"openid": "expired"}]
        client_mock.return_value.fetch_match_page.side_effect = AmsError("请先登录")
        status, payload = self.request_json("/api/auth/import", method="POST")
        self.assertEqual(status, 400)
        self.assertIn("登录态已被腾讯拒绝", payload["error"])
        self.assertEqual(payload["error_kind"], "expired")
        self.assertFalse(payload["retryable"])
        self.assertTrue(payload["auth_invalid"])

    def test_auth_probe_does_not_misreport_network_failure_as_expired_login(self) -> None:
        message = auth_probe_failure_message([requests.ConnectionError("offline")])
        self.assertIn("网络", message)
        self.assertNotIn("登录态已被腾讯拒绝", message)

    def test_sync_failure_replaces_tencent_busy_copy(self) -> None:
        message, auth_invalid = sync_failure(
            AmsError("抱歉，目前访问人数太多！请稍后再试！谢谢！", code=999)
        )

        self.assertEqual(
            message,
            "腾讯战绩接口暂时繁忙，自动重试仍未恢复，请稍后再刷新",
        )
        self.assertFalse(auth_invalid)

    def test_auth_probe_does_not_misreport_busy_service_as_bad_login(self) -> None:
        message = auth_probe_failure_message(
            [AmsError("抱歉，目前访问人数太多！请稍后再试！谢谢！", code=999)]
        )

        self.assertIn("腾讯战绩接口暂时繁忙", message)
        self.assertNotIn("未接受当前登录态", message)

    def test_auth_probe_unknown_failure_is_reported_as_indeterminate(self) -> None:
        message = auth_probe_failure_message([RuntimeError("unexpected response")])

        self.assertIn("无法判断登录态是否失效", message)
        self.assertIn("unexpected response", message)

    @patch("stats_server.remember_validation")
    @patch("stats_server.load_auth")
    @patch("stats_server.sync_matches")
    def test_background_sync_reports_progress_and_partial_failures(
        self, sync_matches_mock, load_auth_mock, remember_validation_mock
    ) -> None:
        auth_snapshot = {"openid": "qq-openid", "acctype": "qc"}
        load_auth_mock.return_value = auth_snapshot

        def fake_sync(*, progress=None, **_kwargs):
            progress(25, "正在读取逐局列表")
            progress(80, "正在补齐逐局详情")
            return {
                "updated_at": "2026-08-09T01:00:00+00:00",
                "matches": [{"details_loaded": False, "income_loaded": False}],
                "detail_failures": [{"id": "match", "kind": "detail", "error": "timeout"}],
            }

        sync_matches_mock.side_effect = fake_sync
        status, started = self.request_json("/api/sync/start", method="POST")
        self.assertEqual(status, 202)
        job_id = started["job_id"]

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status, payload = self.request_json(f"/api/sync/status?id={job_id}")
            self.assertEqual(status, 200)
            if payload["status"] != "running":
                break
            time.sleep(0.02)

        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["progress"], 100)
        self.assertEqual(payload["result"]["detail_failures"], 1)
        self.assertEqual(payload["account_id"], account_id(auth_snapshot))
        self.assertEqual(payload["result"]["account_id"], account_id(auth_snapshot))
        self.assertEqual(sync_matches_mock.call_args.kwargs["auth"], auth_snapshot)
        self.assertEqual(sync_matches_mock.call_args.kwargs["assist_days"], 7)
        remember_validation_mock.assert_called_once_with(
            account_id(auth_snapshot), "success"
        )

    @patch("stats_server.remember_validation")
    @patch("stats_server.load_auth")
    @patch("stats_server.sync_matches")
    def test_background_sync_uses_explicit_requested_account(
        self, sync_matches_mock, load_auth_mock, _remember_validation_mock
    ) -> None:
        auth_snapshot = {"openid": "wechat-user", "acctype": "mini"}
        target_id = account_id(auth_snapshot)
        load_auth_mock.return_value = auth_snapshot
        sync_matches_mock.return_value = {
            "updated_at": None,
            "matches": [],
            "detail_failures": [],
        }

        status, started = self.request_json(
            f"/api/sync/start?account_id={target_id}", method="POST"
        )
        self.assertEqual(status, 202)

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            _, current = self.request_json(
                f"/api/sync/status?id={started['job_id']}"
            )
            if current["status"] != "running":
                break
            time.sleep(0.02)

        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["account_id"], target_id)
        load_auth_mock.assert_called_once_with(target_id)
        self.assertEqual(sync_matches_mock.call_args.kwargs["auth"], auth_snapshot)

    @patch("stats_server.remember_validation")
    @patch("stats_server.load_auth")
    @patch("stats_server.sync_matches")
    def test_explicit_tencent_rejection_marks_only_the_synced_account_expired(
        self, sync_matches_mock, load_auth_mock, remember_validation_mock
    ) -> None:
        auth_snapshot = {"openid": "expired-user", "acctype": "qc"}
        target_id = account_id(auth_snapshot)
        load_auth_mock.return_value = auth_snapshot
        sync_matches_mock.side_effect = AmsError("请先登录", code=101)

        status, started = self.request_json(
            f"/api/sync/start?account_id={target_id}", method="POST"
        )
        self.assertEqual(status, 202)

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            _, current = self.request_json(
                f"/api/sync/status?id={started['job_id']}"
            )
            if current["status"] != "running":
                break
            time.sleep(0.02)

        self.assertEqual(current["status"], "failed")
        self.assertTrue(current["auth_invalid"])
        remember_validation_mock.assert_called_once()
        self.assertEqual(remember_validation_mock.call_args.args[:2], (target_id, "rejected"))

    @patch("stats_server.remember_validation")
    @patch("stats_server.load_auth")
    @patch("stats_server.sync_matches")
    def test_repeated_sync_start_reuses_running_job(
        self, sync_matches_mock, load_auth_mock, _remember_validation_mock
    ) -> None:
        entered = threading.Event()
        release = threading.Event()
        load_auth_mock.return_value = {"openid": "qq-openid", "acctype": "qc"}

        def fake_sync(**_kwargs):
            entered.set()
            self.assertTrue(release.wait(timeout=2))
            return {"updated_at": None, "matches": [], "detail_failures": []}

        sync_matches_mock.side_effect = fake_sync
        first_status, first = self.request_json("/api/sync/start", method="POST")
        self.assertEqual(first_status, 202)
        self.assertTrue(entered.wait(timeout=1))

        second_status, second = self.request_json("/api/sync/start", method="POST")
        self.assertEqual(second_status, 409)
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertEqual(sync_matches_mock.call_count, 1)

        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            _, current = self.request_json(f"/api/sync/status?id={first['job_id']}")
            if current["status"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual(current["status"], "completed")

    def test_send_json_treats_client_disconnect_as_normal_cancellation(self) -> None:
        handler = object.__new__(Handler)
        handler.path = "/api/matches"
        handler.close_connection = False
        handler.request_version = "HTTP/1.1"
        handler.command = "GET"
        handler.requestline = "GET /api/matches HTTP/1.1"
        handler.send_response = unittest.mock.MagicMock()
        handler.send_header = unittest.mock.MagicMock()
        handler.end_headers = unittest.mock.MagicMock()
        handler.wfile = unittest.mock.MagicMock()
        handler.wfile.write.side_effect = ConnectionAbortedError(10053, "client closed")

        with patch("stats_server.LOGGER.debug") as debug_log:
            handler.send_json({"ok": True})

        self.assertTrue(handler.close_connection)
        debug_log.assert_called_once()

    def test_matches_write_failure_does_not_attempt_a_second_response(self) -> None:
        handler = object.__new__(Handler)
        handler.path = "/api/matches"
        handler.send_json = unittest.mock.MagicMock(side_effect=OSError("write failed"))

        with (
            patch("stats_server.dataset", return_value={"matches": []}),
            self.assertRaisesRegex(OSError, "write failed"),
        ):
            handler.do_GET()

        handler.send_json.assert_called_once_with({"matches": []})

    @patch("stats_server.load_auth")
    def test_sync_start_marks_incomplete_saved_auth_invalid(self, load_auth_mock) -> None:
        load_auth_mock.side_effect = SavedAuthError(
            "保存的登录态不完整，请重新读取当前账号"
        )
        status, payload = self.request_json("/api/sync/start", method="POST")

        self.assertEqual(status, 502)
        self.assertTrue(payload["auth_invalid"])
        self.assertIn("重新读取当前账号", payload["error"])

    @patch("stats_server.remember_validation")
    @patch("stats_server.load_sync_auth")
    @patch("stats_server.sync_matches")
    def test_direct_sync_uses_separate_recent_assist_window(
        self, sync_matches_mock, load_sync_auth_mock, remember_validation_mock
    ) -> None:
        auth_snapshot = {"openid": "qq-openid", "acctype": "qc"}
        load_sync_auth_mock.return_value = ("account-id", auth_snapshot)
        sync_matches_mock.return_value = {
            "updated_at": None,
            "matches": [],
            "detail_failures": [],
        }

        status, payload = self.request_json("/api/sync", method="POST")

        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(sync_matches_mock.call_args.kwargs["assist_days"], 7)
        remember_validation_mock.assert_called_once_with("account-id", "success")

    @patch("stats_server.remember_validation")
    @patch("stats_server.load_sync_auth")
    @patch("stats_server.sync_matches")
    def test_direct_sync_marks_incomplete_saved_auth_invalid(
        self, sync_matches_mock, load_sync_auth_mock, remember_validation_mock
    ) -> None:
        load_sync_auth_mock.return_value = (
            "account-id",
            {"openid": "qq-openid", "acctype": "qc"},
        )
        sync_matches_mock.side_effect = SavedAuthError(
            "保存的登录态不完整，请重新读取当前账号"
        )
        status, payload = self.request_json("/api/sync", method="POST")

        self.assertEqual(status, 502)
        self.assertTrue(payload["auth_invalid"])
        self.assertIn("重新读取当前账号", payload["error"])
        remember_validation_mock.assert_called_once()
        self.assertEqual(remember_validation_mock.call_args.args[1], "error")


if __name__ == "__main__":
    unittest.main()
