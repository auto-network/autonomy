// The Charter screen (bead auto-bkoe6): the form drives from the resolved
// identity, Save stays disabled until a field changes, and the save PUTs
// the whole schema payload.
const { JSDOM } = require('jsdom');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const assert = require('node:assert/strict');

const orgSettings = readFileSync(resolve(__dirname, '../../static/js/org-settings.js'), 'utf8');
const charter = readFileSync(resolve(__dirname, '../../static/js/org-charter.js'), 'utf8');
const settle = () => new Promise((r) => setTimeout(r, 0));

const IDENTITY = {
  payload: {
    name: 'Autonomy Network',
    byline: 'AGI platform',
    description: 'The founding text.',
    color: '#6C63FF',
    favicon: '/static/icon-192.png',
    type: 'shared',
  },
};

async function mount() {
  const dom = new JSDOM('<!doctype html><body></body>', { runScripts: 'dangerously', url: 'https://dashboard.test/' });
  const w = dom.window;
  w.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} });
  const puts = [];
  w.fetch = async (url, options = {}) => {
    if (url === '/api/orgs/autonomy' && (!options.method || options.method === 'GET')) {
      return { ok: true, json: async () => ({ org: { slug: 'autonomy' }, identity: IDENTITY, identity_resolved: { name: 'Autonomy Network' } }) };
    }
    if (url === '/api/orgs/autonomy/charter' && options.method === 'PUT') {
      puts.push(JSON.parse(options.body));
      return { ok: true, json: async () => ({ ok: true, setting_id: 'sid-1' }) };
    }
    throw new Error('unexpected fetch ' + url + ' ' + (options.method || 'GET'));
  };
  for (const source of [orgSettings, charter]) {
    const s = w.document.createElement('script');
    s.textContent = source;
    w.document.head.appendChild(s);
  }
  w.AutonomyOrgSettings.open('autonomy');
  await settle();
  w.document.querySelector('[data-testid="orgset-rail-charter"]').click();
  await settle(); await settle();
  return { w, puts };
}

function field(w, name) {
  return w.document.querySelector('.org-charter [data-field="' + name + '"]');
}

async function main() {
  const { w, puts } = await mount();
  const pane = w.document.querySelector('.org-charter');
  assert.ok(pane, 'charter pane mounted');

  // The form drives from the resolved identity payload.
  assert.equal(field(w, 'name').value, 'Autonomy Network');
  assert.equal(field(w, 'byline').value, 'AGI platform');
  assert.equal(field(w, 'description').value, 'The founding text.');
  assert.equal(w.document.querySelector('[data-role="byline-left"]').textContent, String(60 - 'AGI platform'.length));

  // Save is disabled until something changes; Discard is absent when clean.
  const save = () => pane.querySelector('[data-action="save"]');
  assert.equal(save().disabled, true);
  assert.equal(pane.querySelector('[data-action="discard"]'), null);

  // Edit the byline: hint updates, Save enables, Discard appears.
  const byline = field(w, 'byline');
  byline.value = 'AGI platform, refounded';
  byline.dispatchEvent(new w.Event('input', { bubbles: true }));
  await settle();
  assert.equal(save().disabled, false);
  assert.ok(pane.querySelector('[data-action="discard"]'));
  assert.equal(w.document.querySelector('[data-role="byline-left"]').textContent, String(60 - 'AGI platform, refounded'.length));

  // Save PUTs the full schema payload, preserving the untouched type field.
  save().click();
  await settle(); await settle();
  assert.equal(puts.length, 1);
  assert.equal(puts[0].byline, 'AGI platform, refounded');
  assert.equal(puts[0].name, 'Autonomy Network');
  assert.equal(puts[0].type, 'shared');

  // After a save the form is clean again: Save disabled, no Discard.
  assert.equal(save().disabled, true);
  assert.equal(pane.querySelector('[data-action="discard"]'), null);

  // Clearing the name disables Save even though the form is dirty.
  const name = field(w, 'name');
  name.value = '';
  name.dispatchEvent(new w.Event('input', { bubbles: true }));
  await settle();
  assert.equal(save().disabled, true);

  console.log('PASS org charter screen');
  process.exit(0);
}

main().catch((error) => { console.error(error); process.exit(1); });
