/* Run against tools/preview_ui.py. No live account or desktop control is used. */
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
const output = path.resolve(process.env.UI_REPORT_DIR || path.join(root, 'artifacts', 'ui-redesign'));
fs.mkdirSync(output, {recursive: true});
const base = process.env.UI_PREVIEW_URL || 'http://127.0.0.1:4178';
const shotsOnly = process.argv.includes('--workspace-only');
const updateAssets = !process.argv.includes('--no-assets');

async function settled(page) {
  await page.waitForFunction(() => !document.getElementById('app')?.classList.contains('loading'));
  await page.evaluate(() => document.fonts.ready);
  await page.evaluate(() => Promise.all([...document.images]
    .filter(image => image.getClientRects().length && image.loading !== 'lazy')
    .map(image => image.decode().catch(() => {}))));
}
async function bounds(page) {
  return page.evaluate(() => {
    const visible = el => !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
    const overflow = [...document.querySelectorAll('button, input, select, .metric, .warfare-metric, .map-cell, .site-header, .download-row, .site-hero h1')]
      .filter(visible).filter(el => {
        const r = el.getBoundingClientRect();
        return r.left < -1 || r.right > innerWidth + 1 || el.scrollWidth > el.clientWidth + 2;
      }).map(el => el.id || el.className || el.tagName);
    const broken = [...document.images].filter(visible).filter(img => !img.complete || img.naturalWidth === 0).map(img => img.src);
    return {overflow, broken};
  });
}
async function screenshot(page, name) {
  await settled(page);
  await page.screenshot({path: path.join(output, name + '.png'), fullPage: true});
}

