"""L2.B behavioral sweep tests — fast browser-based assertions on user-visible behavior.

Testing hierarchy:
    L1 (unit, 5ms) → L2.A (HTTP contract, 50ms) → **L2.B (behavioral sweep, 200ms/page)**
    → L3 (interactive browser, 3s) → L4 (production smoke, 30s)

Architecture:
    - ONE DASHBOARD_MOCK server (module-scoped fixture, boots once)
    - ONE agent-browser session (module-scoped, reused across all tests)
    - Per page: SPA-navigates via link click, runs ONE batched JS eval
      with all checks, returns structured dict
    - Python asserts on dict values — each assert is a user-visible behavior

Pattern for adding a new page:
    1. Define a JS check function that returns {check_name: value, ...}
    2. Write a test class using _navigate_and_check(path, js_checks)
    3. Each test method asserts one key from the returned dict

Fixture data is self-contained — no external DB, no tmux, no real sessions.
The DASHBOARD_MOCK server reads fixture JSON on every request; the sessions
page seeds its Alpine store from /api/dao/active_sessions (HTTP fallback).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

# ── Fixture data ──────────────────────────────────────────────────────

NOW = int(time.time())

SWEEP_SESSIONS = [
    {
        "session_id": "auto-sweep-alpha",
        "tmux_session": "auto-sweep-alpha",
        "project": "autonomy",
        "type": "container",
        "is_live": True,
        "started_at": NOW - 3600,
        "label": "Alpha — card redesign",
        "role": "designer",
        "entry_count": 150,
        "context_tokens": 80000,
        "last_activity": NOW - 120,
        "last_message": "Working on card CSS",
        "topics": ["Redesigning session cards", "CSS grid layout"],
        "nag_enabled": False,
        "nag_interval": 15,
        "nag_message": "",
    },
    {
        "session_id": "auto-sweep-beta",
        "tmux_session": "auto-sweep-beta",
        "project": "autonomy",
        "type": "container",
        "is_live": True,
        "started_at": NOW - 7200,
        "label": "Beta Builder",
        "role": "builder",
        "entry_count": 200,
        "context_tokens": 120000,
        "last_activity": NOW - 300,
        "last_message": "Compiling assets",
        "topics": ["Asset pipeline", "Webpack config", "Tree shaking"],
        "nag_enabled": False,
        "nag_interval": 15,
        "nag_message": "",
    },
    {
        "session_id": "auto-sweep-gamma",
        "tmux_session": "auto-sweep-gamma",
        "project": "autonomy",
        "type": "container",
        "is_live": True,
        "started_at": NOW - 1800,
        "label": "Gamma Reviewer",
        "role": "reviewer",
        "entry_count": 75,
        "context_tokens": 45000,
        "last_activity": NOW - 60,
        "last_message": "Reviewing PR #42",
        "topics": ["Code review"],
        "nag_enabled": True,
        "nag_interval": 10,
        "nag_message": "Check review status",
    },
    {
        "session_id": "host-sweep-delta",
        "tmux_session": "host-sweep-delta",
        "project": "autonomy",
        "type": "host",
        "is_live": True,
        "started_at": NOW - 14400,
        "label": "Host: merge recovery",
        "role": "coordinator",
        "entry_count": 300,
        "context_tokens": 250000,
        "last_activity": NOW - 900,
        "last_message": "Dolt restarted",
        "topics": [],
        "nag_enabled": False,
        "nag_interval": 15,
        "nag_message": "",
    },
    {
        "session_id": "auto-sweep-epsilon",
        "tmux_session": "auto-sweep-epsilon",
        "project": "autonomy",
        "type": "container",
        "is_live": True,
        "started_at": NOW - 600,
        "label": "Epsilon Session",
        "role": "",
        "entry_count": 50,
        "context_tokens": 30000,
        "last_activity": NOW - 30,
        "last_message": "Idle session",
        "topics": [],
        "nag_enabled": False,
        "nag_interval": 15,
        "nag_message": "",
    },
]

SWEEP_RECENT_SESSIONS = [
    {"id": "src-sweep-aaa111", "type": "session", "date": "2026-03-25",
     "title": "Alpha history session", "project": "autonomy"},
    {"id": "src-sweep-bbb222", "type": "session", "date": "2026-03-24",
     "title": "Beta history session", "project": "autonomy"},
    {"id": "src-sweep-ccc333", "type": "session", "date": "2026-03-23",
     "title": "Gamma history session", "project": "default"},
]

SWEEP_SESSION_ENTRIES = {
    s["session_id"]: [
        {"type": "system", "content": "Session started", "timestamp": NOW - 3600},
        {"type": "user", "content": "Hello", "timestamp": NOW - 3590},
        {"type": "assistant_text", "content": "Hi there", "timestamp": NOW - 3585},
    ]
    for s in SWEEP_SESSIONS
}

# ── Beads (used by /beads, /bead/{id}, /dispatch, nav SSE) ───────────

SWEEP_BEADS = [
    {
        "id": "auto-sweep-b1", "title": "Sweep alpha task",
        "priority": 1, "status": "open", "issue_type": "task",
        "labels": ["readiness:approved"], "created_by": "librarian",
        "description": "First test bead for behavioral sweep",
    },
    {
        "id": "auto-sweep-b2", "title": "Sweep beta bug",
        "priority": 2, "status": "in_progress", "issue_type": "bug",
        "labels": ["readiness:specified", "dashboard"], "created_by": "user",
        "description": "Second test bead with dependencies",
    },
    {
        "id": "auto-sweep-b3", "title": "Sweep gamma feature",
        "priority": 0, "status": "open", "issue_type": "feature",
        "labels": [], "created_by": "librarian",
    },
]

# ── Dispatch runs (used by /timeline, /dispatch/trace) ───────────────

SWEEP_RUNS = [
    {
        "id": "run-sweep-001", "bead_id": "auto-sweep-b1",
        "status": "DONE", "title": "Sweep alpha task",
        "priority": 1, "duration_secs": 300,
        "started_at": "2026-03-25T10:00:00Z",
        "completed_at": "2026-03-25T10:05:00Z",
        "lines_added": 45, "lines_removed": 12, "files_changed": 3,
        "reason": "All tests pass",
        "scores": {"tooling": 4, "clarity": 5, "confidence": 4},
        "time_breakdown": {
            "research_pct": 20, "coding_pct": 60,
            "debugging_pct": 15, "tooling_workaround_pct": 5,
        },
    },
    {
        "id": "run-sweep-002", "bead_id": "auto-sweep-b2",
        "status": "FAILED", "title": "Sweep beta bug",
        "priority": 2, "duration_secs": 120,
        "started_at": "2026-03-25T09:00:00Z",
        "completed_at": "2026-03-25T09:02:00Z",
        "reason": "Tests failed on assertion",
    },
]

# ── Timeline data ────────────────────────────────────────────────────

SWEEP_TIMELINE_ENTRIES = SWEEP_RUNS  # timeline uses same shape as runs

SWEEP_TIMELINE_STATS = {
    "completed_count": 5,
    "success_rate": 0.8,
    "failed_count": 1,
    "blocked_count": 0,
    "avg_duration": 240.0,
    "avg_tooling_score": 3.5,
    "avg_confidence_score": 4.0,
    "avg_clarity_score": 3.8,
}

# ── Collab data ──────────────────────────────────────────────────────

SWEEP_COLLAB_NOTES = [
    {
        "id": "note-sweep-001", "title": "Architecture decision on auth",
        "created_at": "2026-03-25T08:00:00Z", "author": "agent-alpha",
        "project": "autonomy", "tags": ["architecture", "auth"],
        "comment_count": 2, "version": 1, "source_type": "note",
        "preview": "Passkey authentication requires WebAuthn support",
    },
    {
        "id": "note-sweep-002", "title": "Testing strategy update",
        "created_at": "2026-03-24T12:00:00Z", "author": "agent-beta",
        "project": "autonomy", "tags": ["testing"],
        "comment_count": 0, "version": 1, "source_type": "note",
        "preview": "L2.B behavioral sweep covers all pages",
    },
]

SWEEP_THOUGHTS = [
    {
        "id": "thought-sweep-001", "content": "Auth needs passkeys for MFA",
        "status": "captured", "thread_id": None,
        "source_id": None, "turn_number": None,
        "created_at": "2026-03-25T09:00:00Z",
    },
    {
        "id": "thought-sweep-002", "content": "Consider Alpine.js migration path",
        "status": "actioned", "thread_id": "thread-sweep-001",
        "source_id": None, "turn_number": None,
        "created_at": "2026-03-24T14:00:00Z",
    },
]

SWEEP_THREADS = [
    {
        "id": "thread-sweep-001", "title": "Passkey auth design",
        "status": "active", "priority": 1, "capture_count": 3,
        "created_at": "2026-03-24T10:00:00Z",
        "updated_at": "2026-03-25T09:00:00Z",
    },
    {
        "id": "thread-sweep-002", "title": "Performance optimization",
        "status": "resolved", "priority": 2, "capture_count": 1,
        "created_at": "2026-03-23T10:00:00Z",
        "updated_at": "2026-03-24T10:00:00Z",
    },
]

# ── Streams data ─────────────────────────────────────────────────────

SWEEP_STREAMS = [
    {"tag": "pitfall", "count": 12, "description": "Operational hazards and gotchas",
     "last_active": "2026-03-25T10:00:00Z"},
    {"tag": "architecture", "count": 8, "description": "Design decisions and patterns",
     "last_active": "2026-03-24T10:00:00Z"},
    {"tag": "testing", "count": 5, "description": "Testing strategies and patterns",
     "last_active": "2026-03-23T10:00:00Z"},
]

# ── Trace / primer / deps data ───────────────────────────────────────

SWEEP_TRACES = {
    "run-sweep-001": {
        "id": "run-sweep-001", "bead_id": "auto-sweep-b1",
        "status": "DONE", "reason": "All tests pass",
        "duration_secs": 300,
        "started_at": "2026-03-25T10:00:00Z",
        "completed_at": "2026-03-25T10:05:00Z",
        "commit_hash": "abc123def456789",
        "lines_added": 45, "lines_removed": 12, "files_changed": 3,
        "is_live": False,
        "decision": {
            "status": "DONE", "reason": "All tests pass",
            "scores": {"tooling": 4, "clarity": 5, "confidence": 4},
            "time_breakdown": {
                "research_pct": 20, "coding_pct": 60,
                "debugging_pct": 15, "tooling_workaround_pct": 5,
            },
        },
    },
}

SWEEP_PRIMERS = {
    "auto-sweep-b1": {
        "bead_id": "auto-sweep-b1", "title": "Sweep alpha task",
        "description": "First test bead for behavioral sweep",
        "priority": 1, "status": "open",
        "pitfalls": [],
        "provenance": [],
        "similar_beads": [],
    },
}

SWEEP_BEAD_DEPS = {
    "auto-sweep-b2": {
        "blockers": [{"id": "auto-sweep-b1", "title": "Sweep alpha task", "status": "open"}],
        "dependents": [],
    },
}

# ── Dispatch SSE event (pushed after browser connects) ───────────────

DISPATCH_SSE_DATA = {
    "active": [
        {"id": "auto-sweep-b1", "title": "Sweep alpha task",
         "priority": 1, "status": "RUNNING", "duration_secs": 120,
         "snippet": "Working on tests"},
    ],
    "waiting": [
        {"id": "auto-sweep-b3", "title": "Sweep gamma feature",
         "priority": 0, "status": "waiting"},
    ],
    "blocked": [
        {"id": "auto-sweep-b2", "title": "Sweep beta bug",
         "priority": 2, "status": "blocked",
         "blockers": [{"id": "auto-sweep-b1", "title": "Sweep alpha task"}]},
    ],
    "paused": {"dispatch": False, "merge": False},
    "pause_reasons": {},
}

# ── Additional fixture data for trace overlay, bead viewer, host session tests ──

SWEEP_BEAD_DISPATCHED = {
    "id": "auto-sweep-b2",
    "title": "Sweep dispatched bead",
    "priority": 1,
    "status": "closed",
    "labels": [],
    "description": "A test bead that was dispatched and completed successfully.",
}

SWEEP_DISPATCH_RUN = {
    "id": "auto-sweep-b2-20260327-120000",
    "bead_id": "auto-sweep-b2",
    "dir": "auto-sweep-b2-20260327-120000",
    "status": "DONE",
    "started_at": "2026-03-27T12:00:00Z",
    "completed_at": "2026-03-27T12:05:00Z",
    "duration_secs": 300,
    "commit_hash": "abc123def456789",
    "lines_added": 50,
    "lines_removed": 10,
    "files_changed": 3,
    "decision": {
        "status": "DONE",
        "reason": "All tests pass",
        "scores": {"tooling": 4, "clarity": 5, "confidence": 4},
    },
}

SWEEP_TRACE_DATA = {
    "auto-sweep-b2-20260327-120000": {
        "id": "auto-sweep-b2-20260327-120000",
        "bead_id": "auto-sweep-b2",
        "status": "DONE",
        "reason": "All tests pass",
        "duration_secs": 300,
        "commit_hash": "abc123def456789",
        "decision": {
            "status": "DONE",
            "reason": "All tests pass",
            "scores": {"tooling": 4, "clarity": 5, "confidence": 4},
        },
        "experience_report": "# Experience Report\n\nEverything went smoothly.",
        "diff": "+++ tools/test.py\n+def test_it():\n+    assert True",
    },
}

SWEEP_PRIMER_DATA = {
    "auto-sweep-b2": {
        "bead_id": "auto-sweep-b2",
        "title": "Sweep dispatched bead",
        "description": "A test bead that was dispatched and completed.",
        "priority": 1,
        "status": "closed",
    },
}

# Session entries for dispatch run (for overlay panel / bead detail viewer)
SWEEP_DISPATCH_ENTRIES = [
    {"type": "system", "content": "Session started", "timestamp": NOW - 600},
    {"type": "user", "content": "Implement the sweep feature", "timestamp": NOW - 590},
    {"type": "assistant_text", "content": "I will implement the sweep feature now.", "timestamp": NOW - 580},
    {"type": "tool_use", "tool_name": "Edit", "content": "Editing sweep.py",
     "timestamp": NOW - 570},
    {"type": "tool_result", "content": "File saved", "timestamp": NOW - 565},
    {"type": "assistant_text", "content": "The feature is implemented and tests pass.",
     "timestamp": NOW - 550},
]


# ── Experiment data (used by /experiments/{id}) ──────────────────────

SWEEP_EXPERIMENT_ID = "exp-sweep-00000000-0000-0000-0000-000000000001"

SWEEP_EXPERIMENT = {
    "id": SWEEP_EXPERIMENT_ID,
    "title": "Sweep Toolbar Experiment",
    "status": "pending",
    "series_id": SWEEP_EXPERIMENT_ID,
    "series_seq": 3,
    "sibling_ids": [
        "exp-sweep-00000000-0000-0000-0000-000000000001",
        "exp-sweep-00000000-0000-0000-0000-000000000002",
        "exp-sweep-00000000-0000-0000-0000-000000000003",
    ],
    "alpine": 0,
    "variants": [
        {"id": "v-sweep-001", "html": "<h1>Sweep toolbar test</h1>"}
    ],
}


def _build_fixture() -> dict:
    """Build the complete fixture dict for behavioral sweep tests."""
    entries = dict(SWEEP_SESSION_ENTRIES)
    # Add dispatch run entries keyed by run dir name (for dispatch tail)
    entries["auto-sweep-b2-20260327-120000"] = SWEEP_DISPATCH_ENTRIES
    return {
        "active_sessions": SWEEP_SESSIONS,
        "session_entries": entries,
        "recent_sessions": SWEEP_RECENT_SESSIONS,
        "beads": SWEEP_BEADS + [SWEEP_BEAD_DISPATCHED],
        "runs": SWEEP_RUNS + [SWEEP_DISPATCH_RUN],
        "experiments": [SWEEP_EXPERIMENT],
        "timeline_entries": SWEEP_TIMELINE_ENTRIES,
        "timeline_stats": SWEEP_TIMELINE_STATS,
        "collab_notes": SWEEP_COLLAB_NOTES,
        "thoughts": SWEEP_THOUGHTS,
        "threads": SWEEP_THREADS,
        "streams": SWEEP_STREAMS,
        "traces": {**SWEEP_TRACES, **SWEEP_TRACE_DATA},
        "primers": {**SWEEP_PRIMERS, **SWEEP_PRIMER_DATA},
        "bead_deps": SWEEP_BEAD_DEPS,
    }


# ── Module-scoped fixtures ────────────────────────────────────────────

@pytest.fixture(scope="module")
def sweep_server(tmp_path_factory):
    """Boot a DASHBOARD_MOCK uvicorn server on a test port, tear down after module."""
    tmpdir = tmp_path_factory.mktemp("sweep")
    fixture_path = tmpdir / "fixtures.json"
    fixture_path.write_text(json.dumps(_build_fixture(), indent=2))

    events_path = tmpdir / "events.jsonl"
    events_path.write_text("")  # empty — SSE events written after browser connects

    port = 8091
    env = {
        **os.environ,
        "DASHBOARD_MOCK": str(fixture_path),
        "DASHBOARD_MOCK_EVENTS": str(events_path),
        "PYTHONPATH": str(Path(__file__).resolve().parents[3]),  # repo root
    }

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "tools.dashboard.server:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for server to be ready (up to 8s)
    deadline = time.time() + 8
    ready = False
    while time.time() < deadline:
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/dao/active_sessions", timeout=1)
            ready = True
            break
        except Exception:
            time.sleep(0.2)

    if not ready:
        proc.kill()
        out, err = proc.communicate(timeout=3)
        pytest.fail(f"Sweep server failed to start:\nstdout: {out.decode()}\nstderr: {err.decode()}")

    yield {
        "port": port,
        "url": f"http://127.0.0.1:{port}",
        "fixture_path": str(fixture_path),
        "events_path": str(events_path),
    }

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


@pytest.fixture(scope="module")
def browser(sweep_server):
    """Open one agent-browser session, reuse across all tests in module."""
    url = sweep_server["url"] + "/sessions"
    subprocess.run(
        ["agent-browser", "open", url],
        capture_output=True, timeout=10,
    )
    subprocess.run(
        ["agent-browser", "wait", "--load", "networkidle"],
        capture_output=True, timeout=10,
    )

    # Push dispatch SSE events so _sseCache is populated for /dispatch page
    events_path = sweep_server["events_path"]
    with open(events_path, "a") as f:
        f.write(json.dumps({"topic": "dispatch", "data": DISPATCH_SSE_DATA}) + "\n")
        f.write(json.dumps({"topic": "nav", "data": {
            "open_beads": 3, "running_agents": 1, "approved_waiting": 1,
        }}) + "\n")

    # Give Alpine + HTTP fallback + SSE events time to propagate
    time.sleep(1.5)
    yield sweep_server
    subprocess.run(["agent-browser", "close"], capture_output=True, timeout=5)


# ── Helpers ───────────────────────────────────────────────────────────

def _ab_eval_batch(js: str) -> dict | list | str | None:
    """Single agent-browser --json eval call, returns parsed result.

    The JS expression is wrapped in an IIFE to avoid const redeclaration
    across multiple eval calls sharing the same page context.
    """
    wrapped = f"(() => {{ {js} }})()"
    result = subprocess.run(
        ["agent-browser", "--json", "eval", wrapped],
        capture_output=True, text=True, timeout=10,
    )
    stdout = result.stdout.strip()
    if not stdout:
        return None
    # Parse last JSON line that has success+data shape
    for line in reversed(stdout.split("\n")):
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and "data" in parsed:
                data = parsed["data"]
                # Unwrap {origin, result} from eval
                if isinstance(data, dict) and "result" in data:
                    return data["result"]
                return data
            return parsed
        except json.JSONDecodeError:
            continue
    return None


def _navigate_and_check(path: str, js_checks: str, wait_ms: int = 800) -> dict:
    """SPA-navigate to a page, wait, run one batched JS eval, return dict.

    Args:
        path: URL path to navigate to (e.g., "/sessions")
        js_checks: JS code that populates a `r` object with check results
                   and ends with `return r;`
        wait_ms: milliseconds to wait after navigation for Alpine to render
    """
    # Navigate via JS (SPA-style)
    nav_js = f"navigateTo('{path}')"
    subprocess.run(
        ["agent-browser", "eval", nav_js],
        capture_output=True, timeout=10,
    )
    time.sleep(wait_ms / 1000)

    # Run all checks in one eval
    full_js = f"var r = {{}}; {js_checks} return r;"
    return _ab_eval_batch(full_js) or {}


# ── Sessions page JS check bundle ────────────────────────────────────

SESSIONS_PAGE_CHECKS = """
    // Cards exist
    var cards = document.querySelectorAll('[data-testid="session-card"]');
    r.card_count = cards.length;
    r.has_cards = cards.length > 0;

    // Labels visible — collect all non-empty card titles
    var labels = [];
    cards.forEach(function(c) {
        var title = c.querySelector('.sc-title');
        if (title && title.textContent.trim()) labels.push(title.textContent.trim());
    });
    r.labels = labels;
    r.labels_visible = labels.length > 0;

    // Host badge — at least one card should show "Host" role badge
    var hostBadges = document.querySelectorAll('[data-testid="session-role"]');
    var hasHostBadge = false;
    hostBadges.forEach(function(b) {
        if (b.textContent.trim() === 'Host') hasHostBadge = true;
    });
    r.has_host_badge = hasHostBadge;

    // Roles visible — collect all visible role badges
    var roles = [];
    hostBadges.forEach(function(b) {
        var text = b.textContent.trim();
        if (text && b.offsetParent !== null) roles.push(text);
    });
    r.roles = roles;
    r.roles_visible = roles.length > 0;

    // Host card has distinct styling
    var hostCards = document.querySelectorAll('.session-card-host');
    r.host_card_count = hostCards.length;

    // Container cards
    var containerCards = document.querySelectorAll('.session-card-container');
    r.container_card_count = containerCards.length;

    // Turn counts visible (T3 stats)
    var turnVals = [];
    cards.forEach(function(c) {
        var vals = c.querySelectorAll('.sc-t3-val');
        if (vals.length >= 1) turnVals.push(vals[0].textContent.trim());
    });
    r.turn_values = turnVals;
    r.turns_visible = turnVals.filter(function(v) { return v.length > 0; }).length > 0;

    // Context token values visible
    var ctxVals = [];
    cards.forEach(function(c) {
        var vals = c.querySelectorAll('.sc-t3-val');
        if (vals.length >= 2) ctxVals.push(vals[1].textContent.trim());
    });
    r.ctx_values = ctxVals;
    r.ctx_visible = ctxVals.filter(function(v) { return v.length > 0; }).length > 0;

    // Topics visible
    var topicItems = document.querySelectorAll('.sc-topic-item');
    var topicTexts = [];
    topicItems.forEach(function(t) {
        if (t.textContent.trim()) topicTexts.push(t.textContent.trim());
    });
    r.topic_texts = topicTexts;
    r.topics_visible = topicTexts.length > 0;

    // Session IDs in data attributes (for click targeting)
    var sessionIds = [];
    cards.forEach(function(c) {
        var sid = c.getAttribute('data-session-id');
        if (sid) sessionIds.push(sid);
    });
    r.session_ids = sessionIds;

    // Recent sessions section
    var recentRows = document.querySelectorAll('[data-testid="recent-session-row"]');
    r.recent_count = recentRows.length;
    r.has_recent = recentRows.length > 0;

    // Recent session titles
    var recentTitles = [];
    recentRows.forEach(function(row) {
        var spans = row.querySelectorAll('span');
        spans.forEach(function(s) {
            var text = s.textContent.trim();
            if (text.length > 15) recentTitles.push(text);
        });
    });
    r.recent_titles = recentTitles;

    // No raw template syntax visible
    var bodyText = document.body.innerText;
    r.no_jinja = bodyText.indexOf('{{') === -1 && bodyText.indexOf('{%') === -1;
    r.no_alpine_raw = bodyText.indexOf('x-text=') === -1 && bodyText.indexOf('x-show=') === -1;

    // Active sessions section exists
    r.has_active_section = !!document.querySelector('[data-testid="active-sessions-section"]');
    r.has_recent_section = !!document.querySelector('[data-testid="recent-sessions-section"]');
