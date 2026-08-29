"""Fail-loud isolation guards for repository-local operator data paths."""

from __future__ import annotations
from tools.network.idkit.root_factor_policy import mint_password_armor

import sqlite3

import pytest

from tools.data_paths import (
    REFUSE_REAL_DATA_FALLBACK_ENV,
    RealDataFallbackRefused,
)
from tools.graph import cross_org, org_ops, settings_ops
from tools.graph import db as graph_db
from tools.graph.db import GraphDB, resolve_caller_db_path
from tools.graph.migrations import migrate_operator_local
from tools.graph.schemas.personal_identity import PERSONAL_IDENTITY_SET_ID
from tools.network.idkit import KeyPair
from tools.network.ledger import org_ledger_db_path


def test_refuse_mode_rejects_every_unrooted_org_path(monkeypatch, tmp_path):
    """The isolation tripwire fails at resolution, before a live path opens."""
    trap_orgs = tmp_path / "would-have-been-live-orgs"
    trap_legacy = tmp_path / "would-have-been-live-graph.db"
    monkeypatch.delenv("AUTONOMY_ORGS_DIR", raising=False)
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setenv(REFUSE_REAL_DATA_FALLBACK_ENV, "1")
    monkeypatch.setattr(graph_db, "DEFAULT_ORGS_DIR", trap_orgs)
    monkeypatch.setattr(org_ops, "DEFAULT_ORGS_DIR", trap_orgs)

    resolvers = (
        lambda: settings_ops._db_path(None),
        lambda: org_ledger_db_path("missing"),
        lambda: org_ops.list_orgs(),
        lambda: cross_org.list_org_slugs(),
        lambda: migrate_operator_local._resolve_orgs_dir(None),
    )
    for resolve in resolvers:
        with pytest.raises(RealDataFallbackRefused, match="refus"):
            resolve()

    assert not trap_orgs.exists()
    assert not trap_legacy.exists()


def test_refuse_mode_accepts_explicitly_rooted_org_paths(
    monkeypatch, tmp_path,
):
    root = tmp_path / "isolated-orgs"
    monkeypatch.setenv(REFUSE_REAL_DATA_FALLBACK_ENV, "true")
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)

    assert org_ledger_db_path("acme") == root / "acme.db"
    assert org_ops.list_orgs() == []
    assert cross_org.list_org_slugs() == []

    personal = settings_ops._db_path(None)
    assert personal == str(root.parent / "personal.db")
    assert (root.parent / "personal.db").exists()

    # A rooted lookup resolves to the org's own path whether or not the
    # file exists yet — there is nowhere else to escape to. A missing named
    # org fails at OPEN, loudly, naming this path.
    assert resolve_caller_db_path("acme") == root / "acme.db"
    GraphDB.create_org_db("acme").close()
    assert resolve_caller_db_path("acme") == root / "acme.db"


def test_fresh_personal_identity_write_never_uses_legacy_db(
    monkeypatch, tmp_path,
):
    """settings_ops(org=None) deterministically creates personal.db."""
    root = tmp_path / "isolated-orgs"
    legacy = tmp_path / "legacy-graph.db"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv(REFUSE_REAL_DATA_FALLBACK_ENV, raising=False)

    owner = KeyPair.generate()
    with settings_ops.identity_write_context():
        settings_ops.upsert_by_key(
            PERSONAL_IDENTITY_SET_ID,
            1,
            "default",
            {
                "armored_private_key": mint_password_armor(
                    owner, "personal-password", iterations=10_000,
                ),
                "root_pub": owner.public_hex,
                "display_name": "Isolated Operator",
                "created_at": "2026-07-26T00:00:00Z",
            },
            org=None,
        )

    personal = root.parent / "personal.db"
    assert personal.exists()
    with sqlite3.connect(personal) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM settings "
            "WHERE set_id = ? AND key = 'default'",
            (PERSONAL_IDENTITY_SET_ID,),
        ).fetchone()[0] == 1
    assert not legacy.exists()
