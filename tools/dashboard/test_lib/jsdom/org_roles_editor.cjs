// The Roles editor and the invite picker (auto-l05o7, roles design of record
// graph://d1b3db8f-879): cards render capabilities in plain words and never a
// raw scope in the headline; Define role and New version sign through the
// ceremony with the expected payload; a narrower version names what holders
// lose; Grant/Revoke sign with the persona; the picker lists only inviteable
// roles, defaults to the least-privileged one, and composes its sentence
// from the catalog.
const { JSDOM } = require('jsdom');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const assert = require('node:assert/strict');

const orgSettings = readFileSync(resolve(__dirname, '../../static/js/org-settings.js'), 'utf8');
const membership = readFileSync(resolve(__dirname, '../../static/js/org-membership.js'), 'utf8');
// The catalog is an ES module; strip the export keywords so jsdom runs it as
// a classic script and it installs window.AutonomyScopeCatalog as production does.
const catalog = readFileSync(resolve(__dirname, '../../static/js/scope-catalog.js'), 'utf8')
  .replace(/^export default .*$/m, '')
  .replace(/^export function/mg, 'function');
const settle = () => new Promise((r) => setTimeout(r, 0));
// Arrays built inside the jsdom realm are not node's Array; compare by value.
const arr = (x) => Array.from(x || []);

const GENESIS = 'a0'.repeat(32);
const PERSONA_ME = '7c'.repeat(32);
const PERSONA_PRIYA = '5a'.repeat(32);

function view() {
  return {
    founded: true,
    genesis_id: GENESIS,
    org_uuid: '11111111-1111-4111-8111-111111111111',
    heads: ['b4'.repeat(32)],
    members: [
      { persona: PERSONA_ME, display_name: 'Jeremy', avatar: null, color: '#0f766e', roles: ['owner'], sponsor: '9d'.repeat(32), current_key: PERSONA_ME },
      { persona: PERSONA_PRIYA, display_name: 'Priya', avatar: null, color: null, roles: ['member'], sponsor: PERSONA_ME, current_key: PERSONA_PRIYA },
    ],
    role_defs: [
      { name: 'admin', version: 1, claim_requires: 'admin-ack', approver_threshold: 2, scope_set: ['invite:member', 'role:grant:member'],
        holders: [], bare_holders: [], minter_may_invite: true, viewer_may_grant: true,
        threshold_warning: { role: 'admin', approver_threshold: 2, admission_authority_holders: [PERSONA_ME] } },
      { name: 'member', version: 2, claim_requires: 'admin-ack', approver_threshold: 1, scope_set: ['invite:member'],
        holders: [PERSONA_PRIYA], bare_holders: [], minter_may_invite: true, viewer_may_grant: true, threshold_warning: null },
      { name: 'owner', version: 1, claim_requires: 'self', approver_threshold: 1, scope_set: ['*'],
        holders: [PERSONA_ME], bare_holders: [], minter_may_invite: false, viewer_may_grant: true, threshold_warning: null },
    ],
    invites: [],
    pending_claims: [],
    viewer_persona: PERSONA_ME,
    org_key_owner_kem_pub: 'cc'.repeat(32),
  };
}

async function mount(data) {
  const dom = new JSDOM('<!doctype html><body></body>', { runScripts: 'dangerously', url: 'https://dashboard.test/' });
  const w = dom.window;
  w.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} });
  const ceremony = { defines: [], grants: [], revokes: [] };
  w.AutonomyMembershipTestHooks = {
    openRoot: () => ({ seed: new Uint8Array(32) }),
    roleCeremony: {
      defineRole: async (args) => { ceremony.defines.push(args); return { eventId: 'ee'.repeat(32), version: args.version || 1 }; },
      grantRole: async (args) => { ceremony.grants.push(args); return { eventId: 'ef'.repeat(32) }; },
      revokeRole: async (args) => { ceremony.revokes.push(args); return { eventId: 'f0'.repeat(32) }; },
    },
  };
  w.fetch = async (url) => {
    if (url === '/api/orgs/autonomy') return { ok: true, json: async () => ({ identity_resolved: { name: 'Autonomy Network' } }) };
    if (url === '/api/orgs/autonomy/membership') return { ok: true, json: async () => data };
    throw new Error('unexpected fetch ' + url);
  };
  for (const source of [catalog, orgSettings, membership]) {
    const s = w.document.createElement('script');
    s.textContent = source;
    w.document.head.appendChild(s);
  }
  assert.ok(w.AutonomyScopeCatalog, 'catalog global installed');
  w.AutonomyOrgSettings.open('autonomy');
  await settle();
  w.document.querySelector('[data-testid="orgset-rail-membership"]').click();
  await settle(); await settle();
  return { w, ceremony };
}

