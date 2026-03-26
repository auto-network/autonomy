"""
State machine tests for the unified session viewer (Boundary F — pure state derivation).

Tests the state derivation contract from Architecture Spec v7, Section 5.
The derive_state() Python helper mirrors the JS deriveState() that auto-vl46
will implement.  All tests here are pure Python — no server or browser needed.

State table:
    loaded=false, *, *                → "connecting"
    loaded=true,  isLive=true,  linked=true  → "live"
    loaded=true,  isLive=true,  linked=false → "unresolved"
    loaded=true,  isLive=false, *            → "complete"

Field note: current store uses `linked` (session-store.js line 47).
Phase 3 (auto-h4gh) renames linked → resolved.  The logic is identical:
linked=true means "JSONL path known, can tail."
"""
import pytest


# ── State derivation helper ──────────────────────────────────────────
# Pure function encoding the spec's state table (Section 5).
# auto-vl46 implements the same logic in JS; browser tests verify the real UI.

def derive_state(*, loaded: bool, isLive: bool, linked: bool) -> str:
    """Derive viewer state from three source-of-truth fields.

    Args:
        loaded:  True when backfill is complete (client-side transient).
        isLive:  True when the tmux session is alive (from server/SSE).
        linked:  True when JSONL path is known (from server/SSE).
                 # Phase 3 (auto-h4gh) renames linked → resolved

    Returns:
        One of: "connecting", "live", "unresolved", "complete"
    """
    if not loaded:
        return "connecting"
    if not isLive:
        return "complete"
    if linked:
        return "live"
    return "unresolved"


# ── State implications helper ────────────────────────────────────────
# Encodes what each state enables/disables in the UI.

def state_implies(state: str) -> dict:
    """Return UI affordances for a given viewer state."""
    return {
        "send_enabled": state == "live",
        "link_available": state == "unresolved",
        "input_bar_visible": state == "live",
        "spinner_shown": state == "connecting",
    }


# ══════════════════════════════════════════════════════════════════════
# TestStateDerivation — every cell in the state table
# ══════════════════════════════════════════════════════════════════════

class TestStateDerivation:
    """Verify derive_state() matches the spec state table exactly."""

    def test_connecting_when_not_loaded(self):
        """loaded=false, isLive=true, linked=true → 'connecting'"""
        assert derive_state(loaded=False, isLive=True, linked=True) == "connecting"

    def test_connecting_when_not_loaded_any_flags(self):
        """loaded=false with any isLive/linked combination → 'connecting'"""
        for is_live in (True, False):
            for linked in (True, False):
                assert derive_state(loaded=False, isLive=is_live, linked=linked) == "connecting"

    def test_live_when_loaded_live_linked(self):
        """loaded=true, isLive=true, linked=true → 'live'"""
        assert derive_state(loaded=True, isLive=True, linked=True) == "live"

    def test_unresolved_when_live_not_linked(self):
        """loaded=true, isLive=true, linked=false → 'unresolved'"""
        assert derive_state(loaded=True, isLive=True, linked=False) == "unresolved"

    def test_complete_when_not_live(self):
        """loaded=true, isLive=false, linked=true → 'complete'"""
        assert derive_state(loaded=True, isLive=False, linked=True) == "complete"

    def test_complete_when_not_live_not_linked(self):
        """loaded=true, isLive=false, linked=false → 'complete'"""
        assert derive_state(loaded=True, isLive=False, linked=False) == "complete"


# ══════════════════════════════════════════════════════════════════════
# TestStateTransitions — valid transitions between states
# ══════════════════════════════════════════════════════════════════════

