/**
 * Sessions page on-screen search: the header search box narrows the
 * Launching / Active / Recent lists by card metadata as you type, and
 * Enter upgrades the same query to a transcript search restricted to the
 * graph source ids of the cards on screen. Cards render unchanged — this
 * is a filter, not a results view.
 *
 * Run: node --test tools/dashboard/tests/test_sessions_search_filter.js
 */
const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const { SESSIONS_HTML, makeSessionsPage } = require('./sessions_page_harness');

function card(overrides) {
  return Object.assign({
    session_id: 'auto-1', id: 'auto-1', tmux_session: 'auto-1', label: '', project: '',
    role: '', latest: '', topics: [], org: null, type: 'container',
    session_type: 'interactive', is_live: true, graph_source_id: '',
    created_at: 100, last_activity: 100,
  }, overrides);
}

function recentRow(overrides) {
  return Object.assign({
    id: 'src-r1', session_id: 'auto-r1', tmux_session: 'auto-r1', label: '',
    project: '', role: '', latest: '', topics: [], org: null, type: 'container',
    session_type: 'interactive', is_live: false, created_at: '', last_activity_at: '',
    last_activity: 0, entry_count: 0, context_tokens: 0,
  }, overrides);
}

describe('sessions on-screen search — metadata filter', () => {
  it('matches when every token appears somewhere in the card metadata', () => {
    const page = makeSessionsPage();
    page.interactive = [
      card({ session_id: 'a', tmux_session: 'auto-0906-1', label: 'Passkey auth design', project: 'autonomy' }),
      card({ session_id: 'b', tmux_session: 'auto-0906-2', label: 'Backup drill', topics: ['verifying restore'] }),
      card({ session_id: 'c', tmux_session: 'host-0906-3', label: '', latest: 'passkey enrolment failed' }),
    ];
    page.setSearchQuery('passkey');
    assert.deepEqual(page.activeInteractive.map((s) => s.session_id), ['a', 'c']);

    page.setSearchQuery('passkey autonomy');
    assert.deepEqual(page.activeInteractive.map((s) => s.session_id), ['a']);

    page.setSearchQuery('RESTORE');
    assert.deepEqual(page.activeInteractive.map((s) => s.session_id), ['b'], 'topics + case-insensitive');

    page.setSearchQuery('host-0906');
    assert.deepEqual(page.activeInteractive.map((s) => s.session_id), ['c'], 'tmux name is searchable');
  });

  it('matches organization slug and name on resolved org objects', () => {
    const page = makeSessionsPage();
    page.interactive = [
      card({ session_id: 'a', org: { slug: 'anchore', name: 'Anchore Inc' } }),
      card({ session_id: 'b', org: { slug: 'autonomy', name: 'Autonomy' } }),
    ];
    page.setSearchQuery('anchore');
    assert.deepEqual(page.activeInteractive.map((s) => s.session_id), ['a']);
  });

  it('leaves every list untouched with an empty or whitespace query', () => {
    const page = makeSessionsPage();
    page.interactive = [card({ session_id: 'a' }), card({ session_id: 'b' })];
    page.setSearchQuery('   ');
    assert.equal(page.searchActive, false);
    assert.equal(page.activeInteractive.length, 2);
  });

  it('narrows Launching and Recent with the same query', () => {
    const page = makeSessionsPage();
    page.interactive = [
      card({ session_id: 'boot', _launching: true, label: 'New session for deploy' }),
      card({ session_id: 'boot2', _launching: true, label: 'Other' }),
    ];
    page.recent = [
      recentRow({ id: 'src-1', session_id: 'r1', label: 'deploy retrospective' }),
      recentRow({ id: 'src-2', session_id: 'r2', label: 'unrelated' }),
    ];
    page.recentSince = 'all';
    page.setSearchQuery('deploy');
    assert.deepEqual(page.launching.map((s) => s.session_id), ['boot']);
    assert.deepEqual(page.filtered.map((s) => s.session_id), ['r1']);
    assert.match(page.recentEmptyMessage(), /matching “deploy”/);
    assert.equal(page.activeEmptyMessage(), 'No active sessions matching “deploy”');
  });

  it('never reorders the Active list — it only hides non-matching cards', () => {
    const page = makeSessionsPage();
    page.interactive = [
      card({ session_id: 'a', label: 'alpha widget' }),
      card({ session_id: 'b', label: 'beta' }),
      card({ session_id: 'c', label: 'gamma widget' }),
    ];
    page.refreshActiveOrder();
    const before = page.sortedInteractive.map((s) => s.session_id);
    page.setSearchQuery('widget');
    const after = page.sortedInteractive.map((s) => s.session_id);
    assert.deepEqual(after, before.filter((id) => id !== 'b'));
  });
});

