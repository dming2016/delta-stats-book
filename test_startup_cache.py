"""Focused source checks for stale cache and failed automatic synchronization."""

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class StartupCacheTests(unittest.TestCase):
    def test_default_filters_do_not_hide_old_cached_matches(self):
        script = r"""
const fs = require('fs'), assert = require('assert/strict');
const html = fs.readFileSync(process.argv[1], 'utf8');
const start = html.indexOf('function createDefaultModeFilters');
const end = html.indexOf('const state', start);
const defaults = new Function(html.slice(start, end) + ';return createDefaultModeFilters();')();
assert.equal(defaults.sol.preset, 'all');
assert.equal(defaults.mp.preset, 'all');
"""
        result = subprocess.run(["node", "-e", script, str(ROOT / "web/index.html")],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_sync_failure_notice_has_account_guard_and_recovery_action(self):
        html = (ROOT / "web/index.html").read_text(encoding="utf-8")
        for fragment in (
            'id="syncNotice"', 'id="cacheNotice"', 'id="showCachedMatchesButton"',
            "if (state.activeAccountId === accountId) renderSyncNotice(failure, accountId)",
            "notice.accountId !== state.activeAccountId",
            "if (!$('openWechatButton').hidden) void openWechatRecovery()",
            "renderCacheNotice(data, visibleMatches.length)",
        ):
            self.assertIn(fragment, html)


if __name__ == "__main__":
    unittest.main()
