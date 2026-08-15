"""Where the persona record physically lands (auto-6n3we).

Separate from ``test_org_create_ceremony`` because of the fixture, not the
subject. That module sets ``GRAPH_DB``, which collapses every Setting into one
database — ``org=<slug>`` and ``org=None`` both write there, and a read asking
for one org returns rows stamped with whatever org it asked for. Under it, a
scope assertion cannot distinguish the correct design from the broken one, so
writing one there would be theatre.

These tests therefore run against real per-org database FILES and assert on
the files themselves. That is the actual claim: the row belongs in personal.db
and must never appear in the org's shared database.

Why it matters, restated because a future reader will be tempted to "simplify"
this into the other module: an org DB is shared by its members, and this row
answers "which persona is MINE" — a question with a different answer for every
reader. Written under ``org=<slug>``, two members of one org collide on a
single (set_id, key, org) row and each reads the other's identity: silent
misattribution in the record whose whole purpose is correct attribution.
"""

from __future__ import annotations

import sqlite3
import types

import pytest

from tools.graph import org_ops, settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_ORG_KEY_SET_ID,
    NETWORK_PERSONA_SET_ID,
)
from tools.graph.schemas.personal_identity import PERSONAL_IDENTITY_SET_ID
from tools.network.idkit import KeyPair
from tools.network.idkit.armor import encrypt_root_key

PASSWORD = "week-glacier-thirty-nine"


@pytest.fixture
def orgs_env(tmp_path, monkeypatch):
    """Real per-org DB files. Deliberately does NOT set GRAPH_DB."""
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.delenv("AUTONOMY_PERSONAL_PASSWORD", raising=False)

    personal_root = KeyPair.generate()
    with settings_ops.identity_write_context():
        settings_ops.upsert_by_key(
            PERSONAL_IDENTITY_SET_ID, 1, "default",
            {
                "armored_private_key": encrypt_root_key(
                    personal_root, PASSWORD, iterations=10_000
                ),
                "root_pub": personal_root.public_hex,
                "display_name": "Test Owner",
                "created_at": "2026-07-26T00:00:00Z",
            },
            org=None,
        )
    yield types.SimpleNamespace(
        root=tmp_path, orgs=orgs, personal_root=personal_root,
        personal_seed=bytes.fromhex(personal_root.private_hex),
    )
    GraphDB.close_all_pooled()


def _set_ids_in(db_path) -> list[str]:
    """Every set_id physically stored in one database file."""
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return [r[0] for r in conn.execute("SELECT set_id FROM settings")]
    finally:
        conn.close()


def test_the_persona_lands_in_personal_db_and_not_the_orgs_db(orgs_env):
    """The load-bearing assertion, made against the files themselves.

    Asserted as a NEGATIVE on the org's database. A test that only checks "a
    row exists somewhere" passes under the broken org-scoped design, which is
    exactly the design this row must not have.
    """
    org_ops.create_org_with_identity("acme", PASSWORD, root=orgs_env.root)

    personal = _set_ids_in(orgs_env.orgs / "personal.db")
    shared = _set_ids_in(orgs_env.orgs / "acme.db")

    assert personal.count(NETWORK_PERSONA_SET_ID) == 1, (
        "the persona belongs in this operator's own database"
    )
    assert NETWORK_PERSONA_SET_ID not in shared, (
        "the persona must never be written into the org's shared database — "
        "two members would collide on one row and read each other's identity"
    )


def test_the_org_root_key_still_goes_to_the_orgs_own_db(orgs_env):
    """The control. Without it, a bug routing EVERY write to personal.db
    would satisfy the assertion above and look like success.
    """
    org_ops.create_org_with_identity("acme", PASSWORD, root=orgs_env.root)

    assert NETWORK_ORG_KEY_SET_ID in _set_ids_in(orgs_env.orgs / "acme.db")
    assert NETWORK_ORG_KEY_SET_ID not in _set_ids_in(orgs_env.orgs / "personal.db")