"""

# ── Dispatch page JS check bundle ────────────────────────────────────

DISPATCH_PAGE_CHECKS = """
    var bodyText = document.body.innerText;

    // Section headings visible
    var h2s = document.querySelectorAll('h2');
    var headings = [];
    h2s.forEach(function(h) { headings.push(h.textContent.trim()); });
    r.headings = headings;
    r.has_active_heading = headings.some(function(t) { return t.indexOf('Active') !== -1; });
    r.has_waiting_heading = headings.some(function(t) { return t.indexOf('Waiting') !== -1; });
    r.has_blocked_heading = headings.some(function(t) { return t.indexOf('Blocked') !== -1; });

    // Bead titles from fixture visible in active/waiting/blocked sections
    r.has_alpha_title = bodyText.indexOf('Sweep alpha') !== -1;
    r.has_gamma_title = bodyText.indexOf('Sweep gamma') !== -1;
    r.has_beta_title = bodyText.indexOf('Sweep beta') !== -1;

    // Pause toggle buttons visible (rendered from paused object keys)
    var allBtns = document.querySelectorAll('button');
    var pauseLabels = [];
    allBtns.forEach(function(b) {
        var t = b.textContent.trim();
        if (t === 'dispatch' || t === 'merge') pauseLabels.push(t);
    });
    r.pause_labels = pauseLabels;
    r.has_pause_controls = pauseLabels.length >= 2;

    // Status indicators — running count visible
    r.has_running_stat = bodyText.indexOf('running:') !== -1;

    // No template artifacts
    r.no_jinja = bodyText.indexOf('{{') === -1 && bodyText.indexOf('{%') === -1;
