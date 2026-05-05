"""Hydration unit tests for the loadBoard sweep (bead auto-fwwfu).

Drives ``page.js``'s ``init() → loadBoard()`` against a stubbed
``Schema.alpine`` runtime + a stubbed ``global.fetch`` so the test
can assert:

* per-tile / per-thread / per-sprint normalizers capture
  ``member.updated_at`` into ``updatedAt`` (no longer ``ageMin``);
* ``data.snapshotTime`` derives from ``relativeTime(newest member's
  updated_at)`` rather than a render-time string;
* ``data.snapshotTimeRaw`` carries the raw ISO for tooltips;
* the 11th ``/api/worktrees`` fetch populates
  ``data.pendingCommitCount`` as ``sum(commits_ahead)`` across rows;
* an empty / 4xx / 5xx ``/api/worktrees`` response yields a counter
  of ``0`` and does not throw.

These tests live with the plugin per the operator's structural rule
(graph://f6c6c43e-24a). The main pytest ``testpaths`` does not collect
them; run explicitly with::

    pytest tools/dashboard/plugins/coordinator_board/tests/
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


PLUGIN_DIR = Path(__file__).resolve().parents[1]
PAGE_JS = PLUGIN_DIR / "page.js"
SCHEMAS_JS = PLUGIN_DIR.parents[1] / "static" / "js" / "schemas.js"


def _node_available() -> bool:
    return shutil.which("node") is not None


_NODE_DRIVER = r"""
const Schema = require(%(schemas_js)s);
const { coordinatorBoard } = require(%(page_js)s);

global.window = global.window || {};
global.window.crypto = { randomUUID: () => 'fixed-uuid-for-test' };
global.window.Autonomy = {};

const seeded = %(seeded)s;
const worktreeFixture = %(worktree_fixture)s;

// ── /api/worktrees stub via global.fetch ─────────────────────────
//
// page.js calls fetch('/api/worktrees', { credentials: 'same-origin' })
// directly — not through the Autonomy.fetch shim — so we stub
// global.fetch to return whatever the test's ``worktree_fixture`` says.
global.fetch = async (path, _opts) => {
  if (path === '/api/worktrees') {
    if (worktreeFixture && worktreeFixture.kind === 'http_error') {
      return { ok: false, status: worktreeFixture.status || 500,
               json: async () => ({}) };
    }
    return {
      ok: true, status: 200,
      json: async () => (worktreeFixture && worktreeFixture.body) || [],
    };
  }
  return { ok: false, status: 404, json: async () => ({}) };
};

// ── Schema-meta stubs (one per (set_id, revision) the page binds) ──
function meta(setId, revision, accessPattern, keyStrategy, variants) {
  return {
    set_id: setId,
    schema_revision: revision,
    type: 'object',
    properties: {},
    required: [],
    access_pattern: accessPattern || null,
    key_strategy: keyStrategy || null,
    variants: variants || {},
  };
}

const META = {
  'dashboard.coordinator#1':
    meta('dashboard.coordinator', 1, 'singleton', 'fixed:default'),
  'dashboard.coordinator-canvas#1':
    meta('dashboard.coordinator-canvas', 1, 'keyed_per_entity', 'natural'),
  'dashboard.operator-message-to-coordinator#1':
    meta('dashboard.operator-message-to-coordinator', 1, 'singleton', 'fixed:default'),
  'dashboard.coordinator-tile#3':
    meta('dashboard.coordinator-tile', 3, 'keyed_per_entity', 'natural'),
  'dashboard.coordinator-thread#3':
    meta('dashboard.coordinator-thread', 3, 'keyed_per_entity', 'natural'),
  'dashboard.coordinator-decision#1':
    meta('dashboard.coordinator-decision', 1, 'append_only_log', 'uuid_v4'),
  'dashboard.coordinator-sprint#2':
    meta('dashboard.coordinator-sprint', 2, 'keyed_per_entity', 'natural'),
  'dashboard.coordinator-bead#1':
    meta('dashboard.coordinator-bead', 1, 'keyed_per_entity', 'natural'),
  'dashboard.coordinator-convergent-decision#1':
    meta('dashboard.coordinator-convergent-decision', 1, 'keyed_per_entity', 'natural'),
  'dashboard.coordinator-open-followup#1':
    meta('dashboard.coordinator-open-followup', 1, 'keyed_per_entity', 'natural'),
  'dashboard.coordinator-docs#1':
    meta('dashboard.coordinator-docs', 1, 'singleton', 'fixed:default'),
};

const META_PREFIX = '/api/graph/settings/autonomy.schema/';

