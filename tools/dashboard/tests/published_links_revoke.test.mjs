import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createRequire, register} from 'node:module';
const {JSDOM} = createRequire(import.meta.url)('jsdom');
// Revoke on Published Links is operator-initiated: the operator signs the
// revoke in the shared control, so the page never files a legacy
// /api/approvals request and waits on it (auto-fkhq0.10a).
const LINKS = '/static/js/components/link-central-approval.js';
register('data:text/javascript,' + encodeURIComponent(`export async function resolve(s, c, next) {
  if (s === ${JSON.stringify(LINKS)}) return {url: 'data:text/javascript,' + encodeURIComponent('export const operateLinkDirectly = (...a) => globalThis.__operateLinkDirectly(...a);'), shortCircuit: true};
  return next(s, c);
}`));
const SOURCE = fs.readFileSync(new URL('../static/js/published-links.js', import.meta.url), 'utf8');
const SHARE = {token: 'tok-1', type: 'note', title: 'Release notes', description: '', url: 'https://x.test/s/tok-1', platform_url: '/n/1'};
let dom, page, calls, operations, execution, listing;
const tick = () => new Promise((r) => setTimeout(r, 10));
test.beforeEach(async () => {
  dom = new JSDOM('<body></body>', {url: 'https://example.test/'});
  global.window = dom.window; global.document = dom.window.document;
  calls = []; operations = []; execution = {ok: true}; listing = {shares: [SHARE]};
  global.fetch = async (url, options = {}) => {
    calls.push({url: String(url), method: options.method || 'GET'});
    return {ok: true, status: 200, json: async () => listing};
  };
  globalThis.__operateLinkDirectly = async (spec) => { operations.push(spec); return execution; };
  window.registerHandler = () => assert.fail('no approval:decided wait');
  window.openApprovalOverlay = () => assert.fail('no legacy overlay');
  window.AutonomyOrgSettings = {register: (p) => { page = p; }, close() {}};
  (0, eval)(SOURCE);
  const root = await page.render('autonomy', {focus: 'tok-1'});
  document.body.append(root);
});
test.afterEach(() => { dom.window.close(); delete globalThis.__operateLinkDirectly; });
const click = (action) => document.querySelector(`[data-share="tok-1"] [data-action="${action}"]`).click();

test('Revoke Share signs the revoke directly and refreshes the list', async () => {
  click('revoke'); click('confirm-revoke');
  listing = {shares: []};
  for (let n = 0; n < 50 && document.querySelector('[data-share="tok-1"]'); n++) await tick();
  assert.deepEqual(operations, [{op: 'revoke', request: {org: 'autonomy', token: 'tok-1'}, requester: 'Published links'}]);
  assert.ok(!calls.some((c) => c.url.startsWith('/api/approvals')), 'no legacy approval request');
  assert.equal(calls.filter((c) => c.url === '/api/network/published-links').length, 2, 'refreshed after the revoke');
  assert.equal(document.querySelector('[data-share="tok-1"]'), null);
});

test('closing the control without revoking leaves the list as it was', async () => {
  execution = null;
  click('revoke'); click('confirm-revoke');
  for (let n = 0; n < 20; n++) await tick();
  assert.equal(operations.length, 1);
  assert.equal(calls.filter((c) => c.url === '/api/network/published-links').length, 1, 'no refresh');
  assert.ok(document.querySelector('[data-share="tok-1"]'));
  assert.ok(!document.querySelector('[data-share="tok-1"] .pl-detail.open'), 'the confirm closes');
});

test('a refused revoke shows why', async () => {
  globalThis.__operateLinkDirectly = async () => { throw new Error('This link is already revoked.'); };
  click('revoke'); click('confirm-revoke');
  for (let n = 0; n < 50 && !document.querySelector('.pl-error'); n++) await tick();
  assert.match(document.querySelector('.pl-error').textContent, /already revoked/);
});