"""

# ── Beads page JS check bundle ───────────────────────────────────────

BEADS_PAGE_CHECKS = """
    var bodyText = document.body.innerText;

    // View switcher tabs visible
    var tabs = document.querySelectorAll('[role="tab"]');
    var tabLabels = [];
    tabs.forEach(function(t) { tabLabels.push(t.textContent.trim()); });
    r.tab_labels = tabLabels;
    r.has_list_tab = tabLabels.some(function(t) { return t.indexOf('List') !== -1; });
    r.has_board_tab = tabLabels.some(function(t) { return t.indexOf('Board') !== -1; });
    r.has_tree_tab = tabLabels.some(function(t) { return t.indexOf('Tree') !== -1; });
    r.has_deps_tab = tabLabels.some(function(t) { return t.indexOf('Deps') !== -1; });

    // Table rows visible (list view is default)
    var rows = document.querySelectorAll('.bead-table-row');
    r.row_count = rows.length;
    r.has_rows = rows.length > 0;

    // Bead titles visible
    r.has_alpha_bead = bodyText.indexOf('Sweep alpha task') !== -1;
    r.has_beta_bead = bodyText.indexOf('Sweep beta bug') !== -1;

    // Priority badges visible
    r.has_p0 = bodyText.indexOf('P0') !== -1;
    r.has_p1 = bodyText.indexOf('P1') !== -1;
    r.has_p2 = bodyText.indexOf('P2') !== -1;

    // Bead IDs visible in table
    r.has_bead_ids = bodyText.indexOf('auto-sweep-b1') !== -1;

    // Filter controls — priority chips
    var filterBtns = document.querySelectorAll('button[aria-pressed]');
    r.filter_count = filterBtns.length;
    r.has_filters = filterBtns.length >= 5;  // at least P0-P4

    // Column headers
    var ths = document.querySelectorAll('.bead-th');
    var colHeaders = [];
    ths.forEach(function(th) {
        var t = th.textContent.trim();
        if (t) colHeaders.push(t);
    });
    r.col_headers = colHeaders;
    r.has_title_col = colHeaders.some(function(h) { return h.indexOf('Title') !== -1; });
    r.has_pri_col = colHeaders.some(function(h) { return h.indexOf('Pri') !== -1; });

    // No template artifacts
    r.no_jinja = bodyText.indexOf('{{') === -1 && bodyText.indexOf('{%') === -1;
