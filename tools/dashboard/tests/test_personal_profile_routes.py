"""Personal profile service + routes (auto-vlt7j.1).

The mutable Personal profile (``autonomy.user#1/default``) behind
``GET`` / ``PATCH /api/identity/profile`` and its extension of identity
status. Pins:

* no-identity → ``profile: null``;
* a root but no profile row → an UNPERSISTED synthesized fallback (root
  display name, blank biography, derived initials, no avatar) that never
  writes;
* partial PATCH merges and preserves omitted fields;
* explicit-versus-derived initials and blank-initials-restores-derivation;
* biography clearing;
* avatar-field preservation across a text PATCH;
* unknown / avatar-field rejection on PATCH;
* canonical-root enforcement (no identity → refused);
* personal-db pinning under an organization header;
* identity-status compatibility (``personal_identity.display_name`` unchanged,
  plus the same serialized ``profile``);
* the personal-root and passkey rows are byte-identical before and after a
  profile write;
* a generic Settings write of the protected set is refused.
"""
from __future__ import annotations

import sqlite3

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import identity_routes, personal_profile
from tools.graph import settings_ops
from tools.graph.schemas.personal_identity import (
    PASSKEY_SET_ID,
    PERSONAL_IDENTITY_SET_ID,
)
from tools.graph.schemas.registry import SchemaValidationError
from tools.graph.schemas.user import (
    USER_PROFILE_CANONICAL_LABEL,
    USER_PROFILE_REVISION,
    USER_PROFILE_SET_ID,
)
from tools.network.idkit import KeyPair

# Reuse the real ceremony emulation (armor mint, authenticator, enroll) from
# the identity-routes suite rather than duplicating it.
from tools.dashboard.tests.test_identity_routes import (
    HOST,
    _armor,
    _audited_public,
    _enroll,
    _store_identity,
)

ORG = "idorg"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB
    from tools.dashboard.dao import identity_sessions

    GraphDB.close_all_pooled()
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.create_org_db(
        "personal", type_="personal", path=orgs_dir / "personal.db").close()
    # A DIFFERENT ambient org proves personal routing is immune to it.
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
    with TestClient(Starlette(routes=identity_routes.ROUTES),
                    base_url=f"https://{HOST}") as client:
        client._orgs_dir = orgs_dir  # type: ignore[attr-defined]
        yield client
    identity_routes._pending.clear()
    identity_sessions.reset_for_tests()
    GraphDB.close_all_pooled()


@pytest.fixture
def root():
    return KeyPair.generate()


def _profile(env):
    r = env.get("/api/identity/profile")
    assert r.status_code == 200, r.text
    return r.json()["profile"]


def _raw_rows(orgs_dir, set_id):
    """Every stored settings row for *set_id*, as full ordered tuples."""
    conn = sqlite3.connect(str(orgs_dir / "personal.db"))
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM settings WHERE set_id = ? ORDER BY id", (set_id,))
        return [tuple(row) for row in cur.fetchall()]
    finally:
        conn.close()


# ── no identity ───────────────────────────────────────────────


def test_get_profile_null_without_identity(env):
    assert _profile(env) is None


def test_status_profile_null_without_identity(env):
    body = env.get("/api/identity/status").json()
    assert body["profile"] is None
    assert body["personal_identity"] is None


def test_patch_refused_without_canonical_root(env):
    r = env.patch("/api/identity/profile", json={"display_name": "Jeremy"})
    assert r.status_code == 409, r.text
    # Nothing was written.
    assert personal_profile.profile_member() is None


# ── unpersisted fallback (root, no profile row) ────────────────


