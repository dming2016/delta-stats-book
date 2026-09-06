#!/usr/bin/env python3
"""Unit tests for Tencent AMS request handling."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, call, patch

import requests

from delta_api import AmsError, DeltaAmsClient, MATCH_LIST_TOKEN, classify_error


def response_with(payload: dict) -> Mock:
    response = Mock()
    response.json.return_value = payload
    return response


class DeltaApiTests(unittest.TestCase):
    def client(self, raw_dir: Path) -> DeltaAmsClient:
        with patch("delta_api.account_raw_dir", return_value=raw_dir):
            client = DeltaAmsClient({"openid": "user", "acctype": "mini"})
        client.session = Mock()
        client._activity = {
            "tokens": {MATCH_LIST_TOKEN: "450526"},
            "flows": {"450526": {"sIdeUrl": "https://dfm.ams.game.qq.com/ide/"}},
        }
        return client

    def test_match_page_retries_busy_response_then_succeeds(self) -> None:
        busy = {"iRet": 999, "sMsg": "抱歉，目前访问人数太多！请稍后再试！谢谢！"}
        success = {"iRet": 0, "sMsg": "ok", "jData": {"data": []}}
        with TemporaryDirectory() as directory:
            client = self.client(Path(directory))
            client.session.post.side_effect = [response_with(busy), response_with(success)]

            with (
                patch("delta_api.random.uniform", side_effect=lambda low, high: (low + high) / 2),
                patch("delta_api.time.sleep") as sleep_mock,
            ):
                result = client.fetch_match_page(4, 1)

        self.assertEqual(result, success)
        self.assertEqual(client.session.post.call_count, 2)
        sleep_mock.assert_called_once_with(2.0)

    def test_match_page_stops_after_busy_retries_are_exhausted(self) -> None:
        busy = {"iRet": 999, "sMsg": "抱歉，目前访问人数太多！请稍后再试！谢谢！"}
        with TemporaryDirectory() as directory:
            client = self.client(Path(directory))
            client.session.post.side_effect = [response_with(busy) for _ in range(4)]

            with (
                patch("delta_api.random.uniform", side_effect=lambda low, high: (low + high) / 2),
                patch("delta_api.time.sleep") as sleep_mock,
            ):
                with self.assertRaisesRegex(AmsError, "访问人数太多"):
                    client.fetch_match_page(4, 1)

        self.assertEqual(client.session.post.call_count, 4)
        self.assertEqual(
            sleep_mock.call_args_list,
            [call(2.0), call(5.0), call(10.0)],
        )

    def test_match_page_does_not_retry_non_busy_business_error(self) -> None:
        rejected = {"iRet": 101, "sMsg": "请先登录"}
        with TemporaryDirectory() as directory:
            client = self.client(Path(directory))
            client.session.post.return_value = response_with(rejected)

            with patch("delta_api.time.sleep") as sleep_mock:
                with self.assertRaisesRegex(AmsError, "请先登录"):
                    client.fetch_match_page(4, 1)

        client.session.post.assert_called_once()
        sleep_mock.assert_not_called()

    def test_business_error_does_not_overwrite_last_successful_raw_response(self) -> None:
        rejected = {"iRet": 101, "sMsg": "请先登录"}
        with TemporaryDirectory() as directory:
            raw_dir = Path(directory)
            raw_file = raw_dir / "matches-4-1.json"
            raw_file.write_text('{"iRet": 0}', encoding="utf-8")
            client = self.client(raw_dir)
            client.session.post.return_value = response_with(rejected)

            with self.assertRaisesRegex(AmsError, "请先登录"):
                client.fetch_match_page(4, 1)

            self.assertEqual(raw_file.read_text(encoding="utf-8"), '{"iRet": 0}')

    def test_match_page_retry_budget_is_shared_across_pages(self) -> None:
        busy = {"iRet": 999, "sMsg": "抱歉，目前访问人数太多！请稍后再试！谢谢！"}
        success = {"iRet": 0, "sMsg": "ok", "jData": {"data": []}}
        with TemporaryDirectory() as directory:
            client = self.client(Path(directory))
            client.session.post.side_effect = [
                response_with(busy),
                response_with(success),
                response_with(busy),
            ]

            with (
                patch("delta_api.time.monotonic", side_effect=[0.0, 21.0]),
                patch("delta_api.random.uniform", return_value=2.0),
                patch("delta_api.time.sleep") as sleep_mock,
            ):
                client.fetch_match_page(4, 1)
                with self.assertRaisesRegex(AmsError, "访问人数太多"):
                    client.fetch_match_page(4, 2)

        self.assertEqual(client.session.post.call_count, 3)
        sleep_mock.assert_called_once_with(2.0)

    def test_error_classifier_recognizes_explicit_auth_rejection(self) -> None:
        rejected = classify_error(AmsError("请先登录", code=101))

        self.assertEqual(rejected.kind, "expired")
        self.assertTrue(rejected.auth_invalid)
        self.assertFalse(rejected.retryable)

    def test_error_classifier_recognizes_minus_108_as_busy(self) -> None:
        busy = classify_error(AmsError("访问人数太多", code=-108))

        self.assertEqual(busy.kind, "busy")
        self.assertFalse(busy.auth_invalid)
        self.assertTrue(busy.retryable)

    def test_error_classifier_recognizes_retryable_http_statuses(self) -> None:
        for status in (429, 502, 503, 504):
            with self.subTest(status=status):
                response = requests.Response()
                response.status_code = status
                error = requests.HTTPError(f"HTTP {status}", response=response)

                classified = classify_error(error)

                self.assertEqual(classified.kind, "busy")
                self.assertEqual(classified.http_status, status)
                self.assertTrue(classified.retryable)

    def test_response_bearing_http_error_is_not_local_network_failure(self) -> None:
        response = requests.Response()
        response.status_code = 400
        error = requests.HTTPError("HTTP 400", response=response)

        classified = classify_error(error)

        self.assertEqual(classified.kind, "unknown")
        self.assertEqual(classified.http_status, 400)
        self.assertFalse(classified.retryable)

    def test_connection_error_is_retryable_network_failure(self) -> None:
        classified = classify_error(requests.ConnectionError("offline"))

        self.assertEqual(classified.kind, "network")
        self.assertTrue(classified.retryable)
        self.assertFalse(classified.auth_invalid)


if __name__ == "__main__":
    unittest.main()