"""

# ── Timeline page JS check bundle ────────────────────────────────────

TIMELINE_PAGE_CHECKS = """
    var bodyText = document.body.innerText;

    // Timeframe toggle visible (1D, 1W, 1M, All buttons)
    var rangeBtns = [];
    document.querySelectorAll('button').forEach(function(b) {
        var t = b.textContent.trim();
        if (['1D', '1W', '1M', 'All'].indexOf(t) !== -1) rangeBtns.push(t);
    });
    r.range_buttons = rangeBtns;
    r.has_range_toggle = rangeBtns.length === 4;

    // Stats tiles visible — check for tile labels
    r.has_completed_tile = bodyText.indexOf('Completed') !== -1;
    r.has_failed_tile = bodyText.indexOf('Failed') !== -1 || bodyText.indexOf('Blocked') !== -1;
    r.has_duration_tile = bodyText.indexOf('Avg Duration') !== -1;
    r.has_tooling_tile = bodyText.indexOf('Avg Tooling') !== -1;
    r.has_confidence_tile = bodyText.indexOf('Avg Confidence') !== -1;

    // Stats values present (from SWEEP_TIMELINE_STATS)
    r.has_completed_count = bodyText.indexOf('5') !== -1;
    r.has_success_pct = bodyText.indexOf('80%') !== -1;

    // Feed heading
    r.has_feed_heading = bodyText.indexOf('Feed') !== -1;

    // Feed entries visible (timeline cards)
    var tlCards = document.querySelectorAll('.tl-card');
    r.feed_count = tlCards.length;
    r.has_feed_entries = tlCards.length > 0;

    // Status dots visible
    var dots = document.querySelectorAll('.tl-dot');
    r.dot_count = dots.length;
    r.has_status_dots = dots.length > 0;

    // Duration/time visible
    r.has_duration_text = bodyText.indexOf('5m') !== -1 || bodyText.indexOf('2m') !== -1;

    // Stars visible (avg scores from stats tiles)
    var stars = document.querySelectorAll('.tl-star-on, .tl-star-off');
    r.star_count = stars.length;
    r.has_stars = stars.length > 0;

    // No template artifacts
    r.no_jinja = bodyText.indexOf('{{') === -1 && bodyText.indexOf('{%') === -1;
"""

# ── Collab page JS check bundle ──────────────────────────────────────

COLLAB_PAGE_CHECKS = """
    var bodyText = document.body.innerText;

    // Tab strip visible (Recent, Thoughts, Threads, Topics)
    var tabs = document.querySelectorAll('.collab-tab');
    var tabLabels = [];
    tabs.forEach(function(t) { tabLabels.push(t.textContent.trim()); });
    r.tab_labels = tabLabels;
    r.has_recent_tab = tabLabels.some(function(t) { return t.indexOf('Recent') !== -1; });
    r.has_thoughts_tab = tabLabels.some(function(t) { return t.indexOf('Thoughts') !== -1; });
    r.has_threads_tab = tabLabels.some(function(t) { return t.indexOf('Threads') !== -1; });
    r.has_topics_tab = tabLabels.some(function(t) { return t.indexOf('Topics') !== -1; });
    r.tab_count = tabs.length;

    // Recent tab content — note cards visible (Recent is default tab)
    var noteCards = document.querySelectorAll('.note-card');
    r.note_count = noteCards.length;
    r.has_notes = noteCards.length > 0;

    // Note titles visible
    var noteTitles = [];
    document.querySelectorAll('.note-title').forEach(function(el) {
        var t = el.textContent.trim();
        if (t) noteTitles.push(t);
    });
    r.note_titles = noteTitles;
    r.has_note_titles = noteTitles.length > 0;

    // Note type badges visible
    var typeLabels = [];
    document.querySelectorAll('.note-type').forEach(function(el) {
        var t = el.textContent.trim();
        if (t) typeLabels.push(t);
    });
    r.type_labels = typeLabels;
    r.has_type_labels = typeLabels.length > 0;

    // Tags visible
    var tagEls = document.querySelectorAll('.note-tag');
    var tagTexts = [];
    tagEls.forEach(function(el) {
        var t = el.textContent.trim();
        if (t) tagTexts.push(t);
    });
    r.tags = tagTexts;
    r.has_tags = tagTexts.length > 0;

    // Tab counts visible (monospace count after tab label)
    var tabCounts = document.querySelectorAll('.collab-tab-count');
    var countTexts = [];
    tabCounts.forEach(function(el) {
        var t = el.textContent.trim();
        if (t) countTexts.push(t);
    });
    r.tab_counts = countTexts;
    r.has_tab_counts = countTexts.length > 0;

    // Thought capture input present (even though thoughts tab is not active)
    // We check it exists in the DOM (it's rendered but hidden via x-show)
    var thoughtInput = document.querySelector('.thought-input');
    r.has_thought_input = !!thoughtInput;

    // No template artifacts
    r.no_jinja = bodyText.indexOf('{{') === -1 && bodyText.indexOf('{%') === -1;
"""

# ── Streams page JS check bundle ─────────────────────────────────────

STREAMS_PAGE_CHECKS = """
    var bodyText = document.body.innerText;

    // Page heading
    r.has_heading = bodyText.indexOf('Streams') !== -1;

    // Stream tag entries visible
    r.has_pitfall = bodyText.indexOf('#pitfall') !== -1;
    r.has_architecture = bodyText.indexOf('#architecture') !== -1;
    r.has_testing = bodyText.indexOf('#testing') !== -1;

    // Counts visible ("N notes")
    r.has_12_notes = bodyText.indexOf('12 notes') !== -1;
    r.has_8_notes = bodyText.indexOf('8 notes') !== -1;

    // Descriptions visible
    r.has_description = bodyText.indexOf('Operational hazards') !== -1
                     || bodyText.indexOf('Design decisions') !== -1;

    // Links to stream detail pages
    var links = document.querySelectorAll('a[href*="/stream/"]');
    r.link_count = links.length;
    r.has_stream_links = links.length >= 3;

    // No template artifacts
    r.no_jinja = bodyText.indexOf('{{') === -1 && bodyText.indexOf('{%') === -1;
"""

# ── Bead detail page JS check bundle ─────────────────────────────────

BEAD_DETAIL_CHECKS = """
    var bodyText = document.body.innerText;

    // Bead title visible
    r.has_title = bodyText.indexOf('Sweep alpha task') !== -1;

    // Bead ID visible
    r.has_bead_id = bodyText.indexOf('auto-sweep-b1') !== -1;

    // Priority badge visible
    r.has_priority = bodyText.indexOf('P1') !== -1;

    // Status badge visible
    r.has_status = bodyText.indexOf('open') !== -1;

    // Description section visible
    r.has_description = bodyText.indexOf('First test bead') !== -1
                     || bodyText.indexOf('behavioral sweep') !== -1;

    // Labels visible
    r.has_label = bodyText.indexOf('readiness:approved') !== -1;

    // Issue type visible
    r.has_issue_type = bodyText.indexOf('task') !== -1;

    // State is ready (not loading or error)
    var loadingText = document.querySelector('[x-show*="loading"]');
    var errorText = document.querySelector('[x-show*="error"]');
    r.no_loading = !loadingText || loadingText.offsetParent === null
                || loadingText.style.display === 'none';

    // No template artifacts
    r.no_jinja = bodyText.indexOf('{{') === -1 && bodyText.indexOf('{%') === -1;
