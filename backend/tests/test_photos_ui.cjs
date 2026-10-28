// 照片界面测试，配合 photo_ui_server.py
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const base = process.env.PHOTO_TEST_URL || 'http://127.0.0.1:5069';
  const output = path.resolve('.build/camera-ui');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({
    executablePath: process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true
  });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  async function settled() {
    await page.waitForFunction(() => !document.getElementById('photoRefresh').disabled);
  }
  async function fits() {
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Horizontal overflow');
  }
  try {
    await page.goto(base + '/photos');
    await settled();
    assert.equal(await page.locator('.photo-item').count(), 12);
    await page.locator('.photo-open img').first().waitFor();
    await page.waitForFunction(() => document.querySelector('.photo-open img').naturalWidth > 0);
    await page.locator('.photo-open img').evaluateAll(images => images.forEach(img => { img.loading = 'eager'; }));
    await page.waitForFunction(() => Array.from(document.querySelectorAll('.photo-open img')).every(img => img.complete && img.naturalWidth > 0));
    await fits();
    await page.screenshot({ path: path.join(output, 'photos-desktop.png'), fullPage: true });

    await page.getByRole('button', { name: '跌倒', exact: true }).click();
    await settled();
    assert.equal(await page.locator('.photo-item').count(), 4);
    assert.equal(await page.locator('.photo-kind[data-reason="FALL"]').count(), 4);
    assert.equal(await page.getByRole('button', { name: '跌倒', exact: true }).getAttribute('aria-pressed'), 'true');
    await page.getByRole('button', { name: '空气超标', exact: true }).click();
    await settled();
    assert.equal(await page.locator('.photo-kind[data-reason="AIR"]').count(), 4);
    await page.getByRole('button', { name: '全部', exact: true }).click();
    await settled();
    await page.getByRole('button', { name: '加载更早照片' }).click();
    await settled();
    assert.equal(await page.locator('.photo-item').count(), 15);
    assert(await page.locator('#photoMore').isHidden());

    await page.setViewportSize({ width: 390, height: 844 });
    await page.evaluate(() => scrollTo(0, 0));
    await fits();
    await page.screenshot({ path: path.join(output, 'photos-mobile.png'), fullPage: false });
    await page.setViewportSize({ width: 320, height: 780 });
    await fits();
    await page.screenshot({ path: path.join(output, 'photos-mobile-320.png'), fullPage: false });

    await page.goto(base + '/photos?device_id=esp32-no-photo');
    await settled();
    assert.equal(await page.locator('.photo-item').count(), 0);
    assert.match(await page.locator('#photoStatus').textContent(), /尚无照片/);
    assert(await page.locator('#photoScope').isVisible());
    await page.route('**/api/photos?**', route => route.fulfill({ status: 503, body: '{}' }));
    await page.getByRole('button', { name: '刷新照片' }).click();
    await settled();
    assert.match(await page.locator('#photoStatus').textContent(), /照片加载失败/);
    await page.unroute('**/api/photos?**');
    await page.getByRole('button', { name: '刷新照片' }).click();
    await settled();
    assert.match(await page.locator('#photoStatus').textContent(), /尚无照片/);

    await page.goto(base + '/photos?device_id=esp32-preview&boot_id=preview1&event_seq=7');
    await settled();
    assert.equal(await page.locator('.photo-item').count(), 8);
    assert.match(await page.locator('#photoScopeText').textContent(), /这次报警/);
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.locator('#btnTheme').click();
    await page.waitForFunction(() => getComputedStyle(document.querySelector('.workspace-link')).backgroundColor === 'rgb(23, 38, 59)');
    await page.evaluate(() => scrollTo(0, 0));
    await page.screenshot({ path: path.join(output, 'photos-dark.png'), fullPage: false });

    await page.goto(base + '/');
    await settled();
    await page.waitForSelector('#events .ev');
    assert.equal(await page.locator('#photoPreview .photo-item').count(), 3);
    assert.equal(await page.locator('#events .ev').filter({ hasText: '倾倒' }).locator('a[href^="/photos"]').count(), 0);
    const alarmLink = page.locator('#events .ev a[href^="/photos"]').first();
    assert.match(await alarmLink.getAttribute('href'), /boot_id=preview1&event_seq=7/);
    assert.equal(await page.locator('.workspace-link').count(), 2);
    await page.screenshot({ path: path.join(output, 'dashboard-camera-desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 320, height: 780 });
    await page.evaluate(() => scrollTo(0, 0));
    await fits();
    await page.screenshot({ path: path.join(output, 'dashboard-camera-mobile.png'), fullPage: true });
    await alarmLink.click();
    await settled();
    assert.equal(await page.locator('.photo-item').count(), 8);
    assert.deepEqual(errors, []);
    const diagram = await context.newPage();
    await diagram.setViewportSize({ width: 1200, height: 880 });
    await diagram.setContent('<style>body{margin:16px}pre{white-space:pre-wrap;font:16px/1.5 monospace}</style><pre></pre>');
    await diagram.locator('pre').evaluate((el, text) => { el.textContent = text; }, fs.readFileSync('接线说明.txt', 'utf8'));
    await diagram.screenshot({ path: path.join(output, 'wiring.png'), fullPage: true });
    await diagram.close();
    fs.writeFileSync(path.join(output, 'ui-results.json'), JSON.stringify({ passed: true,
      checked: ['real SQLite photo API + image rendering', 'reason filters', 'cursor pagination', '390/320px layout',
        'empty state', 'failure and retry', 'dark theme', 'dashboard preview', 'exact event link', 'TILT has no photo link'],
      jsErrors: errors }, null, 2));
    console.log('Photo UI checks passed; no JavaScript errors. Artifacts: ' + output);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
