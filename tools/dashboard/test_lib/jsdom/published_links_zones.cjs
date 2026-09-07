// Published Links with custom domains (bead auto-lf2k6): the "Publish under"
// select lists the persona apex plus every claimed zone, saving under a zone
// posts {app_label, zone}, and the Custom domains panel claims a zone through
// POST /api/network/serve-zones with the chosen binding kind.
const { JSDOM } = require('jsdom');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const assert = require('node:assert/strict');

const script = readFileSync(resolve(__dirname, '../../static/js/published-links.js'), 'utf8');
const settle = () => new Promise((r) => setTimeout(r, 0));
const failNext = { value: null };
function zoneCard2Publisher(root) { return root.querySelector('[data-service="r-zone"] .pl-publisher').textContent; }

const ZONE = 'autonomy.taplink.net';
const PERSONA = 'persona-77827e972ba4c37d4215';
const DATA = {
  services: [
    { reservation_id: 'r-persona', origin: `https://docs.${PERSONA}.serve.auto.network`, persona_label: PERSONA, app_label: 'docs', state: 'active', target: { session_id: 's1', port: 8000 }, session_title: 'S1', publisher: { persona_pub: 'ab'.repeat(32), display_name: 'Jeremy' }, session_local: true },
    { reservation_id: 'r-zone', origin: `https://themes.${ZONE}`, persona_label: null, zone: ZONE, app_label: 'themes', state: 'active', target: { session_id: 's2', port: 8790 }, session_title: 's2', publisher: { persona_pub: 'cd'.repeat(32), display_name: 'Dean' }, session_local: false },
  ],
  shares: [],
  service_warning: '',
  zones: [{ zone: ZONE, binding_kind: 'parent-txt', state: 'active', verified_at: 1 }],
  persona_domain: `${PERSONA}.serve.auto.network`,
  org_uuid: '2d4b90cb-1e89-452b-82cb-68ca44fd8e52',
};

async function mount() {
  const dom = new JSDOM('<!doctype html><body></body>', { runScripts: 'dangerously', url: 'https://dashboard.test/' });
  const w = dom.window;
  const posts = [];
  w.fetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    if (failNext.value && method === 'POST' && url === '/api/network/serve-zones') {
      const f = failNext.value; failNext.value = null;
      posts.push({ url, method, body: JSON.parse(options.body) });
      return { ok: false, status: f.status, json: async () => f.body };
    }
    if (url === '/api/network/published-links' && method === 'GET') return { ok: true, status: 200, json: async () => DATA };
    if (method === 'POST' || method === 'PUT' || method === 'DELETE') {
      posts.push({ url, method, body: options.body ? JSON.parse(options.body) : null });
      if (url === '/api/network/service-reservations') return { ok: true, status: 201, json: async () => ({ reservation: { reservation_id: 'r-new' } }) };
      if (url === '/api/network/serve-zones' && method === 'POST') return { ok: true, status: 201, json: async () => ({ zone: { zone: 'demo.example.com', state: 'active' } }) };
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    }
    throw new Error('unexpected fetch ' + method + ' ' + url);
  };
  let registered = null;
  w.AutonomyOrgSettings = { register(def) { registered = def; }, close() {} };
  const s = w.document.createElement('script');
  s.textContent = script;
  w.document.head.appendChild(s);
  assert.ok(registered, 'published-links registered itself');
  assert.equal(registered.id, 'published-links');
  const root = await registered.render('autonomy', {});
  w.document.body.appendChild(root);
  await settle();
  return { w, root, posts };
}

