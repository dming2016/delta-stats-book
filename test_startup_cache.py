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

    def test_automatic_recovery_classification_and_account_guards(self):
        script = r"""
const fs = require('fs'), assert = require('assert/strict');
const html = fs.readFileSync(process.argv[1], 'utf8');
const start = html.indexOf('function shouldRecoverSyncAuth');
const end = html.indexOf('function scheduleStartupSync', start);
const state = {activeAccountId: 'a', accountBusy: false, syncRunning: false, authRecoveryController: null};
const calls = [];
const functions = new Function('state', 'isExpiredAuthFailure', 'isPendingAuthState',
  'showRecoveryDialog', 'openWechatRecovery',
  html.slice(start, end) + ';return {shouldRecoverSyncAuth, recoverSyncAuth};')(
  state, info => info.authInvalid, value => value === 'pending_verification',
  (_failure, options) => calls.push(options.accountId), async () => calls.push('open'));
(async () => {
  for (const errorKind of ['busy', 'rate', 'rate_limit', 'rate_limited']) {
    assert.equal(functions.shouldRecoverSyncAuth({errorKind}), true);
  }
  for (const errorKind of ['network', 'timeout', 'unknown', 'operation_busy']) {
    assert.equal(functions.shouldRecoverSyncAuth({errorKind}), false);
  }
  assert.equal(functions.shouldRecoverSyncAuth({authInvalid: true}), true);
  assert.equal(functions.shouldRecoverSyncAuth({authState: 'pending_verification'}), true);
  await functions.recoverSyncAuth('other', {});
  assert.deepEqual(calls, []);
  for (const key of ['accountBusy', 'syncRunning', 'authRecoveryController']) {
    state[key] = true;
    await functions.recoverSyncAuth('a', {});
    assert.deepEqual(calls, []);
    state[key] = false;
  }
  await functions.recoverSyncAuth('a', {});
  assert.deepEqual(calls, ['a', 'open']);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
        result = subprocess.run(["node", "-e", script, str(ROOT / "web/index.html")],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_recovery_retry_cannot_recursively_launch_miniapp(self):
        html = (ROOT / "web/index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count("const synced = await syncData({manual: true, autoRecover: false})"), 2)
        self.assertIn("options?.autoRecover !== false", html)
        sync = html.split("async function syncData", 1)[1].split("function shouldRecoverSyncAuth", 1)[0]
        self.assertLess(sync.index("state.syncRunning = false"), sync.index("if (recoveryFailure) await recoverSyncAuth"))


if __name__ == "__main__":
    unittest.main()
