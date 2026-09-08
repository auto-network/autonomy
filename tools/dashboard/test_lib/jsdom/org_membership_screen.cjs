// The Membership screen (approved revision 8c40dd8d, bead auto-0ch5i):
// QA-2's truthfulness suite — every rendered fact derives from the projection.
const { JSDOM } = require('jsdom');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const assert = require('node:assert/strict');

const orgSettings = readFileSync(resolve(__dirname, '../../static/js/org-settings.js'), 'utf8');
const membership = readFileSync(resolve(__dirname, '../../static/js/org-membership.js'), 'utf8');
const settle = () => new Promise((r) => setTimeout(r, 0));

const GENESIS = 'a0'.repeat(32);
const PERSONA_ME = '7c'.repeat(32);
const PERSONA_DEAN = '5a'.repeat(32);
const INVITE_LIVE = 'c5'.repeat(32);
const BEARER = 'ab'.repeat(32);

function baseView() {
  return {
    founded: true,
    genesis_id: GENESIS,
    org_uuid: '11111111-1111-4111-8111-111111111111',
    heads: ['b4'.repeat(32)],
    members: [
      { persona: PERSONA_ME, display_name: 'Jeremy', avatar: '4fde3638-009', color: '#0f766e', roles: ['owner'], sponsor: '9d'.repeat(32), current_key: PERSONA_ME },
      { persona: PERSONA_DEAN, display_name: null, avatar: null, color: null, roles: ['owner'], sponsor: PERSONA_ME, current_key: PERSONA_DEAN },
    ],
    role_defs: [{ name: 'owner', claim_requires: 'self', approver_threshold: 1, scope_set: ['*'] }],
    invites: [
      { invite_id: INVITE_LIVE, status: 'live', granted_role: 'owner', expiry: Date.now() + 3 * 86400000, sponsor: PERSONA_ME, binding: 'bearer', uses: { max_uses: 5, used: 1, remaining: 4 }, join_url: 'https://auto.network/l/' + '9f'.repeat(16), label: "Dean's invite" },
      { invite_id: 'd6'.repeat(32), status: 'claimed', granted_role: 'owner', expiry: Date.now() - 86400000, sponsor: PERSONA_ME, binding: 'key', uses: { max_uses: 1, used: 1, remaining: 0 }, join_url: null, label: null },
      { invite_id: 'e7'.repeat(32), status: 'expired', granted_role: 'owner', expiry: Date.now() - 2 * 86400000, sponsor: PERSONA_ME, binding: 'bearer', uses: { max_uses: 1, used: 0, remaining: 1 }, join_url: null, label: null },
    ],
    pending_claims: [
      { claim_key: 'f8'.repeat(32), persona_pub: '1c'.repeat(32), invite_ref: INVITE_LIVE, submitted_at: Date.now() - 540000, granted_role: 'owner', introduction: { display_name: 'Priya N.' }, invite_label: "Dean's invite", invite_binding: 'bearer', have: 0, need: 1, ready: false, body: { invite_ref: INVITE_LIVE, persona_pub: '1c'.repeat(32), profile: {}, approvals: [], token: BEARER } },
    ],
    viewer_persona: PERSONA_ME,
  };
}

async function mount(view) {
  const dom = new JSDOM('<!doctype html><body></body>', { runScripts: 'dangerously', url: 'https://dashboard.test/' });
  const w = dom.window;
  w.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} });
  const calls = [];
  w.fetch = async (url, options = {}) => {
    calls.push([url, options]);
    if (url === '/api/orgs/autonomy') {
      return { ok: true, json: async () => ({ identity_resolved: { name: 'Autonomy Network' } }) };
    }
    if (url === '/api/orgs/autonomy/membership') {
      return { ok: true, json: async () => view };
    }
    throw new Error('unexpected fetch ' + url);
  };
  for (const source of [orgSettings, membership]) {
    const s = w.document.createElement('script');
    s.textContent = source;
    w.document.head.appendChild(s);
  }
  w.AutonomyOrgSettings.open('autonomy');
  await settle();
  w.document.querySelector('[data-testid="orgset-rail-membership"]').click();
  await settle(); await settle();
  return { w, calls };
}

