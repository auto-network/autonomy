import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const { JSDOM } = createRequire(import.meta.url)('jsdom');
import { mintPasswordArmor } from '../static/js/ceremony/root-factor-policy.js';
import { bytesToHex } from '../static/js/ceremony/primitives.js';
import { openLinkCentralApproval, linkReviewState, operateLinkDirectly } from '../static/js/components/link-central-approval.js';

// Ported from the retired Worktrees link-sheet tests in approval_adapters.test.mjs
// (auto-fkhq0.10a). Same authority and signing; the Central order is Grant
// first, then sign, then the link operation.
let dom, log, signs, state, signouts, armor, rootPub, result, bootstrap, execution;
const q = s => document.querySelector('[data-testid=approval-dialog]')?.shadowRoot.querySelector(s);
const shadowText = () => document.querySelector('[data-testid=approval-dialog]').shadowRoot.textContent;
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(fn) { for (let i = 0; i < 300 && !fn(); i++) await wait(10); assert.ok(fn(), 'expected visible state: ' + q('#error')?.textContent + ' / ' + q('#result-title')?.textContent + ' / ' + q('#result-copy')?.textContent); }
const reply = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
test.before(async () => {
  const root = await crypto.subtle.generateKey('Ed25519', true, ['sign', 'verify']);
  const seed = new Uint8Array(await crypto.subtle.exportKey('pkcs8', root.privateKey)).slice(-32);
  rootPub = bytesToHex(new Uint8Array(await crypto.subtle.exportKey('raw', root.publicKey)));
  armor = await mintPasswordArmor({ rootSeed: seed, rootPub, password: 'pw', factorId: 'pw', iterations: 10000 }); seed.fill(0);
});
const PAYLOAD = { org: 'org-uuid', target: 'note-id', meta: { label: 'Keep label', ttl: 604800 } };
test.beforeEach(() => {
  dom = new JSDOM('<body></body>', { url: 'https://example.test' });
  global.window = dom.window; global.document = dom.window.document;
  window.matchMedia = () => ({ matches: false });
  window.Element.prototype.getAnimations = () => []; window.Element.prototype.animate = () => ({ cancel() {} });
  window.HTMLCanvasElement.prototype.getContext = () => null;
  log = []; signs = []; signouts = 0;
  state = { signedIn: true, orgs: [{ org: 'org-uuid', live: true }] };
  result = { state: 'pending', machine_label: 'Home' };
  bootstrap = { registry_request: { method: 'TUNNEL', path: '/control/create-link', payload: PAYLOAD }, org_uuid: 'org-uuid', target_preview: null, binding_drift: false, blocking_error: null };
  execution = { ok: true, url: 'https://relay.auto.network/l/abc' };
  window.AutonomyNetworkSession = {
    ready: async () => {}, state: () => state,
    signOnWithRootSeed: async () => { state = { signedIn: true, orgs: [{ org: 'org-uuid', live: true }] }; },
    signOut: async () => { signouts++; },
    _internals: { canonicalJson: JSON.stringify, bytesToHex },
  };
  window.AutonomyNetworkSigner = { signRegistryRequest: async (...args) => { log.push('sign'); signs.push(args); return { signed: 'envelope' }; } };
  global.fetch = async (url, options) => {
    url = String(url);
    if (url === '/api/session/requester') return reply({ project: 'workspace', session_id: 'requester' });
    if (url === '/api/identity/status') return reply({ passkeys: [] });
    if (url === '/api/identity/personal') return reply({ armored_private_key: armor, root_pub: rootPub });
    if (url === '/api/identity/factor-policy') return reply({ armor_version: 3, factors: [{ factor_id: 'pw', label: 'Password' }] });
    if (url.endsWith('/link-operation-bootstrap')) return reply(bootstrap);
    if (url.endsWith('/approval-decision')) { const b = JSON.parse(options.body); log.push('decision:' + b.outcome); return reply({ resolution: { outcome: b.outcome } }); }
    if (url.endsWith('/link-operation')) { log.push('operation'); log.push(JSON.parse(options.body)); return reply({ execution }); }
    if (url === '/api/attention/items/L') return reply({ review: { application_result: result } });
    throw Error('Unexpected request ' + url);
  };
});
test.afterEach(() => { document.querySelector('[data-testid=approval-dialog]')?.remove(); dom.window.close(); });
function item(review = {}, actions = ['granted', 'declined']) {
  return { id: 'L', actions, requester: { href: '/session/workspace/requester' },
    safeReview: { requester_label: 'requester · Release', op: 'publish', org: 'autonomy', target_type: 'note', type_label: 'Note',
      target_title: 'Release note', ttl: 604800, fixed_expiry: false, acting_identity: { name: 'Autonomy Network' }, machine_label: 'Home', ...review } };
}
async function verifyPassword() {
  await until(() => q('#auth')?.hidden === false && !q('input[type=password]').disabled);
  const input = q('input[type=password]'); input.value = 'pw'; input.dispatchEvent(new window.Event('input'));
  q('.verify').click();
}
const operationBody = () => log.find(entry => typeof entry === 'object');