async function main() {
  const { w, root, posts } = await mount();

  // Both cards render; the zone card shows app.zone with no serve.auto.network root.
  const zoneCard = root.querySelector('[data-service="r-zone"]');
  const personaCard = root.querySelector('[data-service="r-persona"]');
  assert.ok(zoneCard && personaCard, 'both service cards rendered');
  assert.equal(zoneCard.querySelector('.pl-host-app').textContent, 'themes');
  assert.equal(zoneCard.querySelector('.pl-host-domain').textContent, ZONE);
  assert.equal(zoneCard.querySelector('.pl-host-root'), null);
  assert.equal(personaCard.querySelector('.pl-host-root').textContent, '.serve.auto.network');

  // The picker lists the persona apex and every claimed zone, current one selected.
  const options = [...personaCard.querySelectorAll('[data-field="domain"] option')].map((o) => [o.value, o.textContent, o.selected]);
  assert.deepEqual(options, [['', `${PERSONA}.serve.auto.network`, true], [ZONE, ZONE, false]]);
  const zoneOptions = [...zoneCard.querySelectorAll('[data-field="domain"] option')].map((o) => [o.value, o.selected]);
  assert.deepEqual(zoneOptions, [['', false], [ZONE, true]]);

  // Moving the persona service under the zone posts {app_label, zone}, rebinds the target, releases the old row.
  personaCard.querySelector('[data-action="rename"]').click();
  await settle();
  const select = root.querySelector('[data-service="r-persona"] [data-field="domain"]');
  select.value = ZONE;
  root.querySelector('[data-service="r-persona"] [data-action="save"]').click();
  await settle(); await settle(); await settle(); await settle();
  assert.deepEqual(posts[0], { url: '/api/network/service-reservations', method: 'POST', body: { app_label: 'docs', zone: ZONE } });
  assert.equal(posts[1].url, '/api/network/service-targets/r-new');
  assert.deepEqual(posts[1].body, { session_id: 's1', port: 8000 });
  assert.equal(posts[2].url, '/api/network/service-reservations/r-persona/state');
  posts.length = 0;

  // Custom domains: the claimed zone is listed with its service count, no binding badge, Release at the end.
  const zonesPanel = root.querySelector('.pl-zones');
  assert.ok(zonesPanel, 'custom domains panel rendered');
  const domainCard = zonesPanel.querySelector(`[data-zone="${ZONE}"]`);
  assert.ok(domainCard, 'claimed zone listed');
  assert.equal(domainCard.querySelector('[data-action="filter"]').textContent, '1 service');
  assert.ok(!domainCard.textContent.includes('parent-txt'), 'binding kind badge removed');
  assert.ok(domainCard.querySelector('.pl-footer-end [data-action="release"]'), 'release sits in the end-aligned footer');
  assert.ok(domainCard.textContent.includes('Any member of this organization can publish here.'));
  assert.equal(root.querySelector('[data-action="zone-add"]').textContent, 'Add custom domain');

  // Publisher shows on every service card; a foreign session is marked as such.
  assert.ok(personaCard.querySelector('.pl-publisher').textContent.includes('Jeremy'));
  assert.ok(zoneCard2Publisher(root).includes("another member's machine"));

  // The count filters the services list; Show all clears it.
  domainCard.querySelector('[data-action="filter"]').click();
  await settle();
  assert.deepEqual([...root.querySelectorAll('[data-service]')].map((c) => c.dataset.service), ['r-zone']);
  assert.ok(root.querySelector('.pl-filter').textContent.includes(ZONE));
  root.querySelector('[data-action="clear-filter"]').click();
  await settle();
  assert.equal(root.querySelectorAll('[data-service]').length, 2);

  // The add form: plain label, live preview, two explained steps with labelled records, no binding choice.
  root.querySelector('[data-action="zone-add"]').click();
  await settle();
  let add = root.querySelector('[data-zone-add]');
  assert.equal(add.querySelector('[data-field="kind"]'), null, 'binding selector removed');
  assert.equal(add.querySelector('.pl-field label').textContent, 'Your domain');
  const input = add.querySelector('[data-field="zone"]');
  input.value = 'Demo.Example.com';
  input.dispatchEvent(new w.Event('input', { bubbles: true }));
  assert.ok(add.querySelector('[data-role="preview"]').textContent.includes('service.demo.example.com'));
  const delegation = [...add.querySelectorAll('[data-role="delegation"] .pl-record')].map((r) => [...r.querySelectorAll('code')].map((c) => c.textContent));
  assert.deepEqual(delegation, [['demo.example.com', 'NS', '2d4b90cb-1e89-452b-82cb-68ca44fd8e52.ns1.auto.network'], ['demo.example.com', 'NS', '2d4b90cb-1e89-452b-82cb-68ca44fd8e52.ns2.auto.network']]);
  assert.equal(add.querySelector('[data-role="authorization"]'), null, 'no separate TXT step');
  assert.equal(add.querySelector('[data-copy]').dataset.copy, '2d4b90cb-1e89-452b-82cb-68ca44fd8e52.ns1.auto.network');
  assert.ok(add.textContent.includes('Name server delegation') && !add.textContent.includes('TXT authorization'));

  // Verify posts the TXT binding; a refusal renders inline above the button in plain words, not in the page header.
  failNext.value = { status: 409, body: { ok: false, error: 'zone_unverified', detail: 'parent does not delegate demo.example.com to ns1.auto.network, ns2.auto.network' } };
  add.querySelector('[data-action="zone-claim"]').click();
  await settle(); await settle(); await settle();
  assert.deepEqual(posts[0], { url: '/api/network/serve-zones', method: 'POST', body: { zone: 'Demo.Example.com', binding_kind: 'ns-token' } });
  add = root.querySelector('[data-zone-add]');
  const status = add.querySelector('[data-role="status"] .pl-status');
  assert.ok(status && status.classList.contains('error'), 'inline status rendered');
  assert.ok(status.textContent.includes('not pointing this name at the two Autonomy name servers'), status.textContent);
  assert.equal(root.querySelector('.pl-error'), null, 'no header error');
  assert.equal(add.querySelector('[data-field="zone"]').value, 'Demo.Example.com', 'draft kept after a refusal');
  posts.length = 0;

  // A successful verify shows the green check inline.
  add.querySelector('[data-action="zone-claim"]').click();
  await settle(); await settle(); await settle(); await settle();
  add = root.querySelector('[data-zone-add]');
  const ok = add.querySelector('[data-role="status"] .pl-status.ok');
  assert.ok(ok && ok.textContent.includes('Verified'), 'green check after success');
  posts.length = 0;

  // Release asks for confirmation, then DELETEs the zone.
  root.querySelector(`[data-zone="${ZONE}"] [data-action="release"]`).click();
  await settle();
  root.querySelector(`[data-zone="${ZONE}"] [data-action="confirm-release"]`).click();
  await settle(); await settle(); await settle();
  assert.deepEqual(posts[0], { url: `/api/network/serve-zones/${encodeURIComponent(ZONE)}`, method: 'DELETE', body: null });
  console.log('PASS published_links_zones');
}

main().catch((e) => { console.error(e); process.exit(1); });
