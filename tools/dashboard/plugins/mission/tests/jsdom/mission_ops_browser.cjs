/* Real Chromium behavioral sweep; same production document/adapter, mocked HTTP inputs. */
'use strict';
const fs = require('fs'), path = require('path'), http = require('http');
const {execFile} = require('child_process'), {promisify} = require('util');
const run = promisify(execFile), assert = require('assert/strict');
const root = path.resolve(__dirname, '../..'), repo = path.resolve(root, '../../../..');
const config = {mission_id: 'm', org: 'autonomy'};
const html = fs.readFileSync(path.join(root, 'ops.html'), 'utf8')
  .replace('__OPS_SCRIPT__', fs.readFileSync(path.join(root, 'ops.js'), 'utf8'))
  .replace('__OPS_CONFIG__', JSON.stringify(config));
const payload = {org: 'autonomy', generated_at: new Date().toISOString(), errors: [],
  missions: [{mission_id: 'm', name: 'Operational work', status: 'active'}],
  pillars: [{mission_id: 'm', pillar_id: 'mc-infra', name: 'Infrastructure', coordinator_session: 'auto-123'}],
  sessions: [{id: 'auto-123', label: 'Builder'}], tasks: [],
  items: [{kind: 'question', state: 'open', title: 'Keep dictated words through a restart',
    ask: 'Should the small fix ship separately?', body: 'A real question has enough background to answer.',
    mission_id: 'm', surface_id: 'mc-infra', item_id: 'q', key: 'mc-infra:q', asked_by: 'auto-123',
    briefing: {artifacts: [{label: 'Review design', href: '/design/abc'}]}}]};
let posted = [];
const server = http.createServer((req, res) => {
  if (req.url.startsWith('/static/vendor/')) {
    const file = path.join(repo, 'tools/dashboard/static/vendor', path.basename(req.url));
    res.setHeader('Content-Type', 'application/javascript'); res.end(fs.readFileSync(file)); return;
  }
  if (req.method === 'POST') {
    let raw = ''; req.on('data', d => { raw += d; }); req.on('end', () => {
      posted.push({path: req.url, ...JSON.parse(raw)}); res.setHeader('Content-Type', 'application/json'); res.end('{"ok":true,"relayed":false}');
    }); return;
  }
  if (req.url === '/api/mission/ops/m') { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(payload)); return; }
  res.setHeader('Content-Type', 'text/html'); res.end(html);
});
const session = 'mission-ops-sweep-' + process.pid;
async function browser(...args) { return (await run('agent-browser', ['--session', session, ...args], {timeout: 35000, maxBuffer: 2e6})).stdout; }
(async () => {
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  await browser('open', 'http://127.0.0.1:' + server.address().port);
  await browser('wait', '[data-testid="ops-root"]');
  for (const [width, height] of [[1440, 1000], [390, 844]]) {
    await browser('set', 'viewport', String(width), String(height));
    const result = await browser('eval', `(async()=>{
      const root=document.querySelector('[data-testid="ops-root"]'), vm=Alpine.$data(root);
      for(let n=0;n<40&&vm.loading;n++)await new Promise(r=>setTimeout(r,50));
      await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
      const out={loaded:vm.issues.length===1,overflow:document.documentElement.scrollWidth>innerWidth};
      root.querySelector('.hero button').click();await Alpine.nextTick();
      await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
      out.question=document.querySelector('.answerbox h2').textContent==='Should the small fix ship separately?';
      out.background=document.body.textContent.includes('enough background');
      out.design=[...document.querySelectorAll('a')].some(a=>a.textContent.includes('Review design')&&a.offsetParent!==null);
      out.detailOverflow=document.documentElement.scrollWidth>innerWidth;
      const ta=document.getElementById('answer');ta.value='Please explain the impact';ta.dispatchEvent(new Event('input',{bubbles:true}));await Alpine.nextTick();
      [...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='Send ↑').click();
      for(let n=0;n<40&&vm.sending;n++)await new Promise(r=>setTimeout(r,50));await Alpine.nextTick();
      out.receipt=document.body.textContent.includes('Saved; relay not confirmed');
      out.stillOpen=vm.current.item.state==='open';vm.edit();vm.back();return out;
    })()`);
    const parsed = JSON.parse(result);
    assert.equal(parsed.loaded, true); assert.equal(parsed.overflow, false);
    assert.equal(parsed.question, true); assert.equal(parsed.background, true); assert.equal(parsed.design, true);
    assert.equal(parsed.detailOverflow, false); assert.equal(parsed.receipt, true); assert.equal(parsed.stillOpen, true);
  }
  assert.equal(posted.length, 2); assert(posted.every(p => p.path.endsWith('/q/reply')));
  console.log('PASS Ops Chromium desktop/mobile behavioral sweep');
})().catch(e => { console.error(e); process.exitCode = 1; }).finally(async () => {
  try { await browser('close'); } catch (_) {} server.close();
});
