/**
 * Recent Sessions per-org floor and empty-state contract.
 *
 * The server ships an age-independent ten-row floor per org in the history
 * snapshot (dao/sessions.py include_org_floor) precisely so a quiet org still
 * has rows to show. The client's Since projection must not hide that floor
 * when an org is selected, and the empty state must be a real sentence —
 * never the literal "No all sessions".
 *
 * Run: node --test tools/dashboard/tests/test_sessions_recent_org_floor.js
 */
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const { SESSIONS_HTML, makeSessionsPage } = require('./sessions_page_harness');

function isoDaysAgo(days) {
  return new Date(Date.now() - days * 24 * 3600 * 1000).toISOString();
}

let nextId = 0;
function row(orgSlug, daysOld, extra) {
  nextId += 1;
  return Object.assign({
    id: 'row-' + nextId,
    session_id: 'sess-' + nextId,
    session_type: 'interactive',
    session_group: 'interactive',
    org: { slug: orgSlug },
    created_at: isoDaysAgo(daysOld + 0.01),
    last_activity_at: isoDaysAgo(daysOld),
    ended_at: isoDaysAgo(daysOld),
  }, extra || {});
}

describe('recent sessions per-org floor', () => {
  it('shows a quiet org its 10 newest sessions even when all are outside the Since window', () => {
    const page = makeSessionsPage();
    page.recent = [
      ...Array.from({ length: 12 }, (_, i) => row('blindhash', 30 + i)),
      ...Array.from({ length: 5 }, (_, i) => row('autonomy', i * 0.01)),
    ];
    page.selectedOrg = 'blindhash';
    page.recentSince = '1d';
    page.recentFilter = 'all';

    const shown = page.filtered;
    assert.equal(shown.length, 10, 'selected org must get its 10-row floor past the Since window');
    assert.ok(shown.every((s) => s.org.slug === 'blindhash'));
    // Floor is the org's NEWEST rows: days 30..39, never 40/41.
    const oldest = Math.min(...shown.map((s) => Date.parse(s.last_activity_at)));
    const cutoff = Date.now() - 40 * 24 * 3600 * 1000;
    assert.ok(oldest > cutoff, 'backfill must take the newest rows first');
  });

  it('shows fewer than 10 when the org simply has fewer sessions', () => {
    const page = makeSessionsPage();
    page.recent = [row('blindhash', 30), row('blindhash', 31), row('autonomy', 0)];
    page.selectedOrg = 'blindhash';
    page.recentSince = '1d';

    assert.equal(page.filtered.length, 2);
  });

  it('does not pad past the Since window when the org already meets the floor', () => {
    const page = makeSessionsPage();
    page.recent = [
      ...Array.from({ length: 11 }, (_, i) => row('autonomy', i * 0.02)), // all within 1d
      ...Array.from({ length: 4 }, (_, i) => row('autonomy', 20 + i)),   // old
    ];
    page.selectedOrg = 'autonomy';
    page.recentSince = '1d';

    assert.ok(
      page.filtered.every((s) => Date.parse(s.last_activity_at) >= Date.now() - 24 * 3600 * 1000),
      'an org with enough recent rows shows only in-window rows',
    );
  });

  it('leaves the All Orgs view governed by the Since window alone', () => {
    const page = makeSessionsPage();
    page.recent = [row('blindhash', 30), row('autonomy', 0.1)];
    page.selectedOrg = '';
    page.recentSince = '1d';

    assert.equal(page.filtered.length, 1);
    assert.equal(page.filtered[0].org.slug, 'autonomy');
  });
});

describe('recent sessions empty state', () => {
  it('never renders the raw chip key ("No all sessions")', () => {
    const page = makeSessionsPage();
    page.recentFilter = 'all';
    page.selectedOrg = '';
    page.recentSince = 'all';
    assert.equal(page.recentEmptyMessage(), 'No sessions');
    assert.ok(!page.recentEmptyMessage().includes('all sessions'));
  });

  it('names the org and window that hid the rows', () => {
    const page = makeSessionsPage();
    page.recentFilter = 'all';
    page.selectedOrg = 'blindhash';
    page.orgFilterList = [{ slug: 'blindhash', name: 'BlindHash' }];
    page.recentSince = '1d';
    assert.equal(page.recentEmptyMessage(), 'No sessions for BlindHash in the last day');
  });

  it('names the type chip when one is active', () => {
    const page = makeSessionsPage();
    page.recentFilter = 'dispatch';
    page.selectedOrg = '';
    page.recentSince = '1w';
    assert.equal(page.recentEmptyMessage(), 'No dispatch sessions in the last week');
  });

  it('is what the template binds, not string concatenation on recentFilter', () => {
    const html = fs.readFileSync(SESSIONS_HTML, 'utf8');
    assert.match(html, /x-text="recentEmptyMessage\(\)"/);
    assert.ok(!html.includes(`'No ' + recentFilter`), 'the ungrammatical literal binding must not return');
  });
});