def test_get_synthesizes_from_root_without_writing(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    prof = _profile(env)
    assert prof["display_name"] == "Jeremy Spilman"
    assert prof["biography"] == ""
    assert prof["initials"] == "JS"          # derived
    assert prof["initials_override"] is None
    assert prof["avatar_attachment_id"] is None
    assert prof["avatar_url"] is None
    assert prof["persisted"] is False
    # A read must never create a row.
    assert personal_profile.profile_member() is None
    # A second read is still unpersisted — proves idempotent no-write.
    assert _profile(env)["persisted"] is False
    assert personal_profile.profile_member() is None


# ── partial update / merge ─────────────────────────────────────


def test_patch_persists_and_merges(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    r = env.patch("/api/identity/profile",
                  json={"biography": "Builder of Autonomy."})
    assert r.status_code == 200, r.text
    prof = r.json()["profile"]
    assert prof["persisted"] is True
    # display_name was read-merged from the root baseline.
    assert prof["display_name"] == "Jeremy Spilman"
    assert prof["biography"] == "Builder of Autonomy."
    assert prof["initials"] == "JS"
    assert prof["updated_at"]
    # A later PATCH of only display_name keeps the biography.
    r2 = env.patch("/api/identity/profile", json={"display_name": "Jer"})
    assert r2.status_code == 200
    assert r2.json()["profile"]["biography"] == "Builder of Autonomy."
    assert r2.json()["profile"]["display_name"] == "Jer"


def test_patch_requires_at_least_one_field(env, root):
    _store_identity(env, root)
    r = env.patch("/api/identity/profile", json={})
    assert r.status_code == 400


def test_patch_trims_values(env, root):
    _store_identity(env, root)
    r = env.patch("/api/identity/profile",
                  json={"display_name": "  Padded Name  "})
    assert r.status_code == 200
    assert r.json()["profile"]["display_name"] == "Padded Name"


# ── explicit vs derived initials ───────────────────────────────


def test_explicit_initials_override(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    r = env.patch("/api/identity/profile", json={"initials": "JX"})
    assert r.status_code == 200
    prof = r.json()["profile"]
    assert prof["initials"] == "JX"
    assert prof["initials_override"] == "JX"


def test_blank_initials_restores_derivation(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    env.patch("/api/identity/profile", json={"initials": "JX"})
    # A blank value removes the override; derivation returns.
    r = env.patch("/api/identity/profile", json={"initials": ""})
    assert r.status_code == 200
    prof = r.json()["profile"]
    assert prof["initials"] == "JS"
    assert prof["initials_override"] is None
    # And the stored row carries no explicit override.
    assert "initials" not in personal_profile.profile_member().payload


def test_changing_name_keeps_explicit_override(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    env.patch("/api/identity/profile", json={"initials": "JX"})
    r = env.patch("/api/identity/profile", json={"display_name": "Alex Doe"})
    assert r.status_code == 200
    prof = r.json()["profile"]
    # The override survives a name change; it is not silently re-derived.
    assert prof["initials"] == "JX"
    assert prof["initials_override"] == "JX"


# ── biography clearing ─────────────────────────────────────────


def test_biography_clearing(env, root):
    _store_identity(env, root)
    env.patch("/api/identity/profile", json={"biography": "Some words."})
    r = env.patch("/api/identity/profile", json={"biography": ""})
    assert r.status_code == 200
    assert r.json()["profile"]["biography"] == ""


# ── avatar preservation & rejection ────────────────────────────


def _seed_avatar(uuid: str) -> None:
    """Simulate the follow-on avatar route: write the server-owned avatar
    field through the same protected context, onto the existing row."""
    existing = personal_profile.profile_member()
    payload = dict(existing.payload) if existing else {"display_name": "Jeremy Spilman"}
    payload["avatar_attachment_id"] = uuid
    payload["updated_at"] = "2026-09-12T00:00:00Z"
    with settings_ops.identity_write_context():
        settings_ops.upsert_by_key(
            USER_PROFILE_SET_ID, USER_PROFILE_REVISION,
            USER_PROFILE_CANONICAL_LABEL, payload, org=None)


def test_text_patch_preserves_avatar_fields(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    env.patch("/api/identity/profile", json={"biography": "hi"})
    uuid = "0192a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
    _seed_avatar(uuid)
    # A subsequent text PATCH must not disturb the avatar reference.
    r = env.patch("/api/identity/profile", json={"display_name": "Jer"})
    assert r.status_code == 200
    prof = r.json()["profile"]
    assert prof["avatar_attachment_id"] == uuid
    assert prof["avatar_url"] == f"/api/attachment/{uuid}?org=personal"
    assert prof["display_name"] == "Jer"


def test_revision_1_row_with_inline_icon_is_rewritten_on_next_write(env, root):
    """A row stored at revision 1 (with the 64x64 inline icon) is migrated in
    place by the first write: the icon is gone, the attachment stays, and
    there is exactly one base row for the singleton key."""
    import base64

    from tools.graph.db import GraphDB

    _store_identity(env, root, name="Jeremy Spilman")
    uuid = "0192a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
    legacy = {
        "display_name": "Jeremy Spilman", "avatar_attachment_id": uuid,
        "avatar_icon_data_uri": "data:image/webp;base64," + base64.b64encode(b"R" * 40).decode(),
        "updated_at": "2026-09-12T00:00:00Z",
    }
    with settings_ops.identity_write_context():
        settings_ops.upsert_by_key(
            USER_PROFILE_SET_ID, 1, USER_PROFILE_CANONICAL_LABEL, legacy, org=None)
    r = env.patch("/api/identity/profile", json={"biography": "hi"})
    assert r.status_code == 200
    prof = r.json()["profile"]
    assert prof["avatar_attachment_id"] == uuid and "avatar_icon_data_uri" not in prof
    db = GraphDB.for_org("personal")
    rows = db.conn.execute(
        "SELECT schema_revision, payload FROM settings WHERE set_id=? AND key=? "
        "AND supersedes IS NULL AND excludes IS NULL",
        (USER_PROFILE_SET_ID, USER_PROFILE_CANONICAL_LABEL)).fetchall()
    assert [row[0] for row in rows] == [USER_PROFILE_REVISION]
    assert "avatar_icon_data_uri" not in rows[0][1]


@pytest.mark.parametrize("field", ["avatar_attachment_id", "avatar_url"])
def test_patch_rejects_avatar_fields(env, root, field):
    _store_identity(env, root)
    r = env.patch("/api/identity/profile", json={field: "whatever"})
    assert r.status_code == 400


def test_patch_rejects_unknown_field(env, root):
    _store_identity(env, root)
    r = env.patch("/api/identity/profile", json={"color": "#123456"})
    assert r.status_code == 400


def test_patch_rejects_invalid_value(env, root):
    _store_identity(env, root)
    r = env.patch("/api/identity/profile", json={"initials": "toolong"})
    assert r.status_code == 400


# ── personal-db pinning under an org header ────────────────────


def test_profile_pinned_to_personal_db_under_org_header(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    r = env.patch(
        "/api/identity/profile?org=idorg",
        json={"biography": "Pinned."},
        headers={"X-Graph-Org": "idorg"},
    )
    assert r.status_code == 200
    # Written to the personal store...
    personal = settings_ops.read_owned_set(
        USER_PROFILE_SET_ID, org="personal").members
    assert len(personal) == 1
    assert personal[0].payload["biography"] == "Pinned."
    # ...and, because the set is home("personal"), asking for it under the
    # organization slug resolves to the SAME single personal row — the row
    # never lands in the organization database.
    org_rows = settings_ops.read_owned_set(
        USER_PROFILE_SET_ID, org=ORG).members
    assert len(org_rows) == 1
    assert org_rows[0].id == personal[0].id
    # The organization database file itself carries no such row.
    conn = sqlite3.connect(str(env._orgs_dir / f"{ORG}.db"))  # type: ignore[attr-defined]
    try:
        n = conn.execute(
            "SELECT count(*) FROM settings WHERE set_id = ?",
            (USER_PROFILE_SET_ID,)).fetchone()[0]
    finally:
        conn.close()
    assert n == 0
    # GET under the same org header still sees it.
    got = env.get("/api/identity/profile?org=idorg",
                  headers={"X-Graph-Org": "idorg"})
    assert got.json()["profile"]["biography"] == "Pinned."


# ── status compatibility ───────────────────────────────────────


def test_status_carries_serialized_profile(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    env.patch("/api/identity/profile", json={"biography": "Bio."})
    body = env.get("/api/identity/status").json()
    # The legacy display-name-only field is unchanged...
    assert body["personal_identity"]["display_name"] == "Jeremy Spilman"
    # ...and the SAME serialized profile is added.
    assert body["profile"] == _profile(env)
    assert body["profile"]["biography"] == "Bio."


# ── protected-set enforcement ──────────────────────────────────


def test_generic_settings_write_refused(env, root):
    _store_identity(env, root)
    with pytest.raises(settings_ops.ProtectedSettingError):
        settings_ops.upsert_by_key(
            USER_PROFILE_SET_ID, USER_PROFILE_REVISION,
            USER_PROFILE_CANONICAL_LABEL,
            {"display_name": "Injected", "updated_at": "2026-09-12T00:00:00Z"},
            org=None,
        )


# ── byte-identical root/passkey rows ───────────────────────────


def test_root_and_passkey_rows_byte_identical_across_profile_write(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    assert _enroll(env, root).status_code == 200
    orgs_dir = env._orgs_dir  # type: ignore[attr-defined]

    root_before = _raw_rows(orgs_dir, PERSONAL_IDENTITY_SET_ID)
    passkey_before = _raw_rows(orgs_dir, PASSKEY_SET_ID)
    assert root_before and passkey_before

    # A full profile write cycle.
    assert env.patch("/api/identity/profile",
                     json={"display_name": "Jer", "biography": "Bio.",
                           "initials": "JX"}).status_code == 200
    assert env.patch("/api/identity/profile",
                     json={"initials": ""}).status_code == 200

    assert _raw_rows(orgs_dir, PERSONAL_IDENTITY_SET_ID) == root_before
    assert _raw_rows(orgs_dir, PASSKEY_SET_ID) == passkey_before
