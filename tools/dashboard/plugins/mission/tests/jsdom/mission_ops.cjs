/* Production HTML + adapter + Alpine; fixtures replace network inputs only. */
'use strict';
const fs = require('fs'), path = require('path'), assert = require('assert/strict');
const {JSDOM} = require('jsdom');
const root = path.resolve(__dirname, '../..');
const repo = path.resolve(root, '../../../..');
const script = fs.readFileSync(path.join(root, 'ops.js'), 'utf8');
const alpine = fs.readFileSync(path.join(repo, 'tools/dashboard/static/vendor/alpine-3.15.12.min.js'), 'utf8');
const config = {mission_id: 'm', org: 'autonomy'};
const html = fs.readFileSync(path.join(root, 'ops.html'), 'utf8')
  .replace('__OPS_SCRIPT__', '').replace('__OPS_CONFIG__', JSON.stringify(config));
const data = {
  org: 'autonomy', selected_mission: 'm', generated_at: new Date().toISOString(), errors: [],
  missions: [{mission_id: 'm', name: 'Our mission', status: 'active'}],
  pillars: [{mission_id: 'm', pillar_id: 'mc-infra', name: 'Infrastructure', coordinator_session: 'auto-123', bead_labels: ['pillar:mc']},
    {mission_id: 'm', pillar_id: 'dead', name: 'Dead seat', coordinator_session: 'auto-999'}],
  sessions: [{id: 'auto-123', label: 'Builder'}, {id: 'auto-456', label: 'Membership'}, {id: 'auto-789', label: 'No task'}],
  tasks: Array.from({length: 120}, (_, n) => ({id: 'task-' + n, title: 'Concrete work ' + n,
    status: n === 119 ? 'closed' : 'open', state: 'specified', readiness: 'ready', priority: 2,
    mission_ids: ['m'], pillar_labels: ['pillar:mc'], assignee: n === 0 ? 'session:auto-123' : n === 1 ? 'auto-456' : '',
    dependencies_known: true, blocks_on: [], updated_at: '2026-09-08T00:00:00Z', closed_at: n === 119 ? '2026-09-08T01:00:00Z' : null,
    mission_ops: n === 0 ? {phase: 'testing', phase_at: new Date().toISOString(), reported_by: 'auto-123'} : {}})),
  items: [{kind: 'question', state: 'open', mission_id: 'm', surface_id: 'mc-infra', key: 'mc-infra:q', item_id: 'q',
    title: 'Keep your words through a restart', ask: 'Should the small fix ship separately?', blocking: true,
    body: 'A concrete background with enough detail.', asked_by: 'auto-456', refs: ['bead:task-0', 'design:abc'],
    briefing: {subtitle: 'The fix is ready for a choice.', options: [{label: 'Separate', consequence: 'Test independently', text: 'Separate the fix.'}],
      artifacts: [{label: 'Design', href: '/design/abc'}, {label: 'Unsafe', href: 'javascript:alert(1)'}]}}]
};
let failWrite = false, posts = [];
const dom = new JSDOM(html, {url: 'https://dashboard.test/api/mission/screen/m/ops', runScripts: 'outside-only', pretendToBeVisual: true});
const w = dom.window;
let itemChanged, unsubscribed = false;
w.Autonomy = {_activePluginId:'mission',voice:{}};
w.dashboardEvents = {onSettingChanged(set, callback) {assert.equal(set,'mission.item');itemChanged=callback;return ()=>{unsubscribed=true;};}};
w.confirm = () => true;
w.fetch = async (url, options = {}) => {
  if (options.method === 'POST') {
    posts.push({url, body: JSON.parse(options.body)});
    if (!failWrite && url.endsWith('/answer')) data.items[0].state = 'answered';
    if (!failWrite && url.endsWith('/reply')) (data.items[0].discussion ||= []).push({by:'a'.repeat(64),at:new Date().toISOString(),text:JSON.parse(options.body).text});
    return {ok: !failWrite, status: failWrite ? 503 : 200, json: async () => ({ok: true, relayed: false})};
  }
  if (url.includes('/detail?')) return {ok: true, json: async () => ({tasks: {'task-0': {desc: 'Actual task specification', comments: []}}})};
  return {ok: true, json: async () => structuredClone(data)};
};
w.eval(script); w.eval(alpine);
const tick = async () => { await new Promise(r => setTimeout(r, 30)); await w.Alpine.nextTick(); };
const click = text => {
  const button = [...w.document.querySelectorAll('button')].find(b => b.textContent.trim() === text);
  assert(button, 'visible control exists: ' + text); button.click();
};
(async () => {
  await tick();
  const vm = w.Alpine.$data(w.document.querySelector('[data-testid="ops-root"]'));
  assert.equal(vm.issues.length, 121);
  assert.equal(vm.featured.headline, 'Keep your words through a restart');
  assert.equal(vm.featured.people.length, 2);
  assert.equal(vm.featured.artifacts.some(a => a.label === 'Unsafe'), false);
  assert.equal(vm.seats[1].status, 'Coordinator not in live roster');
  assert.equal(vm.unlinkedSessions[0].id, 'auto-789');
  assert.equal(vm.issues.find(i => i.id === 'task:task-0').phase, 'Testing');
  assert.equal(vm.issues.find(i => i.id === 'task:task-2').phase, 'Phase unknown');
  // Mission is the default scope; the organization-wide source is retained,
  // but neither its counts nor its other questions leak into this view.
  vm.raw.tasks.push({...vm.raw.tasks[2], id:'outside', mission_ids:[], assignee:'auto-789'});
  vm.raw.items.push({...vm.raw.items[0], key:'other:q', mission_id:'other', title:'Other mission question'});
  vm.project();
  assert.equal(vm.scopedIssues.length, 121);
  assert.equal(vm.count('all'), 120);
  assert(!vm.scopeLabel.includes('121 tasks'));
  assert.equal(vm.unlinkedSessions.length, 0, 'other-scope ownership is not misreported as unlinked');
  vm.scope = 'org'; assert.equal(vm.scopedIssues.length, 123);
  vm.scope = 'mission';
  vm.filter = 'all'; vm.sortBy('headline');
  assert.equal(vm.sortAria('headline'), 'ascending');
  assert.equal(vm.rows[0].headline, 'Concrete work 0');
  vm.sortBy('headline'); assert.equal(vm.rows[0].headline, 'Keep your words through a restart');
  vm.sortBy('updated'); assert.equal(vm.sortDirection, 'desc');
  assert.equal(vm.filteredRows.at(-1).item?.key, 'mc-infra:q', 'unknown timestamps remain last');
  vm.sortBy('sessions'); assert.equal(vm.rows[0].people[0][3], 'auto-123');
  vm.sortBy('phase'); assert.equal(vm.rows[0].phase, 'Decision needed');
  vm.sortKey = 'default';
  // Existing persisted replies must be visible without the in-memory receipt.
  data.items[0].discussion = [{by:'a'.repeat(64),at:new Date().toISOString(),text:'Merge it'},
    {by:'auto-123',at:new Date().toISOString(),text:'Which merge did you mean?'}];
  await vm.refresh(); await vm.open(vm.featured.id); await tick();
  assert.equal(vm.current.hasMemberReply, true);
  assert.equal(vm.conversation[0].text, 'Which merge did you mean?');
  assert(w.document.querySelector('[data-testid="saved-conversation"]').textContent.includes('Merge it'));
  assert.equal(Object.keys(vm.answers).length, 0);
  vm.back();
  vm.filter = 'all'; await tick();
  assert.equal(vm.rows.length, 100); click('Load more'); await tick(); assert.equal(vm.rows.length, 120);
  vm.search = 'Concrete work 118'; await tick(); assert.equal(vm.rows.length, 1); vm.search = '';
  await vm.open(vm.featured.id); await tick();
  assert(w.document.body.textContent.includes('A concrete background'));
  click('SeparateTest independently'); await tick(); assert.equal(vm.draft, 'Separate the fix.'); assert.equal(posts.length, 0);
  failWrite = true; click('Send ↑'); await tick(); assert.equal(vm.draft, 'Separate the fix.'); assert(vm.sendError.includes('Not saved'));
  failWrite = false; click('Send ↑'); await tick();
  assert(posts.at(-1).url.endsWith('/q/reply')); assert.equal(vm.current.item.state, 'open');
  assert.equal(vm.answers[vm.selectedId].receipt, 'Saved; relay not confirmed'); assert.equal(vm.draft, '');
  assert.equal(vm.notice, '', 'no duplicate persistent toast for a reply');
  assert.equal(w.document.getElementById('answer').parentElement.style.display, '', 'composer stays available');
  assert.equal(w.document.querySelectorAll('.receipt').length, 0, 'no receipt panels replace the conversation');
  vm.draft = 'A second message'; await tick(); click('Send ↑'); await tick();
  assert.equal(posts.at(-1).body.text, 'A second message'); assert.equal(vm.draft,'');
  assert.equal(vm.current.item.state,'open');
  vm.draft = 'Keep this follow-up draft'; data.items[0].ask = 'A clearer follow-up from the owner';
  itemChanged({org:'autonomy'}); await new Promise(r=>setTimeout(r,300)); await tick();
  assert.equal(vm.current.question,'A clearer follow-up from the owner');
  assert.equal(vm.draft,'Keep this follow-up draft');
  vm.draft = 'Explain the options'; click('Needs clarification'); await tick();
  assert(posts.at(-1).url.endsWith('/reply')); assert(posts.at(-1).body.text.startsWith('[Needs clarification]'));
  vm.edit(); vm.draft = 'Ship separately'; await tick(); click('Resolve question'); await tick();
  assert(posts.at(-1).url.endsWith('/answer')); assert.equal(vm.current, null); assert.equal(vm.view, 'overview');
  vm.report(); await tick(); vm.reportTitle = 'Menu broken'; vm.reportBody = 'Sharing does nothing';
  await vm.addReport(); await tick(); assert.equal(posts.at(-1).url, '/api/mission/chat/m/mc-infra');
  assert.equal(vm.notice, 'Saved; relay not confirmed');
  vm.raw.pillars = []; assert.equal(vm.reportPillar, null);
  assert.equal(vm.refHref('javascript:alert(1)'), '');
  assert.equal(vm.refHref('graph:abc-123'), 'graph://abc-123');
  assert(!w.document.body.textContent.includes('Saved locally'));
  vm.destroy(); assert.equal(unsubscribed,true); w.Alpine.stopObservingMutations(); dom.window.close(); console.log('PASS Ops production adapter and controls');
})().catch(e => { console.error(e); w.Alpine.stopObservingMutations(); dom.window.close(); process.exitCode = 1; });
