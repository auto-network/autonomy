"""Tests for ``tools.dashboard.voice_buffer.BufferManager`` (S3-3).

Covers the buffer lifecycle (acquire → append → clear / detach +
reconnect / release), the 60s TTL with a fake clock so boundary
behavior is deterministic, the cross-tab guard's eviction callback
return, and the ``buffer_state`` frame helper.

Spec: ``graph://86fd1897-d4d``.
"""

from __future__ import annotations

import pytest

from tools.dashboard import voice_buffer as vb


class _FakeClock:
    """Deterministic clock for TTL boundary testing.

    Tests pin ``now`` explicitly via :meth:`advance` rather than
    relying on real wall-clock progression. Removes flake risk from
    test machines under load."""

    def __init__(self, start_ms: int = 1_000_000):
        self.now = start_ms

    def __call__(self) -> int:
        return self.now

    def advance(self, ms: int) -> None:
        self.now += ms


def _new_mgr(ttl_ms: int = 60_000) -> tuple[vb.BufferManager, _FakeClock]:
    clock = _FakeClock()
    mgr = vb.BufferManager(ttl_ms=ttl_ms, clock=clock)
    return mgr, clock


# ── word_count + buffer_state_frame helpers ─────────────────


@pytest.mark.parametrize("text,expected", [
    ("", 0),
    ("   ", 0),
    ("hello", 1),
    ("hello world", 2),
    ("hello   world", 2),  # multi-space collapse
    ("one\ntwo\tthree", 3),  # any whitespace
])
def test_word_count(text, expected):
    assert vb._word_count(text) == expected


def test_buffer_state_frame_shape():
    frame = vb.buffer_state_frame("hello world friend")
    assert frame == {
        "type": "buffer_state",
        "text": "hello world friend",
        "word_count": 3,
    }


# ── acquire: fresh / existing / superseded ──────────────────


def test_acquire_fresh_returns_empty_text_and_no_prior():
    mgr, _ = _new_mgr()
    result = mgr.acquire("auto-1", evict_callback=object())
    assert result.buffer_text == ""
    assert result.prior_evict_callback is None
    assert mgr.is_tracked("auto-1")


def test_acquire_after_append_returns_existing_text():
    mgr, _ = _new_mgr()
    mgr.acquire("auto-1", evict_callback=object())
    mgr.append_final("auto-1", "first chunk")
    mgr.append_final("auto-1", "second chunk")
    # Detach to enter the TTL window (simulates WS disconnect).
    mgr.detach("auto-1")
    # Reconnect: acquire should restore.
    result = mgr.acquire("auto-1", evict_callback=object())
    assert result.buffer_text == "first chunk second chunk"
    assert result.prior_evict_callback is None  # detach cleared it


def test_acquire_returns_prior_evict_callback_on_cross_tab():
    """Spec: cross-tab guard. New connection for already-attached
    tmux_name returns the prior owner's evict callback so the
    transport can close the prior WS with code 1000 reason
    'superseded'."""
    mgr, _ = _new_mgr()
    cb_a = object()
    cb_b = object()
    mgr.acquire("auto-1", evict_callback=cb_a)
    result = mgr.acquire("auto-1", evict_callback=cb_b)
    assert result.prior_evict_callback is cb_a
    # State preserved (text unchanged); owner swapped.
    assert result.buffer_text == ""


def test_acquire_preserves_buffer_text_on_cross_tab():
    """Cross-tab handoff doesn't discard the in-flight buffer —
    operator's dictation survives a tab refresh."""
    mgr, _ = _new_mgr()
    mgr.acquire("auto-1", evict_callback=object())
    mgr.append_final("auto-1", "in-flight dictation")
    result = mgr.acquire("auto-1", evict_callback=object())
    assert result.buffer_text == "in-flight dictation"


# ── append_final ────────────────────────────────────────────


def test_append_to_empty_buffer():
    mgr, _ = _new_mgr()
    mgr.acquire("auto-1", evict_callback=object())
    mgr.append_final("auto-1", "hello")
    assert mgr.get_text("auto-1") == "hello"


def test_append_joins_with_single_space():
    """WhisperLive emits finals as utterance-level chunks; a single
    space between them is the right join in the common case. The
    transport can pre-normalize if it wants a different join."""
    mgr, _ = _new_mgr()
    mgr.acquire("auto-1", evict_callback=object())
    mgr.append_final("auto-1", "first.")
    mgr.append_final("auto-1", "Second sentence.")
    assert mgr.get_text("auto-1") == "first. Second sentence."


def test_append_to_unknown_tmux_silently_dropped():
    """No buffer → no append. Defensive: a late-arriving transcript
    after release/TTL-eviction must not silently re-create state
    that has no owner."""
    mgr, _ = _new_mgr()
    mgr.append_final("never-acquired", "ghost transcript")
    assert mgr.get_text("never-acquired") == ""
    assert not mgr.is_tracked("never-acquired")


def test_append_empty_string_is_noop():
    mgr, _ = _new_mgr()
    mgr.acquire("auto-1", evict_callback=object())
    mgr.append_final("auto-1", "real")
    mgr.append_final("auto-1", "")
    assert mgr.get_text("auto-1") == "real"


def test_append_non_string_is_noop():
    """Defensive: a transport bug passing bytes or None as text
    must not crash the manager."""
    mgr, _ = _new_mgr()
    mgr.acquire("auto-1", evict_callback=object())
    mgr.append_final("auto-1", None)  # type: ignore[arg-type]
    mgr.append_final("auto-1", b"bytes")  # type: ignore[arg-type]
    assert mgr.get_text("auto-1") == ""


# ── clear ────────────────────────────────────────────────────