for (const [ttl, choice, expected] of [[604800, '2592000', 2592000], [12345, 'custom', 12345], [604800, 'none', null]]) {
  test('Grant first, then the exact signing route and the selected TTL ' + choice, async () => {
    await openLinkCentralApproval(item({ ttl }));
    const select = q('select'); select.value = choice; select.dispatchEvent(new window.Event('change'));
    q('#primary').click();
    await until(() => q('#result-title')?.textContent === 'Link published');
    assert.deepEqual(log.filter(e => typeof e === 'string'), ['decision:granted', 'sign', 'operation']);
    assert.deepEqual(signs[0].slice(0, 2), ['TUNNEL', '/control/create-link']);
    assert.equal(signs[0][2].meta.label, 'Keep label');
    assert.equal(signs[0][2].meta.ttl ?? null, expected);
    assert.deepEqual(signs[0][3], { org: 'autonomy' });
    assert.deepEqual(operationBody(), { envelope: { signed: 'envelope' }, ttl: expected });
    assert.equal(signouts, 0);
  });
}

test('unretained authority unlocks the personal root after the Grant; unchecked retention signs out', async () => {
  state = { signedIn: false, orgs: [] };
  await openLinkCentralApproval(item());
  assert.ok(q('input[type=checkbox]'));
  q('#primary').click(); await verifyPassword();
  await until(() => q('#result-title')?.textContent === 'Link published');
  assert.equal(log[0], 'decision:granted');
  assert.equal(signouts, 1); assert.equal(signs.length, 1);
});

test('review names the organization, target, requesting session and recipient', async () => {
  await openLinkCentralApproval(item({ recipient: { participant_id: 'guest:ab', display_name: 'Alex Guest' } }));
  assert.equal(q('#org-name').textContent, 'Autonomy Network');
  assert.match(shadowText(), /Release note/);
  assert.equal(q('#requester-kind').textContent, 'Requesting session');
  assert.match(shadowText(), /Prepared for/);
  assert.match(shadowText(), /Alex Guest/);
  assert.equal(q('#primary').disabled, false);
});

test('drift blocks the review, and Authorize writes nothing', async () => {
  bootstrap = { ...bootstrap, binding_drift: true };
  await openLinkCentralApproval(item());
  assert.equal(q('#review-unavailable').hidden, false);
  assert.match(q('#review-unavailable').textContent, /changed after the request was prepared/);
  assert.equal(q('#primary').disabled, true);
  q('#primary').click(); await wait(50);
  assert.equal(signs.length, 0); assert.deepEqual(log, []);
});

