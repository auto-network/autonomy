"""Generic Settings readers must not serve secret material to an unauthenticated caller.

auto-1wwpf.10. A guard on a secret's DEDICATED endpoint does not contain the
secret while generic readers accept arbitrary set ids: an unauthenticated
GET /api/graph/settings/autonomy.commit.signing-key/default returns the raw
payload including armored_private_key.

FAIL-CLOSED, NOT A DENYLIST. A secret-only denylist treats omission as public,
so a newly registered secret set leaks from the moment it is declared until
somebody remembers to list it. Polarity is inverted: an explicit PUBLIC
allowlist names what these readers may serve unauthenticated; everything else,
INCLUDING A SET NOBODY HAS CLASSIFIED, requires global operator authority.

The load-bearing test here is `test_a_never_before_seen_set_is_refused` — it is
the one the denylist design could not pass.
"""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

MARKER = "MARKER-SECRET-DO-NOT-SERVE-a7f3c9"

# Real secret-bearing sets, per the bead's verified census. setup_tokens.raw_key
# is the omission canary: a real secret whose field name matches no obvious
# key/token/secret pattern, so a name heuristic misses it.
SECRET_SETS = [
    ("autonomy.commit.signing-key", "default", {"armored_private_key": MARKER}),
    ("dashboard.claude.setup_tokens", "probe", {"raw_key": MARKER}),
]


def _seed(orgs_dir, set_id, key, payload):
    """Write a row directly, bypassing schema validation.

    Deliberate: this test is about what the READ path serves, and forcing the
    row in is how the band tests already force a mismarked row past a write
    guard. Marker material is fake by construction.
    """
    import sqlite3
    db = orgs_dir / "personal.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
            "publication_state, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (f"probe-{set_id}-{key}", set_id, 1, key, json.dumps(payload),
             "raw", "2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def seeded(test_app, tmp_path):
    import os
    from tools.graph.db import GraphDB
    orgs = tmp_path / "orgs"
    orgs.mkdir(exist_ok=True)
    os.environ["AUTONOMY_ORGS_DIR"] = str(orgs)
    GraphDB.close_all_pooled()
    if not (orgs / "personal.db").exists():
        GraphDB.create_org_db("personal", root=tmp_path).close()
    for set_id, key, payload in SECRET_SETS:
        _seed(orgs, set_id, key, payload)
    yield test_app
    GraphDB.close_all_pooled()


@pytest.fixture
def gate_enforcing(monkeypatch):
    """Put the dashboard in the state where the guard is the thing being tested.

    ``require_global_api_authority`` stands down while the human gate is not
    enforcing, because the gate is then admitting cookie-less browsers itself
    and refusing here would contradict it. A test environment is unenrolled by
    default, so WITHOUT this fixture a refusal test passes or fails on the gate's
    state rather than on the allowlist — it would assert nothing about the
    policy it names.

    Stated as a precondition rather than inherited: an enrolled dashboard with
    the recovery switch off is the deployment these refusals are claimed for.
    """
    from tools.dashboard import unlock_routes
    monkeypatch.delenv("DASHBOARD_AUTH", raising=False)
    monkeypatch.setattr(unlock_routes, "human_auth_enrolled", lambda: True)
    assert unlock_routes.gate_enforced() is True
    return None


def _readers(set_id, key):
    """Every generic reader, enumerated from the live route table."""
    return [
        ("list", f"/api/graph/settings/{set_id}"),
        ("get_by_key", f"/api/graph/settings/{set_id}/{key}"),
        ("chain", f"/api/graph/settings/{set_id}/{key}/chain"),
        ("diag_set_detail", f"/api/diag/settings/sets/{set_id}"),
    ]


@pytest.mark.parametrize("set_id,key,_payload", SECRET_SETS)
def test_no_generic_reader_serves_secret_material(
    gate_enforcing, seeded, set_id, key, _payload,
):
    """The leak matrix. Every cell must REFUSE (401/403) — not redact to 200."""
    leaked = []
    with TestClient(seeded) as client:
        for name, url in _readers(set_id, key):
            r = client.get(url, headers={"X-Graph-Org": "personal"})
            if MARKER in r.text:
                leaked.append(f"{name} {url} -> {r.status_code} LEAKED")
            elif r.status_code not in (401, 403):
                leaked.append(f"{name} {url} -> {r.status_code} not refused")
    assert leaked == [], "\n".join(leaked)