Schema._clearCache();
Schema._setFetchOverride(async (path, opts) => {
  if (path.indexOf(META_PREFIX) === 0) {
    const key = decodeURIComponent(path.slice(META_PREFIX.length));
    const payload = META[key];
    if (!payload) return { ok: false, status: 404, json: async () => ({}) };
    return { ok: true, status: 200, json: async () => ({ payload }) };
  }
  if (path === '/api/graph/setting' && opts && opts.method === 'POST') {
    return { ok: true, status: 200, json: async () => ({ id: 'stub' }) };
  }
  // Singleton read.
  const keyed = path.match(/^\/api\/graph\/settings\/([^/?]+)\/([^/?]+)(?:\?.*)?$/);
  if (keyed) {
    const setId = decodeURIComponent(keyed[1]);
    const key = decodeURIComponent(keyed[2]);
    if (setId === 'dashboard.coordinator' && key === 'default') {
      const row = seeded.coordinator || null;
      if (!row) return { ok: false, status: 404, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => row };
    }
    if (setId === 'dashboard.coordinator-docs' && key === 'default') {
      const row = seeded.docs_singleton || null;
      if (!row) return { ok: false, status: 404, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => row };
    }
    if (setId === 'dashboard.operator-message-to-coordinator' && key === 'default') {
      const row = seeded.op_singleton || null;
      if (!row) return { ok: false, status: 404, json: async () => ({}) };
      return { ok: true, status: 200, json: async () => row };
    }
  }
  // List read — /api/graph/settings/<set_id>(?target_revision=N).
  const m = path.match(/^\/api\/graph\/settings\/([^/?]+)(?:\?.*)?$/);
  if (m) {
    const setId = decodeURIComponent(m[1]);
    const buckets = {
      'dashboard.coordinator-canvas': seeded.canvas || [],
      'dashboard.coordinator-tile': seeded.tiles || [],
      'dashboard.coordinator-thread': seeded.threads || [],
      'dashboard.coordinator-sprint': seeded.sprints || [],
      'dashboard.coordinator-decision': seeded.decisions || [],
      'dashboard.coordinator-bead': seeded.beads || [],
      'dashboard.coordinator-convergent-decision': seeded.convergent || [],
      'dashboard.coordinator-open-followup': seeded.followups || [],
      'dashboard.coordinator-docs': seeded.docs || [],
      'dashboard.operator-message-to-coordinator': seeded.op || [],
    };
    return {
      ok: true, status: 200,
      json: async () => ({ members: buckets[setId] || [] }),
    };
  }
  return { ok: false, status: 404, json: async () => ({}) };
});

const c = coordinatorBoard();

let onerrorFired = false;
process.on('uncaughtException', () => { onerrorFired = true; });

