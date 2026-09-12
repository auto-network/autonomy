import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const { JSDOM } = createRequire(import.meta.url)('jsdom');
import { openApprovalDialog, localHref } from '../static/js/components/approval-dialog.js';

let dom, handle;
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; }
const q = selector => document.querySelector(selector);
function open(extra = {}) {
  handle = openApprovalDialog({
    review: { title: 'Allow dashboard access', intro: 'Browser access', requester: { name: 'Release session', href: '/session/workspace/release' } },
    result: { working: 'Allowing access', success: 'Access allowed', fact: { name: 'Release session', href: '/session/workspace/release' } },
    authorize: async () => ({}), execute: async () => {}, ...extra,
  });
  return handle;
}
test.beforeEach(() => {
  dom = new JSDOM('<button id="origin">Open request</button>', { url: 'https://example.test' });
  global.window = dom.window; global.document = dom.window.document;
  window.matchMedia = query => ({ matches: query.includes('max-width') });
  q('#origin').focus();
});
test.afterEach(() => { handle?.close(); dom.window.close(); });

test('review, close, Escape and session navigation never authorize or decline', () => {
  let calls = 0;
  const options = { authorize: async () => { calls++; }, decline: async () => { calls++; } };
  for (const action of ['close', 'escape', 'session']) {
    open(options);
    assert.equal(q('input[type=password]'), null);
    if (action === 'close') q('.approval-close').click();
    if (action === 'escape') document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' }));
    if (action === 'session') {
      const link = q('.approval-requester');
      assert.equal(link.getAttribute('href'), '/session/workspace/release');
      link.addEventListener('click', event => event.preventDefault()); link.click();
    }
    assert.equal(q('.approval-dialog'), null);
    assert.equal(calls, 0);
  }
});

test('mobile Back aborts factors, keeps review and permits another attempt', async () => {
  let writes = 0, firstSignal;
  open({ authorize: ({ mount, signal }) => {
    firstSignal = signal; const input = document.createElement('input'); input.type = 'password'; mount().append(input);
    return new Promise(resolve => signal.addEventListener('abort', () => resolve({}), { once: true }));
  }, execute: async () => { writes++; } });
  q('.approval-primary').click();
  assert.ok(q('.approval-auth-panel input'));
  assert.notEqual(document.activeElement.type, 'password');
  q('.approval-auth-panel .approval-close').click(); await tick();
  assert.equal(firstSignal.aborted, true);
  assert.equal(q('.approval-auth-panel'), null);
  assert.equal(q('.approval-review').hidden, false);
  assert.equal(writes, 0);
  assert.equal(q('.approval-primary').disabled, false);
});

test('authentication moves immediately to working result; execution updates in place', async () => {
  const operation = deferred(); let authenticated, writes = 0;
  const authority = deferred();
  open({ authorize: options => { authenticated = options.onAuthenticated; return authority.promise; },
    execute: async () => { writes++; await operation.promise; } });
  q('.approval-primary').click(); authenticated();
  const result = q('.approval-result');
  assert.equal(result.hidden, false); assert.match(result.textContent, /Allowing access/);
  assert.equal(handle.close(), false);
  authority.resolve({}); await tick(); assert.equal(writes, 1);
  operation.resolve(); await tick();
  assert.equal(q('.approval-result'), result);
  assert.match(result.textContent, /Access allowed/);
  assert.match(result.textContent, /Release session/);
});

test('green authorization can be cancelled by second tap or Cancel before any authority work', async () => {
  let calls = 0;
  for (const cancel of ['.approval-primary', '.approval-secondary']) {
    open({ retained: true, authorize: async () => { calls++; return {}; } });
    q('.approval-primary').click();
    assert.equal(q('.approval-primary').textContent, 'Authorizing…');
    q(cancel).click();
    assert.equal(q('.approval-primary').textContent, 'Authorize');
    handle.close();
  }
  await new Promise(resolve => setTimeout(resolve, 1550));
  assert.equal(calls, 0);
});

test('only explicit confirmed Decline decides; receipt is not a success check', async () => {
  let declines = 0;
  open({ decline: async () => { declines++; } });
  q('.approval-secondary').click(); assert.equal(declines, 0);
  q('.approval-secondary').click(); await tick();
  assert.equal(declines, 1);
  assert.match(q('.approval-result').textContent, /Request declined/);
  assert.equal(q('.approval-result-mark').getAttribute('aria-hidden'), 'true');
});

test('operation error never shows success or automatically resubmits', async () => {
  let calls = 0;
  open({ execute: async () => { calls++; throw new Error('Link could not be published'); } });
  q('.approval-primary').click(); await tick();
  assert.equal(calls, 1);
  assert.match(q('.approval-result').textContent, /Link could not be published/);
  assert.equal(q('.approval-primary').hidden, true);
});

test('navigation accepts only local product paths', () => {
  assert.equal(localHref('/graph/abc'), '/graph/abc');
  for (const value of ['//external.test', '/\\external.test', 'javascript:alert(1)', '/\n/external.test']) assert.equal(localHref(value), null);
});
