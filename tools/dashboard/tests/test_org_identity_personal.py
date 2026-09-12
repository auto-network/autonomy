"""Personal-profile projection into ``resolve_org_identity("personal")``.

auto-vlt7j.3: the shared org-identity resolver overlays the effective Personal
profile (display name, explicit-or-derived initials, bounded compact avatar)
onto the ``personal`` slug ONLY, so every session/org consumer that already
reads ``resolve_org_identity`` / ``resolve_session_org`` shows the person rather
than generic ``Personal`` / ``P`` branding — with no consumer-specific code.

Pins:

* stored profile with avatar → name + initials + avatar overlaid;
* stored profile without avatar → name + initials, no branding cleared;
* explicit initials override wins; derived initials otherwise;
* personal root but no profile row → unpersisted root-name fallback, no write;
* neither profile nor root → seeded/generated ``personal`` / ``P`` / no avatar;
* rename, initials change, avatar replace, and avatar removal are visible on the
  FIRST resolver read after a successful mutation, without process restart;
* unrelated org cache entries are NOT invalidated by a Personal mutation;
* host sessions and ``resolve_session_org`` receive the projected object;
* shared-org and unknown-slug outputs remain byte-for-byte unchanged.

Decided by ``graph://4f9e881c-a9`` §7.
"""
from __future__ import annotations

import base64

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import identity_routes, org_identity, personal_profile
from tools.graph.db import GraphDB

# Reuse the real personal-root ceremony from the identity-routes suite.
from tools.dashboard.tests.test_identity_routes import (
    HOST,
    _store_identity,
)
from tools.network.idkit import KeyPair

ORG = "idorg"

_AVATAR_A = "data:image/webp;base64," + base64.b64encode(b"A" * 40).decode()
_AVATAR_B = "data:image/webp;base64," + base64.b64encode(b"B" * 40).decode()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Personal store + a DIFFERENT ambient org, with the identity routes wired
    so the real PATCH/avatar mutation paths run, and the org-identity caches
    cleared so each test's first resolve is honest."""
    from tools.dashboard.dao import identity_sessions

    GraphDB.close_all_pooled()
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.create_org_db(
        "personal", type_="personal", path=orgs_dir / "personal.db").close()
    # A different ambient org proves personal routing / projection is immune.
    GraphDB.create_org_db(
        ORG, type_="shared", path=orgs_dir / f"{ORG}.db").close()
    monkeypatch.setenv("GRAPH_ORG", ORG)
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET_FILE",
                       str(tmp_path / "session.secret"))
    monkeypatch.setenv("DASHBOARD_IDENTITY_SESSION_DB",
                       str(tmp_path / "identity-sessions.db"))
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    identity_routes._pending.clear()
    identity_sessions.reset_for_tests()
    # Drop any cache built by an earlier test in this process.
    org_identity._identity_cached.cache_clear()
    org_identity.invalidate_personal_identity()
    with TestClient(Starlette(routes=identity_routes.ROUTES),
                    base_url=f"https://{HOST}") as client:
        client._orgs_dir = orgs_dir  # type: ignore[attr-defined]
        yield client
    identity_routes._pending.clear()
    identity_sessions.reset_for_tests()
    org_identity._identity_cached.cache_clear()
    GraphDB.close_all_pooled()


@pytest.fixture
def root():
    return KeyPair.generate()


def _personal():
    return org_identity.resolve_org_identity("personal")


# ── no identity → seeded/generated personal ────────────────────


def test_no_identity_keeps_generated_personal(env):
    identity = _personal()
    assert identity["slug"] == "personal"
    assert identity["name"] == "personal"           # generated (slug)
    assert identity["initial"] == "P"               # generated
    assert identity["favicon"] is None
    assert identity["icon_data_uri"] is None
    assert identity["resolved"] is True


# ── root fallback (no profile row, no write) ───────────────────


