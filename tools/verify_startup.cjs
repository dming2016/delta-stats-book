/* Desktop-startup regression using stale synthetic records and a busy upstream. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const base = process.env.UI_PREVIEW_URL || 'http://127.0.0.1:4180';
const output = path.resolve(__dirname, '../artifacts/startup-regression');
fs.mkdirSync(output, {recursive: true});

(async () => {
  const identity = await fetch(base + '/api/preview').then(r => r.json());
  assert.equal(identity.synthetic_data_only, true);
  const source = await fetch(base + '/api/matches?mode=sol').then(r => r.json());
  const stamp = new Date(Date.now() - 90 * 86400000).toISOString();
  const matches = source.matches.map(row => ({...row, timestamp: stamp, time: '历史缓存'}));
  const accounts = {active_account_id: 'demo-account', accounts: [{
    id: 'demo-account', account_type: 'wechat', display_name: '演示账号',
    auth_state: 'valid', has_credential: true, can_sync: true,
    cache: {exists: true, viewable: true, matches: matches.length},
  }]};
  const browser = await chromium.launch({headless: true, channel: 'msedge'});
  let opens = 0;
  let syncs = 0;
  const errors = [];
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 840}});
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      window.pywebview = {api: {
        get_window_state: async () => ({maximized: false}),
        get_app_info: async () => ({version: '1.9.1', release_notes: []}),
        check_for_update: async () => ({phase: 'up_to_date', current_version: '1.9.1'}),
        get_update_status: async () => ({phase: 'up_to_date', current_version: '1.9.1'}),
      }};
    });
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      let data;
      let status = 200;
      if (url.pathname === '/api/accounts') data = accounts;
      else if (url.pathname === '/api/matches') {
        const start = url.searchParams.get('from');
        const shown = start ? [] : matches;
        data = {...source, matches: shown, updated_at: stamp, sessions: [], available: {total: matches.length},
          summary: shown.length ? source.summary : {}, favorite_friends: [], friends: []};
      } else if (url.pathname === '/api/sync/start') {
        syncs++;
        data = {job_id: 'busy-job', account_id: 'demo-account'};
      } else if (url.pathname === '/api/sync/status') data = {
        id: 'busy-job', account_id: 'demo-account', status: 'failed',
        error: '抱歉，目前访问人数太多！', error_kind: 'busy', auth_invalid: false,
        retryable: true, auth_state: 'valid',
      };
      else if (url.pathname === '/api/wechat/open') {
        opens++;
        data = {ok: true, direct: true};
      } else if (url.pathname === '/api/auth/import') {
        status = 202;
        data = {status: 'waiting_for_miniapp', account_id: 'demo-account', retry_after_seconds: 1};
      } else data = {candidates: []};
      await route.fulfill({status, contentType: 'application/json', body: JSON.stringify(data)});
    });
    await page.goto(base + '/delta-stats-page.html');
    await page.locator('#syncNotice').waitFor({state: 'visible'});
    assert.equal(await page.locator('.match').count(), matches.length);
    assert.equal(await page.locator('html').evaluate(el => el.classList.contains('desktop-window')), true);
    assert.equal(syncs, 1);
    assert.equal(opens, 0);
    assert.equal(await page.evaluate(() => state.accounts[0].auth_state), 'valid');
    const firstRecord = await page.locator('.match summary').first().boundingBox();
    assert.ok(firstRecord.y + firstRecord.height < 840, 'A cached match must be visible in the default desktop viewport.');
    await page.screenshot({path: path.join(output, 'stale-cache-startup.png')});
    await page.locator('#presets [data-days="7"]').click();
    await page.locator('#cacheNotice').waitFor({state: 'visible'});
    assert.equal(await page.locator('.match').count(), 0);
    await page.locator('#showCachedMatchesButton').click();
    await page.locator('.match').first().waitFor();
    assert.equal(await page.locator('.match').count(), matches.length);
    await page.locator('#syncNoticeAction').click();
    await page.waitForFunction(() => document.getElementById('recoveryTitle').textContent.includes('等待'));
    assert.equal(opens, 1);
    await page.locator('#closeRecoveryDialog').click();
    assert.equal(await page.evaluate(() => state.authRecoveryController), null);
    assert.equal(await page.evaluate(() => state.accounts[0].auth_state), 'valid');
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify({
      cached: matches.length, visible: await page.locator('.match').count(),
      syncs, opens, preservedAuth: true, errors,
    }, null, 2));
    console.log('PASS: stale cache visible, startup busy surfaced, WeChat recovery routed, cancellation and auth preserved.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
