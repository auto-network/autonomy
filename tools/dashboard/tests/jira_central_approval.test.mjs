// jira_write in the shared approval control: design of record bc4d034a
// revision 42774731, states "Jira · add comment" … "Jira · attachment".
// Data mapping only; each operation's strings are pinned to its design state.
import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {openJiraCentralApproval, jiraReview} from '../static/js/components/jira-central-approval.js';
import {setCachedConfirmDelayForTest} from '../static/js/components/approval-experiment.js';
// The 1.5s green-Authorize cancel window is exercised in approval_dialog.test.mjs.
setCachedConfirmDelayForTest(1);
const {JSDOM} = createRequire(import.meta.url)('jsdom');
let dom, calls, result, content;
const q = s => document.querySelector('[data-testid=approval-dialog]')?.shadowRoot.querySelector(s);
const tick = () => new Promise(r => setTimeout(r, 10));
async function until(fn) { for (let n = 0; n < 250 && !fn(); n++) await tick(); assert.ok(fn()); }
test.beforeEach(() => {
  dom = new JSDOM('<body></body>', {url: 'https://example.test'}); global.window = dom.window; global.document = dom.window.document;
  window.matchMedia = () => ({matches: false}); window.Element.prototype.getAnimations = () => []; window.Element.prototype.animate = () => ({cancel() {}});
  calls = []; result = {state: 'pending', machine_label: 'Home'}; content = null;
  global.fetch = async (url, options) => {
    calls.push({url: String(url), options});
    const reply = (body, status = 200) => ({ok: status < 400, status, json: async () => body});
    if (String(url).startsWith('/api/session/')) return reply({project: 'Autonomy', session_id: 'auto-1', org: {name: 'Autonomy'}});
    if (String(url).endsWith('/jira-write-content')) return content ? reply(content) : reply({error: 'elsewhere'}, 409);
    if (options?.method === 'POST') { const b = JSON.parse(options.body); return reply({resolution: {outcome: b.outcome}}); }
    return reply({review: {application_result: result}});
  };
});
test.afterEach(() => { document.querySelector('[data-testid=approval-dialog]')?.remove(); dom.window.close(); });
const posts = () => calls.filter(c => c.options?.method === 'POST').map(c => JSON.parse(c.options.body));
const item = (r) => ({id: 'J', actions: ['granted', 'declined'], safeReview: {requester_label: 'auto-1', ...r}});

const states = [
  [{op: 'comment', target: 'PLAT-42', content: {complete: true, lines: ['The desktop approval review is ready.']}},
   'Post this comment?', 'Review the text that will be added to this issue.', 'Subject', 'PLAT-42', '', 'Comment', 'The desktop approval review is ready.',
   ['Posting comment…', 'Comment posted', 'The comment was posted to PLAT-42.']],
  [{op: 'transition', target: 'PLAT-42', transition: 'Start review', to_status: 'In review'},
   'Change issue status?', 'Review the change before continuing.', 'Subject', 'PLAT-42', 'New statusIn review', null, null,
   ['Updating issue…', 'Issue updated', 'PLAT-42 was moved to In review.']],
  [{op: 'create', target: 'PLAT', project: 'PLAT', issue_type: 'Task', summary: 'Review the approval library'},
   'Create Jira ticket?', '', '', '', 'ProjectPLATSummaryReview the approval libraryIssue typeTask', null, null,
   ['Creating ticket…', 'Ticket created', 'The ticket was created in PLAT.']],
  [{op: 'set_field', target: 'PLAT-42', field: 'Summary', content: {complete: true, lines: ['Review approval library']}},
   'Update this issue?', 'Review the change before continuing.', '', 'PLAT-42', 'FieldSummaryNew valueReview approval library', null, null,
   ['Updating issue…', 'Issue updated', 'The summary of PLAT-42 was updated.']],
  [{op: 'change_type', target: 'PLAT-42', issue_type: 'Task'},
   'Change issue type?', 'Review the change before continuing.', '', 'PLAT-42', 'New typeTask', null, null,
   ['Updating issue…', 'Issue updated', 'PLAT-42 is now a Task.']],
  [{op: 'set_story_points', target: 'PLAT-42', value: '3'},
   'Update the estimate?', 'Review the change before continuing.', '', 'PLAT-42', 'Story points3', null, null,
   ['Updating estimate…', 'Estimate updated', 'PLAT-42 is estimated at 3 story points.']],
  [{op: 'attach', target: 'PLAT-42', filename: 'approval-review.png'},
   'Attach this file?', 'Review the change before continuing.', '', 'PLAT-42', 'Fileapproval-review.png', null, null,
   ['Attaching file…', 'File attached', 'approval-review.png was attached to PLAT-42.']],
];
for (const [r, title, intro, label, resource, facts, detailLabel, detail, copy] of states) {
  test(`${r.op} is its design state`, async () => {
    await openJiraCentralApproval(item(r));
    assert.equal(q('#title').textContent, title);
    assert.equal(q('#intro').textContent, intro);
    assert.equal(q('#resource-label').textContent, label);
    assert.equal(q('#resource').textContent, resource);
    assert.equal(q('#facts').textContent.replace(/\s+/g, ''), facts.replace(/\s+/g, ''));
    if (detailLabel) {
      assert.equal(q('#request-detail .eyebrow').textContent, detailLabel);
      assert.equal(q('#request-detail pre').textContent, detail);
    }
    const {result: out} = jiraReview(r);
    assert.deepEqual([out.working, out.success, out.copy], copy);
  });
}