test('a request without its frozen registry request is blocked with a reason, and nothing is written', async () => {
  bootstrap = { ...bootstrap, registry_request: null };
  await openLinkCentralApproval(item());
  assert.equal(q('#review-unavailable').hidden, false);
  assert.match(q('#review-unavailable').textContent, /missing its auto.network details/);
  assert.equal(q('#primary').disabled, true);
  q('#primary').click(); await wait(50);
  assert.deepEqual(log, []);
});

test('an org:join publish shows its fixed expiry and signs the frozen payload with no TTL', async () => {
  const expiry = 1900000000123;
  const orgUuid = '11111111-1111-4111-8111-111111111111';
  state = { signedIn: true, orgs: [{ org: orgUuid, live: true }] };
  const payload = { org: orgUuid, target_uuid: orgUuid, target_type: 'org:join', invite_ref: 'ef'.repeat(32), expires_at: expiry, meta: { label: 'Member invitation' } };
  bootstrap = { ...bootstrap, registry_request: { payload }, org_uuid: orgUuid };
  await openLinkCentralApproval(item({ ttl: null, fixed_expiry: true, absolute_expiry: expiry, target_type: 'org:join' }));
  assert.equal(q('select'), null, 'no duration choice for an invitation');
  assert.ok(shadowText().includes('Link expires'));
  assert.ok(shadowText().includes(new Date(expiry).toLocaleString()));
  q('#primary').click();
  await until(() => q('#result-title')?.textContent === 'Link published');
  assert.deepEqual(signs[0].slice(0, 3), ['TUNNEL', '/control/create-link', payload]);
  assert.deepEqual(operationBody(), { envelope: { signed: 'envelope' } });
});

// auto-eky23 (live 2026-09-28: approval 39fec671372c failed after approval,
// "it takes no duration"): a standing follow link offers no duration, says so,
// and signs the frozen payload with no TTL on it or on the operation.
test('an org:follow publish offers no duration, says No expiration, and signs no TTL', async () => {
  const orgUuid = '22222222-2222-4222-8222-222222222222';
  state = { signedIn: true, orgs: [{ org: orgUuid, live: true }] };
  const payload = { org: orgUuid, target_uuid: orgUuid, target_type: 'org:follow', meta: { label: 'Follow us', org: 'autonomy' } };
  bootstrap = { ...bootstrap, registry_request: { payload }, org_uuid: orgUuid };
  await openLinkCentralApproval(item({ ttl: null, fixed_expiry: true, target_type: 'org:follow' }));
  assert.equal(q('select'), null, 'no duration choice for a follow link');
  assert.ok(shadowText().includes('Link expires'));
  assert.ok(shadowText().includes('No expiration'));
  q('#primary').click();
  await until(() => q('#result-title')?.textContent === 'Link published');
  assert.deepEqual(signs[0].slice(0, 3), ['TUNNEL', '/control/create-link', payload]);
  assert.deepEqual(operationBody(), { envelope: { signed: 'envelope' } });
});

test('decline records a decline and signs nothing', async () => {
  await openLinkCentralApproval(item());
  q('#secondary').click(); q('#primary').click();
  await until(() => q('#result-title')?.textContent === 'Request declined');
  assert.deepEqual(log, ['decision:declined']); assert.equal(signs.length, 0);
});

test('approved but not yet published: Publish signs and operates with no new decision', async () => {
  result = { state: 'awaiting_operation', machine_label: 'Home' };
  await openLinkCentralApproval(item({}, []));
  assert.match(q('#facts').textContent, /StatusApproved, not yet published/);
  assert.doesNotMatch(q('#facts').textContent, /Publish by/);
  q('#primary').click();
  await until(() => q('#result-title')?.textContent === 'Link published');
  assert.deepEqual(log.filter(e => typeof e === 'string'), ['sign', 'operation']);
});

