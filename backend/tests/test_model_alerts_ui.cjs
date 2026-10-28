// 模型界面测试，配合 model_ui_server.py
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const base = process.env.MODEL_UI_URL || 'http://127.0.0.1:5070';
  const out = path.resolve('.build/model-alerts-ui');
  fs.mkdirSync(out, { recursive: true });
  const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH ||
    'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, colorScheme: 'light' });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  async function post(url, data={}) { const r = await context.request.post(base+url, {data}); assert.equal(r.status(),200); }
  async function idle() { await page.waitForFunction(() => !document.getElementById('mlRefresh').disabled); }
  async function fits() { assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'horizontal overflow'); }
  async function ready() { await page.waitForFunction(() => document.getElementById('mlBadge').textContent === '模型监测中'); }
  try {
    await post('/_test/seed', {count:23}); await post('/_test/normal');
    await page.goto(base+'/'); await ready(); await idle();
    await page.waitForFunction(() => document.getElementById('modelMonitor').dataset.device === 'esp32-preview');
    await page.waitForFunction(() => document.querySelectorAll('.ml-event').length === 20);
    assert(await page.locator('#mlPending').isHidden(), 'first load must not replay historical alerts');
    assert.equal(await page.locator('.workspace-link').count(),2);
    assert.equal(await page.locator('#stTotal').textContent(),'0');
    await page.locator('#mlMore').click();
    await page.waitForFunction(() => document.querySelectorAll('.ml-event').length === 23);
    assert(await page.locator('#mlMore').isHidden());
    await fits();
    await page.screenshot({path:path.join(out,'dashboard-model-desktop.png'),fullPage:true});

    await page.evaluate(() => { window.mlTestBeeps=0; window.beep=() => window.mlTestBeeps++; });
    await page.locator('#mlSound').click();
    await page.waitForFunction(() => document.getElementById('mlSound').getAttribute('aria-pressed') === 'true');
    const enabledBeeps = await page.evaluate(() => mlTestBeeps);
    await post('/_test/seed'); await post('/_test/normal');
    await page.waitForFunction(() => document.getElementById('mlPendingTitle').textContent.startsWith('1 条'));
    assert(await page.locator('#mlPending').isVisible());
    assert.equal(await page.evaluate(() => mlTestBeeps), enabledBeeps+1);
    assert.equal(await page.locator('#mlScore').textContent(),'0.100', 'an intervening normal batch must not hide saved alarm');
    assert.equal(await page.locator('#stTotal').textContent(),'0', 'device totals unchanged');
    assert.equal(await page.locator('.ml-event a[href^="/photos"]').count(),0,'model event must not claim a triggered photo');

    await page.route('**/api/ml/events?**', route => route.fulfill({status:503,body:'{}'}));
    await post('/_test/seed'); await post('/_test/normal');
    await page.waitForFunction(() => !document.getElementById('mlFeedback').hidden);
    assert(await page.locator('#mlPending').isVisible());
    await page.unroute('**/api/ml/events?**');
    await page.waitForFunction(() => document.getElementById('mlPendingTitle').textContent.startsWith('2 条'));
    await page.reload(); await ready(); await idle();
    await page.waitForFunction(() => document.getElementById('mlPendingTitle').textContent.startsWith('2 条'));
    assert(await page.locator('#mlPending').isVisible(), 'unread alerts survive reload');
    assert.equal(await page.locator('#mlSound').getAttribute('aria-pressed'),'false');
    await page.locator('#mlAck').click(); assert(await page.locator('#mlPending').isHidden());
    await page.reload(); await ready(); await idle();
    assert(await page.locator('#mlPending').isHidden(), 'acknowledged records must not alert again');

    // 状态异常不清空事件历史
    await page.route('**/api/ml/status?**', route => route.fulfill({status:503,body:'{}'}));
    await page.waitForFunction(() => document.getElementById('mlBadge').textContent.includes('连接中断'));
    assert.equal(await page.locator('#mlScore').textContent(),'--');
    assert(await page.locator('.ml-event').count() >= 20);
    await page.unroute('**/api/ml/status?**'); await ready();
    await page.route('**/api/ml/status?**', route => route.fulfill({json:{ok:true,status:'no_data',model_id:'ui-test-fixture',latest:null}}));
    await page.waitForFunction(() => document.getElementById('mlBadge').textContent === '等待六轴数据');
    assert.equal(await page.locator('#mlScore').textContent(),'--');
    await page.unroute('**/api/ml/status?**'); await ready();

    await post('/_test/seed'); await post('/_test/normal');
    await page.waitForFunction(() => !document.getElementById('mlPending').hidden);
    await page.setViewportSize({width:390,height:844}); await fits();
    await page.evaluate(() => scrollTo(0,0));
    await page.screenshot({path:path.join(out,'model-alarm-mobile.png'),fullPage:false});
    await page.setViewportSize({width:320,height:780}); await fits();
    await page.screenshot({path:path.join(out,'model-alarm-mobile-320.png'),fullPage:false});
    await page.setViewportSize({width:1440,height:1100});
    await page.locator('#btnTheme').click();
    await page.waitForFunction(() => getComputedStyle(document.getElementById('mlSound')).backgroundColor === 'rgb(37, 56, 79)');
    await page.locator('#modelMonitor').scrollIntoViewIfNeeded();
    await fits();
    await page.screenshot({path:path.join(out,'model-alarm-dark.png'),fullPage:false});

    // 校验记录文本转义
    const latest = await (await context.request.get(base+'/api/ml/events?device_id=esp32-preview')).json();
    latest.events[0].model_id='<img src=x onerror=alert(1)>';
    await page.route('**/api/ml/events?**', route => route.fulfill({json:latest}));
    await page.reload(); await ready(); await idle();
    assert.equal(await page.locator('#mlEvents img').count(),0);
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(out,'ui-results.json'),JSON.stringify({passed:true,
      fixture:true,hardwareVerified:false,checks:['initial history suppression','cursor catch-up after normal batch',
      'older pagination','sound opt-in','independent device totals','API outage and recovery',
      'unread reload persistence','acknowledgement deduplication','model no-data/service errors',
      '1440/390/320px layout','dark theme','HTML escaping'],jsErrors:errors},null,2));
    console.log('Model alert UI checks passed. No JavaScript errors. Screenshots: '+out);
  } catch (error) {
    console.error('UI failure state', await page.evaluate(() => ({
      device:document.getElementById('modelMonitor').dataset.device,
      records:document.querySelectorAll('.ml-event').length,
      feedback:document.getElementById('mlFeedback').textContent,
      moreHidden:document.getElementById('mlMore').hidden,
      moreDisabled:document.getElementById('mlMore').disabled,
      status:document.getElementById('mlBadge').textContent
    })));
    throw error;
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode=1; });