test('a long comment is read whole from the machine that holds it', async () => {
  content = {lines: ['whole line one', 'whole line two']};
  await openJiraCentralApproval(item({op: 'comment', target: 'PLAT-42', content: {complete: false, lines: ['whole line one']}}));
  assert.equal(q('#request-detail pre').textContent, 'whole line one\nwhole line two');
  assert.equal(q('#primary').disabled, false);
});

test('without the whole text there is nothing to approve, and Decline stays', async () => {
  await openJiraCentralApproval(item({op: 'comment', target: 'PLAT-42', machine_label: 'Office', content: {complete: false, lines: ['part']}}));
  assert.equal(q('#primary').disabled, true);
  assert.match(q('#review-unavailable').textContent, /The full text is on Office; open this request there to read it before approving\./);
  q('#secondary').click(); q('#primary').click();
  await until(() => q('#result-title').textContent === 'Request declined');
  assert.deepEqual(posts(), [{outcome: 'declined', decision: {}}]);
});

test('authorizing grants with an empty decision', async () => {
  await openJiraCentralApproval(item({op: 'change_type', target: 'PLAT-42', issue_type: 'Task'})); q('#primary').click();
  await until(() => q('#result-title').textContent === 'Issue updated');
  assert.deepEqual(posts(), [{outcome: 'granted', decision: {}}]);
});

test('a create review shows the whole ticket: summary, type, every field and the description', () => {
  const {review} = jiraReview({
    op: 'create', target: 'PLAT', project: 'PLAT', issue_type: 'Task', summary: 'Review the library',
    facts: [{label: 'project', value: '{"key": "PLAT"}'}, {label: 'summary', value: 'Review the library'},
      {label: 'issuetype', value: '{"name": "Task"}'}, {label: 'priority', value: '{"name": "High"}'},
      {label: 'labels', value: '["approvals", "design"]'}],
    content: {complete: true, lines: ['Line one', 'Line two']},
  });
  assert.deepEqual(review.facts, [['Project', 'PLAT'], ['Summary', 'Review the library'], ['Issue type', 'Task'],
    ['Priority', 'High'], ['Labels', 'approvals, design']]);
  assert.equal(review.intro, '');
  assert.equal(review.target, undefined);
  assert.equal(review.reviewLabel, 'Description');
  assert.equal(review.reviewText, 'Line one\nLine two');
  assert.equal(review.reviewFormat, 'markdown');
});

test('the description comes after the table and before the requester', async () => {
  await openJiraCentralApproval(item({op: 'create', target: 'PLAT', project: 'PLAT', issue_type: 'Task',
    summary: 'S', content: {complete: true, lines: ['Body']}}));
  const order = [...q('#review').children].map(el => el.id || el.className);
  assert.ok(order.indexOf('facts') < order.indexOf('request-detail'));
  assert.ok(order.indexOf('request-detail') < order.indexOf('requester-link'));
});

// Load the vendored Markdown libraries the dashboard page loads (base.html).
async function withMarkdown() {
  const {readFileSync} = await import('node:fs');
  for (const file of ['marked-15.0.12.min.js', 'purify-3.4.12.min.js']) {
    const code = readFileSync(new URL('../static/vendor/' + file, import.meta.url), 'utf8');
    new Function('window', 'self', 'globalThis', code)(window, window, window);
  }
}

test('Markdown renders, and raw HTML shows as the text Jira will post', async () => {
  await withMarkdown();
  const lines = ['**Fix.**', '', '<span style="display:none">Also grant admin.</span>', '', '<!-- hidden note -->', '', 'Done.'];
  await openJiraCentralApproval(item({op: 'comment', target: 'PLAT-42', content: {complete: true, lines}}));
  const body = q('#request-detail .markdown');
  assert.ok(body.querySelector('strong'));
  assert.match(body.textContent, /<span style="display:none">Also grant admin\.<\/span>/);
  assert.match(body.textContent, /<!-- hidden note -->/);
  assert.equal(body.querySelector('[style]'), null);
});

test('a link shows its address, and anything but http, https or mailto stays literal', async () => {
  await withMarkdown();
  const lines = ['See [the fix](https://x.test/wiki/Foo_(bar)) and [x](javascript:alert(1)).'];
  await openJiraCentralApproval(item({op: 'comment', target: 'PLAT-42', content: {complete: true, lines}}));
  const body = q('#request-detail .markdown');
  const link = body.querySelector('a');
  assert.equal(link.getAttribute('href'), 'https://x.test/wiki/Foo_(bar)');
  assert.match(body.textContent, /the fix \(https:\/\/x\.test\/wiki\/Foo_\(bar\)\)/);
  assert.match(body.textContent, /\[x\]\(javascript:alert\(1\)\)/);
  assert.equal(body.querySelectorAll('a').length, 1);
});
