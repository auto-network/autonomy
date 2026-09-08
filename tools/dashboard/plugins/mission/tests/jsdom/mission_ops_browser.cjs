/* Real Chromium behavioral sweep; same production document/adapter, mocked HTTP inputs. */
'use strict';
const fs = require('fs'), path = require('path'), http = require('http');
const {execFile} = require('child_process'), {promisify} = require('util');
const run = promisify(execFile), assert = require('assert/strict');
const root = path.resolve(__dirname, '../..'), repo = path.resolve(root, '../../../..');
const config = {mission_id: 'm', org: 'autonomy'};
const app = fs.readFileSync(path.join(repo,'tools/dashboard/static/app.js'),'utf8');
const route = app.slice(app.indexOf('async function route()'));
assert(route.indexOf("new CustomEvent('app:navigating')") >= 0);
assert(route.indexOf("new CustomEvent('app:navigating')") < route.indexOf('window.Autonomy._activePluginId = null'), 'surface cleanup runs before lease identity disappears');
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
let posted = [], failPost = false;
const server = http.createServer((req, res) => {
  if (req.url === '/fail-next') { failPost=true; res.end('ok'); return; }
  if (req.url === '/voice-store.js') { res.setHeader('Content-Type','application/javascript'); res.end(fs.readFileSync(path.join(repo,'tools/dashboard/static/js/lib/voice-store.js'))); return; }
  if (req.url === '/capsule-test') {
    res.setHeader('Content-Type','text/html');
    res.end(`<script>window.Autonomy={_activePluginId:'mission',plugins:[{id:'mission',voice:{live_transcript:true,replace_caption:true,replace_controls:true}}]};</script><script src="/voice-store.js"></script><script defer src="/static/vendor/alpine-3.15.12.min.js"></script><iframe id="outer" src="/capsule-wrapper" style="width:100%;height:95vh"></iframe>`); return;
  }
  if (req.url === '/capsule-wrapper') {
    res.setHeader('Content-Type','text/html'); res.end('<div class="mc-view on"><iframe id="ops" src="/ops" style="width:100%;height:95vh"></iframe></div>'); return;
  }
  if (req.url.startsWith('/static/vendor/')) {
    const file = path.join(repo, 'tools/dashboard/static/vendor', path.basename(req.url));
    res.setHeader('Content-Type', 'application/javascript'); res.end(fs.readFileSync(file)); return;
  }
  if (req.method === 'POST') {
    let raw = ''; req.on('data', d => { raw += d; }); req.on('end', () => {
      posted.push({path: req.url, ...JSON.parse(raw)}); res.setHeader('Content-Type', 'application/json');
      if (failPost) {failPost=false;res.statusCode=503;res.end('{"ok":false}');return;}
      res.end('{"ok":true,"relayed":false}');
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
  await browser('open', 'http://127.0.0.1:' + server.address().port + '/capsule-test');
  const dictation = JSON.parse(await browser('eval', `(async()=>{
    let win,vm;
    for(let n=0;n<80;n++){
      win=document.getElementById('outer')?.contentDocument?.getElementById('ops')?.contentWindow;
      const root=win?.document.querySelector('[data-testid="ops-root"]');
      if(root&&win.Alpine){vm=win.Alpine.$data(root);if(!vm.loading&&vm.issues.length)break;}
      await new Promise(r=>setTimeout(r,50));
    }
    Alpine.store('flags',{get(key){return key==='voice.client_enabled';}});
    const voice=Alpine.store('voice');voice.boundSessionId='auto-123';voice.setMicMode('muted');
    const wait=()=>new Promise(r=>setTimeout(r,30));
    await vm.open(vm.featured.id);await win.Alpine.nextTick();
    vm.draft='Before after';await win.Alpine.nextTick();
    win.document.getElementById('answer').setSelectionRange(7,7);
    voice.setBufferText('first words');win.document.querySelector('.mic').click();await wait();
    const out={imported:vm.draft==='Before first words after',listening:vm.talking===true,owned:voice.surfaceClaim?.controls==='plugin'};
    window.__voiceDiagnostic={draft:vm.draft,error:vm.voiceError,mode:voice.micMode,claim:voice.surfaceClaim,
      chain:[win,win.parent,win.parent.parent].map(w=>({url:w.location.href,active:w.Autonomy?._activePluginId,api:!!w.Autonomy?.voice,enabled:w.Alpine?.store('voice')?.enabled,frame:!!w.frameElement}))};
    voice.setBufferText('corrected words');await wait();out.rewritten=vm.draft==='Before corrected words after';
    vm.voiceSnapshot({revision:0,text:'old hypothesis',sessionId:'auto-123',update:'partial'});out.stale=vm.draft==='Before corrected words after';
    vm.stopDictation();const held=vm.draft;voice.setBufferText('late');await wait();out.paused=vm.draft===held&&voice.micMode==='muted'&&!voice.surfaceClaim;
    voice.clearBuffer();vm.dictate();await wait();voice.setBufferText('fresh');await wait();
    const ta=win.document.getElementById('answer');ta.value='Manually edited';ta.dispatchEvent(new win.Event('input',{bubbles:true}));await wait();voice.setBufferText('must not overwrite');await wait();
    out.edited=vm.draft==='Manually edited'&&!vm.talking;
    voice.clearBuffer();vm.dictate();await wait();voice.setBufferText('kept on back');await wait();const beforeBack=vm.draft;vm.back();voice.setBufferText('wrong question');await wait();out.back=vm.drafts[vm.selectedId]===beforeBack&&!vm.talking&&!voice.surfaceClaim;
    await vm.open(vm.featured.id);await win.Alpine.nextTick();voice.clearBuffer();vm.dictate();await wait();voice.clearBuffer('clear');await wait();out.reset=!vm.talking;
    voice.boundSessionId='';const beforeMissing=vm.draft;vm.dictate();await wait();out.missingBinding=!vm.talking&&vm.draft===beforeMissing&&!!vm.voiceError;
    voice.boundSessionId='auto-123';voice.clearBuffer();vm.dictate();await wait();
    const newer=Autonomy.voice.claimSurface({caption:'plugin',controls:'plugin'});voice.setBufferText('belongs to another surface');vm.stopDictation();out.otherLease=!!voice.surfaceClaim&&voice.bufferText==='belongs to another surface';newer.release();voice.setMicMode('muted');
    voice.clearBuffer();vm.dictate();await wait();document.getElementById('outer').contentDocument.querySelector('.mc-view').classList.remove('on');await wait();out.hidden=!vm.talking&&voice.micMode==='muted';
    document.getElementById('outer').contentDocument.querySelector('.mc-view').classList.add('on');voice.clearBuffer();vm.dictate();await wait();window.dispatchEvent(new Event('app:navigating'));await wait();out.exit=!vm.talking&&!voice.surfaceClaim&&voice.micMode==='muted';
    voice.clearBuffer();vm.dictate();await wait();Autonomy.voice.releaseSurfaceClaim();voice.setMicMode('listening');vm.stopDictation();out.claimless=voice.micMode==='listening';voice.setMicMode('muted');
    voice.clearBuffer();vm.dictate();await wait();voice.setBufferText('keep after failed send');await wait();const failedDraft=vm.draft;await fetch('/fail-next');await vm.submit();out.failedSend=vm.draft===failedDraft&&!!vm.sendError&&!vm.talking&&voice.micMode==='muted';
    vm.raw.items.push({...vm.raw.items[0],key:'mc-infra:q2',item_id:'q2',title:'Second question'});vm.project();voice.clearBuffer();vm.dictate();await wait();voice.setBufferText('only for first question');await wait();const oldId=vm.selectedId,oldDraft=vm.draft;await vm.open('question:m:mc-infra:q2');voice.setBufferText('late after switch');await wait();out.switchQuestion=vm.drafts[oldId]===oldDraft&&vm.draft===''&&!vm.talking;
    Autonomy.plugins[0].voice.live_transcript=false;vm.dictate();await wait();out.noGrant=!vm.talking&&!!vm.voiceError;
    Autonomy.plugins[0].voice.live_transcript=true;
    voice.clearBuffer();vm.dictate();await wait();out.activeBeforeRemoval=vm.talking;document.getElementById('outer').remove();await wait();out.removed=!vm.talking&&voice.micMode==='muted';
    return out;
  })()`));
  for (const [check, passed] of Object.entries(dictation)) {
    if (!passed) console.error('Voice diagnostic', await browser('eval', 'window.__voiceDiagnostic'), dictation);
    assert.equal(passed, true, check);
  }
  assert.equal(posted.length, 3, 'only the explicit failed Send added a request');
  assert(posted.every(p=>p.path.endsWith('/q/reply')), 'no session send or implicit question resolution');
  console.log('PASS Ops Chromium desktop/mobile behavioral sweep');
})().catch(e => { console.error(e); process.exitCode = 1; }).finally(async () => {
  try { await browser('close'); } catch (_) {} server.close();
});
