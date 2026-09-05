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


def test_fleet_member_materialises_synced_orgs_and_skips_onboarding(test_client, monkeypatch):
    """A joined fleet member must NOT be shown 'create/join an org' for orgs it
    already belongs to via the synced roster. page_index materialises the org DB
    stubs from the roster first, so the empty-state gate then sees a
    collaborative org and drops to the board."""
    from tools.dashboard import server
    import tools.network.fleet_sync_scheduler as sched
    monkeypatch.setattr(hb, "has_verified_harness", lambda: True)
    monkeypatch.setattr(server, "_has_personal_identity", lambda: True)
    monkeypatch.setattr(server, "_fleet_enrollment_first_render", lambda: None)
    # No org DB yet...
    state = {"has_org": False}
    monkeypatch.setattr(server, "_has_collaborative_org", lambda: state["has_org"])

    # ...until materialising the synced roster creates it.
    def fake_materialize():
        state["has_org"] = True
        return ["autonomy"]
    monkeypatch.setattr(sched, "materialize_org_scopes_from_roster", fake_materialize)

    r = test_client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/beads"


def test_fresh_machine_with_empty_roster_still_onboards(test_client, monkeypatch):
    """Materialisation is a no-op on a genuine fresh machine (empty roster), so
    the create/join-org step still shows correctly."""
    from tools.dashboard import server
    import tools.network.fleet_sync_scheduler as sched
    monkeypatch.setattr(hb, "has_verified_harness", lambda: True)
    monkeypatch.setattr(server, "_has_personal_identity", lambda: True)
    monkeypatch.setattr(server, "_has_collaborative_org", lambda: False)
    monkeypatch.setattr(server, "_fleet_enrollment_first_render", lambda: None)
    monkeypatch.setattr(sched, "materialize_org_scopes_from_roster", lambda: [])
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


def test_pending_fleet_enrollment_owns_first_render(test_client, monkeypatch):
    """The install's live request is shown before generic setup gates."""
    from tools.dashboard import server

    monkeypatch.setattr(hb, "has_verified_harness", lambda: False)
    monkeypatch.setattr(
        server,
        "_fleet_enrollment_first_render",
        lambda: {
            "status": "pending",
            "code": "A1B2 C3D4 E5F6 0718 192A 3B4C",
            "request_id_prefix": "123456789abc",
        },
    )
    r = test_client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "Confirm this machine" in r.text
    assert "Waiting for approval" in r.text
    assert "A1B2 C3D4 E5F6 0718 192A 3B4C" in r.text
    assert "Set up your assistant" not in r.text


def test_saved_fleet_delivery_renders_approved_after_restart(monkeypatch):
    """Root-delivery recovery does not require another anonymous API poll."""
    from tools.dashboard import server

    recovery = SimpleNamespace(
        request_id="12" * 32,
        verification_code="A1B2 C3D4 E5F6 0718 192A 3B4C",
    )

    class State:
        def latest_any(self):
            return recovery

        def load_delivery(self, request_id):
            assert request_id == recovery.request_id
            return object()

    from tools.network import fleet_enrollment_client
    monkeypatch.setattr(fleet_enrollment_client, "FleetJoinStateStore", State)

    assert server._fleet_enrollment_first_render() == {
        "status": "approved",
        "code": recovery.verification_code,
        "request_id_prefix": recovery.request_id[:12],
    }


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
    # The only POST advances an already-persisted Fleet request. Identity,
    # organization, session, and signing ceremonies remain composed flows.
    assert lowered.count("method: 'post'") == 1
    assert "/api/fleet/enrollment/local-resume" in lowered
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
    assert "/unlock?fleet=1&amp;next=%2Fwelcome%3Ffleet_sync%3D1" in TEMPLATE


def test_post_enrollment_sync_state_is_one_compact_binary_row(test_client):
    r = test_client.get("/welcome?fleet_sync=1")
    assert r.status_code == 200
    assert "Synchronizing with fleet" in r.text
    assert "Synchronization complete" in r.text
    assert "/api/fleet/enrollment/local-sync-status" in r.text
    assert 'ready && !fleetEnrollment && !fleetSync && step === 1' in r.text
    assert 'ready && !fleetEnrollment && !fleetSync && step === 2' in r.text
    assert 'ready && !fleetEnrollment && !fleetSync && step === 3' in r.text


def test_unlock_presents_fleet_ceremony_even_with_a_session(test_client, monkeypatch):
    """A fleet completion is a ROOT ceremony, not a login. ?fleet=1 must render
    the unlock screen even when a session already exists -- otherwise a signed-in
    operator is bounced to /welcome, which routes back to /unlock: an endless
    loop between 'log in' and 'do the ceremony' that never finishes the join."""
    from tools.dashboard import unlock_routes
    monkeypatch.setattr(unlock_routes, "human_auth_enrolled", lambda: True)
    monkeypatch.setattr(
        unlock_routes, "session_from_request", lambda request: object()
    )
    r = test_client.get(
        "/unlock?fleet=1&next=%2Fwelcome%3Ffleet_sync%3D1",
        follow_redirects=False,
    )
    assert r.status_code == 200

    # A plain login (no ceremony asked for) with a live session still
    # short-circuits to next -- we do not re-prompt an already-signed-in user.
    r2 = test_client.get("/unlock?next=%2F", follow_redirects=False)
    assert r2.status_code in (302, 307)
    assert r2.headers["location"] == "/"


def test_unlock_redirects_when_nothing_is_enrolled(test_client, monkeypatch):
    """No human auth at all -> nothing to unlock, even for a fleet ceremony."""
    from tools.dashboard import unlock_routes
    monkeypatch.setattr(unlock_routes, "human_auth_enrolled", lambda: False)
    monkeypatch.setattr(
        unlock_routes, "session_from_request", lambda request: None
    )
    r = test_client.get("/unlock?fleet=1&next=%2F", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/"


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