"""

# ── Trace page JS check bundle ───────────────────────────────────────

TRACE_PAGE_CHECKS = """
    var bodyText = document.body.innerText;

    // Header with bead title (scoped to trace section, not global header)
    var traceSection = document.querySelector('[aria-label="Trace"]');
    var h1 = traceSection ? traceSection.querySelector('h1') : null;
    r.title_text = h1 ? h1.textContent.trim() : '';
    r.has_title = bodyText.indexOf('Sweep alpha') !== -1;

    // Bead ID link
    r.has_bead_link = bodyText.indexOf('auto-sweep-b1') !== -1;

    // Back link to dispatch
    var backLink = document.querySelector('a[href="/dispatch"]');
    r.has_back_link = !!backLink;

    // Decision section visible
    r.has_decision_heading = bodyText.indexOf('Decision') !== -1;
    r.has_status = bodyText.indexOf('DONE') !== -1;
    r.has_reason = bodyText.indexOf('All tests pass') !== -1;

    // Scores visible (stars)
    var stars = document.querySelectorAll('.tl-star-on, .tl-star-off');
    r.star_count = stars.length;
    r.has_stars = stars.length > 0;

    // Score labels
    r.has_tooling_label = bodyText.indexOf('Tooling') !== -1;
    r.has_clarity_label = bodyText.indexOf('Clarity') !== -1;
    r.has_confidence_label = bodyText.indexOf('Confidence') !== -1;

    // Duration visible
    r.has_duration = bodyText.indexOf('5m') !== -1;

    // Diff stats visible
    r.has_diff_add = bodyText.indexOf('+45') !== -1;
    r.has_diff_del = bodyText.indexOf('-12') !== -1;

    // Commit hash visible
    r.has_commit = bodyText.indexOf('abc123def4') !== -1;

    // No template artifacts
    r.no_jinja = bodyText.indexOf('{{') === -1 && bodyText.indexOf('{%') === -1;
"""


# ── Tests ─────────────────────────────────────────────────────────────

class TestSessionsPageBehavior:
    """Sessions page behavioral sweep — one JS eval, many assertions.

    All checks run in a single agent-browser eval call. The `checks` fixture
    caches the result dict so each test method reads from the same snapshot.
    """

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        """Run the sessions page check bundle once, cache for all test methods."""
        result = _navigate_and_check("/sessions", SESSIONS_PAGE_CHECKS, wait_ms=1500)
        request.cls._checks = result

    def test_cards_exist(self):
        """User sees session cards on the page."""
        c = self._checks
        assert c.get("has_cards"), f"No session cards found (count={c.get('card_count')})"
        assert c["card_count"] == 5, f"Expected 5 session cards, got {c['card_count']}"

    def test_labels_visible(self):
        """User sees session labels (titles) on each card."""
        c = self._checks
        assert c.get("labels_visible"), "No session labels visible"
        labels = c.get("labels", [])
        assert len(labels) >= 4, f"Expected at least 4 labels, got {len(labels)}: {labels}"
        # Check specific labels are present
        label_text = " ".join(labels)
        assert "Alpha" in label_text, f"'Alpha' label not found in {labels}"
        assert "Beta" in label_text, f"'Beta' label not found in {labels}"
        assert "Host" in label_text, f"'Host' label not found in {labels}"

    def test_host_badge(self):
        """User sees a 'Host' role badge on the host session card."""
        c = self._checks
        assert c.get("has_host_badge"), "No 'Host' role badge visible"
        assert c.get("host_card_count", 0) >= 1, "No cards with host styling"

    def test_roles_visible(self):
        """User sees role badges (Designer, Builder, Reviewer, etc.) on cards."""
        c = self._checks
        assert c.get("roles_visible"), "No role badges visible"
        roles = c.get("roles", [])
        assert len(roles) >= 3, f"Expected at least 3 role badges, got {len(roles)}: {roles}"
        role_text = " ".join(roles)
        assert "Designer" in role_text, f"'Designer' not in roles: {roles}"
        assert "Builder" in role_text, f"'Builder' not in roles: {roles}"

    def test_turn_counts_visible(self):
        """User sees turn counts (entry count) in the T3 stats row."""
        c = self._checks
        assert c.get("turns_visible"), "No turn counts visible"
        turns = c.get("turn_values", [])
        # Check at least one meaningful value (our fixtures have 50-300)
        numeric = [t for t in turns if t.isdigit() and int(t) > 0]
        assert len(numeric) >= 1, f"No numeric turn values: {turns}"

    def test_context_tokens_visible(self):
        """User sees context token counts (e.g. '80K', '120K') in T3 stats."""
        c = self._checks
        assert c.get("ctx_visible"), "No context token values visible"
        ctx = c.get("ctx_values", [])
        has_k = any("K" in v for v in ctx)
        assert has_k, f"No 'K' formatted token values: {ctx}"

    def test_topics_visible(self):
        """User sees topic lines on cards that have topics."""
        c = self._checks
        assert c.get("topics_visible"), "No topic items visible"
        topics = c.get("topic_texts", [])
        topic_text = " ".join(topics)
        assert "CSS grid" in topic_text or "session cards" in topic_text.lower() or "Redesigning" in topic_text, \
            f"Expected session card topics, got: {topics}"

    def test_recent_sessions(self):
        """User sees the Recent Sessions section with history entries."""
        c = self._checks
        assert c.get("has_recent"), f"No recent sessions visible (count={c.get('recent_count')})"
        assert c["recent_count"] == 3, f"Expected 3 recent sessions, got {c['recent_count']}"

    def test_no_template_artifacts(self):
        """No raw Jinja or Alpine template syntax visible to the user."""
        c = self._checks
        assert c.get("no_jinja"), "Raw Jinja template syntax ({{ or {%) visible on page"
        assert c.get("no_alpine_raw"), "Raw Alpine directive text (x-text=, x-show=) visible"

    def test_page_structure(self):
        """Page has both Active Sessions and Recent Sessions sections."""
        c = self._checks
        assert c.get("has_active_section"), "Missing [data-testid='active-sessions-section']"
        assert c.get("has_recent_section"), "Missing [data-testid='recent-sessions-section']"

    def test_session_ids_in_dom(self):
        """Session cards have data-session-id attributes for click targeting."""
        c = self._checks
        ids = c.get("session_ids", [])
        assert len(ids) == 5, f"Expected 5 session IDs in DOM, got {len(ids)}: {ids}"
        assert "auto-sweep-alpha" in ids, f"alpha session ID not in DOM: {ids}"
        assert "host-sweep-delta" in ids, f"host session ID not in DOM: {ids}"

    def test_container_vs_host_cards(self):
        """Cards are styled differently for host vs container sessions."""
        c = self._checks
        assert c.get("host_card_count", 0) == 1, f"Expected 1 host card, got {c.get('host_card_count')}"
        assert c.get("container_card_count", 0) == 4, f"Expected 4 container cards, got {c.get('container_card_count')}"


class TestDispatchPageBehavior:
    """Dispatch page behavioral sweep — section headings, bead cards, pause controls."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check("/dispatch", DISPATCH_PAGE_CHECKS, wait_ms=1000)
        request.cls._checks = result

    def test_section_headings(self):
        """User sees Active, Waiting, and Blocked section headings."""
        c = self._checks
        assert c.get("has_active_heading"), f"No 'Active' heading, got: {c.get('headings')}"
        assert c.get("has_waiting_heading"), f"No 'Waiting' heading, got: {c.get('headings')}"
        assert c.get("has_blocked_heading"), f"No 'Blocked' heading, got: {c.get('headings')}"

    def test_bead_titles_visible(self):
        """User sees bead titles from fixture data in the dispatch sections."""
        c = self._checks
        assert c.get("has_alpha_title"), "Alpha bead title not visible"
        assert c.get("has_gamma_title"), "Gamma bead title not visible"
        assert c.get("has_beta_title"), "Beta bead title not visible"

    def test_pause_controls(self):
        """User sees dispatch/merge pause toggle buttons."""
        c = self._checks
        assert c.get("has_pause_controls"), f"Pause controls missing, found: {c.get('pause_labels')}"

    def test_running_stat(self):
        """User sees 'running: N' stat indicator."""
        c = self._checks
        assert c.get("has_running_stat"), "No 'running:' stat visible"

    def test_no_template_artifacts(self):
        """No raw Jinja template syntax visible."""
        c = self._checks
        assert c.get("no_jinja"), "Raw Jinja template syntax visible on dispatch page"