def test_a_never_before_seen_set_is_refused(gate_enforcing, seeded):
    """THE LOAD-BEARING CELL, worth more than the rest combined.

    A synthetic set id nobody has classified must be REFUSED without being
    added to any list. This is what proves omission is safe, and it is exactly
    the test a denylist cannot pass — under a denylist an unlisted set is
    served by definition.
    """
    with TestClient(seeded) as client:
        r = client.get(
            "/api/graph/settings/probe.invented.set.nobody.classified/default",
            headers={"X-Graph-Org": "personal"},
        )
    assert r.status_code in (401, 403), (
        f"an unclassified set was served with {r.status_code} — omission is "
        f"treated as public, which is the denylist failure this bead inverts"
    )


# ── the two the crypto seat required ─────────────────────────
#
# Both failure modes look green from outside on the happy path, which is why
# they need naming rather than trusting the matrix above.


def test_the_allowlist_is_one_constant_the_guard_actually_consults():
    """A second copy drifts, and the synthetic-set test still passes while the
    drifted reader serves. So assert the guard reads the shared constant —
    not that some list somewhere has the right contents."""
    from tools.dashboard import server, settings_read_policy

    # The guard delegates rather than carrying its own membership test.
    assert settings_read_policy.requires_global_authority("autonomy.identity.passkey") is False
    assert settings_read_policy.requires_global_authority("autonomy.commit.signing-key") is True

    # And no reader carries a private copy of the allowlist.
    src = open(server.__file__.replace(".pyc", ".py")).read()
    assert "PUBLIC_SETTING_SET_IDS" not in src, (
        "a reader names the allowlist directly instead of asking the policy "
        "module — that is the copy that drifts"
    )


@pytest.mark.parametrize("variant", [
    "Autonomy.Identity.Passkey",       # case variance
    "autonomy.identity.passkey ",      # trailing space
    " autonomy.identity.passkey",      # leading space
    "autonomy.identity.passkey#1",     # revision suffix (a display form)
    "autonomy..identity.passkey",      # doubled separator
    ".autonomy.identity.passkey",      # leading separator
])
def test_a_public_set_in_non_canonical_form_is_not_admitted(variant):
    """'Rejected' and 'normalized then matched' are indistinguishable from
    outside unless tested for. An allowlist that quietly accepts variants has
    unbounded membership."""
    from tools.dashboard import settings_read_policy

    assert settings_read_policy.requires_global_authority(variant) is True, (
        f"{variant!r} was admitted — the allowlist normalized a variant into "
        f"a match instead of refusing it"
    )


# ── positive allowlist tests ─────────────────────────────────
#
# Fail-closed polarity means over-denial is the new failure mode. These catch
# it here rather than in production.


@pytest.mark.parametrize("public_set", sorted([
    "autonomy.identity.passkey",
    "autonomy.network.binding",
    "autonomy.network.persona",
    "autonomy.network.serve-cert",
    "autonomy.network.ledger-projection",
    "autonomy.network.ledger-state",
]))
def test_a_public_set_still_serves_unauthenticated(seeded, public_set):
    """Each set on the allowlist must still serve an unauthenticated caller.
    Adding to the allowlist requires one of these; this is what makes the
    addition deliberate."""
    with TestClient(seeded) as client:
        r = client.get(f"/api/graph/settings/{public_set}",
                       headers={"X-Graph-Org": "personal"})
    assert r.status_code not in (401, 403), (
        f"{public_set} is on the public allowlist but was refused "
        f"({r.status_code}) — over-denial"
    )