class TestStateTransitions:
    """Verify that changing one field produces the expected state transition."""

    def test_connecting_to_live(self):
        """loaded becomes true with linked=true, isLive=true → connecting→live"""
        before = derive_state(loaded=False, isLive=True, linked=True)
        after = derive_state(loaded=True, isLive=True, linked=True)
        assert before == "connecting"
        assert after == "live"

    def test_connecting_to_unresolved(self):
        """loaded becomes true with linked=false, isLive=true → connecting→unresolved"""
        before = derive_state(loaded=False, isLive=True, linked=False)
        after = derive_state(loaded=True, isLive=True, linked=False)
        assert before == "connecting"
        assert after == "unresolved"

    def test_connecting_to_complete(self):
        """loaded becomes true with isLive=false → connecting→complete"""
        before = derive_state(loaded=False, isLive=False, linked=True)
        after = derive_state(loaded=True, isLive=False, linked=True)
        assert before == "connecting"
        assert after == "complete"

    def test_unresolved_to_live(self):
        """linked becomes true while isLive=true → unresolved→live"""
        before = derive_state(loaded=True, isLive=True, linked=False)
        after = derive_state(loaded=True, isLive=True, linked=True)
        assert before == "unresolved"
        assert after == "live"

    def test_live_to_complete(self):
        """isLive becomes false → live→complete"""
        before = derive_state(loaded=True, isLive=True, linked=True)
        after = derive_state(loaded=True, isLive=False, linked=True)
        assert before == "live"
        assert after == "complete"

    def test_unresolved_to_complete(self):
        """isLive becomes false while unresolved → unresolved→complete"""
        before = derive_state(loaded=True, isLive=True, linked=False)
        after = derive_state(loaded=True, isLive=False, linked=False)
        assert before == "unresolved"
        assert after == "complete"


# ══════════════════════════════════════════════════════════════════════
# TestNoReverseTransitions — monotonicity constraints
# ══════════════════════════════════════════════════════════════════════

class TestNoReverseTransitions:
    """Once certain fields change, they never change back (spec invariants)."""

    def test_complete_is_terminal(self):
        """Once isLive=false, cannot go back to true.
        This is enforced by the server — tmux sessions don't resurrect.
        We verify the state machine contract: if someone were to set isLive
        back to true, they'd leave 'complete', which violates the invariant."""
        # Simulate: live → complete → (invalid) live again
        state1 = derive_state(loaded=True, isLive=True, linked=True)
        state2 = derive_state(loaded=True, isLive=False, linked=True)
        # If isLive were ever set back to True (protocol violation), state would
        # revert — this test documents that the state machine itself doesn't
        # enforce monotonicity (the server does).
        state3 = derive_state(loaded=True, isLive=True, linked=True)
        assert state1 == "live"
        assert state2 == "complete"
        # Document: the function is pure — it's the server's job to never
        # broadcast isLive=true after broadcasting isLive=false.
        assert state3 == "live", (
            "State machine is a pure function; monotonicity is enforced by the "
            "server, not by derive_state(). If isLive flips back, state follows."
        )

    def test_linked_never_unsets(self):
        """Once linked=true, never goes back to false.
        Same as above — the server enforces this (once jsonl_path is set,
        it stays set).  The state machine is a pure function of inputs."""
        state1 = derive_state(loaded=True, isLive=True, linked=False)
        state2 = derive_state(loaded=True, isLive=True, linked=True)
        # If linked were ever set back to False (protocol violation):
        state3 = derive_state(loaded=True, isLive=True, linked=False)
        assert state1 == "unresolved"
        assert state2 == "live"
        assert state3 == "unresolved", (
            "State machine is a pure function; monotonicity is enforced by the "
            "server, not by derive_state(). If linked flips back, state follows."
        )


# ══════════════════════════════════════════════════════════════════════
# TestStateImplications — what each state enables/disables
# ══════════════════════════════════════════════════════════════════════

class TestStateImplications:
    """Verify that UI affordances are correctly determined by state."""

    def test_send_enabled_only_when_live(self):
        """Only 'live' state allows message sending."""
        for state in ("connecting", "unresolved", "complete"):
            assert state_implies(state)["send_enabled"] is False, \
                f"send should be disabled in '{state}' state"
        assert state_implies("live")["send_enabled"] is True

    def test_link_available_only_when_unresolved(self):
        """Only 'unresolved' state shows Link Terminal button."""
        for state in ("connecting", "live", "complete"):
            assert state_implies(state)["link_available"] is False, \
                f"link button should be hidden in '{state}' state"
        assert state_implies("unresolved")["link_available"] is True

    def test_input_bar_hidden_when_complete(self):
        """'complete' state has no input bar."""
        assert state_implies("complete")["input_bar_visible"] is False
        assert state_implies("connecting")["input_bar_visible"] is False
        assert state_implies("unresolved")["input_bar_visible"] is False
        assert state_implies("live")["input_bar_visible"] is True

    def test_spinner_shown_when_connecting(self):
        """'connecting' state shows loading indicator."""
        assert state_implies("connecting")["spinner_shown"] is True
        for state in ("live", "unresolved", "complete"):
            assert state_implies(state)["spinner_shown"] is False, \
                f"spinner should be hidden in '{state}' state"
