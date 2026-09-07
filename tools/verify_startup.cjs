/* Synthetic desktop startup and bounded miniapp recovery; no live accounts. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const base = process.env.UI_PREVIEW_URL || 'http://127.0.0.1:4180';
const output = path.resolve(process.env.UI_REPORT_DIR || path.join(__dirname, '../artifacts/light-recovery'));
fs.mkdirSync(output, {recursive: true});

(async () => {
  const identity = await fetch(base + '/api/preview').then(r => r.json());
  assert.equal(identity.synthetic_data_only, true);
  const source = await fetch(base + '/api/matches?mode=sol').then(r => r.json());
  const stamp = new Date(Date.now() - 90 * 86400000).toISOString();
  const matches = source.matches.map(row => ({...row, timestamp: stamp, time: '历史缓存'}));
  const browser = await chromium.launch({headless: true, channel: 'msedge'});
  const errors = [];
  const results = [];
  async function scenario({kind = 'busy', restored = false, retryFails = false, expired = false, mismatch = false} = {}) {
    const account = {
      id: 'demo-account', account_type: 'wechat', display_name: '演示账号',
      auth_state: expired ? 'expired' : 'valid', has_credential: true, can_sync: !expired,
      credential_revision: 'old-revision',
      cache: {exists: true, viewable: true, matches: matches.length},
    };
    const accounts = {active_account_id: account.id, accounts: [account]};
    const calls = {opens: 0, syncs: 0, imports: 0};
    const page = await browser.newPage({viewport: {width: 1280, height: 840}});
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      window.pywebview = {api: {
        get_window_state: async () => ({maximized: false}),
        get_app_info: async () => ({version: '1.9.3', release_notes: []}),
        check_for_update: async () => ({phase: 'up_to_date', current_version: '1.9.3'}),
        get_update_status: async () => ({phase: 'up_to_date', current_version: '1.9.3'}),
      }};
    });
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      let data;
      let status = 200;
      if (url.pathname === '/api/accounts') data = accounts;
      else if (url.pathname === '/api/matches') {
        const shown = url.searchParams.get('from') ? [] : matches;
        data = {...source, account_id: account.id, matches: shown, updated_at: stamp,
          sessions: [], available: {total: matches.length},
          summary: shown.length ? source.summary : {}, favorite_friends: [], friends: []};
      } else if (url.pathname === '/api/sync/start') {
        calls.syncs++;
        data = {job_id: 'test-job', account_id: account.id};
      } else if (url.pathname === '/api/sync/status') {
        data = calls.imports && restored && !retryFails
          ? {status: 'completed', account_id: account.id, result: {account_id: account.id}}
          : {status: 'failed', account_id: account.id, error: kind === 'busy' ? '腾讯战绩接口暂时繁忙 -108' : kind,
            error_kind: kind, auth_invalid: false, retryable: true, auth_state: account.auth_state};
      } else if (url.pathname === '/api/wechat/open') {
        calls.opens++;
        data = {ok: true, direct: true};
      } else if (url.pathname === '/api/auth/import') {
        calls.imports++;
        const body = route.request().postDataJSON();
        assert.equal(body.candidate_id, account.id);
        assert.equal(body.recovery, true);
        if (mismatch) data = {status: 'account_mismatch', detected_account: {id: 'other-account', display_name: '其他账号'}};
        else if (restored) {
          Object.assign(account, {auth_state: 'valid', can_sync: true, credential_revision: 'new-revision'});
          data = {ok: true, ...accounts, credential_revision: 'new-revision'};
        } else {
          status = 202;
          data = {status: 'waiting_for_miniapp', account_id: account.id, retry_after_seconds: 1};
        }
      } else data = {candidates: []};
      await route.fulfill({status, contentType: 'application/json', body: JSON.stringify(data)});
    });
    await page.goto(base + '/delta-stats-page.html');
    await page.waitForFunction(() => state.startupSyncScheduled);
    return {page, calls, account};
  }
  try {
    const {page, calls} = await scenario();
    await page.waitForFunction(() => document.getElementById('recoveryTitle').textContent.includes('等待'));
    assert.equal(calls.opens, 1, 'Startup busy must automatically open the miniapp.');
    assert.equal(calls.syncs, 1);
    await page.locator('#closeRecoveryDialog').click();
    await page.waitForFunction(() => !state.syncRunning && !state.authRecoveryController);
    assert.equal(await page.locator('.match').count(), matches.length);
    assert.equal(await page.evaluate(() => state.accounts[0].auth_state), 'valid');
    const first = await page.locator('.match summary').first().boundingBox();
    assert.ok(first.y + first.height < 840);
    const colors = await page.evaluate(() => ({
      scheme: getComputedStyle(document.documentElement).colorScheme,
      canvas: getComputedStyle(document.body).backgroundColor,
      rail: getComputedStyle(document.querySelector('.workspace-rail')).backgroundColor,
      dialog: getComputedStyle(document.getElementById('recoveryDialog')).backgroundColor,
    }));
    assert.deepEqual(colors, {scheme: 'light', canvas: 'rgb(244, 245, 244)', rail: 'rgb(255, 255, 255)', dialog: 'rgb(255, 255, 255)'});
    await page.screenshot({path: path.join(output, 'light-cache-startup.png')});
    await page.locator('#presets [data-days="7"]').click();
    await page.locator('#cacheNotice').waitFor({state: 'visible'});
    assert.equal(await page.locator('.match').count(), 0);
    await page.locator('#showCachedMatchesButton').click();
    await page.locator('.match').first().waitFor();
    await page.locator('#refreshButton').click();
    await page.locator('#recoveryDialog').waitFor({state: 'visible'});
    await page.waitForFunction(() => document.getElementById('recoveryTitle').textContent.includes('等待'));
    assert.equal(calls.opens, 2, 'A new manual refresh gets its own bounded recovery.');
    await page.locator('#closeRecoveryDialog').click();
    await page.waitForFunction(() => !state.authRecoveryController && !state.syncRunning);
    const cancelledImports = calls.imports;
    await page.waitForTimeout(1200);
    assert.equal(calls.imports, cancelledImports);
    results.push({case: 'startup-manual-cancel-cache-light', ...calls, colors});
    await page.close();

    for (const config of [{restored: true}, {restored: true, retryFails: true}, {restored: true, expired: true}]) {
      const item = await scenario(config);
      await item.page.waitForFunction(() => state.accounts[0]?.credential_revision === 'new-revision');
      await item.page.waitForFunction(() => state.startupSyncScheduled && !state.syncRunning && !state.authRecoveryController);
      assert.equal(item.calls.opens, 1);
      assert.equal(item.calls.syncs, config.expired ? 1 : 2);
      assert.equal(await item.page.evaluate(() => state.accounts[0].auth_state), 'valid');
      assert.equal(await item.page.locator('#syncNotice').isVisible(), Boolean(config.retryFails));
      await item.page.waitForTimeout(400);
      assert.equal(item.calls.opens, 1, 'Retry failure must not recursively launch WeChat.');
      results.push({case: JSON.stringify(config), ...item.calls});
      await item.page.close();
    }
    for (const kind of ['network', 'unknown', 'operation_busy']) {
      const item = await scenario({kind});
      await item.page.locator('#syncNotice').waitFor({state: 'visible'});
      await item.page.waitForFunction(() => !state.syncRunning);
      assert.equal(item.calls.opens, 0);
      assert.equal(await item.page.evaluate(() => state.accounts[0].auth_state), 'valid');
      results.push({case: kind, ...item.calls});
      await item.page.close();
    }
    const other = await scenario({mismatch: true});
    await other.page.waitForFunction(() => document.getElementById('recoveryTitle').textContent.includes('其他账号'));
    assert.equal(await other.page.evaluate(() => state.activeAccountId), 'demo-account');
    assert.equal(other.calls.syncs, 1);
    await other.page.locator('#closeRecoveryDialog').click();
    results.push({case: 'mismatch', ...other.calls});
    await other.page.close();
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'startup-results.json'), JSON.stringify({results, errors}, null, 2));
    console.log('PASS: light UI, old cache, automatic/manual recovery, successful retry, bounded failure, cancellation, account isolation, network preservation.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
