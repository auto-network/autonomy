"""
Browser functional tests for the sessions page.

Tests BEHAVIOR through the user's perspective — not CSS classes or DOM structure.
Uses DASHBOARD_MOCK fixtures for data, agent-browser for interaction.

Every test answers: "Can the user see X?" — not "Does CSS class Y exist?"
"""
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tools.dashboard.tests import fixtures
from tools.dashboard.tests.sessions.conftest import (
    SESSIONS_PAGE_SESSIONS,
    RECENT_SESSIONS,
    sessions_page_fixture,
)


TEST_PORT = 8082


# ── Agent Browser Helpers ─────────────────────────────────────────────

def ab(*args, stdin_text=None, timeout=10):
    """Run agent-browser --json, unwrap response envelope."""
    result = subprocess.run(
        ["agent-browser", "--json"] + list(args),
        capture_output=True, text=True, timeout=timeout,
        input=stdin_text,
    )
    for line in reversed(result.stdout.strip().split("\n")):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict) and "success" in parsed and "data" in parsed:
                return parsed["data"] if not parsed.get("error") else None
            return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def ab_eval(js):
    """Evaluate JS via stdin IIFE, unwrap {origin, result}."""
    wrapped = f"(() => {{\n{js}\n}})()"
    result = ab("eval", "--stdin", stdin_text=wrapped)
    if isinstance(result, dict) and "result" in result:
        return result["result"]
    return result


def ab_raw(*args, timeout=10):
    return subprocess.run(
        ["agent-browser"] + list(args),
        capture_output=True, text=True, timeout=timeout,
    ).stdout


# ── Test Harness ──────────────────────────────────────────────────────