(async () => {
  await c.init();
  // Drain microtasks so any awaited fetch resolutions land.
  await new Promise(resolve => setImmediate(resolve));
  return {
    pendingCommitCount: c.data.pendingCommitCount,
    snapshotTime: c.data.snapshotTime,
    snapshotTimeRaw: c.data.snapshotTimeRaw,
    tiles: c.data.tiles.map(t => ({
      session: t.session, updatedAt: t.updatedAt, hasAgeMin: 'ageMin' in t,
    })),
    threads: c.data.threads.map(t => ({
      session: t.session, updatedAt: t.updatedAt, hasAgeMin: 'ageMin' in t,
    })),
    sprints: c.data.sprints.map(s => ({
      id: s.id, updatedAt: s.updatedAt, hasAgeMin: 'ageMin' in s,
    })),
    onerrorFired,
  };
})().then((out) => {
  process.stdout.write(JSON.stringify(out));
}).catch((e) => {
  process.stderr.write(String(e && e.stack || e));
  process.exit(1);
});
"""


def _run(seeded: dict | None = None, worktree_fixture: dict | None = None) -> dict:
    src = _NODE_DRIVER % {
        "schemas_js": json.dumps(str(SCHEMAS_JS)),
        "page_js": json.dumps(str(PAGE_JS)),
        "seeded": json.dumps(seeded or {}),
        "worktree_fixture": json.dumps(worktree_fixture or {}),
    }
    proc = subprocess.run(
        ["node", "-e", src],
        capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"node driver failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


@pytest.mark.skipif(not _node_available(), reason="node not installed")
class TestNormalizersCaptureUpdatedAt:
    """Tile / thread / sprint normalizers map ``member.updated_at`` →
    ``updatedAt`` (the rendered ``relativeTime`` source); ``ageMin``
    is no longer present on the normalized shape."""

    def test_tiles_carry_updated_at(self):
        out = _run(seeded={
            "tiles": [
                {
                    "key": "auto-foo",
                    "payload": {
                        "label": "Foo", "role": "implementer",
                        "thing": "x", "asks": "fyi",
                    },
                    "updated_at": "2026-04-30T11:50:00Z",
                },
            ],
        })
        assert len(out["tiles"]) == 1, out
        tile = out["tiles"][0]
        assert tile["session"] == "auto-foo"
        assert tile["updatedAt"] == "2026-04-30T11:50:00Z", out
        assert tile["hasAgeMin"] is False, (
            f"normalized tile should not carry ageMin; got {tile}"
        )

    def test_threads_carry_updated_at(self):
        out = _run(seeded={
            "threads": [
                {
                    "key": "auto-blocked",
                    "payload": {
                        "label": "Blocked", "role": "pair",
                        "status": "blocked", "lead": "x",
                    },
                    "updated_at": "2026-04-30T10:00:00Z",
                },
            ],
        })
        assert len(out["threads"]) == 1, out
        thread = out["threads"][0]
        assert thread["session"] == "auto-blocked"
        assert thread["updatedAt"] == "2026-04-30T10:00:00Z"
        assert thread["hasAgeMin"] is False

    def test_sprints_carry_updated_at(self):
        out = _run(seeded={
            "sprints": [
                {
                    "key": "sprint-x",
                    "payload": {"title": "x", "status": "active"},
                    "updated_at": "2026-04-30T09:30:00Z",
                },
            ],
        })
        assert len(out["sprints"]) == 1, out
        sprint = out["sprints"][0]
        assert sprint["id"] == "sprint-x"
        assert sprint["updatedAt"] == "2026-04-30T09:30:00Z"
        assert sprint["hasAgeMin"] is False

    def test_normalizer_falls_back_to_created_at(self):
        out = _run(seeded={
            "tiles": [
                {
                    "key": "auto-foo",
                    "payload": {
                        "label": "Foo", "role": "implementer",
                        "thing": "x", "asks": "fyi",
                    },
                    "created_at": "2026-04-30T08:00:00Z",
                    # No updated_at — _memberTime should fall back.
                },
            ],
        })
        assert out["tiles"][0]["updatedAt"] == "2026-04-30T08:00:00Z"


@pytest.mark.skipif(not _node_available(), reason="node not installed")
class TestSnapshotTimeFromNewestMember:
    """``data.snapshotTime`` reflects the freshest member loaded across
    the 10 parallel-fetched sets (canvas, op, tiles, threads, decisions,
    sprints, beads, convergent, followups, docs)."""

    def test_snapshot_time_is_relative_time_of_newest(self):
        out = _run(seeded={
            "canvas": [{
                "key": "k",
                "payload": {"question": "q"},
                "updated_at": "2026-04-30T10:00:00Z",
            }],
            "tiles": [{
                "key": "auto-foo",
                "payload": {
                    "label": "x", "role": "y", "thing": "z", "asks": "fyi",
                },
                "updated_at": "2026-04-30T11:00:00Z",
            }],
            "threads": [{
                "key": "auto-newest",
                "payload": {
                    "label": "x", "role": "y",
                    "status": "blocked", "lead": "z",
                },
                # Newest across all sets — drives snapshotTime.
                "updated_at": "2030-01-01T00:00:00Z",
            }],
        })
        assert out["snapshotTimeRaw"] == "2030-01-01T00:00:00Z", out
        # snapshotTime is the relative-time render of that ISO. Either
        # the date crosses the >24h threshold (likely — far future), so
        # we fall through to the localized date string with a 4-digit
        # year, OR — if test-day = 2030-01-01 — it could read "just now".
        # Both are acceptable; assert non-empty + contains the year.
        assert out["snapshotTime"], out
        assert "2030" in out["snapshotTime"] or "ago" in out["snapshotTime"] \
            or out["snapshotTime"] == "just now", out

    def test_snapshot_time_empty_when_no_members(self):
        out = _run(seeded={})
        assert out["snapshotTimeRaw"] == "", out
        assert out["snapshotTime"] == "", out


@pytest.mark.skipif(not _node_available(), reason="node not installed")
class TestPendingCommitCountFromWorktrees:
    """``data.pendingCommitCount`` = sum of ``commits_ahead`` across the
    bare-list ``/api/worktrees`` rows."""

    def test_sums_commits_ahead_across_rows(self):
        out = _run(worktree_fixture={"body": [
            {"commits_ahead": 3}, {"commits_ahead": 0},
            {"commits_ahead": 7}, {"commits_ahead": 2},
        ]})
        assert out["pendingCommitCount"] == 12, out

    def test_empty_list_reads_zero(self):
        out = _run(worktree_fixture={"body": []})
        assert out["pendingCommitCount"] == 0, out

    def test_http_error_reads_zero_no_throw(self):
        out = _run(worktree_fixture={"kind": "http_error", "status": 500})
        assert out["pendingCommitCount"] == 0, out
        assert out["onerrorFired"] is False, out

    def test_object_response_coerces_to_zero(self):
        # Defensive coercion: server returns a list but a transient error
        # path could plausibly serialize an object. The reduce should not
        # throw and the counter should read 0.
        out = _run(worktree_fixture={"body": {"oops": "not a list"}})
        assert out["pendingCommitCount"] == 0, out
        assert out["onerrorFired"] is False, out

    def test_missing_commits_ahead_field_treated_as_zero(self):
        out = _run(worktree_fixture={"body": [
            {"commits_ahead": 4},
            {"branch": "x"},  # no commits_ahead — coerced to 0
            {"commits_ahead": 1},
        ]})
        assert out["pendingCommitCount"] == 5, out