def test_the_org_scoped_read_finds_no_persona(orgs_env):
    """The same claim through the API callers actually use.

    Presence of a row under an org-scoped read is not evidence that org owns
    it — canonical rows are visible to peers — so this asserts absence, which
    that visibility cannot manufacture.
    """
    result = org_ops.create_org_with_identity("acme", PASSWORD, root=orgs_env.root)

    scoped = settings_ops.read_owned_set(NETWORK_PERSONA_SET_ID, org="acme").members
    assert scoped == []

    scopeless = settings_ops.read_owned_set(NETWORK_PERSONA_SET_ID, org=None).members
    assert len(scopeless) == 1
    assert scopeless[0].payload["persona_pub"] == result.founder_persona_pub


def test_two_orgs_get_two_rows_with_different_personas(orgs_env):
    """Per-genesis keying, and unlinkability, in one assertion: one personal
    seed across two orgs yields two rows whose personas differ.
    """
    a = org_ops.create_org_with_identity("acme", PASSWORD, root=orgs_env.root)
    b = org_ops.create_org_with_identity("brawn", PASSWORD, root=orgs_env.root)

    rows = settings_ops.read_owned_set(NETWORK_PERSONA_SET_ID, org=None).members
    by_key = {m.key: m.payload for m in rows}
    assert set(by_key) == {a.genesis_id, b.genesis_id}
    assert by_key[a.genesis_id]["persona_pub"] != by_key[b.genesis_id]["persona_pub"]
    # Which org each row belongs to is carried by the key, which the assertion
    # above already checks; the payload does not repeat it.


# ── the join path (auto-6n3we) ───────────────────────────────
#
# persist_outcome is where a join's persona becomes durable. It lives in
# tools/init/join.py but is tested here because the assertion is about WHERE
# the row lands, which needs this module's real per-org databases.


def _join_outcome(**kw):
    from tools.init.join import ADMITTED, JoinOutcome

    base = dict(
        state=ADMITTED, org="acme", invite_ref="inv-1",
        persona_pub="a" * 64, genesis_id="b" * 64, granted_role="member",
    )
    base.update(kw)
    return JoinOutcome(**base)


@pytest.fixture
def quiet_pending_joins(monkeypatch):
    """Record pending_joins traffic instead of standing up its store.

    The DAO is not under test; what is under test is that admission records
    the persona AND still clears the pending row.
    """
    from tools.dashboard.dao import pending_joins

    calls = {"save": [], "delete": []}
    monkeypatch.setattr(pending_joins, "save",
                        lambda **kw: calls["save"].append(kw))
    monkeypatch.setattr(pending_joins, "delete",
                        lambda ref: calls["delete"].append(ref))
    return calls


def test_admission_records_the_persona_and_still_clears_the_pending_row(
    orgs_env, quiet_pending_joins
):
    """Admission is the moment the persona becomes true — and the moment the
    only row carrying it was previously deleted. Both must happen, in that
    order, or the value is erased exactly when it starts being correct.
    """
    from tools.init.join import persist_outcome

    persist_outcome(_join_outcome())

    rows = settings_ops.read_owned_set(NETWORK_PERSONA_SET_ID, org=None).members
    assert len(rows) == 1
    assert rows[0].key == "b" * 64
    assert rows[0].payload["persona_pub"] == "a" * 64
    assert rows[0].payload["source"] == "join"
    assert rows[0].payload["invite_ref"] == "inv-1"

    assert quiet_pending_joins["delete"] == ["inv-1"], (
        "clearing the pending row must not regress"
    )


def test_a_pending_join_records_no_persona(orgs_env, quiet_pending_joins):
    """Not a member yet. Recording here would assert a membership the org has
    not granted, and the claim can still be rejected.
    """
    from tools.init.join import PENDING, persist_outcome

    persist_outcome(_join_outcome(state=PENDING, have=1, need=2))

    assert settings_ops.read_owned_set(NETWORK_PERSONA_SET_ID, org=None).members == []
    assert quiet_pending_joins["save"], "the pending row is still saved"


def test_admission_without_a_genesis_records_nothing_rather_than_guessing(
    orgs_env, quiet_pending_joins
):
    """The genesis is the row's key. An outcome that lost it during threading
    must not invent one — a row under the wrong key is worse than no row,
    because it reads back as a confident answer about the wrong org.
    """
    from tools.init.join import persist_outcome

    persist_outcome(_join_outcome(genesis_id=None))

    assert settings_ops.read_owned_set(NETWORK_PERSONA_SET_ID, org=None).members == []
    assert quiet_pending_joins["delete"] == ["inv-1"]
