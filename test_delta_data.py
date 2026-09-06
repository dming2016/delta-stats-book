from datetime import UTC, datetime, timedelta
import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from account_storage import account_id, cache_file as account_cache_file, raw_dir
from delta_data import (
    apply_mp_detail,
    apply_sol_detail,
    base_map_name,
    detect_sessions,
    enrich_from_raw_cache,
    filter_matches,
    friend_comparisons,
    map_options,
    net_profit_from_detail,
    needs_detail_enrichment,
    normalize_list_row,
    read_cache,
    summary,
    sync_matches,
    warfare_friend_comparisons,
    warfare_summary,
    write_cache,
)
from remote_sync import player_identity, upload_cache
from friend_client import wait_until_ready
from update_protocol import version_key
from shared_server import SharedStore, build_handler, summary as shared_summary


class DeltaDataTests(unittest.TestCase):
    def test_net_profit_formula_uses_final_price(self):
        row = {
            "FinalPrice": "1200",
            "KeyChainCarryOutPrice": "400",
            "KeyChainCarryInPrice": 100,
            "CarryoutSelfPrice": 250,
            "CarryoutSafeBoxPrice": 50,
        }
        self.assertEqual(net_profit_from_detail(row), 850)

    def test_net_profit_formula_uses_keychain_branch(self):
        row = {
            "FinalPrice": "200",
            "KeyChainCarryOutPrice": "500",
            "KeyChainCarryInPrice": 100,
            "CarryoutSelfPrice": 250,
            "CarryoutSafeBoxPrice": 50,
        }
        self.assertEqual(net_profit_from_detail(row), 200)

    def test_sol_list_normalization(self):
        match = normalize_list_row(
            {
                "RoomId": "room",
                "dtEventTime": "2026-08-07 16:00:00",
                "MapId": "2202",
                "EscapeFailReason": 1,
                "DurationS": 600,
                "KillCount": 2,
                "KillPlayerAICount": 1,
                "KillAICount": 5,
                "flowCalGainedPrice": "123456",
            },
            "sol",
        )
        self.assertEqual(match["kills"], 3)
        self.assertEqual(match["deaths"], 0)
        self.assertEqual(match["net_profit"], 123456)
        self.assertEqual(match["map_name"], "零号大坝-机密")
        self.assertEqual(match["timestamp"], "2026-08-07T16:00:00+08:00")
        self.assertEqual(match["difficulty"], "confidential")
        self.assertIsNone(match["gross_income"])
        self.assertFalse(match["income_loaded"])

    def test_friend_and_time_filter(self):
        matches = [
            {
                "timestamp": "2026-08-07T16:00:00+00:00",
                "mode": "sol",
                "teammates": [{"name": "好友甲"}],
            },
            {
                "timestamp": "2026-08-01T16:00:00+00:00",
                "mode": "sol",
                "teammates": [{"name": "好友乙"}],
            },
        ]
        result = filter_matches(
            matches,
            start=datetime(2026, 8, 7, tzinfo=UTC),
            friend="好友甲",
        )
        self.assertEqual(len(result), 1)

    def test_difficulty_and_multiple_friend_filter(self):
        matches = [
            {
                "timestamp": "2026-08-07T18:00:00+08:00",
                "mode": "sol",
                "difficulty": "regular",
                "teammates": [{"name": "好友甲"}],
            },
            {
                "timestamp": "2026-08-07T19:00:00+08:00",
                "mode": "sol",
                "difficulty": "confidential",
                "teammates": [{"name": "好友乙"}],
            },
            {
                "timestamp": "2026-08-07T20:00:00+08:00",
                "mode": "sol",
                "difficulty": "top_secret",
                "teammates": [{"name": "路人"}],
            },
        ]
        result = filter_matches(
            matches,
            difficulty="confidential",
            friends=["好友甲", "好友乙"],
        )
        self.assertEqual([match["difficulty"] for match in result], ["confidential"])

    def test_multiple_friend_filter_supports_any_and_all_casefolded(self):
        matches = [
            {
                "timestamp": "2026-08-07T18:00:00+00:00",
                "mode": "sol",
                "teammates": [{"name": "Alpha"}],
            },
            {
                "timestamp": "2026-08-07T19:00:00+00:00",
                "mode": "sol",
                "teammates": [{"name": "BRAVO"}],
            },
            {
                "timestamp": "2026-08-07T20:00:00+00:00",
                "mode": "sol",
                "teammates": [{"name": "alpha"}, {"name": "Bravo"}],
            },
        ]
        friends = ["ALPHA", "alpha", "bravo"]

        any_matches = filter_matches(matches, friends=friends)
        all_matches = filter_matches(matches, friends=friends, friend_mode="all")
        any_single = filter_matches(matches, friends=["alpha"], friend_mode="any")
        all_single = filter_matches(matches, friends=["ALPHA"], friend_mode="all")

        self.assertEqual(len(any_matches), 3)
        self.assertEqual([match["timestamp"] for match in all_matches], [
            "2026-08-07T20:00:00+00:00"
        ])
        self.assertEqual(any_single, all_single)
        with self.assertRaises(ValueError):
            filter_matches(matches, friends=friends, friend_mode="either")

    def test_time_ranges_are_or_filters_without_including_the_gap(self):
        matches = [
            {
                "timestamp": "2026-08-07T10:00:00+00:00",
                "mode": "sol",
                "teammates": [],
            },
            {
                "timestamp": "2026-08-07T12:00:00+00:00",
                "mode": "sol",
                "teammates": [],
            },
            {
                "timestamp": "2026-08-07T14:00:00+00:00",
                "mode": "sol",
                "teammates": [],
            },
        ]
        first = datetime(2026, 8, 7, 10, tzinfo=UTC)
        last = datetime(2026, 8, 7, 14, tzinfo=UTC)

        result = filter_matches(
            matches,
            time_ranges=[
                (first - timedelta(minutes=15), first + timedelta(minutes=15)),
                (last - timedelta(minutes=15), last + timedelta(minutes=15)),
            ],
        )

        self.assertEqual(
            [match["timestamp"] for match in result],
            ["2026-08-07T10:00:00+00:00", "2026-08-07T14:00:00+00:00"],
        )
        self.assertEqual(filter_matches(matches, time_ranges=[]), [])

    def test_map_filter_is_independent_from_difficulty(self):
        matches = [
            {
                "timestamp": "2026-08-07T18:00:00+08:00",
                "mode": "sol",
                "map_name": "零号大坝-常规",
                "difficulty": "regular",
                "teammates": [],
            },
            {
                "timestamp": "2026-08-07T19:00:00+08:00",
                "mode": "sol",
                "map_name": "零号大坝-机密",
                "difficulty": "confidential",
                "teammates": [],
            },
            {
                "timestamp": "2026-08-07T20:00:00+08:00",
                "mode": "sol",
                "map_name": "巴克什-机密",
                "difficulty": "confidential",
                "teammates": [],
            },
        ]
        result = filter_matches(matches, map_name="零号大坝", difficulty="confidential")
        self.assertEqual([match["map_name"] for match in result], ["零号大坝-机密"])
        self.assertEqual(map_options(matches, "sol"), ["零号大坝", "巴克什"])
        self.assertEqual(base_map_name("零号大坝-永夜"), "零号大坝")

    def test_detect_sessions_uses_match_end_and_sixty_minute_gap(self):
        matches = [
            {"timestamp": "2026-08-07T21:00:00+08:00", "duration_seconds": 900},
            {"timestamp": "2026-08-07T18:30:00+08:00", "duration_seconds": 600},
            {"timestamp": "2026-08-07T18:00:00+08:00", "duration_seconds": 1200},
        ]
        sessions = detect_sessions(matches)
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0]["matches"], 1)
        self.assertEqual(sessions[1]["matches"], 2)
        self.assertEqual(sessions[1]["id"], "2026-08-07T18:00:00+08:00")
        self.assertEqual(sessions[1]["from"], "2026-08-07T18:00:00+08:00")
        self.assertEqual(sessions[1]["to"], "2026-08-07T18:40:00+08:00")

    def test_summary_includes_official_income_and_extractions(self):
        matches = [
            {
                "mode": "sol",
                "net_profit": 75,
                "gross_income": 100,
                "income_loaded": True,
                "kills": 2,
                "deaths": 0,
                "ai_kills": 3,
                "result_code": 1,
            },
            {
                "mode": "sol",
                "net_profit": -40,
                "gross_income": 10,
                "income_loaded": True,
                "kills": 1,
                "deaths": 1,
                "ai_kills": 2,
                "result_code": 0,
            },
        ]
        totals = summary(matches)
        self.assertEqual(totals["net_profit"], 35)
        self.assertEqual(totals["gross_income"], 110)
        self.assertEqual(totals["extractions"], 1)
        self.assertEqual(totals["income_loaded"], 2)
        self.assertEqual(totals["deaths"], 1)
        self.assertEqual(totals["average_kd"], 3.0)

    def test_mp_deaths_and_kd_ignore_unknown_legacy_rows(self):
        match = normalize_list_row(
            {
                "RoomId": "mp-room",
                "dtEventTime": "2026-08-07 16:00:00",
                "MapID": 1001,
                "MatchResult": 1,
                "KillNum": 12,
                "Death": 4,
                "Assist": 6,
                "gametime": 900,
            },
            "mp",
        )
        self.assertEqual(match["deaths"], 4)
        self.assertEqual(match["assists"], 6)

        totals = summary([match, {"mode": "mp", "kills": 99, "deaths": None}])
        self.assertEqual(totals["kd_matches"], 1)
        self.assertEqual(totals["kd_kills"], 12)
        self.assertEqual(totals["deaths"], 4)
        self.assertEqual(totals["average_kd"], 3.0)

    def test_mp_list_normalization_preserves_warfare_fields_and_zeroes(self):
        match = normalize_list_row(
            {
                "RoomId": "mp-room",
                "dtEventTime": "2026-08-07 16:00:00",
                "MapID": "311",
                "MatchResult": 1,
                "KillNum": 0,
                "Death": 0,
                "Assist": 0,
                "TotalScore": 0,
                "RescueTeammateCount": 0,
                "KillPlayer": 0,
                "KilledByPlayer": 0,
                "ArmedForceId": 10010,
                "RoleId": "role-id",
                "gametime": 0,
            },
            "mp",
        )

        self.assertEqual(match["map_name"], "乌姆斯运河-攻防")
        self.assertEqual(match["ruleset"], "攻防")
        self.assertEqual(base_map_name(match["map_name"]), "乌姆斯运河")
        self.assertEqual(match["kills"], 0)
        self.assertEqual(match["deaths"], 0)
        self.assertEqual(match["assists"], 0)
        self.assertEqual(match["total_score"], 0)
        self.assertEqual(match["rescues"], 0)
        self.assertEqual(match["kill_player"], 0)
        self.assertEqual(match["killed_by_player"], 0)
        self.assertEqual(match["operator"], "10010")
        self.assertEqual(match["operator_name"], "威龙")
        self.assertEqual(match["role_id"], "role-id")
        self.assertEqual(match["game_duration"], 0)
        self.assertEqual(match["duration_seconds"], 0)
        self.assertIsNone(match["rank_points"])
        self.assertIsNone(match["side"])
        self.assertEqual(match["warfare_players"], [])
        self.assertIsNone(match["ai_kills"])
        self.assertIsNone(match["net_profit"])

    def test_mp_list_normalization_keeps_absent_fields_unknown(self):
        match = normalize_list_row(
            {
                "RoomId": "mp-room",
                "dtEventTime": "2026-08-07 16:00:00",
            },
            "mp",
        )

        for field in (
            "map_id",
            "result_code",
            "kills",
            "deaths",
            "assists",
            "total_score",
            "rescues",
            "kill_player",
            "killed_by_player",
            "operator",
            "role_id",
            "game_duration",
            "rank_points",
            "side",
            "ruleset",
        ):
            with self.subTest(field=field):
                self.assertIsNone(match[field])
        self.assertEqual(match["result"], "未知")

    def test_mp_detail_includes_assists_for_self_and_teammates(self):
        match = {"mode": "mp", "assists": None}
        payload = {
            "jData": {
                "data": {
                    "data": {
                        "mpDetailList": [
                            {
                                "isTeamMember": True,
                                "isCurrentUser": True,
                                "killNum": "8",
                                "death": "2",
                                "assist": "11",
                                "totalScore": "12000",
                                "rescueTeammateCount": 0,
                                "rank": "450",
                                "color": 1,
                                "armedForceType": 10010,
                                "gameTime": 900,
                                "mapID": 311,
                                "matchResult": 1,
                                "startTime": "1786118400",
                            },
                            {
                                "isTeamMember": True,
                                "isCurrentUser": False,
                                "nickName": "好友甲",
                                "killNum": 4,
                                "death": 1,
                                "assist": 0,
                                "totalScore": 0,
                                "rescueTeammateCount": 0,
                                "rank": 0,
                                "color": 1,
                                "armedForceType": 40005,
                                "gameTime": 0,
                                "mapID": 311,
                                "matchResult": 1,
                                "startTime": "1786118400",
                            },
                            {
                                "isTeamMember": False,
                                "isCurrentUser": False,
                                "nickName": "其他队伍",
                                "assist": 99,
                                "color": 2,
                            },
                        ]
                    }
                }
            }
        }

        apply_mp_detail(match, payload)

        self.assertEqual(match["assists"], 11)
        self.assertEqual(match["total_score"], 12000)
        self.assertEqual(match["rescues"], 0)
        self.assertEqual(match["rank_points"], 450)
        self.assertEqual(match["side"], 1)
        self.assertEqual(match["side_label"], "进攻")
        self.assertEqual(match["operator"], "10010")
        self.assertEqual(match["operator_name"], "威龙")
        self.assertEqual(match["game_duration"], 900)
        self.assertEqual(match["ruleset"], "攻防")
        self.assertEqual(match["assist_stats_version"], 1)
        self.assertEqual(match["teammates"][0]["assists"], 0)
        self.assertEqual(match["teammates"][0]["total_score"], 0)
        self.assertEqual(match["teammates"][0]["rank_points"], 0)
        self.assertEqual(match["teammates"][0]["operator_name"], "露娜")
        self.assertEqual(len(match["teammates"]), 1)
        self.assertEqual(len(match["warfare_players"]), 3)
        self.assertEqual(
            [player["relationship"] for player in match["warfare_players"]],
            ["self", "teammate", "other"],
        )
        other = match["warfare_players"][2]
        self.assertEqual(other["assists"], 99)
        self.assertIsNone(other["kills"])
        self.assertIsNone(other["total_score"])
        self.assertEqual(other["side_label"], "防守")
        self.assertEqual(other["ruleset"], "攻防")

    def test_mp_detail_requires_complete_team_assists_before_marking_version(self):
        incomplete_rows = (
            [
                {
                    "isTeamMember": True,
                    "isCurrentUser": True,
                    "killNum": 8,
                    "death": 2,
                }
            ],
            [
                {
                    "isTeamMember": True,
                    "isCurrentUser": True,
                    "killNum": 8,
                    "death": 2,
                    "assist": 1,
                },
                {
                    "isTeamMember": True,
                    "isCurrentUser": False,
                    "nickName": "好友甲",
                    "killNum": 4,
                    "death": 1,
                },
            ],
        )
        for rows in incomplete_rows:
            with self.subTest(rows=rows):
                match = {"mode": "mp", "assists": None, "assist_stats_version": 0}
                payload = {"jData": {"data": {"data": {"mpDetailList": rows}}}}

                apply_mp_detail(match, payload)

                self.assertEqual(match["assist_stats_version"], 0)
                self.assertIsNone(match["warfare_players"][-1]["assists"])

        with self.assertRaisesRegex(ValueError, "current player"):
            apply_mp_detail(
                {"mode": "mp"},
                {"jData": {"data": {"data": {"mpDetailList": []}}}},
            )

    def test_warfare_summary_uses_only_mp_known_values(self):
        matches = [
            {
                "mode": "mp",
                "time": "2026-08-07 18:00:00",
                "timestamp": "2026-08-07T18:00:00+08:00",
                "result_code": 1,
                "total_score": 1000,
                "kills": 10,
                "deaths": 2,
                "assists": 5,
                "rescues": 1,
                "rank_points": 100,
                "side": 1,
                "operator": "10010",
                "game_duration": 600,
                "warfare_players": [{"relationship": "self"}],
                "warfare_stats_version": 1,
            },
            {
                "mode": "mp",
                "time": "2026-08-07 19:00:00",
                "timestamp": "2026-08-07T19:00:00+08:00",
                "result_code": 2,
                "total_score": 0,
                "kills": 0,
                "deaths": 0,
                "assists": 0,
                "rescues": 0,
                "rank_points": 120,
                "side": 2,
                "operator": "10010",
                "game_duration": 0,
                "warfare_players": [],
                "warfare_stats_version": 1,
            },
            {
                "mode": "mp",
                "time": "2026-08-07 20:00:00",
                "timestamp": "2026-08-07T20:00:00+08:00",
            },
            {
                "mode": "sol",
                "result_code": 1,
                "total_score": 999999,
                "kills": 999,
                "deaths": 1,
                "assists": 999,
                "rescues": 999,
                "rank_points": 999,
                "game_duration": 60,
            },
        ]

        totals = warfare_summary(matches)

        self.assertEqual(totals["matches"], 3)
        self.assertEqual(totals["wins"], 1)
        self.assertEqual(totals["losses"], 1)
        self.assertEqual(totals["unknown_results"], 1)
        self.assertEqual(totals["known_results"], 2)
        self.assertEqual(totals["win_rate"], 50.0)
        self.assertEqual(totals["total_score"], 1000)
        self.assertEqual(totals["average_score"], 500.0)
        self.assertEqual(totals["kills"], 10)
        self.assertEqual(totals["average_kills"], 5.0)
        self.assertEqual(totals["deaths"], 2)
        self.assertEqual(totals["assists"], 5)
        self.assertEqual(totals["rescues"], 1)
        self.assertEqual(totals["kd"], 5.0)
        self.assertEqual(totals["kda"], 7.5)
        self.assertEqual(totals["game_duration"], 600)
        self.assertEqual(totals["average_game_duration"], 300.0)
        self.assertEqual(totals["kills_per_minute"], 1.0)
        self.assertEqual(totals["score_per_minute"], 100.0)
        self.assertEqual(totals["rank_points_latest"], 120)
        self.assertEqual(totals["rank_points_earliest"], 100)
        self.assertEqual(totals["rank_points_change"], 20)
        self.assertEqual(
            totals["rank_points_trend"],
            [
                {"time": "2026-08-07T18:00:00+08:00", "rank_points": 100},
                {"time": "2026-08-07T19:00:00+08:00", "rank_points": 120},
            ],
        )
        self.assertEqual(totals["side_counts"], {"attack": 1, "defense": 1, "unknown": 1})
        self.assertEqual(
            totals["operator_counts"],
            [{"operator": "10010", "operator_name": "威龙", "matches": 2}],
        )
        self.assertEqual(totals["coverage"]["total_score"], 2)
        self.assertEqual(totals["coverage"]["rank_points"], 2)
        self.assertEqual(totals["coverage"]["warfare_players"], 2)

    def test_warfare_summary_does_not_turn_legacy_unknowns_into_zero(self):
        totals = warfare_summary(
            [
                {
                    "mode": "mp",
                    "time": "2026-08-07 18:00:00",
                    "timestamp": "2026-08-07T18:00:00+08:00",
                }
            ]
        )

        for field in (
            "win_rate",
            "total_score",
            "average_score",
            "kills",
            "deaths",
            "assists",
            "rescues",
            "kd",
            "kda",
            "game_duration",
            "kills_per_minute",
            "score_per_minute",
            "rank_points_latest",
            "rank_points_change",
        ):
            with self.subTest(field=field):
                self.assertIsNone(totals[field])
        self.assertEqual(totals["unknown_results"], 1)
        self.assertEqual(totals["coverage"]["assists"], 0)

    def test_warfare_rates_ignore_zero_duration_rows(self):
        totals = warfare_summary(
            [
                {
                    "mode": "mp",
                    "kills": 10,
                    "total_score": 1000,
                    "game_duration": 600,
                },
                {
                    "mode": "mp",
                    "kills": 100,
                    "total_score": 5000,
                    "game_duration": 0,
                },
            ]
        )

        self.assertEqual(totals["kills_per_minute"], 1.0)
        self.assertEqual(totals["score_per_minute"], 100.0)

    def test_warfare_rank_change_requires_two_timed_points(self):
        totals = warfare_summary(
            [
                {
                    "mode": "mp",
                    "timestamp": "2026-08-07T18:00:00+08:00",
                    "rank_points": 100,
                }
            ]
        )

        self.assertEqual(totals["rank_points_latest"], 100)
        self.assertEqual(totals["rank_points_earliest"], 100)
        self.assertIsNone(totals["rank_points_change"])

    def test_warfare_zero_death_ratios_are_marked_infinite(self):
        totals = warfare_summary(
            [{"mode": "mp", "kills": 5, "deaths": 0, "assists": 3}]
        )

        self.assertIsNone(totals["kd"])
        self.assertIsNone(totals["kda"])
        self.assertTrue(totals["kd_infinite"])
        self.assertTrue(totals["kda_infinite"])

    def test_warfare_friend_comparisons_use_shared_mp_matches_and_player_stats(self):
        matches = [
            {
                "mode": "mp",
                "time": "2026-08-07 18:00:00",
                "timestamp": "2026-08-07T18:00:00+08:00",
                "result_code": 1,
                "total_score": 1000,
                "kills": 8,
                "deaths": 2,
                "assists": 4,
                "rescues": 1,
                "game_duration": 600,
                "warfare_players": [
                    {
                        "name": "好友甲",
                        "is_current_user": False,
                        "is_team_member": True,
                        "relationship": "teammate",
                        "total_score": 600,
                        "kills": 4,
                        "deaths": 1,
                        "assists": 3,
                        "rescues": 0,
                        "game_duration": 600,
                    }
                ],
                "teammates": [{"name": "好友甲", "total_score": 1}],
            },
            {
                "mode": "mp",
                "time": "2026-08-07 19:00:00",
                "timestamp": "2026-08-07T19:00:00+08:00",
                "result_code": 2,
                "total_score": 500,
                "kills": 3,
                "deaths": 3,
                "assists": 1,
                "rescues": 0,
                "game_duration": 300,
                "warfare_players": [],
                "teammates": [
                    {
                        "name": "好友甲",
                        "total_score": 300,
                        "kills": 2,
                        "deaths": 2,
                        "assists": 1,
                        "rescues": 1,
                        "game_duration": 300,
                    }
                ],
            },
            {
                "mode": "mp",
                "timestamp": "2026-08-07T20:00:00+08:00",
                "total_score": 999,
                "warfare_players": [],
                "teammates": [{"name": "路人"}],
            },
            {
                "mode": "sol",
                "timestamp": "2026-08-07T21:00:00+08:00",
                "teammates": [{"name": "好友甲"}],
            },
        ]

        comparison = warfare_friend_comparisons(matches, ["好友甲"])[0]

        self.assertEqual(comparison["name"], "好友甲")
        self.assertEqual(comparison["shared_matches"], 2)
        self.assertEqual(comparison["self"]["total_score"], 1500)
        self.assertEqual(comparison["friend"]["total_score"], 900)
        self.assertEqual(comparison["friend"]["kills"], 6)
        self.assertEqual(comparison["friend"]["assists"], 4)
        self.assertEqual(comparison["friend"]["rescues"], 1)

    def test_warfare_friend_comparison_does_not_fallback_after_schema_upgrade(self):
        comparison = warfare_friend_comparisons(
            [
                {
                    "mode": "mp",
                    "warfare_players": [],
                    "warfare_stats_version": 1,
                    "teammates": [{"name": "好友甲", "kills": 99}],
                }
            ],
            ["好友甲"],
        )[0]

        self.assertEqual(comparison["shared_matches"], 0)
        self.assertEqual(comparison["self"]["matches"], 0)
        self.assertIsNone(comparison["friend"]["kills"])

    def test_summary_counts_only_available_mp_assists(self):
        totals = summary(
            [
                {"mode": "mp", "kills": 8, "deaths": 2, "assists": 11},
                {"mode": "mp", "kills": 4, "deaths": 1, "assists": 0},
                {"mode": "mp", "kills": 2, "deaths": 1},
                {"mode": "sol", "kills": 3, "deaths": 0, "assists": None},
            ]
        )

        self.assertEqual(totals["mp_matches"], 3)
        self.assertEqual(totals["assist_matches"], 2)
        self.assertEqual(totals["assists"], 11)
        self.assertEqual(totals["average_assists"], 5.5)

    def test_shared_summary_keeps_zero_and_ignores_unknown_mp_assists(self):
        totals = shared_summary(
            [
                {"mode": "mp", "kills": 4, "assists": 5},
                {"mode": "mp", "kills": 2, "assists": 0},
                {"mode": "mp", "kills": 1},
                {"mode": "sol", "kills": 3, "assists": None},
            ]
        )

        self.assertEqual(totals["mp_matches"], 3)
        self.assertEqual(totals["assist_matches"], 2)
        self.assertEqual(totals["assists"], 5)
        self.assertEqual(totals["average_assists"], 2.5)

    def test_legacy_mp_match_with_unknown_deaths_is_refreshed_once(self):
        threshold = datetime(2026, 8, 7, tzinfo=UTC)
        legacy_mp = {
            "mode": "mp",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "details_loaded": True,
            "deaths": None,
        }
        self.assertTrue(needs_detail_enrichment(legacy_mp, threshold))
        legacy_mp["deaths"] = 4
        legacy_mp["teammate_stats_complete"] = True
        legacy_mp["teammate_stats_version"] = 2
        legacy_mp["assist_stats_version"] = 1
        self.assertFalse(needs_detail_enrichment(legacy_mp, threshold))

    def test_old_mp_assist_schema_only_refetches_recent_matches(self):
        threshold = datetime(2026, 8, 7, tzinfo=UTC)
        old_match = {
            "mode": "mp",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "details_loaded": True,
            "deaths": 4,
            "teammate_stats_version": 2,
            "assist_stats_version": 0,
        }
        recent_match = {**old_match, "timestamp": "2026-08-08T00:00:00+00:00"}

        self.assertFalse(needs_detail_enrichment(old_match, threshold))
        self.assertTrue(needs_detail_enrichment(recent_match, threshold))

    def test_old_warfare_schema_only_refetches_recent_matches(self):
        threshold = datetime(2026, 8, 7, tzinfo=UTC)
        old_match = {
            "mode": "mp",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "details_loaded": True,
            "deaths": 4,
            "teammate_stats_version": 2,
            "assist_stats_version": 1,
            "warfare_stats_version": 0,
        }
        recent_match = {**old_match, "timestamp": "2026-08-08T00:00:00+00:00"}

        self.assertFalse(needs_detail_enrichment(old_match, threshold))
        self.assertTrue(needs_detail_enrichment(recent_match, threshold))

    def test_existing_mp_raw_cache_backfills_assists(self):
        match = {
            "mode": "mp",
            "room_id": "room",
            "assists": None,
            "assist_stats_version": 0,
        }
        payload = {
            "jData": {
                "data": {
                    "data": {
                        "mpDetailList": [
                            {
                                "isTeamMember": True,
                                "isCurrentUser": True,
                                "killNum": 5,
                                "death": 2,
                                "assist": 7,
                            }
                        ]
                    }
                }
            }
        }
        with TemporaryDirectory() as directory:
            raw_path = Path(directory)
            (raw_path / "battlefield-detail-room.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

            self.assertTrue(enrich_from_raw_cache(match, raw_path))

        self.assertEqual(match["assists"], 7)
        self.assertEqual(match["assist_stats_version"], 1)

    def test_invalid_mp_raw_does_not_mark_assists_complete(self):
        match = {
            "mode": "mp",
            "room_id": "room",
            "assists": None,
            "assist_stats_version": 0,
        }
        with TemporaryDirectory() as directory:
            raw_path = Path(directory)
            (raw_path / "battlefield-detail-room.json").write_text(
                json.dumps({"iRet": 999, "sMsg": "busy"}), encoding="utf-8"
            )

            self.assertFalse(enrich_from_raw_cache(match, raw_path))

        self.assertIsNone(match["assists"])
        self.assertEqual(match["assist_stats_version"], 0)

    def test_invalid_sol_raw_is_ignored_during_compatibility_backfill(self):
        malformed_payloads = (
            {"iRet": 0, "jData": {"data": {"unexpected": 1}}},
            {"iRet": 0, "jData": {"data": [None]}},
        )
        for payload in malformed_payloads:
            with self.subTest(payload=payload), TemporaryDirectory() as directory:
                match = {"mode": "sol", "room_id": "room", "net_profit": 0}
                raw_path = Path(directory)
                (raw_path / "detail-room.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )

                self.assertFalse(enrich_from_raw_cache(match, raw_path))

    def test_failed_detail_is_not_retried_before_backoff_expires(self):
        threshold = datetime(2026, 8, 7, tzinfo=UTC)
        match = {
            "mode": "sol",
            "timestamp": "2026-08-08T00:00:00+00:00",
            "details_loaded": False,
            "teammate_stats_version": 0,
            "detail_retry_after": "2099-01-01T00:00:00+00:00",
        }
        self.assertFalse(needs_detail_enrichment(match, threshold))

    def test_sol_detail_keeps_teammate_net_profit_unavailable(self):
        match = {"mode": "sol", "net_profit": 0}
        payload = {
            "jData": {
                "data": [
                    {"vopenid": True, "TeamId": "1", "KillCount": 2},
                    {
                        "vopenid": False,
                        "TeamId": "1",
                        "nickName": "好友甲",
                        "EscapeFailReason": 1,
                        "KillCount": 3,
                        "KillPlayerAICount": 1,
                        "KillAICount": 5,
                        "FinalPrice": 900,
                        "KeyChainCarryInPrice": 100,
                        "CarryoutSelfPrice": 200,
                    },
                ]
            }
        }
        apply_sol_detail(match, payload)
        teammate = match["teammates"][0]
        self.assertIsNone(teammate["net_profit"])
        self.assertEqual(teammate["gross_income"], 900)
        self.assertEqual(teammate["kills"], 4)
        self.assertEqual(teammate["deaths"], 0)
        self.assertTrue(teammate["extracted"])
        self.assertTrue(match["teammate_stats_complete"])

    def test_sol_detail_does_not_treat_every_player_as_teammate_when_self_is_missing(self):
        match = {"mode": "sol", "net_profit": 0}
        payload = {
            "jData": {
                "data": [
                    {"TeamId": "1", "nickName": "其他队伍甲"},
                    {"TeamId": "2", "nickName": "其他队伍乙"},
                ]
            }
        }
        with self.assertRaisesRegex(ValueError, "current player"):
            apply_sol_detail(match, payload)

    def test_friend_comparison_uses_the_same_shared_matches_for_both_players(self):
        matches = [
            {
                "mode": "sol",
                "timestamp": "2026-08-07T18:00:00+08:00",
                "net_profit": 100,
                "income_loaded": False,
                "result_code": 1,
                "kills": 5,
                "deaths": 0,
                "ai_kills": 0,
                "teammates": [
                    {"name": "好友甲", "gross_income": 60, "net_profit": None, "kills": 2, "deaths": 0, "ai_kills": 1, "extracted": True}
                ],
            },
            {
                "mode": "sol",
                "timestamp": "2026-08-07T19:00:00+08:00",
                "net_profit": 200,
                "income_loaded": False,
                "result_code": 2,
                "kills": 4,
                "deaths": 1,
                "ai_kills": 0,
                "teammates": [
                    {"name": "好友甲", "gross_income": 120, "net_profit": None, "kills": 3, "deaths": 1, "ai_kills": 0, "extracted": False}
                ],
            },
        ]
        comparison = friend_comparisons(matches, ["好友甲"])[0]
        self.assertEqual(comparison["self"]["net_profit"], 300)
        self.assertIsNone(comparison["friend"]["net_profit"])
        self.assertEqual(comparison["friend"]["gross_income"], 180)
        self.assertEqual(comparison["self"]["average_kd"], 9.0)
        self.assertEqual(comparison["friend"]["average_kd"], 5.0)
        self.assertEqual(comparison["friend"]["extractions"], 1)

    def test_friend_comparison_includes_mp_assists(self):
        matches = [
            {
                "mode": "mp",
                "timestamp": "2026-08-07T18:00:00+08:00",
                "net_profit": 0,
                "income_loaded": False,
                "result_code": 1,
                "kills": 8,
                "deaths": 2,
                "assists": 9,
                "ai_kills": 0,
                "teammates": [
                    {
                        "name": "好友甲",
                        "gross_income": None,
                        "net_profit": None,
                        "kills": 4,
                        "deaths": 1,
                        "assists": 3,
                        "ai_kills": 0,
                        "extracted": None,
                    }
                ],
            }
        ]

        comparison = friend_comparisons(matches, ["好友甲"])[0]

        self.assertEqual(comparison["self"]["assists"], 9)
        self.assertEqual(comparison["self"]["assist_matches"], 1)
        self.assertEqual(comparison["friend"]["assists"], 3)
        self.assertEqual(comparison["friend"]["average_assists"], 3.0)

    def test_cached_map_images_are_local(self):
        with TemporaryDirectory() as directory:
            cache_path = Path(directory) / "matches.json"
            write_cache(
                {
                    "matches": [
                        {
                            "id": "sol:room:2026-08-07 16:00:00",
                            "time": "2026-08-07 16:00:00",
                            "mode": "sol",
                            "map_id": "2202",
                            "map_image": "https://example.invalid/remote-map.png",
                            "result_code": 1,
                        }
                    ]
                },
                path=cache_path,
            )

            matches = read_cache(path=cache_path)["matches"]

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["map_image"], "/assets/lhdb-jimi.png")
        self.assertTrue(matches[0]["timestamp"].startswith("2026-08-07T16:00:00"))

    def test_remote_player_identity_is_stable_and_not_openid(self):
        first = player_identity({"openid": "secret-account-id"})
        second = player_identity({"openid": "secret-account-id"})
        self.assertEqual(first, second)
        self.assertNotIn("secret-account-id", first)
        self.assertEqual(len(first), 24)

    @patch("remote_sync.requests.post")
    @patch("remote_sync.load_auth")
    @patch("remote_sync.read_json")
    @patch("remote_sync.exists")
    def test_remote_upload_rejects_cache_owned_by_another_account(
        self, exists_mock, read_json_mock, load_auth_mock, post_mock
    ):
        exists_mock.return_value = True
        read_json_mock.return_value = {
            "url": "https://example.invalid",
            "token": "token",
            "display_name": "玩家",
        }
        load_auth_mock.return_value = {"openid": "current", "acctype": "mini"}

        with self.assertRaisesRegex(ValueError, "其他账号"):
            upload_cache({"account_id": "another-account", "matches": []})

        post_mock.assert_not_called()

    @patch("remote_sync.requests.post")
    @patch("remote_sync.load_auth")
    @patch("remote_sync.read_json")
    @patch("remote_sync.exists")
    def test_remote_upload_accepts_empty_cache_owned_by_current_account(
        self, exists_mock, read_json_mock, load_auth_mock, post_mock
    ):
        auth = {"openid": "current", "acctype": "qc"}
        exists_mock.return_value = True
        read_json_mock.return_value = {
            "url": "https://example.invalid",
            "token": "token",
            "display_name": "玩家",
        }
        load_auth_mock.return_value = auth
        post_mock.return_value.json.return_value = {"ok": True}

        with TemporaryDirectory() as directory:
            empty = read_cache(auth, app_dir=Path(directory))
            result = upload_cache(empty)

        self.assertTrue(result["ok"])
        self.assertEqual(empty["account_id"], account_id(auth))
        self.assertEqual(post_mock.call_args.kwargs["json"]["matches"], [])

    def test_shared_store_keeps_players_isolated(self):
        match = {
            "id": "sol:room:2026-08-07 16:00:00",
            "timestamp": "2026-08-07T16:00:00+08:00",
            "mode": "sol",
        }
        with TemporaryDirectory() as directory:
            store = SharedStore(Path(directory) / "shared.sqlite3")
            store.upload({"player_id": "a" * 24, "display_name": "玩家甲", "matches": [match]})
            store.upload({"player_id": "b" * 24, "display_name": "玩家乙", "matches": []})
            self.assertEqual(len(store.players()), 2)
            self.assertEqual(len(store.matches("a" * 24)), 1)
            self.assertEqual(store.matches("b" * 24), [])

    def test_launcher_compares_numeric_versions(self):
        self.assertGreater(version_key("1.2.0"), version_key("1.1.9"))
        self.assertEqual(version_key("invalid"), (0,))

    def test_corrupt_cache_is_quarantined_and_can_be_resynchronized(self):
        with TemporaryDirectory() as directory:
            cache_path = Path(directory) / "matches.json"
            cache_path.write_text("not-json", encoding="utf-8")
            cache = read_cache(path=cache_path)

            self.assertEqual(cache["matches"], [])
            self.assertIn("warning", cache)
            self.assertFalse(cache_path.exists())
            self.assertEqual(len(list(Path(directory).glob("matches.corrupt-*.json"))), 1)

    def test_structurally_corrupt_cache_is_quarantined(self):
        with TemporaryDirectory() as directory:
            cache_path = Path(directory) / "matches.json"
            cache_path.write_text('{"matches": [1]}', encoding="utf-8")

            cache = read_cache(path=cache_path)
            recovered = read_cache(path=cache_path)

            self.assertEqual(cache["matches"], [])
            self.assertIn("warning", cache)
            self.assertEqual(recovered, {"updated_at": None, "matches": []})
            self.assertFalse(cache_path.exists())
            self.assertEqual(len(list(Path(directory).glob("matches.corrupt-*.json"))), 1)

    @patch("delta_data.migrate_legacy_data")
    @patch("delta_data.load_auth", side_effect=ValueError("missing auth"))
    def test_missing_auth_does_not_read_legacy_global_cache(
        self, load_auth_mock, migrate_legacy_mock
    ):
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            legacy_cache = app_dir / "cache" / "matches.json"
            legacy_cache.parent.mkdir(parents=True)
            legacy_cache.write_text(
                json.dumps(
                    {
                        "matches": [
                            {
                                "id": "legacy",
                                "time": "2026-08-07 16:00:00",
                                "mode": "sol",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            cache = read_cache(app_dir=app_dir)

            self.assertEqual(cache, {"updated_at": None, "matches": []})
            self.assertTrue(legacy_cache.is_file())
            load_auth_mock.assert_called_once_with(app_dir=app_dir)
            migrate_legacy_mock.assert_not_called()

    def test_explicit_auth_migrates_legacy_cache_into_account_scope(self):
        auth = {"openid": "legacy-owner", "acctype": "mini"}
        legacy_match = {
            "id": "sol:legacy-room:2026-08-07 16:00:00",
            "time": "2026-08-07 16:00:00",
            "mode": "sol",
        }
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            legacy_cache = app_dir / "cache" / "matches.json"
            legacy_cache.parent.mkdir(parents=True)
            legacy_cache.write_text(
                json.dumps({"updated_at": None, "matches": [legacy_match]}),
                encoding="utf-8",
            )

            cache = read_cache(auth, app_dir=app_dir)

            scoped_cache = account_cache_file(auth, app_dir=app_dir)
            self.assertEqual(cache["matches"][0]["id"], legacy_match["id"])
            self.assertEqual(cache["account_id"], account_id(auth))
            self.assertTrue(scoped_cache.is_file())
            self.assertTrue(legacy_cache.is_file())

    def test_sync_preserves_detail_net_profit_when_list_temporarily_returns_zero(self):
        auth = {"openid": "qq-user", "acctype": "qc"}
        cached_match = {
            "id": "sol:room:2026-08-07 16:00:00",
            "room_id": "room",
            "mode": "sol",
            "time": "2026-08-07 16:00:00",
            "timestamp": "2026-08-07T16:00:00+08:00",
            "net_profit": 123456,
            "gross_income": 200000,
            "income_loaded": True,
            "teammates": [],
            "details_loaded": True,
            "teammate_stats_complete": True,
            "teammate_stats_version": 2,
        }
        sol_page = {
            "jData": {
                "data": [
                    {
                        "RoomId": "room",
                        "dtEventTime": "2026-08-07 16:00:00",
                        "MapId": "2201",
                        "EscapeFailReason": 1,
                        "flowCalGainedPrice": 0,
                    }
                ]
            }
        }
        empty_page = {"jData": {"data": []}}

        with TemporaryDirectory() as directory:
            with (
                patch("delta_data.migrate_legacy_data"),
                patch("delta_data.DeltaAmsClient") as client_class,
                patch("delta_data.read_cache", return_value={"matches": [cached_match]}),
                patch("delta_data.account_raw_dir", return_value=Path(directory)),
                patch("delta_data.write_cache") as write_cache_mock,
            ):
                client = client_class.return_value
                client.activity.return_value = {}
                client.fetch_match_page.side_effect = [sol_page, empty_page]

                result = sync_matches(pages=1, workers=1, auth=auth)

        self.assertEqual(result["matches"][0]["net_profit"], 123456)
        self.assertTrue(result["matches"][0]["details_loaded"])
        client.fetch_match_detail.assert_not_called()
        write_cache_mock.assert_called_once()

    def test_sync_preserves_cached_mp_assists(self):
        auth = {"openid": "qq-user", "acctype": "qc"}
        cached_match = {
            "id": "mp:room:2026-08-07 16:00:00",
            "room_id": "room",
            "mode": "mp",
            "time": "2026-08-07 16:00:00",
            "timestamp": "2026-08-07T16:00:00+08:00",
            "kills": 8,
            "deaths": 2,
            "assists": 11,
            "assist_stats_version": 1,
            "total_score": 12000,
            "rescues": 2,
            "rank_points": 450,
            "side": 1,
            "side_label": "进攻",
            "operator": "10010",
            "operator_name": "威龙",
            "game_duration": 900,
            "ruleset": "攻防",
            "warfare_players": [
                {
                    "name": "好友甲",
                    "relationship": "teammate",
                    "assists": 0,
                    "total_score": 5000,
                }
            ],
            "warfare_stats_version": 1,
            "teammates": [{"name": "好友甲", "assists": 0, "total_score": 5000}],
            "details_loaded": True,
            "teammate_stats_complete": True,
            "teammate_stats_version": 2,
        }
        mp_page = {
            "jData": {
                "data": [
                    {
                        "RoomId": "room",
                        "dtEventTime": "2026-08-07 16:00:00",
                        "MapID": "33",
                        "MatchResult": 1,
                        "KillNum": 8,
                        "Death": 2,
                        "gametime": 900,
                    }
                ]
            }
        }
        empty_page = {"jData": {"data": []}}

        with TemporaryDirectory() as directory:
            with (
                patch("delta_data.migrate_legacy_data"),
                patch("delta_data.DeltaAmsClient") as client_class,
                patch("delta_data.read_cache", return_value={"matches": [cached_match]}),
                patch("delta_data.account_raw_dir", return_value=Path(directory)),
                patch("delta_data.write_cache") as write_cache_mock,
            ):
                client = client_class.return_value
                client.activity.return_value = {}
                client.fetch_match_page.side_effect = [empty_page, mp_page]

                result = sync_matches(pages=1, workers=1, auth=auth)

        match = result["matches"][0]
        self.assertEqual(match["assists"], 11)
        self.assertEqual(match["assist_stats_version"], 1)
        self.assertEqual(match["teammates"][0]["assists"], 0)
        self.assertEqual(match["total_score"], 12000)
        self.assertEqual(match["rescues"], 2)
        self.assertEqual(match["rank_points"], 450)
        self.assertEqual(match["side"], 1)
        self.assertEqual(match["operator"], "10010")
        self.assertEqual(match["game_duration"], 900)
        self.assertEqual(match["warfare_players"][0]["total_score"], 5000)
        self.assertEqual(match["warfare_stats_version"], 1)
        write_cache_mock.assert_called_once()

    def test_sync_uses_a_separate_recent_window_for_missing_mp_assists(self):
        auth = {"openid": "qq-user", "acctype": "qc"}
        cached_match = {
            "id": "mp:old-room:2025-01-01 00:00:00",
            "room_id": "old-room",
            "mode": "mp",
            "time": "2025-01-01 00:00:00",
            "timestamp": "2025-01-01T00:00:00+08:00",
            "kills": 8,
            "deaths": 2,
            "assists": None,
            "assist_stats_version": 0,
            "teammates": [],
            "details_loaded": True,
            "teammate_stats_complete": True,
            "teammate_stats_version": 2,
        }
        empty_page = {"jData": {"data": []}}

        with TemporaryDirectory() as directory:
            with (
                patch("delta_data.migrate_legacy_data"),
                patch("delta_data.DeltaAmsClient") as client_class,
                patch("delta_data.read_cache", return_value={"matches": [cached_match]}),
                patch("delta_data.account_raw_dir", return_value=Path(directory)),
                patch(
                    "delta_data.enrich_one",
                    side_effect=lambda _auth, _activity, match: match,
                ) as enrich_mock,
                patch("delta_data.write_cache"),
            ):
                client = client_class.return_value
                client.activity.return_value = {}
                client.fetch_match_page.side_effect = [empty_page, empty_page]

                sync_matches(
                    pages=1,
                    detail_days=3650,
                    assist_days=7,
                    workers=1,
                    auth=auth,
                )

        enrich_mock.assert_not_called()

    def test_read_cache_backfills_mp_assists_from_raw_without_network(self):
        auth = {"openid": "wechat-user", "acctype": "mini"}
        payload = {
            "jData": {
                "data": {
                    "data": {
                        "mpDetailList": [
                            {
                                "isTeamMember": True,
                                "isCurrentUser": True,
                                "killNum": 5,
                                "death": 2,
                                "assist": 7,
                            }
                        ]
                    }
                }
            }
        }
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            write_cache(
                {
                    "matches": [
                        {
                            "id": "mp:room:2026-08-07 16:00:00",
                            "room_id": "room",
                            "mode": "mp",
                            "time": "2026-08-07 16:00:00",
                            "assists": None,
                            "assist_stats_version": 0,
                        }
                    ]
                },
                auth=auth,
                app_dir=app_dir,
            )
            account_raw = raw_dir(auth, app_dir=app_dir)
            account_raw.mkdir(parents=True)
            (account_raw / "battlefield-detail-room.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

            loaded = read_cache(auth, app_dir=app_dir)
            stored = json.loads(
                account_cache_file(auth, app_dir=app_dir).read_text(encoding="utf-8")
            )

        self.assertEqual(loaded["matches"][0]["assists"], 7)
        self.assertEqual(loaded["matches"][0]["assist_stats_version"], 1)
        self.assertIsNone(stored["matches"][0]["assists"])
        self.assertEqual(stored["matches"][0]["assist_stats_version"], 0)

    def test_read_cache_backfills_new_warfare_schema_from_existing_raw(self):
        auth = {"openid": "wechat-user", "acctype": "mini"}
        payload = {
            "jData": {
                "data": {
                    "data": {
                        "mpDetailList": [
                            {
                                "isTeamMember": True,
                                "isCurrentUser": True,
                                "nickName": "本人",
                                "killNum": 5,
                                "death": 2,
                                "assist": 7,
                                "totalScore": 12000,
                                "rescueTeammateCount": 1,
                                "rank": 450,
                                "color": 1,
                                "armedForceType": 10010,
                                "gameTime": 900,
                                "mapID": 311,
                                "matchResult": 1,
                                "startTime": "1786118400",
                            }
                        ]
                    }
                }
            }
        }
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            write_cache(
                {
                    "matches": [
                        {
                            "id": "mp:room:2026-08-07 16:00:00",
                            "room_id": "room",
                            "mode": "mp",
                            "time": "2026-08-07 16:00:00",
                            "deaths": 2,
                            "assists": 7,
                            "assist_stats_version": 1,
                            "teammate_stats_version": 2,
                        }
                    ]
                },
                auth=auth,
                app_dir=app_dir,
            )
            account_raw = raw_dir(auth, app_dir=app_dir)
            account_raw.mkdir(parents=True)
            (account_raw / "battlefield-detail-room.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )

            loaded = read_cache(auth, app_dir=app_dir)
            stored = json.loads(
                account_cache_file(auth, app_dir=app_dir).read_text(encoding="utf-8")
            )

        match = loaded["matches"][0]
        self.assertEqual(match["warfare_stats_version"], 1)
        self.assertEqual(match["total_score"], 12000)
        self.assertEqual(match["rank_points"], 450)
        self.assertEqual(match["ruleset"], "攻防")
        self.assertEqual(len(match["warfare_players"]), 1)
        self.assertNotIn("warfare_stats_version", stored["matches"][0])

    def test_read_cache_repairs_legacy_warfare_placeholders_without_fake_zeroes(self):
        with TemporaryDirectory() as directory:
            cache_path = Path(directory) / "matches.json"
            write_cache(
                {
                    "matches": [
                        {
                            "id": "mp:room:2026-08-07 16:00:00",
                            "room_id": "room",
                            "mode": "mp",
                            "time": "2026-08-07 16:00:00",
                            "map_id": 311,
                            "map_name": "地图 311",
                            "ai_kills": 0,
                            "net_profit": 0,
                            "gross_income": 0,
                            "warfare_stats_version": 1,
                            "warfare_players": [
                                {
                                    "name": "本人",
                                    "map_id": 311,
                                    "map_name": "地图 311",
                                    "ruleset": "占领",
                                    "is_current_user": True,
                                    "is_team_member": True,
                                }
                            ],
                            "teammates": [],
                        }
                    ]
                },
                path=cache_path,
            )

            match = read_cache(path=cache_path)["matches"][0]

        self.assertEqual(match["map_name"], "乌姆斯运河-攻防")
        self.assertEqual(match["ruleset"], "攻防")
        self.assertIsNone(match["ai_kills"])
        self.assertIsNone(match["net_profit"])
        self.assertIsNone(match["gross_income"])
        self.assertEqual(match["warfare_players"][0]["map_name"], "乌姆斯运河-攻防")
        self.assertEqual(match["warfare_players"][0]["ruleset"], "攻防")

    def test_read_cache_ignores_malformed_success_mp_raw(self):
        auth = {"openid": "wechat-user", "acctype": "mini"}
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            write_cache(
                {
                    "matches": [
                        {
                            "id": "mp:room:2026-08-07 16:00:00",
                            "room_id": "room",
                            "mode": "mp",
                            "time": "2026-08-07 16:00:00",
                            "assists": None,
                            "assist_stats_version": 0,
                        }
                    ]
                },
                auth=auth,
                app_dir=app_dir,
            )
            account_raw = raw_dir(auth, app_dir=app_dir)
            account_raw.mkdir(parents=True)
            (account_raw / "battlefield-detail-room.json").write_text(
                json.dumps({"iRet": 0, "jData": {"data": {"data": None}}}),
                encoding="utf-8",
            )

            loaded = read_cache(auth, app_dir=app_dir)

        self.assertIsNone(loaded["matches"][0]["assists"])
        self.assertEqual(loaded["matches"][0]["assist_stats_version"], 0)

    def test_local_match_caches_are_isolated_by_account(self):
        wechat = {"openid": "wechat-user", "acctype": "mini"}
        qq = {"openid": "qq-user", "acctype": "qc"}
        second_qq = {"openid": "second-qq-user", "acctype": "qc"}
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            write_cache(
                {
                    "matches": [
                        {"id": "wechat", "time": "2026-08-07 16:00:00", "mode": "sol"}
                    ]
                },
                auth=wechat,
                app_dir=app_dir,
            )
            write_cache(
                {
                    "matches": [
                        {"id": "qq", "time": "2026-08-07 16:00:00", "mode": "sol"}
                    ]
                },
                auth=qq,
                app_dir=app_dir,
            )
            write_cache(
                {
                    "matches": [
                        {
                            "id": "second-qq",
                            "time": "2026-08-07 16:00:00",
                            "mode": "sol",
                        }
                    ]
                },
                auth=second_qq,
                app_dir=app_dir,
            )

            self.assertEqual(read_cache(wechat, app_dir=app_dir)["matches"][0]["id"], "wechat")
            self.assertEqual(read_cache(qq, app_dir=app_dir)["matches"][0]["id"], "qq")
            self.assertEqual(
                read_cache(second_qq, app_dir=app_dir)["matches"][0]["id"],
                "second-qq",
            )

    def test_local_raw_files_are_isolated_by_account_for_the_same_room(self):
        wechat = {"openid": "wechat-user", "acctype": "mini"}
        qq = {"openid": "qq-user", "acctype": "qc"}
        with TemporaryDirectory() as directory:
            app_dir = Path(directory)
            wechat_raw = raw_dir(wechat, app_dir=app_dir)
            qq_raw = raw_dir(qq, app_dir=app_dir)
            wechat_raw.mkdir(parents=True)
            qq_raw.mkdir(parents=True)
            filename = "detail-same-room.json"
            (wechat_raw / filename).write_text("wechat", encoding="utf-8")
            (qq_raw / filename).write_text("qq", encoding="utf-8")

            self.assertEqual((wechat_raw / filename).read_text(encoding="utf-8"), "wechat")
            self.assertEqual((qq_raw / filename).read_text(encoding="utf-8"), "qq")
            self.assertNotEqual(wechat_raw, qq_raw)

    def test_shared_server_serves_desktop_release(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "releases"
            release.mkdir()
            (release / "version.json").write_text(
                json.dumps({"version": "1.0.0", "file": "DeltaStatsApp.exe"}),
                encoding="utf-8",
            )
            (release / "DeltaStatsApp.exe").write_bytes(b"desktop-app")
            (release / "DeltaStatsLauncher.exe").write_bytes(b"desktop-launcher")
            (release / "DeltaStatsAssistant.zip").write_bytes(b"complete-package")
            store = SharedStore(root / "shared.sqlite3")
            server = ThreadingHTTPServer(("127.0.0.1", 0), build_handler(store, "token", release))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                wait_until_ready(server.server_address[1])
                base = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(f"{base}/updates/version.json") as response:
                    manifest = json.load(response)
                with urllib.request.urlopen(f"{base}/updates/DeltaStatsApp.exe") as response:
                    binary = response.read()
                with urllib.request.urlopen(f"{base}/updates/DeltaStatsLauncher.exe") as response:
                    launcher_binary = response.read()
                with urllib.request.urlopen(f"{base}/") as response:
                    landing_page = response.read().decode("utf-8")
                with urllib.request.urlopen(f"{base}/downloads/DeltaStatsAssistant.zip") as response:
                    package = response.read()
                    disposition = response.headers.get("Content-Disposition")
                with urllib.request.urlopen(f"{base}/delta-stats-page.html") as response:
                    stats_page = response.read().decode("utf-8")
                self.assertEqual(manifest["version"], "1.0.0")
                self.assertEqual(binary, b"desktop-app")
                self.assertEqual(launcher_binary, b"desktop-launcher")
                self.assertIn("三角洲战绩本", landing_page)
                self.assertEqual(package, b"complete-package")
                self.assertEqual(disposition, 'attachment; filename="DeltaStatsAssistant.zip"')
                self.assertIn("逐局战绩", stats_page)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
