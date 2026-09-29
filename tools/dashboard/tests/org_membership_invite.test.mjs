// auto-xvqxz: inviting is ONE approval. The Membership screen opens one
// approval dialog, and its single unlock signs the invitation, prepares the
// link operation, signs it with the same open root, and publishes.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
const { JSDOM } = createRequire(import.meta.url)('jsdom');

const SOURCE = readFileSync(new URL('../static/js/org-membership.js', import.meta.url), 'utf8');
const URL_SOURCE = readFileSync(new URL('../static/js/invitation-url.js', import.meta.url), 'utf8');
// Values built inside the jsdom window live in its realm; compare plain copies.
const plain = value => JSON.parse(JSON.stringify(value));
const VIEW = {
  founded: true, genesis_id: 'genesis-1', org_uuid: 'org-uuid-1', heads: [],
  members: [], invites: [], pending_claims: [], viewer_persona: 'me',
  role_defs: [{ name: 'member', scope_set: [], claim_requires: 'admin-ack', version: 1 }],
};
const PAYLOAD = {
  org: 'org-uuid-1', target_uuid: 'org-uuid-1', target_type: 'org:join',
  invite_ref: 'invite-1', expires_at: 1234, meta: { label: "Dean's invite" },
};

let window, page, events, dialog, replies;

function reply(status, body) {
  return { ok: status < 400, status, json: async () => body };
}

// The screen polls every 5 s; each test closes its window (clearing the
// timer) when it ends — a suite-level hook would wait for the loop to drain.
async function setup(t, overrides = {}) {
  const dom = new JSDOM('<body></body>', { url: 'https://example.test', runScripts: 'outside-only' });
  t.after(() => dom.window.close());
  window = dom.window;
  events = [];
  dialog = null;
  replies = {
    prepare: reply(200, { operation_id: 'op-1', signing: { registry_request: { payload: PAYLOAD }, org_uuid: 'org-uuid-1' } }),
    carryOut: reply(200, { execution: { ok: true, url: 'https://auto.network/l/' + 'c'.repeat(32), channel_pub: 'ce'.repeat(32) } }),
    ...overrides,
  };
  window.AutonomyOrgSettings = { register: definition => { page = definition; } };
  window.AutonomyMembershipTestHooks = {
    openRoot: () => { events.push('unlock'); return { seed: new Uint8Array(32).fill(7), rootPub: 'root-pub' }; },
    inviteCeremony: {
      mintOrgInvite: async args => {
        events.push(['mint', args.role, args.maxUses, args.personalRootSeed[0]]);
        return { inviteId: 'invite-1', bearer: 'b'.repeat(64), expiry: 1234 };
      },
    },
    linkSigning: {
      _matchingApprovalAuthority: () => false,
      signLinkWithOpenRoot: async (req, opened) => {
        events.push(['sign-link', req.registryRequest.payload, opened.seed[0], req.allowSessionApprovals]);
        return { envelope: { payload: req.registryRequest.payload } };
      },
    },
    approvalDialog: { openApprovalDialog: options => { dialog = options; return {}; } },
  };
  window.fetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    const body = options.body ? JSON.parse(options.body) : null;
    events.push([method, url, body]);
    if (url === '/api/orgs/boatlore/membership') return reply(200, VIEW);
    if (url === '/api/orgs') return reply(200, { orgs: [{ org: { slug: 'boatlore' }, identity: { payload: { name: 'Boatlore', favicon: '' } } }] });
    if (url === '/api/links/operations') return replies.prepare;
    if (url === '/api/links/operations/op-1') return replies.carryOut;
    if (url === '/api/network/ledger/invite/bearer') return reply(200, { ok: true, stored: true });
    throw new Error('Unexpected request ' + method + ' ' + url);
  };
  window.eval(URL_SOURCE);
  window.eval(SOURCE);
  const root = await page.render('boatlore');
  window.document.body.append(root);
  root.querySelector('[data-tab="invites"]').click();
  root.querySelector('[data-action="open-mint"]').click();
  const label = root.querySelector('[data-mint-label]');
  label.value = "Dean's invite";
  label.dispatchEvent(new window.Event('input'));
  root.querySelector('[data-action="mint"]').click();
  for (let i = 0; i < 100 && !dialog; i++) await new Promise(resolve => setTimeout(resolve, 5));
  assert.ok(dialog, 'the one approval dialog opened');
  return root;
}

