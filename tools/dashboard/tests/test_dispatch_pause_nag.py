"""Tests for dispatch pause nag — periodic alerts when dispatch queue is stuck."""

import asyncio
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tools.dashboard.session_monitor import (
    _get_dispatch_pause_message,
    _format_pause_duration,
    SessionMonitor,
)


# ── DB isolation ──────────────────────────────────────────────────

@pytest.fixture
def isolate_db(tmp_path, monkeypatch):
    """Each test gets a fresh dashboard.db via init_db()."""
    db_path = tmp_path / "dashboard.db"
    monkeypatch.setenv("DASHBOARD_DB", str(db_path))
    from tools.dashboard.dao import dashboard_db as db
    old_conn = db._conn
    db._conn = None
    db._DB_PATH = db_path
    db.init_db(db_path)
    yield db
    if db._conn:
        db._conn.close()
    db._conn = old_conn


# ── _format_pause_duration tests ──────────────────────────────────

class TestFormatPauseDuration:
    def test_none_returns_empty(self):
        assert _format_pause_duration(None) == ""

    def test_recent_timestamp(self):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert _format_pause_duration(ts) == " (<1m ago)"

    def test_minutes_ago(self):
        ts = (datetime.now(timezone.utc) - timedelta(minutes=45)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        result = _format_pause_duration(ts)
        assert "45m ago" in result

    def test_hours_ago(self):
        ts = (datetime.now(timezone.utc) - timedelta(hours=2, minutes=10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        result = _format_pause_duration(ts)
        assert "2h" in result
        assert "10m ago" in result

    def test_invalid_timestamp_returns_empty(self):
        assert _format_pause_duration("not-a-date") == ""


# ── _get_dispatch_pause_message tests ─────────────────────────────

class TestGetDispatchPauseMessage:
    def test_not_paused_returns_none(self):
        mock_db = MagicMock()
        mock_db.is_paused.return_value = False
        mock_db.get_pause_reason.return_value = None
        with patch.dict("sys.modules", {"agents.dispatch_db": mock_db}):
            result = _get_dispatch_pause_message()
        assert result is None

    def test_global_pause_with_message(self):
        mock_db = MagicMock()
        mock_db.is_paused.return_value = True
        mock_db.get_pause_reason.return_value = {
            "reason": "auth",
            "paused_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bead_id": "auto-xyz",
            "message": "API key expired",
        }
        with patch.dict("sys.modules", {"agents.dispatch_db": mock_db}):
            result = _get_dispatch_pause_message()
        assert result is not None
        assert "Dispatch paused: API key expired" in result
        assert "ago)" in result

    def test_global_pause_reason_only(self):
        """Falls back to 'reason' field when 'message' is absent."""
        mock_db = MagicMock()
        mock_db.is_paused.return_value = True
        mock_db.get_pause_reason.return_value = {
            "reason": "merge_blocked",
        }
        with patch.dict("sys.modules", {"agents.dispatch_db": mock_db}):
            result = _get_dispatch_pause_message()
        assert result == "Dispatch paused: merge_blocked"

    def test_global_pause_no_reason_dict(self):
        """is_paused() returns True but get_pause_reason() returns None."""
        mock_db = MagicMock()
        mock_db.is_paused.return_value = True
        mock_db.get_pause_reason.return_value = None
        with patch.dict("sys.modules", {"agents.dispatch_db": mock_db}):
            result = _get_dispatch_pause_message()
        assert result == "Dispatch paused: unknown"

    def test_label_pause_from_state_file(self, tmp_path):
        """Per-label pause via dispatch.state file."""
        state_file = tmp_path / "data" / "dispatch.state"
        state_file.parent.mkdir(parents=True)
        state_file.write_text(json.dumps({
            "dashboard": True,
            "dashboard_reason": "smoke failed on auto-abc",
        }))

        mock_db = MagicMock()
        mock_db.is_paused.return_value = False
        mock_db.get_pause_reason.return_value = None

        fake_module = tmp_path / "tools" / "dashboard" / "session_monitor.py"
        fake_module.parent.mkdir(parents=True)
        fake_module.touch()

        import tools.dashboard.session_monitor as sm
        orig_file = sm.__file__
        try:
            sm.__file__ = str(fake_module)
            with patch.dict("sys.modules", {"agents.dispatch_db": mock_db}):
                result = _get_dispatch_pause_message()
        finally:
            sm.__file__ = orig_file

        assert result is not None
        assert "smoke failed on auto-abc" in result

    def test_label_pause_fallback_reason(self, tmp_path):
        """Per-label pause without explicit reason string."""
        state_file = tmp_path / "data" / "dispatch.state"
        state_file.parent.mkdir(parents=True)
        state_file.write_text(json.dumps({"myqueue": True}))

        mock_db = MagicMock()
        mock_db.is_paused.return_value = False
        mock_db.get_pause_reason.return_value = None

        fake_module = tmp_path / "tools" / "dashboard" / "session_monitor.py"
        fake_module.parent.mkdir(parents=True, exist_ok=True)
        fake_module.touch()

        import tools.dashboard.session_monitor as sm
        orig_file = sm.__file__
        try:
            sm.__file__ = str(fake_module)
            with patch.dict("sys.modules", {"agents.dispatch_db": mock_db}):
                result = _get_dispatch_pause_message()
        finally:
            sm.__file__ = orig_file

        assert result == "Dispatch paused: myqueue queue paused"


# ── _check_dispatch_pause_nag integration tests ──────────────────

def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestCheckDispatchPauseNag:
    """Test the SessionMonitor._check_dispatch_pause_nag method."""

    @pytest.fixture
    def monitor(self):
        return SessionMonitor()

    def _insert_subscriber(self, db, tmux_name="host-test-session"):
        """Insert a live session with dispatch_nag=1."""
        db.get_conn().execute(
            """INSERT OR REPLACE INTO tmux_sessions
            (tmux_name, type, project, created_at, is_live, dispatch_nag)
            VALUES (?, ?, ?, ?, 1, 1)""",
            (tmux_name, "host", "test", time.time()),
        )
        db.get_conn().commit()

    def test_nag_fires_when_paused(self, monitor, isolate_db):
        """Nag should fire to dispatch_nag subscribers when paused."""
        self._insert_subscriber(isolate_db)
        now = time.time()

        with patch(
            "tools.dashboard.session_monitor._get_dispatch_pause_message",
            return_value="Dispatch paused: smoke failed on auto-xyz (5m ago)",
        ):
            with patch.object(
                SessionMonitor, "_check_tmux", return_value=True,
            ):
                with patch(
                    "tools.dashboard.session_monitor._send_nag_crosstalk"
                ) as mock_send:
                    _run(monitor._check_dispatch_pause_nag(now))

        mock_send.assert_called_once_with(
            "host-test-session",
            "Dispatch paused: smoke failed on auto-xyz (5m ago)",
        )
        assert monitor._last_pause_nag_sent == now

    def test_nag_skips_when_not_paused(self, monitor, isolate_db):
        """Nag should not fire when not paused."""
        self._insert_subscriber(isolate_db)
        now = time.time()

        with patch(
            "tools.dashboard.session_monitor._get_dispatch_pause_message",
            return_value=None,
        ):
            with patch(
                "tools.dashboard.session_monitor._send_nag_crosstalk"
            ) as mock_send:
                _run(monitor._check_dispatch_pause_nag(now))

        mock_send.assert_not_called()
        assert monitor._last_pause_nag_sent == 0.0

    def test_nag_respects_interval(self, monitor, isolate_db):
        """Nag should not fire again within the 15-minute interval."""
        self._insert_subscriber(isolate_db)
        now = time.time()
        monitor._last_pause_nag_sent = now - 60  # Only 1 minute ago

        with patch(
            "tools.dashboard.session_monitor._get_dispatch_pause_message",
        ) as mock_msg:
            with patch(
                "tools.dashboard.session_monitor._send_nag_crosstalk"
            ) as mock_send:
                _run(monitor._check_dispatch_pause_nag(now))

        # Should not even check pause state — interval not elapsed
        mock_msg.assert_not_called()
        mock_send.assert_not_called()

    def test_nag_fires_after_interval(self, monitor, isolate_db):
        """Nag should fire again after 15 minutes have passed."""
        self._insert_subscriber(isolate_db)
        now = time.time()
        monitor._last_pause_nag_sent = now - (16 * 60)  # 16 minutes ago

        with patch(
            "tools.dashboard.session_monitor._get_dispatch_pause_message",
            return_value="Dispatch paused: auth failure",
        ):
            with patch.object(
                SessionMonitor, "_check_tmux", return_value=True,
            ):
                with patch(
                    "tools.dashboard.session_monitor._send_nag_crosstalk"
                ) as mock_send:
                    _run(monitor._check_dispatch_pause_nag(now))

        mock_send.assert_called_once()
        assert monitor._last_pause_nag_sent == now

    def test_nag_skips_dead_sessions(self, monitor, isolate_db):
        """Nag should skip subscribers whose tmux session is dead."""
        self._insert_subscriber(isolate_db)
        now = time.time()

        with patch(
            "tools.dashboard.session_monitor._get_dispatch_pause_message",
            return_value="Dispatch paused: test",
        ):
            with patch.object(
                SessionMonitor, "_check_tmux", return_value=False,
            ):
                with patch(
                    "tools.dashboard.session_monitor._send_nag_crosstalk"
                ) as mock_send:
                    _run(monitor._check_dispatch_pause_nag(now))

        mock_send.assert_not_called()
        assert monitor._last_pause_nag_sent == 0.0

    def test_nag_no_subscribers(self, monitor, isolate_db):
        """Nag should not fire when there are no dispatch_nag subscribers."""
        now = time.time()

        with patch(
            "tools.dashboard.session_monitor._get_dispatch_pause_message",
            return_value="Dispatch paused: test",
        ):
            with patch(
                "tools.dashboard.session_monitor._send_nag_crosstalk"
            ) as mock_send:
                _run(monitor._check_dispatch_pause_nag(now))

        mock_send.assert_not_called()

    def test_stops_when_resumed(self, monitor, isolate_db):
        """Nag should stop when dispatch resumes (message returns None)."""
        self._insert_subscriber(isolate_db)
        now = time.time()
        monitor._last_pause_nag_sent = now - (20 * 60)

        with patch(
            "tools.dashboard.session_monitor._get_dispatch_pause_message",
            return_value=None,
        ):
            with patch(
                "tools.dashboard.session_monitor._send_nag_crosstalk"
            ) as mock_send:
                _run(monitor._check_dispatch_pause_nag(now))

        mock_send.assert_not_called()

    def test_multiple_subscribers(self, monitor, isolate_db):
        """Nag should fire to all live dispatch_nag subscribers."""
        self._insert_subscriber(isolate_db, "host-one")
        self._insert_subscriber(isolate_db, "host-two")
        now = time.time()

        with patch(
            "tools.dashboard.session_monitor._get_dispatch_pause_message",
            return_value="Dispatch paused: test",
        ):
            with patch.object(
                SessionMonitor, "_check_tmux", return_value=True,
            ):
                with patch(
                    "tools.dashboard.session_monitor._send_nag_crosstalk"
                ) as mock_send:
                    _run(monitor._check_dispatch_pause_nag(now))

        assert mock_send.call_count == 2
        sent_to = {call.args[0] for call in mock_send.call_args_list}
        assert sent_to == {"host-one", "host-two"}
        assert monitor._last_pause_nag_sent == now