class SessionsTestHarness:
    """Manages test server + fixture + browser for sessions page tests."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.fixture_path = tmp_path / "fixtures.json"
        self.proc = None

    def set_fixture(self, fixture_dict):
        """Swap the fixture data. Mock DAO reads fresh on every request."""
        fixtures.write_fixture(fixture_dict, self.fixture_path)

    def start_server(self):
        # Kill any stale server on our port
        subprocess.run(
            ["pkill", "-f", f"uvicorn.*{TEST_PORT}"],
            capture_output=True, timeout=3,
        )
        time.sleep(1)

        env = os.environ.copy()
        env["DASHBOARD_MOCK"] = str(self.fixture_path)
        repo_root = str(Path(__file__).resolve().parents[4])
        env["PYTHONPATH"] = repo_root
        self.proc = subprocess.Popen(
            ["python3", "-m", "uvicorn", "tools.dashboard.server:app",
             "--host", "127.0.0.1", "--port", str(TEST_PORT)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=repo_root,
        )
        import httpx
        for _ in range(20):
            try:
                if httpx.get(f"http://localhost:{TEST_PORT}/sessions", timeout=1).status_code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.5)
        self.stop()
        raise RuntimeError("Server failed to start")

    def stop(self):
        if self.proc:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def open_sessions_page(self):
        ab_raw("close")
        ab_raw("open", f"http://localhost:{TEST_PORT}/sessions",
               "--ignore-https-errors")
        time.sleep(3)
        ab_raw("set", "viewport", "430", "900")
        time.sleep(0.5)

    def card_count(self):
        """Count rendered session cards."""
        return ab_eval("""
            var cards = document.querySelectorAll('[data-testid="session-card"]');
            return cards.length;
        """)

    def card_session_ids(self):
        """Get data-session-id values from all cards."""
        return ab_eval("""
            var cards = document.querySelectorAll('[data-testid="session-card"]');
            return Array.from(cards).map(function(c) { return c.dataset.sessionId; });
        """) or []

    def visible_text(self):
        return ab_eval("return document.body.innerText") or ""

    def store_topics(self, session_id):
        """Read topics from Alpine.store('sessions')[session_id].topics."""
        return ab_eval(f"""
            var sessions = Alpine.store('sessions');
            var s = sessions && sessions['{session_id}'];
            if (s) return s.topics;
            return null;
        """)

    def store_nag(self, session_id):
        """Read nag fields from Alpine store."""
        return ab_eval(f"""
            var sessions = Alpine.store('sessions');
            var s = sessions && sessions['{session_id}'];
            if (s) return {{
                nagEnabled: s.nagEnabled,
                nagInterval: s.nagInterval,
                nagMessage: s.nagMessage
            }};
            return null;
        """)

    def topic_texts(self):
        """Get visible topic text from all sc-topic-item elements."""
        return ab_eval("""
            var items = document.querySelectorAll('.sc-topic-item');
            return Array.from(items).map(function(el) { return el.textContent.trim(); });
        """) or []

    def nag_bells_visible(self):
        """Count visible (active) nag bell indicators."""
        return ab_eval("""
            var bells = document.querySelectorAll('.sc-nag:not(.sc-nag-off)');
            return bells.length;
        """)

    def stats_text(self):
        """Get text content of stats row elements."""
        return ab_eval("""
            var vals = document.querySelectorAll('.sc-t3-val');
            return Array.from(vals).map(function(el) { return el.textContent.trim(); });
        """) or []


# ── Module-scoped fixture ────────────────────────────────────────────

@pytest.fixture(scope="module")
def h(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("sessions")
    harness = SessionsTestHarness(tmp)
    harness.set_fixture(sessions_page_fixture())
    harness.start_server()
    harness.open_sessions_page()
    yield harness
    ab_raw("close")
    harness.stop()


# ── Tests ────────────────────────────────────────────────────────────


class TestUserCanSeeSessions:
    """When I open /sessions, can I see my active sessions?"""

    def test_cards_render(self, h):
        count = h.card_count()
        assert count >= 5, f"expected 5+ session cards, got {count}"

    def test_session_ids_set(self, h):
        ids = h.card_session_ids()
        assert "auto-test-alpha" in ids
        assert "host-test-delta" in ids

    def test_labels_visible(self, h):
        text = h.visible_text()
        assert "Alpha" in text
        assert "Beta Builder" in text

    def test_host_badge(self, h):
        text = h.visible_text()
        assert "Host" in text

    def test_role_badges(self, h):
        text = h.visible_text()
        assert "Designer" in text
        assert "Builder" in text


class TestTopicsRender:
    """Topics from the API appear on session cards."""

    def test_store_has_topics(self, h):
        """Acceptance test: store.topics is populated (not empty default)."""
        topics = h.store_topics("auto-test-alpha")
        assert topics is not None
        assert len(topics) >= 2, f"expected 2+ topics, got {topics}"

    def test_topics_visible_on_cards(self, h):
        topics = h.topic_texts()
        assert len(topics) >= 3, f"expected 3+ topic items visible, got {topics}"

    def test_topic_text_matches_fixtures(self, h):
        topics = h.topic_texts()
        assert "Redesigning session cards" in topics
        assert "Asset pipeline" in topics


class TestNagIndicator:
    """Sessions with active nag show a bell indicator."""

    def test_active_nag_bell_visible(self, h):
        # Gamma has nag_enabled — at least 1 visible (non-off) bell per card
        # Cards have both compact and stats row bells, so count may vary
        count = h.nag_bells_visible()
        assert count >= 1, f"expected at least 1 active nag bell, got {count}"


class TestStatsRow:
    """Turn counts and context tokens appear in stats."""

    def test_turns_visible(self, h):
        text = h.visible_text()
        assert "150" in text or "200" in text or "300" in text

    def test_context_tokens_visible(self, h):
        # 80000 → "80K", 120000 → "120K", 250000 → "250K"
        text = h.visible_text()
        assert "80K" in text or "120K" in text or "250K" in text


class TestRecentSessions:
    """Recent sessions section shows historical sessions."""

    def test_heading_visible(self, h):
        text = h.visible_text()
        assert "Recent Sessions" in text

    def test_entries_visible(self, h):
        text = h.visible_text()
        # Recent session titles from fixture
        assert "alpha history" in text.lower() or "beta history" in text.lower()


class TestEmptyState:
    """When there are no sessions, show an appropriate message."""

    def test_empty_message_shown(self, h):
        h.set_fixture(fixtures.empty_sessions())
        h.open_sessions_page()
        time.sleep(2)
        text = h.visible_text()
        assert "No active sessions" in text or "No sessions" in text

    def test_state_restored(self, h):
        h.set_fixture(sessions_page_fixture())
        h.open_sessions_page()
        time.sleep(2)
        count = h.card_count()
        assert count >= 5
