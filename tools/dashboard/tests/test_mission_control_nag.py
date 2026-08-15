"""Tests for the Mission Control outstanding-questions idle nag —
SessionMonitor._check_mission_control_nag. Mirrors the established pattern
in test_dispatch_pause_nag.py (SessionMonitor() direct instantiation,
_run() sync-wraps the coroutine, patch the data source + _send_nag_crosstalk
+ _check_tmux)."""

import asyncio
import time
from unittest.mock import patch

import pytest

from tools.dashboard.session_monitor import SessionMonitor, _build_mission_control_nag_message


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _session_row(tmux_name, *, last_activity, created_at=None, state="ACTIVE"):
    return {
        "tmux_name": tmux_name,
        "last_activity": last_activity,
        "created_at": created_at if created_at is not None else last_activity,
        "state": state,
    }


class TestBuildMissionControlNagMessage:
    def test_singular_question_wording(self):
        msg = _build_mission_control_nag_message([{"question": "only one"}])
        assert "1 outstanding Mission Control question:" in msg
        assert "- only one" in msg

    def test_plural_question_wording(self):
        msg = _build_mission_control_nag_message([{"question": "a"}, {"question": "b"}])
        assert "2 outstanding Mission Control questions:" in msg

    def test_truncates_long_questions(self):
        long_q = "x" * 200
        msg = _build_mission_control_nag_message([{"question": long_q}])
        assert "..." in msg
        assert len(msg.splitlines()[1]) < 150

    def test_full_entry_renders_the_answer_route(self):
        # Positive pair to the graceful-degradation path: an entry WITH
        # its ids must still emit the POST answer route, so degradation
        # can never silently swallow it for every entry.
        msg = _build_mission_control_nag_message([{
            "question": "why 3 triggers?",
            "mission_id": "m-123",
            "entry_id": "e-456",
        }])
        assert "POST /api/missions/m-123/questions/e-456/answer" in msg

    def test_pillar_entry_renders_pillar_scoped_route(self):
        msg = _build_mission_control_nag_message([{
            "question": "why?",
            "pillar_id": "p-9",
            "mission_id": "m-1",
            "entry_id": "e-7",
        }])
        assert "POST /api/pillars/p-9/questions/e-7/answer" in msg

    def test_caps_listed_entries_and_summarizes_the_rest(self):
        entries = [{"question": f"q{i}"} for i in range(8)]
        msg = _build_mission_control_nag_message(entries)
        assert "q0" in msg
        assert "q4" in msg
        assert "q5" not in msg
        assert "...and 3 more." in msg


