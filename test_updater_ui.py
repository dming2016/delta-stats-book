#!/usr/bin/env python3
"""Focused tests for updater progress calculations."""

from __future__ import annotations

import unittest

from updater_ui import format_download_progress, format_duration


class UpdaterUiTests(unittest.TestCase):
    def test_download_progress_uses_full_elapsed_time_for_average_speed(self) -> None:
        megabyte = 1024 * 1024

        text = format_download_progress(5 * megabyte, 8 * megabyte, 10)

        self.assertEqual(
            text,
            "5.0 MB / 8.0 MB · 全程平均 512.0 KB/s · 预计剩余 6 秒",
        )

    def test_download_progress_omits_estimate_when_complete(self) -> None:
        megabyte = 1024 * 1024

        text = format_download_progress(8 * megabyte, 8 * megabyte, 4)

        self.assertEqual(text, "8.0 MB / 8.0 MB · 全程平均 2.0 MB/s")

    def test_duration_formats_minutes_and_hours(self) -> None:
        self.assertEqual(format_duration(61.1), "1 分钟 2 秒")
        self.assertEqual(format_duration(3661), "1 小时 1 分钟")


if __name__ == "__main__":
    unittest.main()
