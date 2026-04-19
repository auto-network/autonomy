"""
Behavioral sweep for the 4 nag-bell visual states.

Bead auto-yupl — session card bell must reflect both idle-nag
(`nag_enabled`) and dispatch-nag (`dispatch_nag_enabled`) with a
distinct class per state so a fleet of cards is scannable at a glance.

State matrix:
  off/off              → .sc-nag-off       (grayscale, 20% opacity)
  idle only            → .sc-nag-idle      (default amber bell)
  dispatch only        → .sc-nag-dispatch  (cyan via hue-rotate)
  both                 → .sc-nag-both      (cyan + amber glow halo)

The test asserts the class applied to the element — not a screenshot diff.
Template at tools/dashboard/templates/partials/session-card.html.
"""
import json
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tools.dashboard.tests import fixtures
from tools.dashboard.tests.fixtures import make_session
from tools.dashboard.tests.sessions.test_browser import (
    TEST_PORT,
    SessionsTestHarness,
    ab_eval,
    ab_raw,
)


# Four sessions, one per state.
NAG_STATE_SESSIONS = [
    {**make_session("auto-nag-off",      label="Nag: off/off"),
     "nag_enabled": False, "dispatch_nag_enabled": False},
    {**make_session("auto-nag-idle",     label="Nag: idle only"),
     "nag_enabled": True,  "dispatch_nag_enabled": False,
     "nag_interval": 15,   "nag_message": ""},
    {**make_session("auto-nag-dispatch", label="Nag: dispatch only"),
     "nag_enabled": False, "dispatch_nag_enabled": True},
    {**make_session("auto-nag-both",     label="Nag: both on"),
     "nag_enabled": True,  "dispatch_nag_enabled": True,
     "nag_interval": 15,   "nag_message": ""},
]


def _fixture():
    return {
        "beads": [],
        "active_sessions": NAG_STATE_SESSIONS,
        "session_entries": {s["session_id"]: [] for s in NAG_STATE_SESSIONS},
        "experiments": [fixtures.make_experiment(fixtures.TEST_EXPERIMENT_ID)],
    }


@pytest.fixture(scope="module")
def h(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("nag-bell")
    harness = SessionsTestHarness(tmp)
    harness.set_fixture(_fixture())
    harness.start_server()
    harness.open_sessions_page()
    # Allow SSE + Alpine reactivity to paint all four cards.
    time.sleep(1)
    yield harness
    ab_raw("close")
    harness.stop()


def _bell_class_for(session_id):
    """Return the bell span's className for a given session card.

    Card has two bell instances (compact row + stats row); the display-none
    wrapper hides the one the current zoom level doesn't use, but the class
    binding is identical on both, so we read from the first one.
    """
    js = (
        "var card = document.querySelector("
        f"'[data-testid=\"session-card\"][data-session-id=\"{session_id}\"]'"
        ");"
        "if (!card) return null;"
        "var bell = card.querySelector('.sc-nag');"
        "return bell ? bell.className : null;"
    )
    return ab_eval(js)


class TestNagBellFourStates:
    """Each state combination maps to its documented .sc-nag-* class."""

    def test_off_off_class(self, h):
        cls = _bell_class_for("auto-nag-off")
        assert cls is not None, "bell span not rendered for off/off session"
        assert "sc-nag-off" in cls, f"expected sc-nag-off, got {cls!r}"
        assert "sc-nag-idle" not in cls
        assert "sc-nag-dispatch" not in cls
        assert "sc-nag-both" not in cls

    def test_idle_only_class(self, h):
        cls = _bell_class_for("auto-nag-idle")
        assert cls is not None
        assert "sc-nag-idle" in cls, f"expected sc-nag-idle, got {cls!r}"
        assert "sc-nag-off" not in cls
        assert "sc-nag-dispatch" not in cls
        assert "sc-nag-both" not in cls

    def test_dispatch_only_class(self, h):
        cls = _bell_class_for("auto-nag-dispatch")
        assert cls is not None
        assert "sc-nag-dispatch" in cls, f"expected sc-nag-dispatch, got {cls!r}"
        assert "sc-nag-off" not in cls
        assert "sc-nag-idle" not in cls
        assert "sc-nag-both" not in cls

    def test_both_on_class(self, h):
        cls = _bell_class_for("auto-nag-both")
        assert cls is not None
        assert "sc-nag-both" in cls, f"expected sc-nag-both, got {cls!r}"
        assert "sc-nag-off" not in cls
        assert "sc-nag-idle" not in cls
        assert "sc-nag-dispatch" not in cls


class TestNagBellStyleDistinctness:
    """Each state resolves to a visually distinguishable computed style.

    Guards against a refactor that accidentally leaves one of the class
    rules empty or makes two states render identically.
    """

    def _computed(self, session_id):
        js = (
            "var card = document.querySelector("
            f"'[data-testid=\"session-card\"][data-session-id=\"{session_id}\"]'"
            ");"
            "if (!card) return null;"
            "var bell = card.querySelector('.sc-nag');"
            "if (!bell) return null;"
            "var cs = getComputedStyle(bell);"
            "return {opacity: cs.opacity, filter: cs.filter,"
            " textShadow: cs.textShadow, animationName: cs.animationName};"
        )
        return ab_eval(js)

    def test_off_is_dimmed(self, h):
        s = self._computed("auto-nag-off")
        assert s is not None
        assert float(s["opacity"]) < 0.5, f"off bell not dimmed: {s}"
        assert "grayscale" in s["filter"], f"off bell not grayscale: {s}"

    def test_dispatch_uses_hue_rotate(self, h):
        s = self._computed("auto-nag-dispatch")
        assert s is not None
        assert "hue-rotate" in s["filter"], f"dispatch bell missing hue-rotate: {s}"

    def test_both_has_pulse_animation(self, h):
        s = self._computed("auto-nag-both")
        assert s is not None
        assert s["animationName"] == "sc-nag-both-pulse", (
            f"both bell missing pulse animation: {s}"
        )
