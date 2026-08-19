"""The content store that keeps a vault secret in its own scoped database.

These tests pin the three properties the operator required of a ``@vaulted``
secret's storage: it is sealed by the SAME code an organization uses; the
sealed bytes land INSIDE the database the secret is scoped to (no ``content.db``
sidecar, no ``bodies/`` directory); and a personal secret and an org secret
route to different files and never cross.
"""

from __future__ import annotations

import sqlite3

import pytest

from tools.graph.tests.vault_read_harness import VaultWorld
from tools.vault import storage_object
from tools.vault.storage_object import Holdings
from tools.vault.db_content_store import DbContentStore, vault_db_path_for
from tools.network.storagekit.keycontrol import KeyControlStore
from tools.network.storagekit.errors import ContentHashMismatchError


def _seal(tmp_path, db_path, payload):
    """Seal *payload* through the unchanged org code, with both stores backed
    by *db_path*. Returns ``(world, locator)``."""
    world = VaultWorld(tmp_path / "scaffold", member_count=1)
    world.content_store = DbContentStore(db_path)
    world.store = world.content_store
    world.key_control = KeyControlStore(db_path)
    world.sync()
    locator = world.sealer(
        set_id="autonomy.vault",
        schema_revision=1,
        key="audited",
        setting_id="row-1",
        payload=payload,
        tier="audited",
        org=None,
    )
    return world, locator


def _holdings(world, db_path):
    with KeyControlStore(db_path) as kc:
        return Holdings(
            secrets=dict(world.world.held(world.author)),
            descriptors=kc.states,
            bridges=list(kc.accepted_bridges()),
        )


def test_secret_round_trips_with_ciphertext_inside_the_scoped_db(tmp_path):
    db_path = tmp_path / "personal.db"
    token = {"GH_TOKEN": "ghp_the_operators_actual_github_token"}

    world, locator = _seal(tmp_path, db_path, token)
    opened = storage_object.open_revision_for_member(
        locator, holdings=_holdings(world, db_path), content_store=DbContentStore(db_path)
    )
    assert opened == token  # sealed and opened by the org code, unchanged

    # The ciphertext is a row IN this database, not a file beside it.
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT count(*) FROM vault_content_bodies").fetchone()[0] == 1
    conn.close()
    # No sidecar content store was created next to personal.db.
    assert not (db_path.parent / "content").exists()
    assert not (db_path.parent / "content.db").exists()
    assert not (db_path.parent / "bodies").exists()


def test_ciphertext_and_key_control_share_one_file(tmp_path):
    db_path = tmp_path / "personal.db"
    world, _ = _seal(tmp_path, db_path, {"K": "v"})

    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    # Both halves of the vault footprint live in the one scoped database.
    assert {"vault_content_bodies", "vault_content_objects"} <= tables
    assert any(t.startswith("keycontrol_") for t in tables)


def test_put_object_is_idempotent_and_hash_guarded(tmp_path):
    db_path = tmp_path / "personal.db"
    world, locator = _seal(tmp_path, db_path, {"K": "v"})
    reference = storage_object.parse_locator(locator)
    header, body = DbContentStore(db_path).get_object(
        reference["object_id"], reference["revision_id"]
    )

    fresh = DbContentStore(tmp_path / "fresh.db")
    first = fresh.put_object(header, body)
    assert fresh.put_object(header, body) == first  # byte-identical replay is a no-op
    with pytest.raises(ContentHashMismatchError):
        fresh.put_object(header, body + b"tamper")  # body must hash to its header


def test_personal_and_org_route_to_different_files(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("GRAPH_DB", raising=False)

    personal = vault_db_path_for(None)
    org = vault_db_path_for("acme")

    assert personal.name == "personal.db"
    assert org.name == "acme.db"
    assert personal != org
    # An org secret's file is not personal.db, and vice versa.
    assert "personal.db" not in str(org)
    assert "acme" not in str(personal)
