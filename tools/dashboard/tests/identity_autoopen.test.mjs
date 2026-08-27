/* The drawer must greet a pending first-device enrollment WITHOUT a click.
 *
 * unlock.js stashes the detection and lands on the shell home; this suite
 * proves the identity indicator's init() then auto-opens the profile drawer
 * straight into Manage credentials (where the enrollment dialog greets) —
 * and, as the control, stays closed when nothing is pending.
 *
 *   node --test tools/dashboard/tests/identity_autoopen.test.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const here = path.dirname(fileURLToPath(import.meta.url));
const require2 = createRequire(import.meta.url);
const { JSDOM } = require2('jsdom');
const INDICATOR = path.join(here, '..', 'static', 'js', 'identity-indicator.js');

const flush = () => new Promise((r) => setImmediate(r));
async function settle(n = 20) { for (let i = 0; i < n; i += 1) await flush(); }

function bootShell({ pendingSlot, failStatusFirst } = {}) {
  const dom = new JSDOM(
    '<!doctype html><html><body><div id="identity-indicator"></div></body></html>',
    { url: 'https://localhost/', pretendToBeVisual: true },
  );
  const win = dom.window;
  const signedIn = {
    personal_identity: { display_name: 'Jeremy Spilman' },
    passkeys: [{ credential_id: 'Y3JlZC1B' }],
    signed_in: true, enforced: true, method: 'passkey',
    onboarding_needed: false, gate_disabled: false,
  };
  let statusFailures = failStatusFirst ? 1 : 0;
  win.fetch = async (url) => {
    if (String(url).includes('/api/identity/status')) {
      if (statusFailures > 0) { statusFailures -= 1; return { ok: false, status: 503, json: async () => ({ error: 'cold start' }) }; }
      return { ok: true, status: 200, json: async () => signedIn };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  };
  if (pendingSlot) {
    win.sessionStorage.setItem('autonomy.factor.pending-slot', JSON.stringify(pendingSlot));
  }
  // the module captures `window` at load time: install globals, then require fresh
  global.window = win;
  global.document = win.document;
  Object.defineProperty(globalThis, 'navigator', { value: win.navigator, configurable: true });
  delete require2.cache[require2.resolve(INDICATOR)];
  const indicator = require2(INDICATOR);
  return { win, indicator };
}

test('a stashed pending enrollment auto-opens the drawer into Manage credentials — zero clicks', async () => {
  const { win, indicator } = bootShell({
    pendingSlot: {
      factor_id: 'pk.mac', credential_id: 'Y3JlZC1B',
      recipient_public_key: 'a'.repeat(64), label: 'New device',
    },
  });
  indicator.init();
  await settle();
  assert.equal(indicator._state().panelOpen, true, 'drawer opened itself');
  assert.ok(win.document.querySelector('.identity-credentials-mount'),
    'and went straight to Manage credentials (the enrollment greeting mounts here)');
});

test('without a pending enrollment the drawer stays closed', async () => {
  const { win, indicator } = bootShell({});
  indicator.init();
  await settle();
  assert.equal(indicator._state().panelOpen, false, 'no surprise drawer');
  assert.equal(win.document.querySelector('.identity-credentials-mount'), null);
});

test('the greeting is STICKY: a cold-start status failure delays it, the next refresh opens it', async () => {
  const { win, indicator } = bootShell({
    failStatusFirst: true,
    pendingSlot: {
      factor_id: 'pk.mac', credential_id: 'Y3JlZC1B',
      recipient_public_key: 'a'.repeat(64), label: 'New device',
    },
  });
  indicator.init();
  await settle();
  assert.equal(indicator._state().panelOpen, false, 'error state cannot host the panel yet');
  await indicator.refresh();   // e.g. visibilitychange / identity-changed
  await settle();
  assert.equal(indicator._state().panelOpen, true, 'greeting arrives with the working refresh');
  assert.ok(win.document.querySelector('.identity-credentials-mount'));
});

test('a stash that appears AFTER an empty check still greets on the next refresh (PWA-restored shell)', async () => {
  // The live iPhone bug: the shell page ran a status refresh before any stash
  // existed (restored from cache / loaded pre-login), and the one-shot greeter
  // consumed itself on "nothing pending" — so a stash written later (login in
  // another page, app resume) never greeted. The empty check must not burn the
  // one-shot; only an actual greeting does.
  const { win, indicator } = bootShell({});
  indicator.init();
  await settle();
  assert.equal(indicator._state().panelOpen, false, 'nothing pending yet — closed');
  win.sessionStorage.setItem('autonomy.factor.pending-slot', JSON.stringify({
    factor_id: 'pk.mac', credential_id: 'Y3JlZC1B',
    recipient_public_key: 'a'.repeat(64), label: 'New device',
  }));
  await indicator.refresh();
  await settle();
  assert.equal(indicator._state().panelOpen, true, 'the late stash greets on the next refresh');
  assert.ok(win.document.querySelector('.identity-credentials-mount'));
});