class TestBeadsPageBehavior:
    """Beads page behavioral sweep — view tabs, table rows, filters."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check("/beads", BEADS_PAGE_CHECKS, wait_ms=1000)
        request.cls._checks = result

    def test_view_tabs(self):
        """User sees List, Board, Tree, and Deps view tabs."""
        c = self._checks
        assert c.get("has_list_tab"), f"No 'List' tab, got: {c.get('tab_labels')}"
        assert c.get("has_board_tab"), f"No 'Board' tab, got: {c.get('tab_labels')}"
        assert c.get("has_tree_tab"), f"No 'Tree' tab, got: {c.get('tab_labels')}"
        assert c.get("has_deps_tab"), f"No 'Deps' tab, got: {c.get('tab_labels')}"

    def test_bead_rows(self):
        """User sees bead rows in the list view."""
        c = self._checks
        assert c.get("has_rows"), f"No bead rows visible (count={c.get('row_count')})"
        assert c["row_count"] >= 3, f"Expected at least 3 rows, got {c['row_count']}"

    def test_bead_titles(self):
        """User sees bead titles from fixture data."""
        c = self._checks
        assert c.get("has_alpha_bead"), "Alpha bead title not visible"
        assert c.get("has_beta_bead"), "Beta bead title not visible"

    def test_priority_badges(self):
        """User sees priority badges (P0, P1, P2)."""
        c = self._checks
        assert c.get("has_p1"), "P1 badge not visible"
        assert c.get("has_p2"), "P2 badge not visible"

    def test_bead_ids_visible(self):
        """User sees bead IDs in the table."""
        c = self._checks
        assert c.get("has_bead_ids"), "Bead IDs not visible in table"

    def test_filter_controls(self):
        """User sees filter controls (priority chips, phase chips)."""
        c = self._checks
        assert c.get("has_filters"), f"Not enough filter controls (count={c.get('filter_count')})"

    def test_column_headers(self):
        """User sees table column headers (Title, Pri, etc.)."""
        c = self._checks
        assert c.get("has_title_col"), f"No 'Title' column header, got: {c.get('col_headers')}"
        assert c.get("has_pri_col"), f"No 'Pri' column header, got: {c.get('col_headers')}"

    def test_no_template_artifacts(self):
        """No raw Jinja template syntax visible."""
        c = self._checks
        assert c.get("no_jinja"), "Raw Jinja template syntax visible on beads page"


class TestTimelinePageBehavior:
    """Timeline page behavioral sweep — stats tiles, feed entries, status indicators."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check("/timeline", TIMELINE_PAGE_CHECKS, wait_ms=1000)
        request.cls._checks = result

    def test_range_toggle(self):
        """User sees timeframe toggle buttons (1D, 1W, 1M, All)."""
        c = self._checks
        assert c.get("has_range_toggle"), f"Missing range toggle, found: {c.get('range_buttons')}"

    def test_stats_tiles(self):
        """User sees stats tiles (Completed, Failed/Blocked, Avg Duration, etc.)."""
        c = self._checks
        assert c.get("has_completed_tile"), "No 'Completed' tile visible"
        assert c.get("has_duration_tile"), "No 'Avg Duration' tile visible"
        assert c.get("has_tooling_tile"), "No 'Avg Tooling' tile visible"
        assert c.get("has_confidence_tile"), "No 'Avg Confidence' tile visible"

    def test_stats_values(self):
        """User sees actual stats values from fixture data."""
        c = self._checks
        assert c.get("has_completed_count"), "Completed count '5' not visible"
        assert c.get("has_success_pct"), "Success rate '80%' not visible"

    def test_feed_entries(self):
        """User sees feed entries (timeline cards)."""
        c = self._checks
        assert c.get("has_feed_entries"), f"No feed entries visible (count={c.get('feed_count')})"

    def test_status_indicators(self):
        """User sees status dots on timeline entries."""
        c = self._checks
        assert c.get("has_status_dots"), f"No status dots visible (count={c.get('dot_count')})"

    def test_stars_visible(self):
        """User sees star ratings in stats tiles."""
        c = self._checks
        assert c.get("has_stars"), f"No stars visible (count={c.get('star_count')})"

    def test_no_template_artifacts(self):
        """No raw Jinja template syntax visible."""
        c = self._checks
        assert c.get("no_jinja"), "Raw Jinja template syntax visible on timeline page"


class TestCollabPageBehavior:
    """Collab page behavioral sweep — tabs, notes, thought input."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check("/collab", COLLAB_PAGE_CHECKS, wait_ms=1000)
        request.cls._checks = result

    def test_tab_strip(self):
        """User sees tab strip with Recent, Thoughts, Threads, Topics."""
        c = self._checks
        assert c.get("has_recent_tab"), f"No 'Recent' tab, got: {c.get('tab_labels')}"
        assert c.get("has_thoughts_tab"), f"No 'Thoughts' tab, got: {c.get('tab_labels')}"
        assert c.get("has_threads_tab"), f"No 'Threads' tab, got: {c.get('tab_labels')}"
        assert c.get("has_topics_tab"), f"No 'Topics' tab, got: {c.get('tab_labels')}"
        assert c.get("tab_count") == 4, f"Expected 4 tabs, got {c.get('tab_count')}"

    def test_recent_notes(self):
        """User sees recent note cards in the default tab."""
        c = self._checks
        assert c.get("has_notes"), f"No note cards visible (count={c.get('note_count')})"
        assert c.get("note_count") >= 2, f"Expected at least 2 notes, got {c.get('note_count')}"

    def test_note_titles(self):
        """User sees note titles from fixture data."""
        c = self._checks
        assert c.get("has_note_titles"), f"No note titles visible"
        titles = c.get("note_titles", [])
        title_text = " ".join(titles)
        assert "Architecture" in title_text or "Testing" in title_text, \
            f"Expected fixture note titles, got: {titles}"

    def test_type_labels(self):
        """User sees note type labels (NOTE, THOUGHT, etc.)."""
        c = self._checks
        assert c.get("has_type_labels"), f"No type labels visible"

    def test_tags_visible(self):
        """User sees tag chips on note cards."""
        c = self._checks
        assert c.get("has_tags"), f"No tags visible"
        tags = c.get("tags", [])
        assert any("architecture" in t or "testing" in t or "auth" in t for t in tags), \
            f"Expected fixture tags, got: {tags}"

    def test_tab_counts(self):
        """User sees counts next to tab labels."""
        c = self._checks
        assert c.get("has_tab_counts"), f"No tab counts visible, got: {c.get('tab_counts')}"

    def test_thought_input(self):
        """Thought capture input exists in DOM (rendered for Thoughts tab)."""
        c = self._checks
        assert c.get("has_thought_input"), "No thought capture input found in DOM"

    def test_no_template_artifacts(self):
        """No raw Jinja template syntax visible."""
        c = self._checks
        assert c.get("no_jinja"), "Raw Jinja template syntax visible on collab page"


class TestStreamsPageBehavior:
    """Streams page behavioral sweep — heading, stream entries, counts."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check("/streams", STREAMS_PAGE_CHECKS, wait_ms=1000)
        request.cls._checks = result

    def test_heading(self):
        """User sees 'Streams' heading."""
        c = self._checks
        assert c.get("has_heading"), "No 'Streams' heading visible"

    def test_stream_tags(self):
        """User sees stream tag names (#pitfall, #architecture, #testing)."""
        c = self._checks
        assert c.get("has_pitfall"), "No '#pitfall' stream visible"
        assert c.get("has_architecture"), "No '#architecture' stream visible"
        assert c.get("has_testing"), "No '#testing' stream visible"

    def test_counts(self):
        """User sees note counts for each stream."""
        c = self._checks
        assert c.get("has_12_notes"), "No '12 notes' count visible for #pitfall"
        assert c.get("has_8_notes"), "No '8 notes' count visible for #architecture"

    def test_descriptions(self):
        """User sees descriptions for streams."""
        c = self._checks
        assert c.get("has_description"), "No stream descriptions visible"

    def test_links(self):
        """User sees links to stream detail pages."""
        c = self._checks
        assert c.get("has_stream_links"), f"Not enough stream links (count={c.get('link_count')})"

    def test_no_template_artifacts(self):
        """No raw Jinja template syntax visible."""
        c = self._checks
        assert c.get("no_jinja"), "Raw Jinja template syntax visible on streams page"