async function main() {
  // ── unfounded ──
  {
    const { w } = await mount({ founded: false });
    assert.match(w.document.querySelector('.mem-empty').textContent, /no membership ledger yet/);
  }

  // ── the full projection ──
  const view = baseView();
  const { w } = await mount(view);
  const pane = () => w.document.querySelector('.org-membership');
  const text = () => pane().textContent;

  // Title and rail badge (pending request => blocking tone).
  // The badge contributes the org initial to textContent when no favicon exists.
  assert.match(w.document.querySelector('.orgset-title').textContent.replace(/\s+/g, ' ').trim(), /Autonomy Network–Membership$/);
  assert.equal(w.document.querySelector('[data-testid="orgset-count-membership"]').textContent, '1');

  // Opens on Invitations because a request is pending; superscript counts
  // live + pending only.
  assert.equal(w.document.querySelector('.mem-tabs .on').textContent, 'Invitations2');

  // The dossier row: introduction name, provenance, no key material.
  assert.match(text(), /Priya N\./);
  assert.match(text(), /Via Dean's invite/);
  assert.ok(!/[0-9a-f]{12}/.test(text()), 'key material leaked into the pane');
  assert.ok(!text().includes(BEARER), 'bearer leaked into the pane');
  assert.equal(pane().querySelector('[data-action="approve"]').textContent, 'Approve');
  assert.ok(!text().includes('…'), 'ellipsis button labels are ruled out');

  // Live-only invitations: claimed and expired rows do not render; the live
  // row shows its name, role, expiry, and capacity, with icon actions.
  assert.equal(pane().querySelectorAll('[data-invite]').length, 1);
  assert.match(text(), /Dean's invite/);
  assert.match(text(), /Owner · Expires in 3 days · 1 of 5 used/);
  // No share or copy on a live row: the redeemable link needs the bearer,
  // which lives only in the minting browser's fragment, so either control
  // could only hand out a URL that cannot be redeemed (auto-c7xbs).
  assert.equal(pane().querySelector('[data-action="share"]'), null);
  assert.equal(pane().querySelector('[data-action="copy"]'), null);
  assert.match(text(), /Link shown once when created/);
  assert.ok(pane().querySelector('[data-action="deactivate"]'));

  // Deactivate needs a confirm step before any ceremony.
  pane().querySelector('[data-action="deactivate"]').click();
  await settle();
  assert.ok(pane().querySelector('[data-action="confirm-deactivate"]'));
  pane().querySelector('[data-action="keep"]').click();
  await settle();
  assert.ok(!pane().querySelector('[data-action="confirm-deactivate"]'));

  // Members: presence right, no role at top level, expansion carries it.
  w.document.querySelector('[data-tab="members"]').click();
  await settle();
  const rows = pane().querySelectorAll('[data-member]');
  assert.equal(rows.length, 2);
  assert.match(rows[0].textContent, /Jeremy/);
  assert.match(rows[0].textContent, /You/);
  assert.ok(!rows[0].textContent.includes('Owner'), 'role must not render on the row face');
  assert.match(rows[1].textContent, /Unnamed member/);
  rows[1].click();
  await settle();
  const detail = pane().querySelector('.mem-detail');
  assert.match(detail.textContent, /Role/);
  assert.match(detail.textContent, /Owner/);
  assert.match(detail.textContent, /Invited by/);
  assert.match(detail.textContent, /Jeremy/);
  assert.ok(detail.querySelector('[data-action="message"]'));
  assert.ok(detail.querySelector('[data-action="profile"]'));

  // Search narrows without losing the input.
  const search = pane().querySelector('.mem-search');
  search.value = 'jer';
  search.dispatchEvent(new w.Event('input', { bubbles: true }));
  await settle();
  assert.equal(pane().querySelectorAll('[data-member]').length, 1);
  assert.match(pane().querySelector('[data-count-note]').textContent, /1 of 2 members/);
  search.value = '';
  search.dispatchEvent(new w.Event('input', { bubbles: true }));
  await settle();

  // Roles: no duplicate heading, count links to the filtered member list,
  // expansion explains powers and joining.
  w.document.querySelector('[data-tab="roles"]').click();
  await settle();
  assert.ok(!/Roles\s*Roles/.test(text()), 'duplicate Roles heading');
  const roleRow = pane().querySelector('[data-role="owner"]');
  assert.match(roleRow.textContent, /2 members/);
  roleRow.click();
  await settle();
  assert.match(pane().querySelector('.mem-detail').textContent, /Full authority/);
  assert.match(pane().querySelector('.mem-detail').textContent, /A direct invitation admits immediately/);
  roleRow.querySelector('[data-action="filter-role"]').click();
  await settle();
  assert.equal(w.document.querySelector('.mem-tabs .on').textContent, 'Members2');
  assert.equal(pane().querySelectorAll('[data-member]').length, 2);

  // ── members-only org: no requests, opens on Members, badge silent ──
  {
    const quiet = baseView();
    quiet.pending_claims = [];
    quiet.invites = [];
    const mounted = await mount(quiet);
    const qp = mounted.w.document.querySelector('.org-membership');
    assert.equal(mounted.w.document.querySelector('.mem-tabs .on').textContent, 'Members2');
    assert.equal(mounted.w.document.querySelector('[data-testid="orgset-count-membership"]').textContent, '');
    mounted.w.document.querySelector('[data-tab="invites"]').click();
    await settle();
    assert.match(qp.textContent, /No active invitations\. Create and share invitation links/);
    assert.ok(qp.querySelector('[data-action="open-mint"]'));
  }

  console.log('PASS org membership screen');
  // The screen's quiet 5s refresh timer would otherwise keep node alive.
  process.exit(0);
}

main().catch((error) => { console.error(error); process.exit(1); });
