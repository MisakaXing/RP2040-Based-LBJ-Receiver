// Execute the actual embedded page script with minimal browser fixtures.
const assert = require('node:assert/strict');
const vm = require('node:vm');
let html = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { html += chunk; });
process.stdin.on('end', async () => {
  try {
    const nodes = new Map();
    const el = id => {
      if (!nodes.has(id)) {
        const classes = new Set();
        nodes.set(id, {textContent: '', dataset: {}, style: {}, offsetWidth: 1,
          classList: {add: x => classes.add(x), remove: x => classes.delete(x),
            contains: x => classes.has(x), toggle: (x, on) => on ? classes.add(x) : classes.delete(x)}});
      }
      return nodes.get(id);
    };
    let eventSource, nextPoll, fetches = 0, announcements = 0;
    class EventSource {
      constructor() { this.readyState = 0; this.handlers = {}; eventSource = this; }
      addEventListener(name, cb) { this.handlers[name] = cb; }
      close() { this.readyState = 2; }
    }
    const context = vm.createContext({document: {body: {dataset: {recordId: '0'}},
      getElementById: el, addEventListener() {}},
      window: {EventSource, addEventListener() {}}, EventSource, TextDecoder,
      setTimeout: () => 1, clearTimeout() {},
      fetch: async url => { assert.ok(url.startsWith('/api/latest?t=')); fetches++;
        return {ok: true, json: async () => nextPoll}; }});
    vm.runInContext(html.split('<script>')[1].split('</script>')[0], context);
    context.countAnnouncement = () => { announcements++; };
    vm.runInContext('announce=countAnnouncement', context);
    const snapshot = (id, battery, temp) => ({update_id: String(id), available: true,
      train_no: '57721', device: {battery_percent: battery, core_temp_c: temp}});
    const push = d => eventSource.handlers.train({data: JSON.stringify(d)});
    eventSource.readyState = 1; eventSource.onopen();
    push(snapshot(1, 19, 45.1));
    assert.equal(el('battery').textContent, '19%');
    assert.equal(el('temperature').textContent, '45.1°C');
    assert.ok(el('batteryCard').classList.contains('danger'));
    assert.ok(el('tempCard').classList.contains('danger'));
    assert.match(el('batteryNote').textContent, /警告/);
    assert.match(el('tempNote').textContent, /警告/);
    assert.equal(announcements, 1);
    push(snapshot(2, 20, 45));
    assert.ok(!el('batteryCard').classList.contains('danger'));
    assert.ok(!el('tempCard').classList.contains('danger'));
    push(snapshot(2, null, null));
    assert.equal(el('battery').textContent, '---');
    assert.equal(el('temperature').textContent, '---');
    assert.equal(announcements, 2); // No new-train alert for unchanged ID.
    push(snapshot(1, 1, 80)); // An older response must not restore warnings.
    assert.equal(el('battery').textContent, '---');
    eventSource.readyState = 2; eventSource.onerror();
    nextPoll = snapshot(3, 10, 50);
    vm.runInContext('fallbackPoll()', context);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(fetches, 1);
    assert.equal(el('train').textContent, '57721');
    assert.equal(el('battery').textContent, '10%');
    assert.equal(el('temperature').textContent, '50.0°C');
    assert.ok(el('tempCard').classList.contains('danger'));
    assert.equal(announcements, 3);
    console.log('PASS: thresholds, recovery, unknown, stale response, shared SSE/polling');
  } catch (error) { console.error(error); process.exitCode = 1; }
});
