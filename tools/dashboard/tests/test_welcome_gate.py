"""Tests for the Layer-1 onboarding empty-state gate (bead auto-inpkd).

The Welcome shell renders instead of the session UI while the machine lacks
a personal identity or a collaborative organization, and passes through
silently once both hold — the same server-side-decision pattern as the
Layer-0 harness bootstrap gate, one layer up.

Covers:
- the two detectors (identity, collaborative org) and their seed exclusion,
- the gate predicate, incl. fail-open on read error,
- the behavioral gate on GET / (open serves the shell; complete → /beads),
- the /welcome route always serving the shell,
- copy speaks interface not engineering (same guard as bootstrap/join),
- the shell is chrome only: no ceremony/crypto, no second implementation of
  create-org / join / launch — it composes the delivered flows.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.dashboard import harness_bootstrap as hb


TEMPLATE = (
    Path(__file__).resolve().parents[1] / "templates" / "welcome.html"
).read_text(encoding="utf-8")


def _orgs(*slugs):
    return [SimpleNamespace(slug=s, type="shared") for s in slugs]


# ── Detectors ─────────────────────────────────────────────────────────

def test_collaborative_org_excludes_personal_seed(monkeypatch):
    from tools.dashboard import server
    from tools.graph import org_ops

    monkeypatch.setattr(org_ops, "list_orgs", lambda *a, **k: _orgs("personal"))
    assert server._has_collaborative_org() is False

    monkeypatch.setattr(org_ops, "list_orgs",
                        lambda *a, **k: _orgs("personal", "acme"))
    assert server._has_collaborative_org() is True


def test_collaborative_org_false_when_empty(monkeypatch):
    from tools.dashboard import server
    from tools.graph import org_ops
    monkeypatch.setattr(org_ops, "list_orgs", lambda *a, **k: [])
    assert server._has_collaborative_org() is False


def test_personal_identity_signal(monkeypatch):
    from tools.dashboard import server
    from tools.dashboard import identity_routes

    monkeypatch.setattr(identity_routes, "_personal_member", lambda: None)
    assert server._has_personal_identity() is False

    monkeypatch.setattr(identity_routes, "_personal_member",
                        lambda: SimpleNamespace(payload={}))
    assert server._has_personal_identity() is False

    monkeypatch.setattr(
        identity_routes, "_personal_member",
        lambda: SimpleNamespace(payload={"armored_private_key": "armor"}),
    )
    assert server._has_personal_identity() is True


# ── Gate predicate ────────────────────────────────────────────────────

@pytest.mark.parametrize("ident,org,open_", [
    (False, False, True),    # fresh
    (False, True, True),     # no identity yet
    (True, False, True),     # identity but no org (invite-join or solo)
    (True, True, False),     # complete → gate closed
])
def test_gate_predicate(monkeypatch, ident, org, open_):
    from tools.dashboard import server
    monkeypatch.setattr(server, "_has_personal_identity", lambda: ident)
    monkeypatch.setattr(server, "_has_collaborative_org", lambda: org)
    assert server._welcome_gate_open() is open_


def test_gate_fails_open_on_error(monkeypatch):
    """A substrate hiccup must never trap the dashboard behind onboarding."""
    from tools.dashboard import server

    def boom():
        raise RuntimeError("orgs dir unreadable")

    monkeypatch.setattr(server, "_has_personal_identity", boom)
    monkeypatch.setattr(server, "_has_collaborative_org", lambda: True)
    assert server._welcome_gate_open() is False


# ── Behavioral gate on GET / ──────────────────────────────────────────

def test_empty_state_serves_welcome(test_client, monkeypatch):
    """No identity → GET / serves the Welcome shell, not the session UI."""
    from tools.dashboard import server
    monkeypatch.setattr(hb, "has_verified_harness", lambda: True)
    monkeypatch.setattr(server, "_has_personal_identity", lambda: False)
    monkeypatch.setattr(server, "_has_collaborative_org", lambda: True)
    r = test_client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "Three steps and this machine is yours." in r.text


def test_identity_without_org_serves_welcome(test_client, monkeypatch):
    from tools.dashboard import server
    monkeypatch.setattr(hb, "has_verified_harness", lambda: True)
    monkeypatch.setattr(server, "_has_personal_identity", lambda: True)
    monkeypatch.setattr(server, "_has_collaborative_org", lambda: False)
    r = test_client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "Welcome" in r.text


def test_complete_state_falls_through(test_client, monkeypatch):
    """Identity + a collaborative org → GET / falls through to the board."""
    from tools.dashboard import server
    monkeypatch.setattr(hb, "has_verified_harness", lambda: True)
    monkeypatch.setattr(server, "_has_personal_identity", lambda: True)
    monkeypatch.setattr(server, "_has_collaborative_org", lambda: True)
    r = test_client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/beads"


def test_bootstrap_gate_precedes_welcome(test_client, monkeypatch):
    """An unverified harness still wins — bootstrap is Layer 0."""
    from tools.dashboard import server
    monkeypatch.setattr(hb, "has_verified_harness", lambda: False)
    # Even with the empty-state condition held, the harness gate is first.
    monkeypatch.setattr(server, "_has_personal_identity", lambda: False)
    r = test_client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "Set up your assistant" in r.text


def test_welcome_route_always_served(test_client):
    r = test_client.get("/welcome")
    assert r.status_code == 200
    assert "Three steps and this machine is yours." in r.text


# ── Copy: interface, not engineering ──────────────────────────────────

def test_screens_speak_interface_not_engineering():
    """User-facing copy states what to do, never how the system works.

    Same guard the bootstrap and invitation pages carry, extended to the
    onboarding shell (UI copy rule, ruled repeatedly).
    """
    visible = TEMPLATE[TEMPLATE.index("<body"):TEMPLATE.index("</main>")].lower()
    for leaked in ("this page", "own tool", "notices", "no-op", "oauth",
                   "run its command", "probe", "verif", "endpoint",
                   "record", "gate"):
        assert leaked not in visible, leaked


# ── Chrome only: no ceremony, no second implementation ────────────────

def test_shell_carries_no_ceremony_code():
    """The shell sequences; it never re-implements a ceremony.

    Grep-level acceptance (bead auto-inpkd): no key generation, no armor, no
    WebAuthn, no org/session creation body — those live in the one delivered
    path for each action.
    """
    lowered = TEMPLATE.lower()
    for banned in ("generateed25519", "armorseed", "navigator.credentials",
                   "publickeycredential", "/api/identity/personal",
                   "/api/identity/passkey", "deriveorgslug"):
        assert banned not in lowered, banned
    # No create-org / join / session POST is issued from the shell itself.
    assert "method: 'post'" not in lowered
    assert '"post"' not in lowered


def test_shell_composes_the_delivered_flows():
    """Each step links into the single delivered path, never a duplicate."""
    # identity → the delivered ceremony
    assert "AutonomyOnboarding" in TEMPLATE
    # create → the delivered create-org screen
    assert "AutonomyCreateOrg" in TEMPLATE
    # join → the delivered stepped accept flow (same target the profile menu uses)
    assert "/network/join" in TEMPLATE
    # workspace → the session UI
    assert "/beads" in TEMPLATE
    # and it reads the same status the profile menu reads (one source of truth)
    assert "/api/identity/status" in TEMPLATE
    assert "/api/orgs" in TEMPLATE


def test_invitation_context_detected_and_carried():
    """Arrived-via-invitation: detect URL context, carry it to /network/join."""
    # detection mirrors network-join.js (search or hash present)
    assert "location.search" in TEMPLATE and "location.hash" in TEMPLATE
    # the bearer-bearing fragment rides along untouched to the accept flow
    assert "'/network/join' + location.search + location.hash" in TEMPLATE


def test_design_variants_present():
    """The three committed design states render (fresh / mid / ready)."""
    assert "step === 1" in TEMPLATE   # fresh
    assert "step === 2" in TEMPLATE   # organization
    assert "step === 3" in TEMPLATE   # ready
    assert "Go to your workspace" in TEMPLATE
