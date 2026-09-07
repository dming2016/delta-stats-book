/* Synthetic bridge only: never starts WeChat or reads a real account. */
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const base = process.env.UI_PREVIEW_URL || 'http://127.0.0.1:4180';
const output = path.resolve(process.env.UI_REPORT_DIR || 'artifacts/miniapp-1.9.4');
fs.mkdirSync(output, {recursive: true});

(async () => {
  assert.equal((await fetch(base + '/api/preview').then(r => r.json())).synthetic_data_only, true);
  const browser = await chromium.launch({headless: true, channel: 'msedge'});
  const errors = [];
  const results = [];
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 840}});
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base + '/delta-stats-page.html');
    await page.locator('.match').first().waitFor();
    const calls = {http: 0, writes: 0};
    await page.route('**/api/**', async route => {
      if (route.request().url().endsWith('/api/wechat/open')) calls.http++;
      else if (route.request().method() === 'POST') calls.writes++;
      await route.abort('timedout');
    });
    await page.evaluate(() => {
      window.nativeCalls = 0;
      window.pywebview = {api: {open_wechat: async () => {
        window.nativeCalls++;
        return {ok: true, direct: true};
      }}};
      state.syncRunning = true;
      state.accountBusy = true;
      showRecoveryDialog({message: '请求超时（10 秒）', error_kind: 'network'});
    });
    assert.equal(await page.locator('#openWechatButton').isVisible(), false);
    await page.locator('#recoveryLaunchMiniappButton').click();
    await page.waitForFunction(() => document.getElementById('miniappLaunchStatus').textContent.includes('已向微信'));
    assert.equal(await page.evaluate(() => nativeCalls), 1);
    assert.deepEqual(calls, {http: 0, writes: 0});
    assert.equal(await page.evaluate(() => state.syncRunning && state.accountBusy), true);
    await page.screenshot({path: path.join(output, 'timeout-direct-launch.png')});
    results.push('timeout dialog launches natively despite blocked HTTP and busy account');

    await page.locator('#closeRecoveryDialog').click();
    await page.evaluate(() => {
      state.accounts = [];
      state.activeAccountId = '';
      window.pywebview.api.open_wechat = () => {
        window.nativeCalls++;
        return new Promise(resolve => { window.finishLaunch = resolve; });
      };
    });
    await page.locator('#launchMiniappButton').click();
    await page.waitForFunction(() => typeof window.finishLaunch === 'function');
    await page.evaluate(() => {
      void openMiniapp();
      document.getElementById('recoveryLaunchMiniappButton').click();
    });
    assert.equal(await page.evaluate(() => nativeCalls), 2);
    assert.equal(await page.locator('#launchMiniappButton').isDisabled(), true);
    await page.evaluate(() => finishLaunch({ok: true, direct: false, message: '请手动进入小程序'}));
    await page.waitForFunction(() => !document.getElementById('launchMiniappButton').disabled);
    assert.ok((await page.locator('#syncState').textContent()).includes('请手动进入小程序'));
    assert.deepEqual(calls, {http: 0, writes: 0});
    results.push('no account required; duplicate clicks share one launch; fallback feedback');

    for (const failure of ['returned', 'rejected']) {
      await page.evaluate(kind => {
        window.pywebview.api.open_wechat = async () => {
          nativeCalls++;
          if (kind === 'rejected') throw new Error('bridge unavailable');
          return {ok: false, message: '未检测到微信协议'};
        };
      }, failure);
      await page.locator('#launchMiniappButton').click();
      await page.waitForFunction(() => document.getElementById('syncState').textContent.includes('打开小程序失败'));
      assert.equal(calls.http, 0, 'Native errors must not silently retry over HTTP.');
      assert.equal(await page.locator('#launchMiniappButton').isEnabled(), true);
    }
    results.push('native rejection and failure preserve retry control without HTTP fallback');

    await page.clock.install();
    await page.evaluate(() => {
      window.pywebview.api.open_wechat = () => new Promise(resolve => { window.finishLaunch = resolve; });
      void openMiniapp();
    });
    await page.clock.fastForward(15001);
    assert.ok((await page.locator('#syncState').textContent()).includes('打开请求尚未返回'));
    assert.equal(await page.locator('#launchMiniappButton').isDisabled(), true);
    await page.evaluate(() => finishLaunch({ok: true, direct: true}));
    await page.waitForFunction(() => !document.getElementById('launchMiniappButton').disabled);
    await page.clock.resume();
    results.push('native timeout is visible; late completion cannot enable duplicate dispatch');

    await page.unroute('**/api/**');
    await page.route('**/api/wechat/open', async route => {
      calls.http++;
      assert.equal(route.request().method(), 'POST');
      await route.fulfill({json: {ok: true, direct: true}});
    });
    await page.evaluate(() => { delete window.pywebview.api.open_wechat; });
    await page.locator('#launchMiniappButton').click();
    await page.waitForFunction(() => document.getElementById('syncState').textContent.includes('已向微信'));
    assert.equal(calls.http, 1);
    assert.equal(calls.writes, 0);
    results.push('older bridge/browser retains fixed HTTP endpoint');

    for (const width of [1280, 1024, 768, 390, 360]) {
      await page.setViewportSize({width, height: 840});
      await page.evaluate(() => showRecoveryDialog({message: '请求超时（10 秒）', error_kind: 'network'}));
      const overflow = await page.locator('#recoveryDialog button').evaluateAll(buttons =>
        buttons.filter(b => b.getClientRects().length && (b.scrollWidth > b.clientWidth + 2
          || b.getBoundingClientRect().right > innerWidth || b.getBoundingClientRect().left < 0)).map(b => b.id));
      assert.deepEqual(overflow, []);
      await page.screenshot({path: path.join(output, `miniapp-dialog-${width}.png`)});
      await page.locator('#closeRecoveryDialog').click();
    }
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'miniapp-results.json'), JSON.stringify({results, calls, errors}, null, 2));
    console.log('PASS: ' + results.join('; '));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
