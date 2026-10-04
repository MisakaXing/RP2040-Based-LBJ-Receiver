const assert = require('node:assert/strict');
const vm = require('node:vm');
let html = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', x => html += x);
process.stdin.on('end', async () => {
  try {
    const nodes = new Map(), timers = new Map(), responses = [], events = {}; let clock = 0, source, calls = [], delayed;
    const el = id => { if (!nodes.has(id)) { const classes = new Set(); nodes.set(id, {
      textContent: '', dataset: {}, style: {}, offsetWidth: 1, disabled: false,
      classList: {add: x => classes.add(x), remove: x => classes.delete(x),
        toggle: (x,on) => on ? classes.add(x) : classes.delete(x)}}); } return nodes.get(id); };
    class EventSource {constructor(){this.readyState=0;this.handlers={};source=this}
      addEventListener(n,f){this.handlers[n]=f} close(){this.readyState=2} }
    const snapshot = id => ({available:true,update_id:String(id),train_no:String(id),
      time:'2026-10-04 10:'+String(id%60).padStart(2,'0'),device:{battery_percent:50}});
    const ctx = vm.createContext({document:{body:{dataset:{recordId:'0'},classList:el('body').classList},
      getElementById:el,addEventListener:(name,fn)=>events[name]=fn},window:{EventSource,addEventListener(){}},EventSource,TextDecoder,AbortController,
      setTimeout:(f,ms)=>{const id=++clock;timers.set(id,{f,ms});return id},clearTimeout:id=>timers.delete(id),
      fetch:async url=>{calls.push(url);if(delayed)return new Promise(r=>delayed=r);
        if(responses.length){const response=responses.shift();if(response instanceof Error)throw response;
          return {ok:response.status===200,status:response.status,json:async()=>response.body};}
        const params=new URL(url,'http://device').searchParams,current=Number(params.get('index'));
        const index=current<0?2:(current+Number(params.get('step'))+3)%3;
        return {ok:true,status:200,json:async()=>({...snapshot(100+index),history_index:index,history_count:3})};}});
    vm.runInContext(html.split('<script>')[1].split('</script>')[0],ctx);
    const run = code => vm.runInContext(code,ctx);
    assert.ok(el('historyUp').disabled);
    await run('browseHistory(1)');assert.equal(calls.length,0);
    source.readyState=1;source.onopen();source.handlers.session({data:JSON.stringify({history_token:'secret'})});
    source.handlers.train({data:JSON.stringify(snapshot(1))});assert.ok(!el('historyUp').disabled);
    await el('historyUp').onclick();assert.match(calls.at(-1),/index=-1&step=-1/);
    assert.match(el('state').textContent,/3 \/ 3/);
    assert.equal(el('historyReceived').hidden,false);
    assert.equal(el('historyReceived').textContent,'接收于 2026-10-04 10:42');
    source.handlers.train({data:JSON.stringify(snapshot(2))});assert.equal(el('train').textContent,'102');
    assert.equal(el('received').textContent,'2026-10-04 10:42');
    assert.equal(el('historyReceived').textContent,'接收于 2026-10-04 10:42');
    await el('historyUp').onclick();assert.match(calls.at(-1),/index=2&step=-1/);assert.equal(el('train').textContent,'101');
    await el('historyDown').onclick();assert.match(calls.at(-1),/index=1&step=1/);assert.equal(el('train').textContent,'102');
    await el('historyDown').onclick();assert.equal(el('train').textContent,'100');
    await el('historyUp').onclick();assert.equal(el('train').textContent,'102');
    run('returnLive()');assert.equal(el('train').textContent,'2');
    assert.equal(el('historyReceived').hidden,true);
    assert.equal(el('received').textContent,'2026-10-04 10:02');
    await run('browseHistory(1)');assert.match(calls.at(-1),/index=-1/);
    [...timers.values()].find(t=>t.ms===20000).f();assert.equal(el('train').textContent,'2');
    delayed=true;const pending=run('browseHistory(-1)');await new Promise(r=>setImmediate(r));
    assert.ok(el('historyUp').disabled);run('returnLive()');
    delayed({ok:true,json:async()=>({...snapshot(999),history_index:0,history_count:3})});delayed=null;
    await pending;assert.equal(el('train').textContent,'2'); // Ignore late response after return.
    const reply = (status,body) => responses.push({status,body});
    reply(200,{empty:true,count:0});await run('browseHistory(-1)');
    assert.equal(el('train').textContent,'2');assert.match(el('historyHint').textContent,/暂无/);
    reply(409,{error:'history_changed'});await run('browseHistory(-1)');
    assert.equal(el('train').textContent,'2');assert.match(el('historyHint').textContent,/已变更/);
    reply(503,{error:'history_read_failed'});await run('browseHistory(-1)');
    assert.match(el('historyHint').textContent,/读取失败/);assert.ok(!el('historyUp').disabled);
    responses.push(new Error('network offline'));await run('browseHistory(-1)');
    assert.match(el('historyHint').textContent,/读取失败/);assert.equal(el('train').textContent,'2');
    // Busy retries are bounded and do not consume a history index or prevent return.
    const fireRetry = async () => {await new Promise(r=>setImmediate(r));
      const entry=[...timers.entries()].find(([,t])=>t.ms===400);assert.ok(entry);
      timers.delete(entry[0]);entry[1].f();await new Promise(r=>setImmediate(r));};
    reply(503,{error:'receiver_busy'});
    let retrying=run('browseHistory(-1)');await fireRetry();await retrying;
    assert.equal(el('train').textContent,'102');assert.match(el('state').textContent,/3 \/ 3/);
    run('returnLive()');const beforeRetries=calls.length;
    for(let i=0;i<8;i++)reply(503,{error:'receiver_busy'});
    retrying=run('browseHistory(1)');for(let i=0;i<7;i++)await fireRetry();await retrying;
    assert.equal(calls.length-beforeRetries,8);assert.match(el('historyHint').textContent,/读取失败/);
    assert.equal(el('train').textContent,'2');assert.ok(!el('historyUp').disabled);
    reply(503,{error:'receiver_busy'});retrying=run('browseHistory(1)');
    await new Promise(r=>setImmediate(r));run('returnLive()');const beforeCancel=calls.length;
    await fireRetry();await retrying;assert.equal(calls.length,beforeCancel);
    assert.equal(el('train').textContent,'2');
    await run('browseHistory(1)');const priorIdle=run('historyTimer');events.pointerdown();
    assert.ok(!timers.has(priorIdle));assert.equal(timers.get(run('historyTimer')).ms,20000);
    run('returnLive()');reply(403,{error:'sse_required'});await run('browseHistory(-1)');
    assert.ok(el('historyUp').disabled);assert.equal(run('historyToken'),null);
    source.handlers.session({data:JSON.stringify({history_token:'secret-2'})});
    await run('browseHistory(-1)');source.readyState=0;source.onerror();
    assert.equal(el('train').textContent,'2');assert.ok(el('historyUp').disabled);
    assert.match(el('historyHint').textContent,/轮询模式/);
    console.log('PASS history: authorization, navigation, live preservation, idle, stale result, empty/reset, errors, bounded busy retries, cancellation, 403, disconnect');
  } catch(e) {console.error(e);process.exitCode=1;}
});