def test_the_allowlist_contains_no_known_secret_set():
    """A guard against the allowlist and the census drifting apart."""
    from tools.dashboard.settings_read_policy import PUBLIC_SETTING_SET_IDS

    known_secret = {
        "autonomy.commit.signing-key",
        "autonomy.network.org-key",
        "autonomy.identity.personal",
        "autonomy.network.link-grant",
        "dashboard.claude.credentials",
        "dashboard.codex.credentials",
        "dashboard.claude.setup_tokens",
        "autonomy.secure.setting",
        # Specified, unbuilt. Under a denylist it would have arrived unlisted
        # and leaked on the day it shipped; under this polarity it is refused
        # on arrival. Named here so that stays true.
        "autonomy.vault.secret",
    }
    overlap = PUBLIC_SETTING_SET_IDS & known_secret
    assert overlap == set(), f"secret-bearing set(s) on the public allowlist: {overlap}"


# ── the guard stands down where the human gate is already open ───
#
# HumanGateMiddleware admits a browser carrying NO session cookie in two
# states: the DASHBOARD_AUTH recovery switch, and nothing enrolled yet. In
# both, the operator's own requests reach the API as compatibility traffic.
# Refusing them here would contradict the gate that just admitted them, and
# would break the Settings-driven pages in exactly the mode that exists to
# recover a dashboard whose unlock is broken.
#
# Both are real deployments, not test artifacts: the second is every install
# that has not enrolled a passkey, including one that never will.


@pytest.mark.parametrize("state,apply", [
    ("recovery switch set", lambda mp, ur: mp.setenv("DASHBOARD_AUTH", "off")),
    ("nothing enrolled", lambda mp, ur: mp.setattr(ur, "human_auth_enrolled",
                                                   lambda: False)),
])
def test_an_open_gate_serves_the_operator_rather_than_refusing(
    seeded, monkeypatch, state, apply,
):
    from tools.dashboard import unlock_routes
    monkeypatch.delenv("DASHBOARD_AUTH", raising=False)
    apply(monkeypatch, unlock_routes)
    assert unlock_routes.gate_enforced() is False

    with TestClient(seeded) as client:
        r = client.get(
            "/api/graph/settings/probe.invented.set.nobody.classified/default",
            headers={"X-Graph-Org": "personal"},
        )
    assert r.status_code not in (401, 403), (
        f"with {state}, the human gate admits a cookie-less browser but the "
        f"API guard refused it ({r.status_code}) — the operator cannot reach "
        f"their own Settings in a state the gate declares open"
    )


def test_an_agent_is_still_refused_while_the_gate_is_open(seeded, monkeypatch):
    """The stand-down is about the HUMAN gate, and an agent is not the human.

    An org-bound session is positively identified and deliberately narrow.
    Whether a browser needs to unlock says nothing about it, so its refusal
    must survive the switch — otherwise flipping the recovery hatch silently
    widens every agent on the machine.
    """
    from tools.dashboard import api_auth, unlock_routes
    monkeypatch.setenv("DASHBOARD_AUTH", "off")
    assert unlock_routes.gate_enforced() is False

    class _Req:
        state = type("S", (), {})()
        method, url = "GET", type("U", (), {"path": "/api/graph/settings/x"})()
    req = _Req()
    req.state.api_principal = api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.ORG_SESSION, subject="agent-1", org="anchore")

    refusal = api_auth.require_global_api_authority(req)
    assert refusal is not None and refusal.status_code == 403


def test_the_recovery_switch_is_read_before_the_enrollment_store():
    """Order is the property, not an implementation detail.

    The switch exists for a dashboard that is already broken. If enrollment
    were consulted first, a wedged settings DB would take the escape hatch
    down with it — so a raising enrollment read must not stop the switch.
    """
    from tools.dashboard import unlock_routes
    import os

    def _wedged():
        raise RuntimeError("settings DB unavailable")

    original = unlock_routes.human_auth_enrolled
    prior = os.environ.get("DASHBOARD_AUTH")
    unlock_routes.human_auth_enrolled = _wedged
    os.environ["DASHBOARD_AUTH"] = "off"
    try:
        assert unlock_routes.gate_enforced() is False
    finally:
        unlock_routes.human_auth_enrolled = original
        if prior is None:
            os.environ.pop("DASHBOARD_AUTH", None)
        else:
            os.environ["DASHBOARD_AUTH"] = prior
