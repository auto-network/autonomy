import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
const { JSDOM } = createRequire(import.meta.url)('jsdom');
import { openApprovalDialog, localHref } from '../static/js/components/approval-dialog.js';

let dom, handle;
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; }
const q = selector => selector === '#origin' ? document.querySelector(selector) : document.querySelector('[data-testid=approval-dialog]')?.shadowRoot.querySelector(selector);
function open(extra = {}) {
  handle = openApprovalDialog({
    review: { title: 'Allow dashboard access', intro: 'Browser access', requester: { name: 'Release session', href: '/session/workspace/release' } },
    result: { working: 'Allowing access', success: 'Access allowed', fact: { name: 'Release session', href: '/session/workspace/release' } },
    authorize: async () => ({}), execute: async () => ({execution:{ok:true}}), ...extra,
  });
  return handle;
}
test.beforeEach(() => {
  dom = new JSDOM('<button id="origin">Open request</button>', { url: 'https://example.test' });
  global.window = dom.window; global.document = dom.window.document;
  window.matchMedia = query => ({ matches: query.includes('max-width') });
  window.Element.prototype.getAnimations=()=>[]; window.Element.prototype.animate=()=>({cancel(){}}); q('#origin').focus();
});
test.afterEach(() => { handle?.dispose(); dom.window.close(); });

test('visible viewport bounds update and listeners are removed on close', () => {
  const viewport=new window.EventTarget();viewport.height=650;viewport.offsetTop=20;
  Object.defineProperty(window,'visualViewport',{value:viewport,configurable:true});
  open();const host=document.querySelector('[data-testid=approval-dialog]');
  assert.equal(host.style.getPropertyValue('--approval-height'),'650px');
  assert.equal(host.style.getPropertyValue('--approval-top'),'20px');
  viewport.height=400;viewport.dispatchEvent(new window.Event('resize'));
  assert.equal(host.style.getPropertyValue('--approval-height'),'400px');
  handle.close();viewport.height=700;viewport.dispatchEvent(new window.Event('resize'));
  assert.equal(host.style.getPropertyValue('--approval-height'),'400px');
});

test('review, close, Escape and session navigation never authorize or decline', () => {
  let calls = 0;
  const options = { authorize: async () => { calls++; }, decline: async () => { calls++; } };
  for (const action of ['close', 'escape', 'session']) {
    open(options);
    assert.equal(q('#auth').hidden, true);
    if (action === 'close') q('#close').click();
    if (action === 'escape') handle.root.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' }));
    if (action === 'session') {
      const link = q('#requester-link');
      assert.equal(link.getAttribute('href'), '/session/workspace/release');
      link.addEventListener('click', event => event.preventDefault()); link.click();
    }
    assert.equal(document.querySelector('[data-testid=approval-dialog]'), null);
    assert.equal(calls, 0);
  }
});

test('mobile Back aborts factors, keeps review and permits another attempt', async () => {
  let writes = 0, firstSignal;
  open({ authorize: ({ view, signal }) => {
    firstSignal = signal; view({policy:{op:'factor',factor_id:'password'},done:[],busy:false,password(){},passkey(){}});
    return new Promise(resolve => signal.addEventListener('abort', () => resolve({}), { once: true }));
  }, execute: async () => { writes++; } });
  q('#primary').click();
  assert.ok(q('.auth-panel input'));
  assert.notEqual(document.activeElement.type, 'password');
  q('.auth-panel-close').click(); await tick();
  assert.equal(firstSignal.aborted, true);
  assert.equal(q('.auth-panel'), null);
  assert.equal(q('#review').hidden, false);
  assert.equal(writes, 0);
  assert.equal(q('#primary').disabled, false);
});

test('authentication moves immediately to working result; execution updates in place', async () => {
  const operation = deferred(); let authenticated, writes = 0;
  const authority = deferred();
  open({ authorize: options => { authenticated = options.onAuthenticated; return authority.promise; },
    execute: async () => { writes++; await operation.promise; return {execution:{ok:true}}; } });
  q('#primary').click(); authenticated();
  const result = q('#result');
  assert.equal(result.hidden, false); assert.match(result.textContent, /Allowing access/);
  assert.equal(handle.close(), false);
  authority.resolve({}); await tick(); assert.equal(writes, 1);
  operation.resolve(); await tick();
  assert.equal(q('#result'), result);
  assert.match(result.textContent, /Access allowed/);
  assert.match(result.textContent, /Release session/);
});

test('green authorization can be cancelled by second tap or Cancel before any authority work', async () => {
  let calls = 0;
  for (const cancel of ['#primary', '#secondary']) {
    open({ retained: true, authorize: async () => { calls++; return {}; } });
    q('#primary').click();
    assert.equal(q('#primary').textContent, 'Authorizing…');
    q(cancel).click();
    assert.equal(q('#primary').textContent, 'Authorize');
    handle.close();
  }
  await new Promise(resolve => setTimeout(resolve, 1550));
  assert.equal(calls, 0);
});

test('only explicit confirmed Decline decides; receipt is not a success check', async () => {
  let declines = 0;
  open({ decline: async () => { declines++; } });
  q('#secondary').click(); assert.equal(declines, 0);
  q('#primary').click(); await tick();
  assert.equal(declines, 1);
  assert.match(q('#result').textContent, /Request declined/);
  assert.ok(q('.result-mark').classList.contains('declined'));
});

test('operation error never shows success or automatically resubmits', async () => {
  let calls = 0;
  open({ execute: async () => { calls++; throw new Error('Link could not be published'); } });
  q('#primary').click(); await tick();
  assert.equal(calls, 1);
  assert.match(q('#result').textContent, /Link could not be published/);
  assert.equal(q('#primary').textContent, 'Done');
});

test('navigation accepts only local product paths', () => {
  assert.equal(localHref('/graph/abc'), '/graph/abc');
  for (const value of ['//external.test', '/\\external.test', 'javascript:alert(1)', '/\n/external.test']) assert.equal(localHref(value), null);
});
