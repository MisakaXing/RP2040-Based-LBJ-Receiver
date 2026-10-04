// Pipe WirelessPortal._render_page() to stdin. Requires playwright + Chrome.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let html = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', value => html += value);
process.stdin.on('end', async () => {
  const chrome = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  const browser = await chromium.launch({headless: true,
    executablePath: process.env.LBJ_BROWSER_EXECUTABLE ||
      (process.platform === 'darwin' && fs.existsSync(chrome) ? chrome : undefined)});
  try {
    for (const [name, width, height] of [
      ['phone',390,844], ['small-phone',360,640], ['ipad',820,1180], ['desktop',1440,900]]) {
      const page = await browser.newPage({viewport:{width,height},deviceScaleFactor:2});
      const errors = [];
      page.on('pageerror', error => errors.push(String(error)));
      await page.addInitScript(() => {
        window.EventSource = class {
          constructor() {
            this.readyState=0;this.listeners={};window.testStream=this;
            setTimeout(() => {
              this.readyState=1;this.onopen();
              this.listeners.session({data:JSON.stringify({history_token:'test-token'})});
              this.listeners.train({data:JSON.stringify({available:true,update_id:'1',train_no:'57082',
                loco:'轨道探伤车-04782A',speed:'127',km:'1000.0',route:'BEA9B9E3CFDF2020',
                time:'2026-10-04 22:30',cab:'A端',rssi:'-89.0dBm',type:'train_data_full',
                longitude:116.285307,latitude:39.893762,
                device:{usb_power:true,battery_voltage:4.18,core_temp_c:42.1}})});
            },20);
          }
          addEventListener(name, callback){this.listeners[name]=callback}
          close(){this.readyState=2}
        };
      });
      await page.route('http://receiver/**', route => {
        const url = new URL(route.request().url());
        if (url.pathname === '/api/history') return route.fulfill({json:{available:true,
          update_id:'1',train_no:'57721',loco:'FXD1BA-0001A',speed:'---',km:'---',
          route:'BEA9B9E3CFDF2020',time:'2026-10-03 21:32',cab:'A端',rssi:'-97.0dBm',
          type:'basic_only',longitude:114.1,latitude:30.2,history_index:9998,history_count:9999,
          device:{usb_power:true,battery_voltage:4.18,core_temp_c:42.1}}});
        return route.fulfill({contentType:'text/html',body:html});
      });
      await page.goto('http://receiver/');
      await page.waitForFunction(() => !document.getElementById('historyUp').disabled);
      assert.equal(await page.locator('#route').textContent(),'京广线');
      const dimensions = await page.evaluate(() => ({
        width:document.documentElement.scrollWidth,height:document.documentElement.scrollHeight,
        buttons:[...document.querySelectorAll('.history-nav button,#soundBtn')]
          .map(button => button.getBoundingClientRect().height)}));
      assert.ok(dimensions.width <= width, 'horizontal overflow');
      assert.ok(dimensions.buttons.every(value => value >= 44), 'touch targets too small');
      if (name === 'phone') assert.ok(dimensions.height <= height, JSON.stringify(dimensions));
      const screenshot = async state => {
        if (process.env.LBJ_SCREENSHOT_DIR) await page.screenshot({
          path:path.join(process.env.LBJ_SCREENSHOT_DIR,`${name}-${state}.png`),fullPage:true});
      };
      await screenshot('live');
      await page.locator('#historyDown').click();
      await page.waitForFunction(() => document.getElementById('state').textContent.includes('9999 / 9999'));
      assert.equal(await page.locator('#train').textContent(),'57721');
      assert.equal(await page.locator('#received').textContent(),'2026-10-03 21:32');
      assert.equal(await page.locator('#historyReceived').textContent(),'接收于 2026-10-03 21:32');
      assert.ok(await page.locator('#historyReceived').isVisible());
      const historySize = await page.evaluate(() => ({width:document.documentElement.scrollWidth,
        height:document.documentElement.scrollHeight}));
      assert.ok(historySize.width <= width);
      if (name === 'phone') assert.ok(historySize.height <= height,JSON.stringify(historySize));
      await screenshot('history');
      await page.locator('#historyBack').click();
      assert.equal(await page.locator('#train').textContent(),'57082');
      await page.evaluate(() => {testStream.readyState=2;testStream.onerror()});
      assert.ok(await page.locator('#historyUp').isDisabled());
      assert.match(await page.locator('#historyHint').textContent(),/轮询/);
      assert.deepEqual(errors,[]);
      console.log('PASS layout',name,dimensions);
      await page.close();
    }
  } finally {await browser.close()}
});
