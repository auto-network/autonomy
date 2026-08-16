"""A maxed account is not a candidate, and a failed poll does not erase why.

Usage within a window is monotonic: it only rises until the window resets. So
a stored reading stays true as a lower bound until its own ``resets_at``,
regardless of how old it is or how badly a later poll went.

Both halves of the old behaviour threw that away, and they compounded. A maxed
account answers ``/usage`` with 429; the poller overwrote the reading that
proved it was maxed with ``status='unavailable'``; the selector read that as
usage-UNKNOWN, failed its "every token has fresh telemetry" gate, and fell back
to choosing uniformly at random — handing out the one account that could not
serve a session, at exactly the moment it could not.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agents import session_launcher
from tools.dashboard import harness_usage_settings as usage


NOW = int(time.time())


def _reading(used_percent, *, resets_in=3600, reached=None):
    return {
        "status": "ok",
        "account_id": "acct",
        "windows": {"long": {"used_percent": used_percent,
                             "window_minutes": 10080,
                             "resets_at": NOW + resets_in}},
        "rate_limit_reached_type": reached,
    }


# ── the reading itself ───────────────────────────────────────


def test_a_reading_holds_until_its_own_window_resets():
    assert usage.reading_still_valid(_reading(95), now_epoch=NOW)


def test_a_reading_stops_holding_once_the_window_has_reset():
    assert not usage.reading_still_valid(_reading(95, resets_in=-1), now_epoch=NOW)


def test_age_alone_does_not_invalidate_it():
    """The point of monotonicity: an old reading is still a lower bound."""
    ancient = _reading(95)
    assert usage.reading_still_valid(ancient, now_epoch=NOW + 3599)


def test_a_full_window_is_exhaustion():
    assert usage.is_exhausted(_reading(100), now_epoch=NOW)


def test_a_rate_limit_marker_is_exhaustion():
    assert usage.is_exhausted(_reading(80, reached="seven_day"), now_epoch=NOW)


def test_headroom_is_not_exhaustion():
    assert not usage.is_exhausted(_reading(12), now_epoch=NOW)


def test_an_unavailable_row_claims_nothing_either_way():
    unavailable = {"status": "unavailable", "windows": {}}
    assert not usage.reading_still_valid(unavailable, now_epoch=NOW)
    assert not usage.is_exhausted(unavailable, now_epoch=NOW)


# ── the selector ─────────────────────────────────────────────


def _token(key):
    return SimpleNamespace(key=key, payload={"raw_key": f"tok-{key}"})


@pytest.fixture
def picker(monkeypatch):
    """Two accounts: ``maxed`` is exhausted, ``fresh`` has headroom."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        session_launcher, "_setup_token_rows",
        lambda: [_token("maxed"), _token("fresh")])
    monkeypatch.setattr(session_launcher, "_credentials_rows", lambda: [])
    monkeypatch.setattr(
        session_launcher, "_claude_usage_rows",
        lambda: [dict(_reading(100), account_id="maxed"),
                 dict(_reading(10), account_id="fresh")])
    return session_launcher


def test_the_exhausted_account_is_never_chosen(picker):
    """Ten draws, because the bug's symptom was a coin flip."""
    picked = {picker._resolve_credentials_via_substrate(prefer_alias=None)["token"]
              for _ in range(10)}

    assert picked == {"tok-fresh"}


def test_it_is_still_chosen_when_it_is_the_only_one(monkeypatch):
    """Excluding the last account would refuse every launch outright.

    A maxed account that is all you have is still what you have; the launch
    failing on the provider's terms is more useful than never attempting it.
    """
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        session_launcher, "_setup_token_rows", lambda: [_token("maxed")])
    monkeypatch.setattr(session_launcher, "_credentials_rows", lambda: [])
    monkeypatch.setattr(
        session_launcher, "_claude_usage_rows",
        lambda: [dict(_reading(100), account_id="maxed")])

    assert session_launcher._resolve_credentials_via_substrate(prefer_alias=None)[
        "token"] == "tok-maxed"


def test_an_old_but_open_reading_still_drives_the_choice(monkeypatch):
    """The reading is hours old and its window has not reset — it decides."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(
        session_launcher, "_setup_token_rows",
        lambda: [_token("maxed"), _token("fresh")])
    monkeypatch.setattr(session_launcher, "_credentials_rows", lambda: [])
    monkeypatch.setattr(
        session_launcher, "_claude_usage_rows",
        lambda: [dict(_reading(100), account_id="maxed",
                      updated_at="2020-01-01T00:00:00Z"),
                 dict(_reading(10), account_id="fresh",
                      updated_at="2020-01-01T00:00:00Z")])

    picked = {session_launcher._resolve_credentials_via_substrate(prefer_alias=None)["token"]
              for _ in range(10)}

    assert picked == {"tok-fresh"}
