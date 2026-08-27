/* The cold-boot last-session restore must NOT jump into a session while a
 * device enrollment is pending — the ruled flow: a locked system unlocked by
 * an under-enrolled passkey lands on the shell home and the drawer greets with
 * the enrollment dialog (which session views do not render). This drives the
 * REAL _maybeRestoreLastSession from app.js, extracted verbatim, so a
 * regression in the shipped code fails here. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const appSrc = readFileSync(
  path.join(path.dirname(fileURLToPath(import.meta.url)), '../static/app.js'), 'utf8');

function slice(name) {
  const i = appSrc.indexOf(name);
  assert.ok(i > -1, name + ' present in app.js');
  // function body: from its declaration to the next top-of-line closing brace
  const start = appSrc.lastIndexOf('\n', i);
  const end = appSrc.indexOf('\n}\n', i);
  return appSrc.slice(start, end + 3);
}

function harness({ pendingKey, live = true } = {}) {
  const calls = { navigated: null, fetched: [] };
  const storage = () => {
    const m = new Map();
    return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) };
  };
  const sandbox = {
    window: { location: { pathname: '/' } },
    localStorage: storage(),
    sessionStorage: storage(),
    fetch: async (url) => { calls.fetched.push(url); return { ok: true, json: async () => ({ is_live: live }) }; },
    navigateTo: (p) => { calls.navigated = p; },
    encodeURIComponent,
  };
  sandbox.localStorage.setItem('lastSessionPath', '/session/autonomy/auto-123');
  if (pendingKey) sandbox.sessionStorage.setItem(pendingKey, '{"factor_id":"pk.1"}');
  const src = "const _LAST_SESSION_KEY = 'lastSessionPath';"
    + slice('function _isSessionPath') + slice('async function _maybeRestoreLastSession')
    + '\n;__run = _maybeRestoreLastSession;';
  const fn = new Function('window', 'localStorage', 'sessionStorage', 'fetch', 'navigateTo',
    'let __run;' + src + 'return __run;');
  const run = fn(sandbox.window, sandbox.localStorage, sandbox.sessionStorage, sandbox.fetch, sandbox.navigateTo);
  return { run, calls };
}

test('without a pending enrollment, cold boot resumes the live last session', async () => {
  const { run, calls } = harness({});
  await run();
  assert.equal(calls.navigated, '/session/autonomy/auto-123');
});

for (const key of ['autonomy.factor.pending-slot', 'autonomy.factor.slot-enrolled', 'autonomy.factor.open-credentials']) {
  test('a pending enrollment stash (' + key + ') keeps the boot on the shell home', async () => {
    const { run, calls } = harness({ pendingKey: key });
    await run();
    assert.equal(calls.navigated, null, 'no jump into the session');
    assert.equal(calls.fetched.length, 0, 'restore bails before the liveness check');
  });
}