const names = () => events.map(event => (typeof event === 'string' ? event : event[0] === 'GET' || event[0] === 'POST' ? event[0] + ' ' + event[1] : event[0]));

test('the dialog explains both halves and nothing is signed before Approve', async (t) => {
  await setup(t);
  assert.equal(dialog.review.title, 'Invite someone to Boatlore');
  assert.match(dialog.review.intro, /signs the invitation/);
  assert.match(dialog.review.intro, /publishes the link/);
  assert.deepEqual(plain(dialog.review.facts.map(([label]) => label)), ['Joins as', 'Link expires', 'Can be used by', 'Join link']);
  assert.equal(dialog.review.target.name, "Dean's invite");
  assert.equal(dialog.result.success, 'Invitation published');
  assert.ok(!names().some(name => name === 'unlock' || name === 'mint' || name.startsWith('POST /api/links')));
});

test('one unlock signs the invitation and its link, then Done shows the link', async (t) => {
  const root = await setup(t);
  const signed = await dialog.authorize({});
  assert.deepEqual(plain(signed), { envelope: { payload: PAYLOAD } });
  const order = names();
  assert.equal(order.filter(name => name === 'unlock').length, 1);
  const at = name => order.indexOf(name);
  assert.ok(at('unlock') < at('mint') && at('mint') < at('POST /api/links/operations')
    && at('POST /api/links/operations') < at('sign-link'), order.join(' | '));
  const sign = events.find(event => event[0] === 'sign-link');
  assert.equal(sign[2], 7, 'the link is signed with the root the invitation opened');
  const prepared = events.find(event => event[1] === '/api/links/operations')[2];
  assert.deepEqual(plain(prepared), {
    op: 'publish',
    request: { org: 'boatlore', target_uuid: 'org-uuid-1', target_type: 'org:join',
      invite_ref: 'invite-1', expires_at: 1234, meta: { label: "Dean's invite" } },
  });

  const outcome = await dialog.execute(signed);
  assert.equal(outcome.execution.ok, true);
  const carried = events.find(event => event[1] === '/api/links/operations/op-1')[2];
  assert.deepEqual(plain(carried), { envelope: { payload: PAYLOAD } });
  const retained = events.find(event => event[1] === '/api/network/ledger/invite/bearer')[2];
  assert.deepEqual(plain(retained), { org: 'boatlore', invite_ref: 'invite-1', token: 'b'.repeat(64) });

  dialog.onClose();
  const code = root.querySelector('.mem-link-code');
  assert.ok(code, 'Done lands on the show-once panel');
  assert.match(code.textContent, /^https:\/\/auto\.network\/l\/c{32}#/);
  assert.match(root.textContent, /copy it again from Invitations/);
});

test('a publish that fails says the invitation has no link, and signs nothing else', async (t) => {
  await setup(t, { carryOut: reply(200, { execution: { ok: false, error: 'the serving tunnel did not come up' } }) });
  const signed = await dialog.authorize({});
  await assert.rejects(dialog.execute(signed), error => {
    assert.match(error.message, /serving tunnel did not come up/);
    assert.match(error.message, /deactivate it if that was unintended/);
    return true;
  });
  assert.ok(!names().some(name => /revoke|bearer/.test(name)));
});

test('a refused link operation stops before the link is signed', async (t) => {
  await setup(t, { prepare: reply(422, { error: 'invalid_request', detail: 'boatlore is not registered with auto.network yet' }) });
  await assert.rejects(dialog.authorize({}), /not registered with auto\.network yet.*deactivate it/);
  assert.ok(!names().includes('sign-link'));
});
