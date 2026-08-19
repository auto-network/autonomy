"""The generic Settings readers refuse a caller with no credential.

auto-1wwpf.10. A guard on a secret's DEDICATED endpoint contains nothing while
generic readers take an arbitrary ``set_id``: an unauthenticated
``GET /api/graph/settings/autonomy.commit.signing-key/default`` returned 200
with ``armored_private_key`` — with no header at all, so not even by naming an
org.

THE PREDICATE IS "AUTHENTICATED", NOT "THE OPERATOR", and the difference is
the whole point of this file. An earlier attempt demanded global operator
authority and 401'd every agent on the fleet: agents read Settings constantly
as normal operation and carry an ORG bearer, never the operator's cookie. So
the test that matters most here is not the refusal — it is
``test_an_org_bearer_still_reads_settings``, which is the case that broke.

What an authenticated caller may then READ is a different question, answered
by the token forcing the org and by each schema's ``@home`` and
``@publication_band``. Six secret-bearing sets are pinned ``max=raw`` and are
structurally never read-through-able, which is why nothing here keeps a list
of secret set ids: a newly registered secret is protected by declaring its
band, not by someone remembering to edit a constant.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from tools.graph.db import GraphDB

# Every generic reader, by shape rather than by name: each takes an arbitrary
# set id, or dumps every set, so none of them can be contained by guarding one
# secret's own endpoint.
GENERIC_READERS = [
    ("list", "/api/graph/settings/autonomy.commit.signing-key"),
    ("get_by_key", "/api/graph/settings/autonomy.commit.signing-key/default"),
    ("chain", "/api/graph/settings/autonomy.commit.signing-key/default/chain"),
    ("diag_stats", "/api/diag/settings"),
    ("diag_sets", "/api/diag/settings/sets"),
    ("diag_set_detail", "/api/diag/settings/sets/autonomy.commit.signing-key"),
]

MARKER = "MARKER-PRIVATE-KEY-DO-NOT-SERVE-4c1f7a"


@pytest.fixture
def gate_enforcing(monkeypatch):
    """An enrolled dashboard with the recovery switch off.

    The guard stands down while the human gate is deliberately open, so
    without this a refusal test asserts nothing: a test environment is
    unenrolled, the gate admits everyone, and every call would pass for a
    reason that has nothing to do with credentials.
    """
    from tools.dashboard import unlock_routes
    monkeypatch.delenv("DASHBOARD_AUTH", raising=False)
    monkeypatch.setattr(unlock_routes, "human_auth_enrolled", lambda: True)
    assert unlock_routes.gate_enforced() is True


@pytest.fixture
def seeded(test_app, tmp_path, monkeypatch):
    """A signing-key row with fake marker material, forced past validation.

    Written directly rather than through ``add_setting``: this suite is about
    what the READ path serves, and forcing the row in is how a read guard is
    tested independently of whichever write guard happens to exist.
    """
    import json
    import sqlite3
    orgs = tmp_path / "orgs"
    orgs.mkdir(exist_ok=True)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    GraphDB.close_all_pooled()
    if not (orgs / "personal.db").exists():
        GraphDB.create_org_db("personal", root=tmp_path).close()
    conn = sqlite3.connect(orgs / "personal.db")
    try:
        conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
            "publication_state, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("probe-signing-key", "autonomy.commit.signing-key", 1, "default",
             json.dumps({"armored_private_key": MARKER}), "raw",
             "2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()
    yield test_app
    GraphDB.close_all_pooled()


# ── the refusal ──────────────────────────────────────────────


@pytest.mark.parametrize("name,url", GENERIC_READERS)
def test_no_generic_reader_serves_a_caller_without_a_credential(
    gate_enforcing, seeded, name, url,
):
    with TestClient(seeded) as client:
        response = client.get(url)

    assert response.status_code == 401, (
        f"{name} served an unauthenticated caller with "
        f"{response.status_code}; every generic reader takes an arbitrary "
        f"set id, so this one is the whole containment"
    )
    assert MARKER not in response.text


def test_naming_an_org_does_not_substitute_for_a_credential(
    gate_enforcing, seeded,
):
    """`X-Graph-Org` is a scope selector, never an authenticator.

    The live leak needed no header at all, so this is not the vector — but a
    guard that accepted a named org as evidence of anything would reopen it
    from a different direction.
    """
    with TestClient(seeded) as client:
        response = client.get(
            "/api/graph/settings/autonomy.commit.signing-key/default",
            headers={"X-Graph-Org": "personal"},
        )

    assert response.status_code == 401
    assert MARKER not in response.text


# ── THE ONE THAT MATTERS: the case the first attempt broke ───


def test_an_org_bearer_still_reads_settings(gate_enforcing, seeded, monkeypatch):
    """An org-bound agent is authenticated and must keep reading Settings.

    This is the regression that took `graph set read` down fleet-wide for
    every agent. It asserts through a REAL principal rather than by patching
    the guard away — a fixture that disables the thing under test would have
    passed while the fleet was broken.
    """
    from tools.dashboard import api_auth

    monkeypatch.setattr(
        api_auth, "principal_from_request",
        lambda request: api_auth.ApiPrincipal(
            api_auth.ApiPrincipalKind.ORG_SESSION,
            subject="auto-test", org="anchore",
        ),
    )
    with TestClient(seeded) as client:
        response = client.get("/api/graph/settings/autonomy.workspace.mount")

    assert response.status_code != 401, (
        "an org-bound agent was refused; agents read Settings as ordinary "
        "operation and hold an org bearer, never the operator's cookie"
    )


def test_the_operator_cookie_still_reads_settings(
    gate_enforcing, seeded, monkeypatch,
):
    from tools.dashboard import api_auth

    monkeypatch.setattr(
        api_auth, "principal_from_request",
        lambda request: api_auth.ApiPrincipal(
            api_auth.ApiPrincipalKind.OPERATOR_COOKIE, subject="sid-test",
        ),
    )
    with TestClient(seeded) as client:
        response = client.get("/api/graph/settings/autonomy.workspace.mount")

    assert response.status_code != 401


# ── the gate stand-down ──────────────────────────────────────


def test_an_open_gate_serves_rather_than_refusing(seeded, monkeypatch):
    """While the gate admits cookie-less browsers, refusing here contradicts it.

    Not a weakening: the gate is what decides whether this dashboard is open,
    and both of its open states — the recovery switch, and nothing enrolled —
    are ones a real deployment sits in.
    """
    from tools.dashboard import unlock_routes
    monkeypatch.setenv("DASHBOARD_AUTH", "off")
    assert unlock_routes.gate_enforced() is False

    with TestClient(seeded) as client:
        response = client.get("/api/graph/settings/autonomy.workspace.mount")

    assert response.status_code != 401


# ── no list of secret sets ───────────────────────────────────


def test_the_readers_keep_no_allowlist_of_set_ids():
    """Protection is declared on the schema, never enumerated here.

    A hand-kept list treats OMISSION as public, so a newly registered secret
    set leaks from the moment it is declared until somebody remembers it.
    `@publication_band(max="raw")` makes that structural instead.
    """
    from tools.dashboard import server

    source = open(server.__file__.replace(".pyc", ".py")).read()
    for banned in ("PUBLIC_SETTING_SET_IDS", "settings_read_policy"):
        assert banned not in source, (
            f"{banned} is back: a per-set allowlist in the readers is the "
            f"shape auto-eptjy's publication bands replaced"
        )


def test_every_secret_bearing_set_is_band_pinned():
    """The structural guarantee this guard leans on, asserted where it is used."""
    from tools.graph import schemas

    for set_id in (
        "autonomy.secure.setting",
        "autonomy.commit.signing-key",
        "autonomy.credential-file",
        "autonomy.vault.secret",
        "dashboard.claude.credentials",
        "dashboard.claude.setup_tokens",
    ):
        band = next(
            (b for rev in range(1, 6)
             if (b := schemas.declared_band(set_id, rev)) is not None),
            None,
        )
        assert band is not None and band[1] == "raw", (
            f"{set_id} declares {band!r} rather than max=raw, so it can reach "
            f"a peer-visible state and this guard is no longer sufficient"
        )