class TestBeadDetailPageBehavior:
    """Bead detail page behavioral sweep — title, priority, status, description."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check("/bead/auto-sweep-b1", BEAD_DETAIL_CHECKS, wait_ms=1000)
        request.cls._checks = result

    def test_title(self):
        """User sees the bead title."""
        c = self._checks
        assert c.get("has_title"), "Bead title 'Sweep alpha task' not visible"

    def test_bead_id(self):
        """User sees the bead ID."""
        c = self._checks
        assert c.get("has_bead_id"), "Bead ID 'auto-sweep-b1' not visible"

    def test_priority_badge(self):
        """User sees priority badge (P1)."""
        c = self._checks
        assert c.get("has_priority"), "Priority badge P1 not visible"

    def test_status_badge(self):
        """User sees status badge (open)."""
        c = self._checks
        assert c.get("has_status"), "Status badge 'open' not visible"

    def test_description(self):
        """User sees the bead description text."""
        c = self._checks
        assert c.get("has_description"), "Bead description not visible"

    def test_labels(self):
        """User sees the bead labels."""
        c = self._checks
        assert c.get("has_label"), "Label 'readiness:approved' not visible"

    def test_no_template_artifacts(self):
        """No raw Jinja template syntax visible."""
        c = self._checks
        assert c.get("no_jinja"), "Raw Jinja template syntax visible on bead detail page"


class TestTracePageBehavior:
    """Trace page behavioral sweep — header, decision, scores."""

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        result = _navigate_and_check(
            "/dispatch/trace/run-sweep-001", TRACE_PAGE_CHECKS, wait_ms=1000,
        )
        request.cls._checks = result

    def test_title(self):
        """User sees the bead title in the trace header."""
        c = self._checks
        assert c.get("has_title"), f"Bead title not visible, h1 text: '{c.get('title_text')}'"

    def test_bead_link(self):
        """User sees a link to the bead detail page."""
        c = self._checks
        assert c.get("has_bead_link"), "Bead ID link not visible"

    def test_back_link(self):
        """User sees a back link to dispatch page."""
        c = self._checks
        assert c.get("has_back_link"), "Back link to /dispatch not found"

    def test_decision_section(self):
        """User sees the Decision section with status and reason."""
        c = self._checks
        assert c.get("has_decision_heading"), "Decision heading not visible"
        assert c.get("has_status"), "Status 'DONE' not visible"
        assert c.get("has_reason"), "Reason 'All tests pass' not visible"

    def test_scores(self):
        """User sees star ratings for tooling, clarity, confidence."""
        c = self._checks
        assert c.get("has_stars"), f"No stars visible (count={c.get('star_count')})"
        assert c.get("has_tooling_label"), "Tooling label not visible"
        assert c.get("has_clarity_label"), "Clarity label not visible"
        assert c.get("has_confidence_label"), "Confidence label not visible"

    def test_diff_stats(self):
        """User sees diff stats (+added -removed)."""
        c = self._checks
        assert c.get("has_diff_add"), "+45 not visible"
        assert c.get("has_diff_del"), "-12 not visible"

    def test_no_template_artifacts(self):
        """No raw Jinja template syntax visible."""
        c = self._checks
        assert c.get("no_jinja"), "Raw Jinja template syntax visible on trace page"


# ── Trace overlay JS check bundle (Bug #1: raw Jinja in overlay) ─────

TRACE_OVERLAY_CHECKS = """
    // Trace page loaded — look for title inside #content (not site header)
    var content = document.getElementById('content');
    var titleEl = content ? content.querySelector('h1') : null;
    r.has_title = !!(titleEl && titleEl.textContent.trim().length > 0);
    r.title_text = titleEl ? titleEl.textContent.trim() : '';

    // Decision section rendered
    var decisionSection = content ? content.querySelector('section[aria-label="Decision"]') : null;
    r.has_decision = !!decisionSection;

    // Trace section exists (Alpine component rendered the fragment)
    var traceSection = content ? content.querySelector('section[aria-label="Trace"]') : null;
    r.trace_section_exists = !!traceSection;

    // No raw Jinja syntax in the live panel overlay body.
    // base.html serves the overlay panel via _load_template() which does NOT
    // render Jinja. The {% include "partials/session-entries.html" %} appears
    // as literal text in the DOM instead of the rendered partial content.
    var panelBody = document.getElementById('live-panel-body');
    r.panel_body_exists = !!panelBody;
    if (panelBody) {
        r.overlay_has_raw_jinja = panelBody.innerHTML.indexOf('{%') >= 0;
        r.overlay_has_include = panelBody.innerHTML.indexOf('include') >= 0
            && panelBody.innerHTML.indexOf('{%') >= 0;
    } else {
        r.overlay_has_raw_jinja = false;
        r.overlay_has_include = false;
    }

    // Check visible page text for raw Jinja (only catches visible content)
    var bodyText = document.body.innerText;
    r.no_jinja_visible = bodyText.indexOf('{%') === -1 && bodyText.indexOf('{{') === -1;
"""


class TestTraceOverlayBehavior:
    """Trace page overlay behavioral sweep — verifies overlay panel
    does not contain raw Jinja template syntax.

    Bug: base.html is served via _load_template() (raw file read) instead of
    Jinja rendering. The {% include "partials/session-entries.html" %} directive
    in the overlay panel body appears as literal text, visible when the panel
    opens for live or completed dispatch runs.
    """

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        """Navigate to trace page, run check bundle, cache results."""
        result = _navigate_and_check(
            "/dispatch/trace/auto-sweep-b2-20260327-120000",
            TRACE_OVERLAY_CHECKS,
            wait_ms=3000,
        )
        request.cls._checks = result

    def test_trace_section_exists(self):
        """Trace page fragment was loaded and Alpine component initialised."""
        c = self._checks
        assert c.get("trace_section_exists"), \
            "Trace section[aria-label='Trace'] not found — fragment may not have loaded"

    def test_trace_loads(self):
        """Trace page renders the bead title after fetching trace data."""
        c = self._checks
        assert c.get("has_title"), \
            f"Trace page title not rendered in #content (title='{c.get('title_text', '')}')"

    def test_decision_visible(self):
        """Decision section is rendered with status and scores."""
        c = self._checks
        assert c.get("has_decision"), \
            "User should see the Decision section with status, reason, and scores"

    def test_no_raw_jinja_in_overlay(self):
        """Overlay session panel must not contain raw Jinja template directives.

        The user should see rendered session entries, not literal
        {% include "partials/session-entries.html" %} text.
        """
        c = self._checks
        assert c.get("panel_body_exists"), "Overlay panel body (#live-panel-body) missing from DOM"
        assert not c.get("overlay_has_raw_jinja"), \
            ("Overlay panel HTML contains raw Jinja syntax ({%%). "
             "The {% include %} directive was not processed by the template engine. "
             "User would see literal template text instead of session entries.")


# ── Host session viewer JS check bundle ──────────────────────────

HOST_SESSION_CHECKS = """
    // Session viewer — scope to #content to avoid matching overlay panel
    var content = document.getElementById('content');
    var viewerEl = content ? content.querySelector('.session-viewer') : null;
    r.viewer_exists = !!viewerEl;

    // State machine — check if ready (.sv-ready appears when state='ready')
    var readyEl = content ? content.querySelector('.sv-ready') : null;
    r.is_ready = !!readyEl;

    // Session entries visible (in the page viewer, not the overlay)
    var entries = content ? content.querySelectorAll('.sc-entry') : [];
    r.entry_count = entries.length;
    r.has_entries = entries.length > 0;

    // Textarea / input bar exists (for sending messages to live session)
    // x-ref is not a DOM attribute; query by element type + parent class
    var textarea = content ? content.querySelector('.sv-input textarea') : null;
    r.has_textarea = !!textarea;

    // Input bar container
    var inputBar = content ? content.querySelector('.sv-input') : null;
    r.has_input_bar = !!inputBar;

    // Session header visible
    var header = content ? content.querySelector('[data-testid="session-header"]') : null;
    r.has_header = !!header;

    // No raw template syntax
    var bodyText = document.body.innerText;
    r.no_jinja = bodyText.indexOf('{%') === -1 && bodyText.indexOf('{{') === -1;