(async () => {
  const identity = await fetch(base + '/api/preview').then(response => response.json());
  assert.deepEqual(identity, {synthetic_data_only: true, writes_enabled: false},
    'Screenshots must only be generated from the isolated synthetic preview.');
  const browser = await chromium.launch({headless: true, channel: 'msedge'});
  const errors = [];
  const results = [];
  try {
    let page = await browser.newPage({viewport: {width: 1500, height: 940}, deviceScaleFactor: 1});
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base + '/delta-stats-page.html');
    await page.locator('.match').first().waitFor();
    await screenshot(page, 'workspace-desktop');
    results.push({view: 'workspace-desktop', ...await bounds(page)});
    // The screenshots use the actual application renderer with synthetic data.
    if (updateAssets) {
      await page.screenshot({path: path.join(root, 'web/assets/workspace-overview-1.9.0.png')});
      await require('sharp')(path.join(root, 'web/assets/workspace-overview-1.9.0.png'))
        .extract({left: 204, top: 396, width: 1264, height: 515})
        .toFile(path.join(root, 'web/assets/workspace-hero-1.9.0.png'));
    }
    await page.locator('#favoriteFilters input').first().check();
    await page.locator('.friend-metric-row').first().waitFor();
    await screenshot(page, 'workspace-friends');
    if (updateAssets) await page.screenshot({path: path.join(root, 'web/assets/workspace-friends-1.9.0.png')});
    await page.locator('#resetFiltersButton').click();
    await settled(page);
    assert.equal(await page.locator('#favoriteFilters input:checked').count(), 0);
    await page.locator('#sessionPickerSummary').click();
    await page.locator('.session-option').first().waitFor();
    await screenshot(page, 'workspace-sessions');
    if (updateAssets) await page.screenshot({path: path.join(root, 'web/assets/workspace-sessions-1.9.0.png')});
    await page.locator('#sessionOptions input').first().check();
    await page.locator('#closeSessionsButton').click();
    await settled(page);
    assert.equal(await page.locator('#sessionOptions input:checked').count(), 1);
    await page.locator('#customTime > summary').click();
    await page.locator('#fromInput').fill('2026-01-01T00:00');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#customTime').getAttribute('open'), null);
    await page.locator('#resetFiltersButton').click();
    await settled(page);
    await page.locator('.match summary').first().click();
    await screenshot(page, 'workspace-match-detail');
    results.push({view: 'match-detail', ...await bounds(page)});
    await page.locator('[data-mode="mp"]').click();
    await page.locator('.warfare-match').first().waitFor();
    assert.equal(await page.locator('#firebreakView').isVisible(), false);
    assert.equal(await page.locator('[data-mode="mp"]').getAttribute('aria-pressed'), 'true');
    await screenshot(page, 'workspace-warfare');
    results.push({view: 'warfare', ...await bounds(page)});
    await page.locator('[data-workspace-action="accounts"]').click();
    await page.locator('#accountDialog').waitFor({state: 'visible'});
    await screenshot(page, 'workspace-accounts');
    await page.locator('#closeAccountDialog').click();
    await page.locator('[data-workspace-action="about"]').click();
    await page.locator('#aboutDialog').waitFor({state: 'visible'});
    await screenshot(page, 'workspace-about');
    await page.locator('#closeAboutDialog').click();
    for (const width of [1280, 1024, 768, 390, 360]) {
      await page.setViewportSize({width, height: 900});
      await page.locator('[data-mode="sol"]').click();
      await page.locator('.match').first().waitFor();
      await screenshot(page, `workspace-${width}`);
      results.push({view: `workspace-${width}`, ...await bounds(page)});
      await page.locator('[data-mode="mp"]').click();
      await page.locator('.warfare-match').first().waitFor();
      await screenshot(page, `warfare-${width}`);
      results.push({view: `warfare-${width}`, ...await bounds(page)});
    }
    await page.setViewportSize({width: 1280, height: 860});
    await page.evaluate(() => {
      state.appInfo = {version: '1.9.0', release_notes: ['新版界面演示']};
      state.updateStatus = normalizeUpdateStatus({
        phase: 'available', current_version: '1.8.7', latest_version: '1.9.0',
        update_available: true, release_notes: ['独立玩法导航与全新战绩工作区', '统一筛选与账号体验'],
      });
      renderUpdateStatus();
      document.getElementById('updateDialog').showModal();
    });
    await screenshot(page, 'workspace-update');
    results.push({view: 'update-desktop', ...await bounds(page)});
    await page.setViewportSize({width: 390, height: 844});
    await screenshot(page, 'workspace-update-mobile');
    results.push({view: 'update-mobile', ...await bounds(page)});
    await page.locator('#deferUpdateButton').click();
    await page.locator('[data-mode="sol"]').click();
    await settled(page);
    await page.evaluate(() => { document.getElementById('contentScroll').scrollTop = 10000; });
    await screenshot(page, 'workspace-mobile-records');
    await page.route('**/api/accounts', route => route.fulfill({
      contentType: 'application/json', body: JSON.stringify({accounts: [], active_account_id: null}),
    }));
    await page.reload();
    await page.waitForFunction(() => document.getElementById('authLabel').textContent === '尚未添加账号');
    await screenshot(page, 'workspace-no-account');
    assert.equal(await page.locator('#connectButton').isVisible(), true);
    results.push({view: 'no-account', ...await bounds(page)});
    if (!shotsOnly) {
      await page.close();
      page = await browser.newPage({viewport: {width: 1440, height: 980}, deviceScaleFactor: 1});
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(base + '/', {waitUntil: 'domcontentloaded'});
      await page.locator('.product-screenshot').first().waitFor();
      await page.evaluate(() => Promise.all([...document.images].map(img => {
        img.loading = 'eager';
        return img.decode().catch(() => {});
      })));
      await screenshot(page, 'website-desktop');
      results.push({view: 'website-desktop', ...await bounds(page)});
      for (const tab of ['friends', 'sessions', 'overview']) {
        await page.locator(`[data-preview="${tab}"]`).click();
        assert.equal(await page.locator(`[data-preview="${tab}"]`).getAttribute('aria-selected'), 'true');
      }
      await page.locator('#faq details summary').first().click();
      assert.equal(await page.locator('#faq details').first().getAttribute('open'), '');
      for (const width of [768, 390, 360]) {
        await page.setViewportSize({width, height: 844});
        await page.evaluate(() => scrollTo(0, 0));
        await screenshot(page, `website-${width}`);
        results.push({view: `website-${width}`, ...await bounds(page)});
      }
      await page.locator('#siteMenuButton').click();
      assert.equal(await page.locator('#siteNav').isVisible(), true);
      await page.locator('#siteNav a[href="#start"]').click();
      assert.equal(await page.locator('#siteMenuButton').getAttribute('aria-expanded'), 'false');
    }
    fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify({errors, results}, null, 2));
    console.log(JSON.stringify({errors, results}, null, 2));
    assert.deepEqual(errors, []);
    for (const result of results) {
      assert.deepEqual(result.overflow, [], result.view + ' overflow');
      assert.deepEqual(result.broken, [], result.view + ' broken image');
    }
  } catch (error) {
    for (const context of browser.contexts()) {
      for (const page of context.pages()) {
        console.error('Failure page:', page.url());
        console.error(await page.locator('#syncState').textContent({timeout: 1000}).catch(() => 'No workspace status'));
        await page.screenshot({path: path.join(output, 'failure.png'), timeout: 3000}).catch(() => {});
      }
    }
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