describe('sessions on-screen search — transcript search', () => {
  it('restricts /api/search to the on-screen source ids and keeps hit cards visible', async () => {
    const calls = [];
    const page = makeSessionsPage({
      fetch(url) {
        calls.push(url);
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve([
            { source_id: 'src-b', source_title: 'Beta', excerpts: [] },
            { source_id: 'src-r2', source_title: 'Recent two', excerpts: [] },
          ]),
        });
      },
    });
    page.interactive = [
      card({ session_id: 'a', label: 'rollback plan', graph_source_id: 'src-a' }),
      card({ session_id: 'b', label: 'beta', graph_source_id: 'src-b' }),
      card({ session_id: 'c', label: 'no source yet', graph_source_id: '' }),
    ];
    page.recent = [
      recentRow({ id: 'src-r1', session_id: 'r1', label: 'nothing' }),
      recentRow({ id: 'src-r2', session_id: 'r2', label: 'nothing either' }),
    ];
    page.recentSince = 'all';

    page.setSearchQuery('rollback');
    assert.deepEqual(page.activeInteractive.map((s) => s.session_id), ['a'], 'metadata-only before Enter');
    assert.match(page.searchSummary, /Enter searches transcripts/);

    await page.runTranscriptSearch();

    assert.equal(calls.length, 1);
    const url = new URL(calls[0], 'http://x');
    assert.equal(url.pathname, '/api/search');
    assert.equal(url.searchParams.get('q'), 'rollback');
    assert.equal(url.searchParams.get('group'), '1');
    assert.deepEqual(url.searchParams.get('source_ids').split(','), ['src-a', 'src-b', 'src-r1', 'src-r2']);
    assert.equal(url.searchParams.get('limit'), '4', 'limit covers every candidate source');

    assert.deepEqual(page.activeInteractive.map((s) => s.session_id), ['a', 'b'], 'metadata OR transcript hit');
    assert.deepEqual(page.filtered.map((s) => s.session_id), ['r2']);
    assert.match(page.searchSummary, /2 transcript hits/);
  });

  it('drops transcript hits the moment the typed query changes', async () => {
    const page = makeSessionsPage({
      fetch() {
        return Promise.resolve({ ok: true, json: () => Promise.resolve([{ source_id: 'src-b' }]) });
      },
    });
    page.interactive = [
      card({ session_id: 'a', label: 'alpha', graph_source_id: 'src-a' }),
      card({ session_id: 'b', label: 'beta', graph_source_id: 'src-b' }),
    ];
    page.setSearchQuery('alpha');
    await page.runTranscriptSearch();
    assert.deepEqual(page.activeInteractive.map((s) => s.session_id), ['a', 'b']);

    page.setSearchQuery('alph');
    assert.deepEqual(page.activeInteractive.map((s) => s.session_id), ['a'], 'stale hits do not apply');
    assert.match(page.searchSummary, /Enter searches transcripts/);
  });

  it('ignores a response that arrives after the query was superseded', async () => {
    let resolveFetch;
    const page = makeSessionsPage({
      fetch() {
        return new Promise((resolve) => { resolveFetch = resolve; });
      },
    });
    page.interactive = [card({ session_id: 'b', label: 'beta', graph_source_id: 'src-b' })];
    page.setSearchQuery('alpha');
    const run = page.runTranscriptSearch();
    assert.equal(page.searchFts.pending, true);
    page.setSearchQuery('gamma');
    resolveFetch({ ok: true, json: () => Promise.resolve([{ source_id: 'src-b' }]) });
    await run;
    assert.equal(page.searchFts.query, '');
    assert.deepEqual(page.activeInteractive, []);
  });

  it('reports a failed transcript search without hiding metadata matches', async () => {
    const page = makeSessionsPage({
      fetch() { return Promise.resolve({ ok: false, status: 500 }); },
    });
    page.interactive = [card({ session_id: 'a', label: 'alpha', graph_source_id: 'src-a' })];
    page.setSearchQuery('alpha');
    await page.runTranscriptSearch();
    assert.equal(page.searchFts.error, 'search failed (500)');
    assert.deepEqual(page.activeInteractive.map((s) => s.session_id), ['a']);
    assert.match(page.searchSummary, /transcript search failed/);
  });

  it('skips the request entirely when nothing on screen has a source id', async () => {
    let called = 0;
    const page = makeSessionsPage({ fetch() { called += 1; return Promise.resolve({ ok: true, json: () => [] }); } });
    page.interactive = [card({ session_id: 'a', label: 'alpha' })];
    page.setSearchQuery('alpha');
    await page.runTranscriptSearch();
    assert.equal(called, 0);
    assert.equal(page.searchFts.query, 'alpha');
  });

  it('clearSearch resets the query and any transcript hits', async () => {
    const page = makeSessionsPage({
      fetch() { return Promise.resolve({ ok: true, json: () => Promise.resolve([{ source_id: 'src-a' }]) }); },
    });
    page.interactive = [card({ session_id: 'a', graph_source_id: 'src-a' }), card({ session_id: 'b' })];
    page.setSearchQuery('zzz');
    await page.runTranscriptSearch();
    assert.deepEqual(page.activeInteractive.map((s) => s.session_id), ['a']);
    page.clearSearch();
    assert.equal(page.searchActive, false);
    assert.equal(page.searchFts.count, 0);
    assert.equal(page.activeInteractive.length, 2);
  });
});

describe('sessions on-screen search — markup contract', () => {
  it('renders the chip with query, summary and a clear control, and search-aware empty states', () => {
    const html = fs.readFileSync(SESSIONS_HTML, 'utf8');
    assert.match(html, /data-testid="sessions-search-chip"/);
    assert.match(html, /x-show="searchActive"/);
    assert.match(html, /x-text="searchQuery"/);
    assert.match(html, /x-text="searchSummary"/);
    assert.match(html, /@click="clearSearch\(\)"/);
    assert.match(html, /x-text="activeEmptyMessage\(\)"/);
    assert.ok(
      html.indexOf('data-testid="sessions-search-chip"') < html.indexOf('data-testid="launching-section"'),
      'chip sits above the first card list',
    );
  });
});
