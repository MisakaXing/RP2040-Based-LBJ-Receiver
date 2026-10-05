const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
(async () => {
  const chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  const executablePath = process.env.LBJ_BROWSER_EXECUTABLE ||
    (process.platform === 'darwin' && fs.existsSync(chrome) ? chrome : undefined);
  const browser = await chromium.launch({executablePath, headless: true});
  try {
    const a = await browser.newContext({viewport: {width: 390, height: 844}});
    const b = await browser.newContext({viewport: {width: 820, height: 1180}});
    const c = await browser.newContext({viewport: {width: 1440, height: 900}});
    const first = await a.newPage(), second = await b.newPage(), third = await c.newPage();
    const errors = [];
    for (const page of [first, second, third]) page.on('pageerror', e => errors.push(String(e)));
    const url = 'http://127.0.0.1:' + process.argv[2] + '/';
    await first.goto(url);
    await first.waitForFunction(() => !document.getElementById('historyDown').disabled);
    console.log('PASS native TCP + EventSource owner acquired');
    await Promise.all([second.goto(url), third.goto(url)]);
    await Promise.all([second, third].map(page => page.waitForFunction(() =>
      document.getElementById('refreshState').textContent.includes('兼容模式'))));
    for (const page of [second, third]) {
      assert.ok(await page.locator('#historyDown').isDisabled());
      assert.match(await page.locator('#historyHint').textContent(), /轮询/);
      assert.equal(await page.evaluate(async () =>
        (await fetch('/api/history?index=0&token=wrong')).status), 403);
    }
    console.log('PASS second + third polling, unauthorized history 403');
    const coldStart = Date.now();
    await first.locator('#historyUp').click();
    await first.waitForFunction(() => document.getElementById('state').textContent.includes('9999 / 9999'));
    assert.equal(await first.locator('#train').textContent(), 'K9998');
    assert.equal(await first.locator('#received').textContent(), '2026-08-29 18:00');
    assert.equal(await first.locator('#historyReceived').textContent(), '接收于 2026-08-29 18:00');
    assert.ok(await first.locator('#historyReceived').isVisible());
    const coldMs = Date.now() - coldStart;
    const cacheBefore = await first.evaluate(async () => (await fetch('/__test/history_cache')).json());
    assert.ok(cacheBefore.cached > 1 && cacheBefore.cached <= 8);
    const warmStart = Date.now();
    await first.locator('#historyUp').click();
    await first.waitForFunction(() => document.getElementById('state').textContent.includes('9998 / 9999'));
    assert.equal(await first.locator('#train').textContent(), 'K9997');
    assert.equal(await first.locator('#historyReceived').textContent(), '接收于 2026-08-29 18:00');
    const warmMs = Date.now() - warmStart;
    const cacheAfter = await first.evaluate(async () => (await fetch('/__test/history_cache')).json());
    assert.equal(cacheAfter.lines, cacheBefore.lines, 'warm navigation must not read Flash');
    console.log('PASS native history cache: cold=' + coldMs + 'ms warm=' + warmMs + 'ms cached=' + cacheAfter.cached + ' warm_Flash_reads=0');
    await new Promise(r => setTimeout(r, 3500));
    assert.equal(await first.locator('#train').textContent(), 'K9997');
    const before = await second.locator('#train').textContent();
    await new Promise(r => setTimeout(r, 5500));
    assert.notEqual(await second.locator('#train').textContent(), before);
    console.log('PASS full 9999 navigation, live preservation, polling updates');
    await first.waitForFunction(() => document.getElementById('state').textContent === '最近一次列车信息',
      {}, {timeout: 22000});
    assert.ok((await first.locator('#train').textContent()).startsWith('K100'));
    console.log('PASS native 20-second idle return');
    assert.ok(await first.locator('#historyReceived').isHidden());
    await first.locator('#historyDown').click();
    await first.waitForFunction(() => document.getElementById('state').textContent.includes('9999 / 9999'));
    await first.locator('#historyDown').click();
    await first.waitForFunction(() => document.getElementById('state').textContent.includes('1 / 9999'));
    await first.locator('#historyUp').click();
    await first.waitForFunction(() => document.getElementById('state').textContent.includes('9999 / 9999'));
    await first.locator('#historyBack').click();
    assert.equal(await first.locator('#state').textContent(), '最近一次列车信息');
    console.log('PASS bidirectional wrap + explicit return');
    await a.close();
    await Promise.race([second, third].map(page => page.waitForFunction(() =>
      !document.getElementById('historyDown').disabled, {}, {timeout: 45000})));
    const promoted = await second.locator('#historyDown').isDisabled() ? third : second;
    const polling = promoted === second ? third : second;
    await promoted.locator('#historyDown').click();
    await promoted.waitForFunction(() => document.getElementById('state').textContent.includes('/ 9999'));
    assert.ok(await polling.locator('#historyDown').isDisabled());
    assert.deepEqual(errors, []);
    console.log('PASS owner close => one new SSE owner; remaining page polls; no JS errors');
  } finally {
    await browser.close();
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