"""


class TestHostSessionInputBehavior:
    """Host session viewer behavioral sweep — verifies input bar exists for
    live host sessions.

    Bug: In production, /api/session/{project}/{id}/tail returns is_live: false
    for idle host sessions because it checks .meta.json + 120s mtime freshness
    instead of reading the DB is_live field. The session viewer then hides the
    input bar.

    In mock mode, the tail endpoint hardcodes is_live: true and omits the
    `type` field, so the textarea may appear (masking the production bug).
    The L2.A contract test below catches the missing `type` field.
    """

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        """Navigate to host session viewer, run check bundle."""
        result = _navigate_and_check(
            "/session/autonomy/host-sweep-delta",
            HOST_SESSION_CHECKS,
            wait_ms=3000,
        )
        request.cls._checks = result

    def test_session_loads(self):
        """Session viewer reaches ready state."""
        c = self._checks
        assert c.get("viewer_exists"), "Session viewer component not found in #content"
        assert c.get("is_ready"), "Session viewer did not reach 'ready' state"

    def test_entries_visible(self):
        """User sees session conversation entries."""
        c = self._checks
        assert c.get("has_entries"), \
            f"No session entries visible (count={c.get('entry_count', 0)})"

    def test_input_bar_for_live_host(self):
        """Live host session should show an input bar (Link Terminal or textarea).

        Host sessions show a 'Link Terminal' button until linked to a tmux session,
        then show a textarea. Either way, the .sv-input bar should be present.
        """
        c = self._checks
        assert c.get("has_input_bar"), \
            ("No input bar visible for live host session. "
             "User cannot interact with this session. "
             "Expected .sv-input element (Link Terminal button or textarea).")

    def test_no_template_artifacts(self):
        """No raw Jinja template syntax visible on session viewer page."""
        c = self._checks
        assert c.get("no_jinja"), "Raw Jinja template syntax visible on session viewer page"


class TestHostSessionTailContract:
    """L2.A contract test: /api/session/{project}/{id}/tail must return
    `type` and `is_live` consistent with the session registry.

    The mock tail handler currently omits the `type` field, so the session
    viewer cannot distinguish host from container sessions. This masks the
    production bug where host sessions show no input bar.
    """

    @pytest.fixture(scope="class", autouse=True)
    def tail_response(self, browser, request):
        """Fetch tail response for host session directly via HTTP.

        Depends on `browser` fixture to ensure sweep_server is running.
        """
        import urllib.request
        port = browser["port"]
        url = f"http://127.0.0.1:{port}/api/session/autonomy/host-sweep-delta/tail?after=0"
        resp = urllib.request.urlopen(url, timeout=5)
        data = json.loads(resp.read().decode())
        request.cls._tail = data

    def test_tail_returns_type(self):
        """Tail response must include `type` field for host sessions.

        The session viewer needs `type: 'host'` to show the correct UI
        (Link Terminal flow instead of direct textarea).
        """
        t = self._tail
        assert "type" in t, \
            ("Tail response missing `type` field. "
             "Session viewer cannot distinguish host from container sessions. "
             f"Response keys: {list(t.keys())}")
        assert t["type"] == "host", \
            f"Expected type='host' for host session, got type='{t.get('type')}'"

    def test_tail_returns_is_live(self):
        """Tail response must include is_live: true for a live host session."""
        t = self._tail
        assert "is_live" in t, "Tail response missing `is_live` field"
        assert t["is_live"] is True, \
            f"Expected is_live=true for live host session, got is_live={t.get('is_live')}"


# ── Experiment toolbar JS check bundle ────────────────────────────

EXPERIMENT_TOOLBAR_CHECKS = """(async () => {
  // Wait for the experiment page component to initialize (up to 8s)
  for (var i = 0; i < 80 && !window._experimentPage; i++) {
    await new Promise(r => setTimeout(r, 100));
  }
  var ep = window._experimentPage;
  if (!ep) return JSON.stringify({error: 'no _experimentPage after 8s'});
  // Wait for state=ready (API call completes, toolbar renders)
  for (var i = 0; i < 50 && ep.state !== 'ready'; i++) {
    await new Promise(r => setTimeout(r, 100));
  }
  if (ep.state !== 'ready') return JSON.stringify({error: 'state=' + ep.state});
  var q = (id) => document.querySelector('[data-testid="' + id + '"]');
  var r = {};
  var tick = async () => { await Alpine.nextTick(); await new Promise(r => setTimeout(r, 150)); };

  // DISCONNECTED: chatOpen=false, chatConnected=false (initial state)
  ep.chatOpen = false; ep.chatConnected = false;
  await tick();
  r.disc_iter = !!q('toolbar-iter-desktop');
  r.disc_no_capture = !q('toolbar-capture');
  r.disc_chat_class = q('toolbar-chat-toggle')?.classList.contains('chat-disconnected');
  r.disc_no_session = !q('toolbar-session-row');

  // PICKER: chatOpen=true, chatConnected=false
  ep.chatOpen = true;
  await tick();
  r.picker_title = q('toolbar-title')?.textContent?.includes('Select');
  r.picker_chat_class = q('toolbar-chat-toggle')?.classList.contains('chat-open');
  r.picker_no_capture = !q('toolbar-capture');
  r.picker_no_iter = !q('toolbar-iter-desktop');

  // LIVE_CHAT: chatOpen=true, chatConnected=true
  ep.chatConnected = true; ep.chatSessionLabel = 'Test session label';
  await tick();
  r.chat_no_capture = !q('toolbar-capture');
  r.chat_session = !!q('toolbar-session-row');
  r.chat_prime = !!q('toolbar-prime');
  r.chat_disconnect = !!q('toolbar-disconnect');
  r.chat_icon = q('toolbar-chat-toggle')?.classList.contains('chat-connected-shown');
  r.chat_no_iter = !q('toolbar-iter-desktop');

  // LIVE_UI: chatOpen=false, chatConnected=true
  ep.chatOpen = false;
  await tick();
  r.live_capture = !!q('toolbar-capture');
  r.live_chat_green = q('toolbar-chat-toggle')?.classList.contains('chat-connected-hidden');

  return JSON.stringify(r);
})()"""


def _navigate_and_eval_async(path: str, js_expr: str, wait_ms: int = 800) -> dict:
    """SPA-navigate to a page, wait, run an async JS expression, return parsed dict.

    Unlike _navigate_and_check (which wraps in `var r = {}; ... return r;`), this
    passes the JS expression directly — suitable for async IIFEs that return Promises.
    """
    nav_js = f"navigateTo('{path}')"
    subprocess.run(
        ["agent-browser", "eval", nav_js],
        capture_output=True, timeout=10,
    )
    time.sleep(wait_ms / 1000)

    result = subprocess.run(
        ["agent-browser", "--json", "eval", js_expr],
        capture_output=True, text=True, timeout=15,
    )
    stdout = result.stdout.strip()
    if not stdout:
        return {}
    # Parse last JSON line that has success+data shape
    for line in reversed(stdout.split("\n")):
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and "data" in parsed:
                data = parsed["data"]
                if isinstance(data, dict) and "result" in data:
                    val = data["result"]
                    # If result is a JSON string, parse it
                    if isinstance(val, str):
                        try:
                            return json.loads(val)
                        except (json.JSONDecodeError, TypeError):
                            pass
                    if isinstance(val, dict):
                        return val
                    return {}
                if isinstance(data, dict):
                    return data
            if isinstance(parsed, str):
                try:
                    return json.loads(parsed)
                except (json.JSONDecodeError, TypeError):
                    pass
        except json.JSONDecodeError:
            continue
    return {}


class TestExperimentToolbar:
    """Experiment toolbar behavioral sweep — 4-state state machine via Alpine.nextTick().

    One async batched eval cycles through all 4 states (DISCONNECTED, LIVE_UI,
    LIVE_CHAT, PICKER) using await Alpine.nextTick() between state changes.
    """

    @pytest.fixture(scope="class", autouse=True)
    def checks(self, browser, request):
        """Navigate to experiment page, wait for Alpine, run async check bundle."""
        result = _navigate_and_eval_async(
            f"/experiments/{SWEEP_EXPERIMENT_ID}",
            EXPERIMENT_TOOLBAR_CHECKS,
            wait_ms=1000,
        )
        request.cls._checks = result

    # ── DISCONNECTED state ────────────────────────────────────────

    def test_disconnected_iter_nav(self):
        """DISCONNECTED: iteration nav is visible."""
        c = self._checks
        assert c.get("disc_iter"), "Iter nav not visible in DISCONNECTED state"

    def test_disconnected_no_capture(self):
        """DISCONNECTED: capture button is hidden."""
        c = self._checks
        assert c.get("disc_no_capture"), "Capture button should be hidden in DISCONNECTED"

    def test_disconnected_chat_class(self):
        """DISCONNECTED: chat toggle has chat-disconnected class."""
        c = self._checks
        assert c.get("disc_chat_class"), "Chat toggle missing 'chat-disconnected' class"

    def test_disconnected_no_session_row(self):
        """DISCONNECTED: no session row visible."""
        c = self._checks
        assert c.get("disc_no_session"), "Session row should be hidden in DISCONNECTED"

    # ── LIVE_UI state ─────────────────────────────────────────────

    def test_live_ui_capture(self):
        """LIVE_UI: capture button is visible."""
        c = self._checks
        assert c.get("live_capture"), "Capture button not visible in LIVE_UI"

    def test_live_ui_chat_green(self):
        """LIVE_UI: chat toggle has chat-connected-hidden class."""
        c = self._checks
        assert c.get("live_chat_green"), "Chat toggle missing 'chat-connected-hidden' class"

    # ── LIVE_CHAT state ───────────────────────────────────────────

    def test_live_chat_no_capture(self):
        """LIVE_CHAT: capture button is hidden."""
        c = self._checks
        assert c.get("chat_no_capture"), "Capture should be hidden in LIVE_CHAT"

    def test_live_chat_session_row(self):
        """LIVE_CHAT: session row is visible."""
        c = self._checks
        assert c.get("chat_session"), "Session row not visible in LIVE_CHAT"

    def test_live_chat_prime(self):
        """LIVE_CHAT: prime button is visible."""
        c = self._checks
        assert c.get("chat_prime"), "Prime button not visible in LIVE_CHAT"

    def test_live_chat_disconnect(self):
        """LIVE_CHAT: disconnect button is visible."""
        c = self._checks
        assert c.get("chat_disconnect"), "Disconnect button not visible in LIVE_CHAT"

    def test_live_chat_icon(self):
        """LIVE_CHAT: chat toggle has chat-connected-shown class."""
        c = self._checks
        assert c.get("chat_icon"), "Chat toggle missing 'chat-connected-shown' class"

    def test_live_chat_no_iter(self):
        """LIVE_CHAT: iteration nav is hidden."""
        c = self._checks
        assert c.get("chat_no_iter"), "Iter nav should be hidden in LIVE_CHAT"

    # ── PICKER state ──────────────────────────────────────────────

    def test_picker_title(self):
        """PICKER: title contains 'Select'."""
        c = self._checks
        assert c.get("picker_title"), "Title should contain 'Select' in PICKER state"

    def test_picker_chat_class(self):
        """PICKER: chat toggle has chat-open class."""
        c = self._checks
        assert c.get("picker_chat_class"), "Chat toggle missing 'chat-open' class"

    def test_picker_no_capture(self):
        """PICKER: capture button is hidden."""
        c = self._checks
        assert c.get("picker_no_capture"), "Capture should be hidden in PICKER"

    def test_picker_no_iter(self):
        """PICKER: iteration nav is hidden."""
        c = self._checks
        assert c.get("picker_no_iter"), "Iter nav should be hidden in PICKER"