def test_clear_empties_buffer_but_preserves_ownership():
    """clear is what commit / discard call. The operator is still
    attached and may keep dictating — text goes to '' but the
    record stays so the next append doesn't re-create from scratch."""
    mgr, _ = _new_mgr()
    mgr.acquire("auto-1", evict_callback=object())
    mgr.append_final("auto-1", "to be cleared")
    mgr.clear("auto-1")
    assert mgr.get_text("auto-1") == ""
    assert mgr.is_tracked("auto-1")  # ownership preserved
    mgr.append_final("auto-1", "post-clear dictation")
    assert mgr.get_text("auto-1") == "post-clear dictation"


def test_clear_unknown_is_noop():
    mgr, _ = _new_mgr()
    mgr.clear("never-acquired")  # no crash


# ── release ──────────────────────────────────────────────────


def test_release_drops_buffer_and_ownership():
    """release is what the 'end' control frame triggers. Operator
    deliberately ended → no TTL grace."""
    mgr, _ = _new_mgr()
    mgr.acquire("auto-1", evict_callback=object())
    mgr.append_final("auto-1", "discardable")
    mgr.release("auto-1")
    assert not mgr.is_tracked("auto-1")
    # Subsequent appends silently dropped — record is gone.
    mgr.append_final("auto-1", "ghost")
    assert mgr.get_text("auto-1") == ""


def test_release_unknown_is_noop():
    mgr, _ = _new_mgr()
    mgr.release("never-acquired")


# ── detach + TTL ─────────────────────────────────────────────


def test_detach_starts_ttl_countdown_buffer_preserved_within_window():
    mgr, clock = _new_mgr(ttl_ms=60_000)
    mgr.acquire("auto-1", evict_callback=object())
    mgr.append_final("auto-1", "preserved across blip")
    mgr.detach("auto-1")
    # 30s into the TTL window — buffer should still be present.
    clock.advance(30_000)
    assert mgr.is_tracked("auto-1")
    assert mgr.get_text("auto-1") == "preserved across blip"


def test_detach_buffer_evicted_after_ttl():
    mgr, clock = _new_mgr(ttl_ms=60_000)
    mgr.acquire("auto-1", evict_callback=object())
    mgr.append_final("auto-1", "should drop")
    mgr.detach("auto-1")
    # 61s past detach — over TTL.
    clock.advance(61_000)
    # Any public method prunes — get_text returns empty.
    assert mgr.get_text("auto-1") == ""
    assert not mgr.is_tracked("auto-1")


def test_detach_buffer_preserved_exactly_at_ttl():
    """Boundary: exactly at TTL is INSIDE the window (strict >).
    One ms past TTL evicts. Pins the comparator so a future >=
    refactor would break the test loudly."""
    mgr, clock = _new_mgr(ttl_ms=60_000)
    mgr.acquire("auto-1", evict_callback=object())
    mgr.append_final("auto-1", "edge case")
    mgr.detach("auto-1")
    clock.advance(60_000)
    assert mgr.is_tracked("auto-1")
    clock.advance(1)
    assert not mgr.is_tracked("auto-1")


def test_reattach_within_ttl_clears_detached_marker():
    """Reattach by acquire restores ownership AND keeps the buffer.
    detached_at_ms goes back to None so subsequent prunes don't
    evict the now-attached record."""
    mgr, clock = _new_mgr(ttl_ms=60_000)
    mgr.acquire("auto-1", evict_callback=object())
    mgr.append_final("auto-1", "survives blip")
    mgr.detach("auto-1")
    clock.advance(30_000)
    mgr.acquire("auto-1", evict_callback=object())  # reattach
    # Now advance way past the TTL — but the reattach reset the
    # marker, so the record should NOT be evicted.
    clock.advance(120_000)
    assert mgr.is_tracked("auto-1")
    assert mgr.get_text("auto-1") == "survives blip"


def test_detach_clears_evict_callback():
    """After detach, the prior WS is closed (or closing); a new
    connection within the TTL window should NOT receive a stale
    callback to evict a dead WS."""
    mgr, _ = _new_mgr()
    cb_a = object()
    mgr.acquire("auto-1", evict_callback=cb_a)
    mgr.detach("auto-1")
    result = mgr.acquire("auto-1", evict_callback=object())
    assert result.prior_evict_callback is None


# ── Cross-tab + TTL interaction ──────────────────────────────


def test_cross_tab_supersede_still_returns_prior_callback_when_attached():
    """Cross-tab guard fires when prior owner is still ATTACHED
    (not detached). Distinct from the reattach-after-detach case
    which has no live prior to supersede."""
    mgr, _ = _new_mgr()
    cb_a = object()
    cb_b = object()
    mgr.acquire("auto-1", evict_callback=cb_a)
    # No detach — second acquire is a same-tmux_name supersede.
    result = mgr.acquire("auto-1", evict_callback=cb_b)
    assert result.prior_evict_callback is cb_a


def test_distinct_tmux_names_dont_interfere():
    mgr, _ = _new_mgr()
    mgr.acquire("auto-1", evict_callback=object())
    mgr.acquire("auto-2", evict_callback=object())
    mgr.append_final("auto-1", "for one")
    mgr.append_final("auto-2", "for two")
    assert mgr.get_text("auto-1") == "for one"
    assert mgr.get_text("auto-2") == "for two"


def test_active_count_reflects_pruning():
    mgr, clock = _new_mgr(ttl_ms=10_000)
    mgr.acquire("a", evict_callback=object())
    mgr.acquire("b", evict_callback=object())
    mgr.acquire("c", evict_callback=object())
    assert mgr.active_count() == 3
    mgr.detach("a")
    mgr.detach("b")
    clock.advance(11_000)
    # a, b TTL-evicted; c still attached.
    assert mgr.active_count() == 1
    assert mgr.is_tracked("c")