class TestCheckMissionControlNag:
    @pytest.fixture
    def monitor(self):
        return SessionMonitor()

    def test_fires_when_idle_past_threshold(self, monitor):
        now = time.time()
        sessions = [_session_row("auto-schema", last_activity=now - 120)]
        open_by_session = {"auto-schema": [{"question": "why 3 triggers?"}]}

        with patch(
            "tools.dashboard.dao.mission_control_db.list_coordinators_with_open_questions",
            return_value=open_by_session,
        ):
            with patch(
                "tools.dashboard.dao.mission_control_db.get_coordinator_nag_state",
                return_value=None,
            ):
                with patch(
                    "tools.dashboard.dao.mission_control_db.mark_coordinator_nagged",
                ) as mock_mark:
                    with patch.object(SessionMonitor, "_check_tmux", return_value=True):
                        with patch(
                            "tools.dashboard.session_monitor._send_nag_crosstalk",
                        ) as mock_send:
                            _run(monitor._check_mission_control_nag(sessions, now))

        mock_send.assert_called_once()
        assert mock_send.call_args.args[0] == "auto-schema"
        assert "why 3 triggers?" in mock_send.call_args.args[1]
        mock_mark.assert_called_once_with("auto-schema")

    def test_skips_when_no_open_questions(self, monitor):
        now = time.time()
        sessions = [_session_row("auto-schema", last_activity=now - 120)]

        with patch(
            "tools.dashboard.dao.mission_control_db.list_coordinators_with_open_questions",
            return_value={},
        ):
            with patch("tools.dashboard.session_monitor._send_nag_crosstalk") as mock_send:
                _run(monitor._check_mission_control_nag(sessions, now))

        mock_send.assert_not_called()

    def test_skips_when_not_yet_idle_long_enough(self, monitor):
        now = time.time()
        sessions = [_session_row("auto-schema", last_activity=now - 5)]
        open_by_session = {"auto-schema": [{"question": "q"}]}

        with patch(
            "tools.dashboard.dao.mission_control_db.list_coordinators_with_open_questions",
            return_value=open_by_session,
        ):
            with patch("tools.dashboard.session_monitor._send_nag_crosstalk") as mock_send:
                _run(monitor._check_mission_control_nag(sessions, now))

        mock_send.assert_not_called()

    def test_skips_within_cooldown_since_last_nag(self, monitor):
        now = time.time()
        sessions = [_session_row("auto-schema", last_activity=now - 120)]
        open_by_session = {"auto-schema": [{"question": "q"}]}

        with patch(
            "tools.dashboard.dao.mission_control_db.list_coordinators_with_open_questions",
            return_value=open_by_session,
        ):
            with patch(
                "tools.dashboard.dao.mission_control_db.get_coordinator_nag_state",
                return_value=now - 10,  # nagged 10s ago, well within the 60s cooldown
            ):
                with patch("tools.dashboard.session_monitor._send_nag_crosstalk") as mock_send:
                    _run(monitor._check_mission_control_nag(sessions, now))

        mock_send.assert_not_called()

    def test_fires_again_after_cooldown_elapsed(self, monitor):
        now = time.time()
        sessions = [_session_row("auto-schema", last_activity=now - 120)]
        open_by_session = {"auto-schema": [{"question": "q"}]}

        with patch(
            "tools.dashboard.dao.mission_control_db.list_coordinators_with_open_questions",
            return_value=open_by_session,
        ):
            with patch(
                "tools.dashboard.dao.mission_control_db.get_coordinator_nag_state",
                return_value=now - 90,  # past the 60s cooldown
            ):
                with patch("tools.dashboard.dao.mission_control_db.mark_coordinator_nagged"):
                    with patch.object(SessionMonitor, "_check_tmux", return_value=True):
                        with patch(
                            "tools.dashboard.session_monitor._send_nag_crosstalk",
                        ) as mock_send:
                            _run(monitor._check_mission_control_nag(sessions, now))

        mock_send.assert_called_once()

    def test_skips_dead_sessions(self, monitor):
        now = time.time()
        sessions = [_session_row("auto-schema", last_activity=now - 120)]
        open_by_session = {"auto-schema": [{"question": "q"}]}

        with patch(
            "tools.dashboard.dao.mission_control_db.list_coordinators_with_open_questions",
            return_value=open_by_session,
        ):
            with patch(
                "tools.dashboard.dao.mission_control_db.get_coordinator_nag_state",
                return_value=None,
            ):
                with patch.object(SessionMonitor, "_check_tmux", return_value=False):
                    with patch(
                        "tools.dashboard.session_monitor._send_nag_crosstalk",
                    ) as mock_send:
                        _run(monitor._check_mission_control_nag(sessions, now))

        mock_send.assert_not_called()

    def test_skips_ended_sessions_even_with_open_questions(self, monitor):
        now = time.time()
        sessions = [_session_row("auto-schema", last_activity=now - 120, state="ENDED")]
        open_by_session = {"auto-schema": [{"question": "q"}]}

        with patch(
            "tools.dashboard.dao.mission_control_db.list_coordinators_with_open_questions",
            return_value=open_by_session,
        ):
            with patch("tools.dashboard.session_monitor._send_nag_crosstalk") as mock_send:
                _run(monitor._check_mission_control_nag(sessions, now))

        mock_send.assert_not_called()

    def test_skips_session_not_in_live_sessions_list(self, monitor):
        """A coordinator_session with open questions that isn't a currently
        live tmux session at all (long gone, or never was one) must not
        crash the sweep."""
        now = time.time()
        sessions: list[dict] = []
        open_by_session = {"auto-ghost": [{"question": "q"}]}

        with patch(
            "tools.dashboard.dao.mission_control_db.list_coordinators_with_open_questions",
            return_value=open_by_session,
        ):
            with patch("tools.dashboard.session_monitor._send_nag_crosstalk") as mock_send:
                _run(monitor._check_mission_control_nag(sessions, now))

        mock_send.assert_not_called()

    def test_query_failure_does_not_raise(self, monitor):
        """A Mission Control DB hiccup must not break the whole liveness
        loop tick -- this nag feature is additive, not load-bearing."""
        now = time.time()
        sessions = [_session_row("auto-schema", last_activity=now - 120)]

        with patch(
            "tools.dashboard.dao.mission_control_db.list_coordinators_with_open_questions",
            side_effect=RuntimeError("db unavailable"),
        ):
            _run(monitor._check_mission_control_nag(sessions, now))  # must not raise

    def test_multiple_sessions_each_nagged_independently(self, monitor):
        now = time.time()
        sessions = [
            _session_row("auto-a", last_activity=now - 120),
            _session_row("auto-b", last_activity=now - 120),
        ]
        open_by_session = {
            "auto-a": [{"question": "qa"}],
            "auto-b": [{"question": "qb"}],
        }

        with patch(
            "tools.dashboard.dao.mission_control_db.list_coordinators_with_open_questions",
            return_value=open_by_session,
        ):
            with patch(
                "tools.dashboard.dao.mission_control_db.get_coordinator_nag_state",
                return_value=None,
            ):
                with patch("tools.dashboard.dao.mission_control_db.mark_coordinator_nagged"):
                    with patch.object(SessionMonitor, "_check_tmux", return_value=True):
                        with patch(
                            "tools.dashboard.session_monitor._send_nag_crosstalk",
                        ) as mock_send:
                            _run(monitor._check_mission_control_nag(sessions, now))

        assert mock_send.call_count == 2
        targets = {call.args[0] for call in mock_send.call_args_list}
        assert targets == {"auto-a", "auto-b"}
