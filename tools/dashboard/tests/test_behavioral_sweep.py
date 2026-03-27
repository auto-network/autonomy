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

# Minimal beads + runs for nav SSE (dispatch page not tested yet)
SWEEP_BEADS = [
    {"id": "auto-sweep-b1", "title": "Sweep test bead", "priority": 1,
     "status": "open", "labels": []},
]


def _build_fixture() -> dict:
    """Build the complete fixture dict for behavioral sweep tests."""
    return {
        "active_sessions": SWEEP_SESSIONS,
        "session_entries": SWEEP_SESSION_ENTRIES,
        "recent_sessions": SWEEP_RECENT_SESSIONS,
        "beads": SWEEP_BEADS,
        "runs": [],
        "experiments": [],
    }


# ── Module-scoped fixtures ────────────────────────────────────────────

@pytest.fixture(scope="module")
def sweep_server(tmp_path_factory):
    """Boot a DASHBOARD_MOCK uvicorn server on a test port, tear down after module."""
    tmpdir = tmp_path_factory.mktemp("sweep")
    fixture_path = tmpdir / "fixtures.json"
    fixture_path.write_text(json.dumps(_build_fixture(), indent=2))

    events_path = tmpdir / "events.jsonl"
    events_path.write_text("")  # empty — no SSE events needed for sessions page

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

    yield {"port": port, "url": f"http://127.0.0.1:{port}", "fixture_path": str(fixture_path)}

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
    # Give Alpine + HTTP fallback time to populate the store
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