test('a revoke signs the revoke route with an empty payload and no TTL', async () => {
  bootstrap = { ...bootstrap, registry_request: { method: 'TUNNEL', path: '/control/revoke-link', payload: {} } };
  await openLinkCentralApproval(item({ op: 'revoke', ttl: null }));
  assert.equal(q('select'), null);
  q('#primary').click();
  await until(() => q('#result-title')?.textContent === 'Link revoked');
  assert.deepEqual(signs[0].slice(0, 3), ['TUNNEL', '/control/revoke-link', {}]);
  assert.deepEqual(operationBody(), { envelope: { signed: 'envelope' } });
});

test('a failed operation is reported with the server text, and nothing is re-granted', async () => {
  execution = { ok: false, error: 'the registry refused the link' };
  await openLinkCentralApproval(item());
  q('#primary').click();
  await until(() => q('#result-title')?.textContent === 'Could not complete the request');
  assert.match(q('#result-copy').textContent, /registry refused the link/);
  assert.equal(log.filter(e => e === 'decision:granted').length, 1);
});

test('elsewhere and expired states say why and offer no action', async () => {
  result = { state: 'elsewhere', machine_label: 'Office NUC' };
  await openLinkCentralApproval(item());
  assert.equal(q('#primary').disabled, true);
  assert.match(q('#review-unavailable').textContent, /Only Office NUC can carry this out/);
});

// The operator's own link requests (org invitation, asset share, fleet
// invitation): prepared, reviewed, signed only on explicit confirm, published.
function direct({ status = 200, prepared } = {}) {
  const base = global.fetch;
  global.fetch = async (url, options) => {
    url = String(url);
    if (url === '/api/links/operations') {
      log.push('prepare'); log.push(JSON.parse(options.body));
      return reply(prepared || { operation_id: 'op-1',
        review: { op: 'publish', org: 'autonomy', target_type: 'note', type_label: 'Note', target_title: 'Release note', ttl: 604800, fixed_expiry: false, acting_identity: { name: 'Autonomy Network' } },
        signing: bootstrap }, status);
    }
    if (url === '/api/links/operations/op-1') { log.push('operation'); log.push(JSON.parse(options.body)); return reply({ execution }); }
    return base(url, options);
  };
}
test('a direct request shows the review, signs only on confirm, and resolves to the execution', async () => {
  direct();
  const request = { org: 'autonomy', target_type: 'note', target_uuid: 'note-id', meta: {} };
  const outcome = operateLinkDirectly({ op: 'publish', request, requester: 'Share by link' });
  await until(() => q('#primary'));
  // A retained session could sign at once; it still waits for the operator.
  await wait(50); assert.equal(signs.length, 0);
  assert.deepEqual(log[1], { op: 'publish', request });
  q('#primary').click();
  await until(() => q('#result-title')?.textContent === 'Link published');
  q('#close')?.click(); q('#primary')?.click();
  assert.deepEqual(await outcome, execution);
  assert.deepEqual(log.filter(e => typeof e === 'string'), ['prepare', 'sign', 'operation']);
  assert.deepEqual(operationBody(), { op: 'publish', request });
  assert.deepEqual(log.filter(e => typeof e === 'object').pop(), { envelope: { signed: 'envelope' }, ttl: 604800 });
});
test('closing a direct review without confirming signs and publishes nothing', async () => {
  direct();
  const outcome = operateLinkDirectly({ op: 'publish', request: { org: 'autonomy' } });
  await until(() => q('#close'));
  q('#close').click();
  assert.equal(await outcome, null);
  assert.equal(signs.length, 0);
  assert.ok(!log.includes('operation'));
});
test('a planner refusal reports its detail and opens no review', async () => {
  direct({ status: 422, prepared: { error: 'invalid_request', detail: 'register autonomy from its organization page (/orgs/autonomy) first' } });
  await assert.rejects(operateLinkDirectly({ op: 'publish', request: { org: 'autonomy' } }), /register autonomy from its organization page/);
  assert.equal(document.querySelector('[data-testid=approval-dialog]'), null);
});