async function main() {
  const { w, ceremony } = await mount(view());
  const pane = () => w.document.querySelector('.org-membership');
  const text = () => pane().textContent;

  // ── Roles tab: plain words, versions, warning chip, no raw scopes on the face ──
  w.document.querySelector('[data-tab="roles"]').click();
  await settle();
  const rows = pane().querySelectorAll('[data-role]');
  assert.equal(rows.length, 3);
  assert.ok(!text().includes('invite:member'), 'raw scope on the headline view');
  assert.match(pane().querySelector('[data-role="member"]').textContent, /v2/);
  assert.match(pane().querySelector('[data-role="admin"]').textContent, /Approvers short/);
  assert.ok(pane().querySelector('[data-action="open-define"]'));
  assert.ok(!pane().querySelector('[data-action="starter-roles"]'), 'starter roles only when Owner is alone');

  pane().querySelector('[data-role="member"]').click();
  await settle();
  const caps = pane().querySelector('[data-testid="role-caps"]');
  assert.match(caps.textContent, /Invite people as member/);
  assert.ok(!caps.textContent.includes('invite:member'));
  assert.ok(pane().querySelector('[data-action="open-version"]'));

  // ── Define role: capabilities are catalog choices; the ceremony gets the payload ──
  pane().querySelector('[data-action="open-define"]').click();
  await settle();
  const editor = () => pane().querySelector('[data-testid="role-editor"]');
  assert.ok(editor());
  const nameInput = editor().querySelector('[data-role-name-input]');
  nameInput.value = 'reviewer';
  nameInput.dispatchEvent(new w.Event('input', { bubbles: true }));
  const box = editor().querySelector('[data-role-scope="invite:member"]');
  assert.ok(box, 'templated capability expanded for a known role');
  box.checked = true;
  box.dispatchEvent(new w.Event('change', { bubbles: true }));
  await settle();
  assert.match(pane().querySelector('[data-testid="role-preview"]').textContent, /Reviewer: invite people as member\. Joins after 1 approval\./);
  pane().querySelector('[data-action="role-submit"]').click();
  await settle(); await settle(); await settle();
  assert.equal(ceremony.defines.length, 1);
  const defined = ceremony.defines[0];
  assert.equal(defined.name, 'reviewer');
  assert.deepEqual(arr(defined.scopeSet), ['invite:member']);
  assert.equal(defined.claimRequires, 'admin-ack');
  assert.equal(defined.approverThreshold, 1);
  assert.equal(defined.version, null, 'a new role lets the ceremony settle the version');
  assert.equal(defined.org, 'autonomy');

  // ── New version: narrowing names what holders lose ──
  await settle();
  w.document.querySelector('[data-tab="roles"]').click();
  await settle();
  pane().querySelector('[data-role="member"]').click();
  await settle();
  pane().querySelector('[data-action="open-version"]').click();
  await settle();
  const narrow = editor().querySelector('[data-role-scope="invite:member"]');
  assert.equal(narrow.checked, true, 'new version starts from the current set');
  narrow.checked = false;
  narrow.dispatchEvent(new w.Event('change', { bubbles: true }));
  await settle();
  assert.match(pane().querySelector('[data-testid="role-loss"]').textContent, /Removing invite people as member from everyone who holds Member/);
  assert.match(pane().querySelector('[data-testid="role-loss"]').textContent, /contraction/);
  pane().querySelector('[data-action="role-submit"]').click();
  await settle(); await settle(); await settle();
  assert.equal(ceremony.defines.length, 2);
  assert.equal(ceremony.defines[1].name, 'member');
  assert.equal(ceremony.defines[1].version, 3, 'a new version is pinned to current + 1');
  assert.deepEqual(arr(ceremony.defines[1].scopeSet), []);

  // ── Grant / Revoke from the member detail ──
  w.document.querySelector('[data-tab="members"]').click();
  await settle();
  const priya = pane().querySelector('[data-member="' + PERSONA_PRIYA + '"]');
  priya.click();
  await settle();
  const detail = pane().querySelector('.mem-detail');
  const grantAdmin = detail.querySelector('[data-action="grant-role"][data-role-name="admin"]');
  const revokeMember = detail.querySelector('[data-action="revoke-role"][data-role-name="member"]');
  assert.ok(grantAdmin && revokeMember, 'grant for roles not held, revoke for roles held');
  assert.ok(detail.querySelector('[data-action="message"]'), 'existing detail actions survive');
  grantAdmin.click();
  await settle(); await settle(); await settle();
  assert.equal(ceremony.grants.length, 1);
  assert.equal(ceremony.grants[0].persona, PERSONA_PRIYA);
  assert.equal(ceremony.grants[0].role, 'admin');
  assert.equal(ceremony.grants[0].genesisId, GENESIS);

  // ── Picker: inviteable roles only, least privilege first, catalog sentence ──
  w.document.querySelector('[data-tab="invites"]').click();
  await settle();
  pane().querySelector('[data-action="open-mint"]').click();
  await settle();
  const select = pane().querySelector('[data-mint-role]');
  assert.ok(select, 'two inviteable roles show the picker');
  const options = Array.from(select.options).map((o) => o.value);
  assert.deepEqual(options, ['admin', 'member'], 'owner is not inviteable by this viewer');
  assert.equal(select.value, 'member', 'defaults to the least-privileged role');
  assert.match(text(), /This invitation grants Member: invite people as member\. Joins after 1 approval\./);
  select.value = 'admin';
  select.dispatchEvent(new w.Event('change', { bubbles: true }));
  await settle();
  assert.match(text(), /This invitation grants Admin: invite people as member, approve and grant member\. Joins after 2 approvals\./);

  // ── Starter roles appear only when Owner is alone ──
  {
    const lone = view();
    lone.role_defs = [Object.assign({}, lone.role_defs[2], { minter_may_invite: true })];
    lone.members = [lone.members[0]];
    const m = await mount(lone);
    m.w.document.querySelector('[data-tab="roles"]').click();
    await settle();
    const p = m.w.document.querySelector('.org-membership');
    assert.match(p.querySelector('[data-action="starter-roles"]').textContent, /Add the Member role/);
    p.querySelector('[data-action="starter-roles"]').click();
    await settle(); await settle(); await settle(); await settle();
    // The starter action signs exactly the ruled set: Member alone today.
    assert.deepEqual(m.ceremony.defines.map((d) => d.name), ['member']);
    assert.deepEqual(arr(m.ceremony.defines[0].scopeSet), []);
    assert.equal(m.ceremony.defines[0].claimRequires, 'admin-ack');
    assert.equal(m.ceremony.defines[0].approverThreshold, 1);
    // With one inviteable role the picker hides but the sentence is composed.
    m.w.document.querySelector('[data-tab="invites"]').click();
    await settle();
    p.querySelector('[data-action="open-mint"]').click();
    await settle();
    assert.ok(!p.querySelector('[data-mint-role]'));
    assert.match(p.textContent, /grants owner: full authority — the only role this organization defines today\./i);
  }

  console.log('PASS org roles editor');
  process.exit(0);
}

main().catch((error) => { console.error(error); process.exit(1); });
