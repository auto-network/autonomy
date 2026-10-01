// visitor_token in the shared approval control: design of record bc4d034a
// revision 42774731, state "Approve a visitor". Data mapping only.
import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {openVisitorCentralApproval, visitorReview} from '../static/js/components/visitor-central-approval.js';
import {setCachedConfirmDelayForTest} from '../static/js/components/approval-experiment.js';
// The 1.5s green-Authorize cancel window is exercised in approval_dialog.test.mjs.
setCachedConfirmDelayForTest(1);
const {JSDOM} = createRequire(import.meta.url)('jsdom');
let dom, calls, result;
const q = s => document.querySelector('[data-testid=approval-dialog]')?.shadowRoot.querySelector(s);
const tick = () => new Promise(r => setTimeout(r, 10));
async function until(fn) { for (let n = 0; n < 250 && !fn(); n++) await tick(); assert.ok(fn()); }
const review = {display_name: 'Jordan', reason: 'I would like to review the project update.',
  requester_label: 'auto-1 · Mission Control', machine_label: 'Home'};
const item = (actions = ['granted', 'declined']) => ({id: 'V', actions, safeReview: review});
test.beforeEach(() => {
  dom = new JSDOM('<body></body>', {url: 'https://example.test'}); global.window = dom.window; global.document = dom.window.document;
  window.matchMedia = () => ({matches: false}); window.Element.prototype.getAnimations = () => []; window.Element.prototype.animate = () => ({cancel() {}});
  calls = []; result = {state: 'pending', machine_label: 'Home'};
  global.fetch = async (url, options) => {
    calls.push({url: String(url), options});
    const reply = body => ({ok: true, status: 200, json: async () => body});
    if (String(url).startsWith('/api/session/')) return reply({project: 'Autonomy', session_id: 'auto-1', org: {name: 'Autonomy'}});
    if (options?.method === 'POST') { const b = JSON.parse(options.body); return reply({resolution: {outcome: b.outcome}}); }
    return reply({review: {application_result: result}});
  };
});
test.afterEach(() => { document.querySelector('[data-testid=approval-dialog]')?.remove(); dom.window.close(); });
const posts = () => calls.filter(c => c.options?.method === 'POST').map(c => JSON.parse(c.options.body));

test('the review is the design state: subject, reason for visiting, requested by', async () => {
  await openVisitorCentralApproval(item());
  assert.equal(q('#title').textContent, 'Let this person in?');
  assert.equal(q('#intro').textContent, 'Review who is asking to enter.');
  assert.equal(q('#resource-label').textContent, 'Subject');
  assert.equal(q('#resource').textContent, 'Jordan');
  assert.equal(q('#requester-kind').textContent, 'Requested by');
  assert.equal(q('#org-name').textContent, 'Autonomy');
  assert.equal(q('#request-detail .eyebrow').textContent, 'Reason for visiting');
  assert.equal(q('#request-detail pre').textContent, 'I would like to review the project update.');
  assert.equal(q('#auth').hidden, true);
});

test('the result wording is the design state', () => {
  const {result: copy} = visitorReview(review, {name: 'x'});
  assert.equal(copy.working, 'Allowing visitor…');
  assert.equal(copy.success, 'Visitor approved');
  assert.equal(copy.copy, 'Jordan’s visitor request was approved.');
});

test('authorizing grants with an empty decision', async () => {
  await openVisitorCentralApproval(item()); q('#primary').click();
  await until(() => q('#result-title').textContent === 'Visitor approved');
  assert.deepEqual(posts(), [{outcome: 'granted', decision: {}}]);
});

test('decline records a decline with an empty decision', async () => {
  await openVisitorCentralApproval(item()); q('#secondary').click(); q('#primary').click();
  await until(() => q('#result-title').textContent === 'Request declined');
  assert.deepEqual(posts(), [{outcome: 'declined', decision: {}}]);
});

test('another machine, awaiting collection and minted offer no decision', async () => {
  result = {state: 'elsewhere', machine_label: 'Office NUC'};
  await openVisitorCentralApproval(item());
  assert.equal(q('#primary').disabled, true);
  assert.match(q('#review-unavailable').textContent, /Requested on Office NUC; decide it there\./);
});
