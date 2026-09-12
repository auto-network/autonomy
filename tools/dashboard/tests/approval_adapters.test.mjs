import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const { JSDOM } = createRequire(import.meta.url)('jsdom');
import { mintPasswordArmor } from '../static/js/ceremony/root-factor-policy.js';
import { bytesToHex } from '../static/js/ceremony/primitives.js';
let dom, instance, factory, centralFactory, requests, signs, state, signouts, armor, rootPub, publicKey;
const q = selector => selector==='[data-testid=approval-dialog]'?document.querySelector(selector):document.querySelector('[data-testid=approval-dialog]')?.shadowRoot.querySelector(selector);
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(fn) { for (let i = 0; i < 300 && !fn(); i++) await wait(10); assert.ok(fn(), 'expected visible state: '+q('#error')?.textContent+' / '+q('#result-title')?.textContent); }
function reply(data) { return { ok: true, json: async () => data }; }
test.before(async () => {
  const root = await crypto.subtle.generateKey('Ed25519', true, ['sign', 'verify']);
  publicKey = root.publicKey;
  const seed = new Uint8Array(await crypto.subtle.exportKey('pkcs8', root.privateKey)).slice(-32);
  rootPub = bytesToHex(new Uint8Array(await crypto.subtle.exportKey('raw', root.publicKey)));
  armor = await mintPasswordArmor({ rootSeed: seed, rootPub, password: 'pw', factorId: 'pw', iterations: 10000 }); seed.fill(0);
});
test.beforeEach(async () => {
  dom = new JSDOM('<body></body>', { url: 'https://example.test' });
  global.window = dom.window; global.document = dom.window.document;
  window.matchMedia = () => ({ matches: false });
  window.Element.prototype.getAnimations=()=>[];window.Element.prototype.animate=()=>({cancel(){}});
  window.HTMLCanvasElement.prototype.getContext = () => null;
  global.Alpine = { data: (_name, value) => { factory = value; } };
  requests = []; signs = []; signouts = 0;
  state = { signedIn: true, orgs: [{ org: 'org-uuid', live: true }] };
  window.AutonomyNetworkSession = {
    ready: async () => {}, state: () => state,
    signOnWithRootSeed: async () => { state = { signedIn: true, orgs: [{ org: 'org-uuid', live: true }] }; },
    signOut: async () => { signouts++; },
    _internals: { canonicalJson: JSON.stringify, bytesToHex },
  };
  window.AutonomyNetworkSigner = { signRegistryRequest: async (...args) => { signs.push(args); return { signed: 'existing-envelope' }; } };
  global.fetch = async (url, options) => {
    requests.push({ url, options });
    if (url === '/api/session/requester') return reply({ project: 'workspace', session_id: 'requester' });
    if (url === '/api/identity/status') return reply({ passkeys: [] });
    if (url === '/api/identity/personal') return reply({ armored_private_key: armor, root_pub: rootPub });
    if (url === '/api/identity/factor-policy') return reply({ armor_version: 3, factors: [{ factor_id: 'pw', label: 'Password' }] });
    if (url.endsWith('/decision')) return reply({ ok: true });
    if (url.endsWith('?wait=20')) return reply({ result: { execution: { ok: true } } });
    throw Error('Unexpected request ' + url);
  };
  await import('../static/js/pages/worktrees.js?test=' + Math.random());
  document.dispatchEvent(new window.Event('alpine:init'));
  instance = factory({});
});
test.afterEach(() => { instance?._sharedApprovalDialog?.dispose(); dom.window.close(); });
function request(ttl = 604800) {
  return { id: 'publish-1', kind: 'link_publish', session: 'requester', session_label: 'Release', target_title: 'Release note', type_label: 'Note', ttl,
    request: { org: 'autonomy', target_type: 'note', target_uuid: 'note-id' },
    acting_identity: { name: 'Autonomy Network' },
    registry_request: { payload: { org: 'org-uuid', target: 'note-id', meta: { label: 'Keep label', ttl } } } };
}
async function verifyPassword() {
  await until(() => q('#auth')?.hidden === false && !q('input[type=password]').disabled);
  const input = q('input[type=password]'); input.value = 'pw'; input.dispatchEvent(new window.Event('input'));
  q('.verify').click();
}
for (const [ttl, choice, expected] of [[604800, '2592000', 2592000], [12345, 'custom', 12345], [604800, 'none', null]]) {
  test('link adapter preserves exact signing route and selected TTL ' + choice, async () => {
    await instance._approvalKinds.link_publish.open(instance, request(ttl));
    const select = q('select'); select.value = choice; select.dispatchEvent(new window.Event('change'));
    q('#primary').click(); assert.equal(signs.length, 0);
    await until(() => q('#result-title')?.textContent === 'Link published');
    assert.equal(signs.length, 1);
    assert.deepEqual(signs[0].slice(0, 2), ['TUNNEL', '/control/create-link']);
    assert.equal(signs[0][2].meta.label, 'Keep label');
    assert.equal(signs[0][2].meta.ttl ?? null, expected);
    assert.deepEqual(signs[0][3], { org: 'autonomy' });
    const write = requests.find(row => row.options?.method === 'POST');
    assert.deepEqual(JSON.parse(write.options.body), { approved: true, envelope: { signed: 'existing-envelope' }, ttl: expected });
    assert.equal(signouts, 0);
  });
}
test('link unretained authority uses embedded root; unchecked retention signs out', async () => {
  state = { signedIn: false, orgs: [] };
  await instance._approvalKinds.link_publish.open(instance, request());
  assert.ok(q('input[type=checkbox]'));
  q('#primary').click(); await verifyPassword();
  await until(() => q('#result-title')?.textContent === 'Link published');
  assert.equal(signouts, 1); assert.equal(signs.length, 1);
});
test('link close and external resolution leave no actionable shared dialog or decision write', async () => {
  await instance._approvalKinds.link_publish.open(instance, request());
  q('#close').click();
  assert.equal(instance._sharedApprovalId, null);
  await instance._approvalKinds.link_publish.open(instance, request());
  instance._markApprovalDecided('publish-1');
  assert.equal(q('[data-testid=approval-dialog]'), null);
  assert.equal(requests.filter(row => row.options?.method === 'POST').length, 0);
});
test('a request arriving during execution is not acknowledged as rendered or substituted for the active request', async () => {
  const first = { ...request(), result: null }, second = { ...request(), id: 'publish-2', result: null };
  const acknowledgments = [];
  window.AutonomyWebPush = { acknowledgeApproval: async id => { acknowledgments.push(id); } };
  Object.defineProperty(document, 'visibilityState', { value: 'visible' });
  const rootFetch = global.fetch; let finish;
  global.fetch = async (url, options) => {
    if (url === '/api/approvals/publish-1') return reply(first);
    if (url === '/api/approvals/publish-2') return reply(second);
    if (url.endsWith('?wait=20')) return new Promise(resolve => { finish = () => resolve(reply({ result: { execution: { ok: true } } })); });
    return rootFetch(url, options);
  };
  await instance.openApprovalRequest(first.id);
  q('#primary').click(); await until(() => finish);
  await instance.openApprovalRequest(second.id);
  assert.equal(instance._sharedApprovalId, first.id);
  assert.deepEqual(acknowledgments, [first.id]);
  finish(); await until(() => q('#result-title')?.textContent === 'Link published');
  await instance.openApprovalRequest(second.id);
  assert.equal(instance._sharedApprovalId, second.id);
  assert.deepEqual(acknowledgments, [first.id, second.id]);
});
for (const applied of [false, true]) {
  test('Central exact grant signing and confirmed execution=' + applied, async () => {
    await import('../static/js/components/central-attention.js?test=' + Math.random());
    centralFactory ||= window.centralAttentionSurface;
    instance = centralFactory(); instance.refresh = async () => {};
    const rootFetch = global.fetch;
    let decision;
    global.fetch = async (url, options) => {
      if (url.endsWith('/approval-decision')) {
        decision = JSON.parse(options.body); return reply({ resolution: { outcome: 'granted' } });
      }
      if (url.startsWith('/api/attention/items/')) return reply({ review: { application_result: applied ? { execution: { ok: true } } : null } });
      return rootFetch(url, options);
    };
    const grant = { v: 1, nonce: 'fixed-server-nonce', grantee: 'frozen-scope-bound-requester-hash', scope: ['dashboard'], expires_at: 1900000000 };
    await instance.openDashboardApproval({ id: 'attention-1', requester: {href:'/session/workspace/requester',byline:'Workspace'}, safeReview: { grant, requester_label: 'Release', expires_at: grant.expires_at }, actions: ['granted', 'declined'] });
    assert.equal(q('#requester-link').getAttribute('href'),'/session/workspace/requester');
    q('#primary').click(); await verifyPassword();
    await until(() => q('#result-title')?.textContent === (applied ? 'Access allowed' : 'Could not complete the request'));
    assert.deepEqual(decision.decision.grant, grant);
    assert.equal(decision.outcome, 'granted');
    const input = new TextEncoder().encode('autonomy.identity.dashboard-access-grant.v1\n' + JSON.stringify(grant));
    assert.equal(await crypto.subtle.verify('Ed25519', publicKey, Buffer.from(decision.decision.signature, 'hex'), input), true);
  });
}
