/**
 * Frozen Active-session ordering across Launching membership changes.
 *
 * Run: node --test tools/dashboard/tests/test_sessions_active_order.js
 */
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
// The shared page harness: the same window/document/Alpine stubs every
// sessions-page test uses, so a new load-time listener in sessions.js cannot
// break this file's private copy again (it had no window.addEventListener).
const { makeSessionsPage } = require('./sessions_page_harness');

function ids(page) {
  return Array.from(page.sortedInteractive, (session) => session.session_id);
}

describe('sessions frozen Active order', () => {
  it('promotes a card that finishes Launching, then holds it until an explicit refresh', () => {
    const page = makeSessionsPage();
    page.activeSort = 'turns';
    page.activeSortDirection = 'desc';
    page.interactive = [
      { session_id: 'active-a', entry_count: 20, created_at: 30 },
      { session_id: 'active-b', entry_count: 10, created_at: 20 },
      { session_id: 'new-session', entry_count: 0, created_at: 40, _launching: true },
    ];

    page.refreshActiveOrder();
    assert.deepEqual(ids(page), ['active-a', 'active-b']);

    page.interactive[2]._launching = false;
    page._reconcileActiveOrder();
    assert.deepEqual(ids(page), ['new-session', 'active-a', 'active-b']);

    // Ordinary live card updates keep the just-launched card pinned while
    // the operator remains on this foreground list.
    page.interactive[1].entry_count = 100;
    page._reconcileActiveOrder();
    assert.deepEqual(ids(page), ['new-session', 'active-a', 'active-b']);

    // Returning to Sessions (or explicitly changing sort controls) calls the
    // established refresh boundary and applies the selected ranking again.
    page.refreshActiveOrder();
    assert.deepEqual(ids(page), ['active-b', 'active-a', 'new-session']);
  });

  it('still appends unrelated active membership without moving survivors', () => {
    const page = makeSessionsPage();
    page.interactive = [
      { session_id: 'active-a', created_at: 30 },
      { session_id: 'active-b', created_at: 20 },
    ];
    page.refreshActiveOrder();

    page.interactive.push({ session_id: 'background-arrival', created_at: 40 });
    page._reconcileActiveOrder();

    assert.deepEqual(ids(page), ['active-a', 'active-b', 'background-arrival']);
  });
});