def test_root_name_fallback_without_writing(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    identity = _personal()
    assert identity["name"] == "Jeremy Spilman"
    assert identity["initial"] == "JS"              # derived from root name
    assert identity["favicon"] is None
    assert identity["icon_data_uri"] is None
    # A resolve must never persist a profile row.
    assert personal_profile.profile_member() is None


# ── stored profile without avatar ──────────────────────────────


def test_stored_profile_without_avatar(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    assert env.patch("/api/identity/profile",
                     json={"display_name": "Jer Doe"}).status_code == 200
    identity = _personal()
    assert identity["name"] == "Jer Doe"
    assert identity["initial"] == "JD"              # derived from new name
    # No avatar → no unrelated branding invented.
    assert identity["favicon"] is None
    assert identity["icon_data_uri"] is None


def test_explicit_initials_override(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    assert env.patch("/api/identity/profile",
                     json={"initials": "JX"}).status_code == 200
    assert _personal()["initial"] == "JX"


# ── stored profile with avatar ─────────────────────────────────


def test_stored_profile_with_avatar(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    env.patch("/api/identity/profile", json={"display_name": "Jer"})
    personal_profile.set_avatar("0192a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b", _AVATAR_A)
    identity = _personal()
    assert identity["name"] == "Jer"
    # A valid compact avatar replaces BOTH favicon and icon_data_uri.
    assert identity["favicon"] == _AVATAR_A
    assert identity["icon_data_uri"] == _AVATAR_A


# ── freshness on first read after mutation (no restart) ────────


def test_rename_is_fresh_on_next_read(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    assert _personal()["name"] == "Jeremy Spilman"
    assert env.patch("/api/identity/profile",
                     json={"display_name": "Renamed Person"}).status_code == 200
    # First resolve after the mutation already reflects the rename.
    identity = _personal()
    assert identity["name"] == "Renamed Person"
    assert identity["initial"] == "RP"


def test_initials_change_is_fresh_on_next_read(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    assert _personal()["initial"] == "JS"
    env.patch("/api/identity/profile", json={"initials": "ZZ"})
    assert _personal()["initial"] == "ZZ"
    # Clearing the override restores derivation, also fresh.
    env.patch("/api/identity/profile", json={"initials": ""})
    assert _personal()["initial"] == "JS"


def test_avatar_replace_and_removal_are_fresh(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    personal_profile.set_avatar("aaaaaaaa-c3d4-7e5f-8a9b-0c1d2e3f4a5b", _AVATAR_A)
    assert _personal()["favicon"] == _AVATAR_A
    # Replace.
    personal_profile.set_avatar("bbbbbbbb-c3d4-7e5f-8a9b-0c1d2e3f4a5b", _AVATAR_B)
    assert _personal()["icon_data_uri"] == _AVATAR_B
    # Remove → back to initials, no branding left behind.
    personal_profile.clear_avatar()
    identity = _personal()
    assert identity["favicon"] is None
    assert identity["icon_data_uri"] is None
    assert identity["initial"] == "JS"


# ── unrelated org identities are not invalidated ───────────────


def test_personal_mutation_does_not_invalidate_other_orgs(env, root):
    # Resolve an unrelated slug and remember its object.
    before = org_identity.resolve_org_identity("anchore")
    # A Personal mutation bumps only the Personal generation.
    _store_identity(env, root, name="Jeremy Spilman")
    env.patch("/api/identity/profile", json={"display_name": "Someone"})
    after = org_identity.resolve_org_identity("anchore")
    assert after == before                       # byte-for-byte identical


# ── host-session routing receives the projection ───────────────


def test_host_session_gets_projected_personal(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    env.patch("/api/identity/profile", json={"display_name": "Host Person"})
    # A host session maps to the personal slug (session_org_slug).
    org = org_identity.resolve_session_org({"session_type": "host"})
    assert org["slug"] == "personal"
    assert org["name"] == "Host Person"
    assert org["initial"] == "HP"


# ── shared-org / unknown-slug byte-for-byte unchanged ──────────


def test_shared_org_and_unknown_unaffected_by_personal_generation(env, root):
    shared_before = org_identity.resolve_org_identity("anchore")
    unknown_before = org_identity.resolve_org_identity(None)
    # Mutate the Personal profile a few times.
    _store_identity(env, root, name="Jeremy Spilman")
    env.patch("/api/identity/profile", json={"display_name": "X"})
    env.patch("/api/identity/profile", json={"initials": "QQ"})
    assert org_identity.resolve_org_identity("anchore") == shared_before
    assert org_identity.resolve_org_identity(None) == unknown_before
    # And no personal fields ever leaked into a non-personal identity.
    assert "personal" not in shared_before["slug"]
