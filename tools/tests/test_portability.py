"""End-to-end acceptance for portable node-volume snapshot and restore."""

from __future__ import annotations
from tools.network.idkit.root_factor_policy import mint_password_armor, open_armor_with_password

import io
import json
import os
import re
import sqlite3
import tarfile
import time
from pathlib import Path

import pytest

from tools.data_paths import STORE_MANIFEST
from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.models import Source
from tools.network.idkit import (
    KeyPair,
    Subject,
    derive_persona,
    issue_cert,
    verify_signature,
)
from tools.network.ledger.found import found_org_ledger
from tools.network.ledger.store import LedgerStore
from tools.portability import (
    PortabilityError,
    SNAPSHOT_MANIFEST,
    VOLUME_SCHEMA_VERSION,
    VOLUME_STAMP,
    create_snapshot,
    migrate_on_mount,
    restore_snapshot,
)

PERSONAL_PASSPHRASE = "portable-personal-password"
ORG_PASSPHRASE = "portable-organization-password"
PORTABLE_ORG_UUID = "018f6b2a-7c4d-7e11-8a3b-9d5c1e2f4a6b"


@pytest.fixture(autouse=True)
def _clear_store_environment(monkeypatch):
    for store in STORE_MANIFEST:
        if store.env:
            monkeypatch.delenv(store.env, raising=False)
    monkeypatch.delenv("AUTONOMY_REFUSE_REAL_DATA_FALLBACK", raising=False)
    yield

    # Initializers intentionally keep process-lifetime connections. Tests
    # migrate several scratch volumes in one process, so close those pools.
    from tools.dashboard.dao import auth_db, dashboard_db, identity_sessions

    for module in (auth_db, dashboard_db):
        conn = getattr(module, "_conn", None)
        if conn is not None:
            conn.close()
            module._conn = None
    for conn, _lock in identity_sessions._connections.values():
        conn.close()
    identity_sessions._connections.clear()


def _seed_serving_credential(volume, *, personal_seed, delegate, cert,
                             persona_pub, org_root_pub) -> str:
    """Write one machine's serving credential into *volume* the way the
    product writes it, and return the vault row key.

    Three rows, through the real settings path with the volume as the data
    root: the registry binding (the serve-cert row is keyed by its org uuid),
    this machine's ``autonomy.machine.serve-cert`` row, and the sealed
    ``autonomy.machine.vault.audited`` row holding the key. The audited seal
    is a cold write to the operator's published delegate recipient, so no
    warm vault is needed here — only at the read, which is what the restored
    node has to do.
    """
    from tools.graph import settings_ops
    from tools.graph.db import GraphDB
    from tools.graph.schemas.machine_serve_cert import (
        MACHINE_SERVE_CERT_REVISION,
        MACHINE_SERVE_CERT_SET_ID,
        serving_key_vault_key,
    )
    from tools.graph.schemas.machine_vault import (
        MACHINE_VAULT_AUDITED_REVISION,
        MACHINE_VAULT_AUDITED_SET_ID,
    )
    from tools.graph.schemas.network_identity import (
        NETWORK_BINDING_REVISION,
        NETWORK_BINDING_SET_ID,
    )
    from tools.graph.schemas.vault_policy_class import VAULT_POLICY_CLASS_SET_ID
    from tools.vault.key_holder import _scoped_db
    from tools.vault.personal_object import derive_delegate_audited_recipient
    from tools.vault.store import VaultStore

    previous = os.environ.get("AUTONOMY_DATA_ROOT")
    os.environ["AUTONOMY_DATA_ROOT"] = str(volume)
    try:
        GraphDB.close_all_pooled()
        settings_ops.upsert_by_key(
            NETWORK_BINDING_SET_ID,
            NETWORK_BINDING_REVISION,
            "registry.invalid",
            {
                "org_uuid": PORTABLE_ORG_UUID,
                "root_pub": org_root_pub,
                "registry_url": "https://registry.invalid",
                "recovery_policy": {"mode": "none"},
                "binding_expires_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 86_400)
                ),
            },
            org="autonomy",
        )
        _, recipient_public = derive_delegate_audited_recipient(personal_seed)
        with VaultStore(_scoped_db(VAULT_POLICY_CLASS_SET_ID, None)) as store:
            store.put_delegate_audited_recipient(recipient_public)
        vault_key = serving_key_vault_key(PORTABLE_ORG_UUID, delegate.public_hex)
        settings_ops.write_by_key(
            MACHINE_VAULT_AUDITED_SET_ID,
            MACHINE_VAULT_AUDITED_REVISION,
            vault_key,
            {"value": delegate.private_hex},
            org="machine",
        )
        settings_ops.upsert_by_key(
            MACHINE_SERVE_CERT_SET_ID,
            MACHINE_SERVE_CERT_REVISION,
            PORTABLE_ORG_UUID,
            {
                "cert": cert.to_json().decode("ascii"),
                "persona_pub": persona_pub,
                "not_after": cert.not_after,
                "child_pub": delegate.public_hex,
                "vault_key": vault_key,
            },
            org="machine",
        )
        GraphDB.close_all_pooled()
    finally:
        if previous is None:
            os.environ.pop("AUTONOMY_DATA_ROOT", None)
        else:
            os.environ["AUTONOMY_DATA_ROOT"] = previous
        settings_ops.set_personal_delegate_audited_key(None)
    return vault_key


def _seed_node(volume: Path) -> dict:
    migrate_on_mount(volume, tls=False)

    # Personal root material: a real passphrase-encrypted root whose seed also
    # derives the founder persona below.  The round-trip therefore proves key
    # continuity, not merely preservation of an opaque fixture string.
    personal = volume / "personal.db"
    personal_seed = bytes.fromhex("34" * 32)
    personal_root = KeyPair.from_private_hex(personal_seed.hex())
    identity_payload = json.dumps({
        "root_pub": personal_root.public_hex,
        "armored_private_key": mint_password_armor(
            personal_root,
            PERSONAL_PASSPHRASE,
            iterations=10_000,
        ),
        "recovery": {"threshold": 2},
    }, sort_keys=True)
    with sqlite3.connect(personal) as conn:
        conn.execute(
            "INSERT INTO settings("
            "id,set_id,schema_revision,key,payload,created_at,updated_at,"
            "publication_state"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (
                "portable-identity",
                "autonomy.identity.portability",
                1,
                "default",
                identity_payload,
                "2026-07-26T00:00:00Z",
                "2026-07-26T00:00:00Z",
            "canonical",
            ),
        )

    # A real founded org ledger proves memberships survive, not merely bytes.
    org_root = KeyPair.generate()
    with LedgerStore(volume / "orgs" / "autonomy.db") as store:
        founded = found_org_ledger(
            store,
            org_id=PORTABLE_ORG_UUID,
            org_root=org_root,
            personal_root_seed=personal_seed,
            now=1_720_000_000_000,
        )
        assert founded.founder_persona_pub in store.fold().members
    org_db = volume / "orgs" / "autonomy.db"
    GraphDB(org_db).close()
    org_key_payload = json.dumps({
        "root_pub": org_root.public_hex,
        "armored_private_key": mint_password_armor(
            org_root,
            ORG_PASSPHRASE,
            iterations=10_000,
        ),
    }, sort_keys=True)
    with sqlite3.connect(org_db) as conn:
        conn.execute(
            "INSERT INTO settings("
            "id,set_id,schema_revision,key,payload,created_at,updated_at,"
            "publication_state"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (
                "portable-org-key",
                "autonomy.network.org-key",
                1,
                "default",
                org_key_payload,
                "2026-07-26T00:00:00Z",
                "2026-07-26T00:00:00Z",
            "canonical",
            ),
        )

    # The serving credential as the product has stored it since 2026-09-20
    # (graph://67d0aa5f-885): this machine's serve-cert row and a machine-vault
    # row holding the delegate key, both machine-homed, plus the registry
    # binding the serve-cert row is keyed by. No key file exists anywhere --
    # the key is released into ramfs at connector launch, which is tmpfs and
    # deliberately does NOT travel in a snapshot. Written through the product's
    # own settings path so the seal, the routing and the schema are real.
    delegate = KeyPair.generate()
    now = int(time.time())
    # The serving delegate is issued BY the org persona, directly (the schema
    # requires depth 1 from persona_pub), never by the org root.
    persona = derive_persona(personal_seed, founded.genesis_id)
    assert persona.public_hex == founded.founder_persona_pub
    cert = issue_cert(
        persona,
        delegate.public_hex,
        scope=("tunnel:serve",),
        org=PORTABLE_ORG_UUID,
        subject=Subject("persona", persona.public_hex),
        not_before=now - 60,
        not_after=now + 86_400,
    )
    # No viewer certificate: it is retired (graph://807b4e11-3e9); a viewer
    # verifies the per-link channel key instead.
    vault_key = _seed_serving_credential(
        volume,
        personal_seed=personal_seed,
        delegate=delegate,
        cert=cert,
        persona_pub=persona.public_hex,
        org_root_pub=org_root.public_hex,
    )

    graph = GraphDB(volume / "personal.db")
    graph.insert_source(Source(
        id="portable-source",
        type="note",
        platform="local",
        title="Portable content",
        file_path="note:portable",
        metadata={},
    ))
    graph.close()

    with sqlite3.connect(volume / "dashboard.db") as conn:
        conn.execute(
            "INSERT INTO tmux_sessions("
            "tmux_name,type,project,state,created_at,last_activity"
            ") VALUES(?,?,?,?,?,?)",
            ("portable-session", "container", "autonomy", "ENDED", 1.0, 1.0),
        )

    (volume / "tls.key").write_bytes(b"same-private-tls-key")
    (volume / "tls.key").chmod(0o600)
    (volume / "tls.crt").write_bytes(b"same-public-tls-cert")
    (volume / "agent-runs" / "portable-run").mkdir()
    (volume / "agent-runs" / "portable-run" / "result.json").write_text(
        '{"status":"complete"}\n',
        encoding="utf-8",
    )
    (volume / "session-traces" / "portable.jsonl").write_text(
        '{"turn":1}\n',
        encoding="utf-8",
    )

    return {
        "identity_payload": identity_payload,
        "org_key_payload": org_key_payload,
        "founder": founded.founder_persona_pub,
        "genesis": founded.genesis_id,
        "serve_child_pub": delegate.public_hex,
        "serve_key_hex": delegate.private_hex,
        "serve_vault_key": vault_key,
    }


def _assert_same_node(volume: Path, expected: dict) -> None:
    with sqlite3.connect(volume / "personal.db") as conn:
        row = conn.execute(
            "SELECT payload FROM settings WHERE id='portable-identity'"
        ).fetchone()
    assert row == (expected["identity_payload"],)
    identity = json.loads(row[0])
    personal_root = open_armor_with_password(
        identity["armored_private_key"],
        PERSONAL_PASSPHRASE,
    )
    assert personal_root.public_hex == identity["root_pub"]
    restored_persona = derive_persona(
        bytes.fromhex(personal_root.private_hex),
        expected["genesis"],
    )
    assert restored_persona.public_hex == expected["founder"]

    with LedgerStore(volume / "orgs" / "autonomy.db") as store:
        state = store.fold()
        assert expected["founder"] in state.members
        genesis = next(
            event for event in store.events()
            if event.payload["type"] == "genesis"
        )
        assert genesis.event_id == expected["genesis"]
    with sqlite3.connect(volume / "orgs" / "autonomy.db") as conn:
        row = conn.execute(
            "SELECT payload FROM settings WHERE id='portable-org-key'"
        ).fetchone()
    assert row == (expected["org_key_payload"],)
    org_identity = json.loads(row[0])
    org_root = open_armor_with_password(
        org_identity["armored_private_key"],
        ORG_PASSPHRASE,
    )
    assert org_root.public_hex == org_identity["root_pub"] == state.root
    proof = b"portable-node-constitutional-proof"
    verify_signature(org_root.public_hex, org_root.sign_hex(proof), proof)

    with sqlite3.connect(volume / "personal.db") as conn:
        row = conn.execute(
            "SELECT title FROM sources WHERE id='portable-source'"
        ).fetchone()
    assert row == ("Portable content",)

    with sqlite3.connect(volume / "dashboard.db") as conn:
        row = conn.execute(
            "SELECT state FROM tmux_sessions WHERE tmux_name='portable-session'"
        ).fetchone()
    assert row == ("ENDED",)
    assert (volume / "tls.key").read_bytes() == b"same-private-tls-key"
    assert (volume / "tls.crt").read_bytes() == b"same-public-tls-cert"
    # The serving key is a sealed machine-vault row, not a file: nothing
    # under network/ carries key material on either node any more.
    assert not list((volume / "network").glob("*.key"))


def test_snapshot_restore_to_fresh_node_preserves_identity_membership_and_data(
    tmp_path,
    monkeypatch,
):
    node_a = tmp_path / "node-a"
    expected = _seed_node(node_a)
    artifact = tmp_path / "portable.tar.gz"

    manifest = create_snapshot(node_a, artifact, quiesced=True)
    assert manifest["consistency"] == "quiesced"
    assert manifest["volume_schema_version"] == VOLUME_SCHEMA_VERSION
    assert [row["key"] for row in manifest["stores"]] == [
        store.key for store in STORE_MANIFEST
    ]

    node_b = tmp_path / "node-b"
    restored = restore_snapshot(artifact, node_b)
    assert restored["snapshot_id"] == manifest["snapshot_id"]
    for row in manifest["files"]:
        path = node_b / row["path"]
        assert path.is_file()
        assert path.stat().st_size == row["size"]

    # Startup migration on the fresh node is idempotent and leaves its
    # cryptographic identity, ledger membership, graph, config, and keys intact.
    stamp_before = (node_b / VOLUME_STAMP).read_bytes()
    report = migrate_on_mount(node_b, tls=False)
    assert report["from_version"] == report["to_version"] == VOLUME_SCHEMA_VERSION
    assert (node_b / VOLUME_STAMP).read_bytes() == stamp_before
    _assert_same_node(node_b, expected)

    # The row resolves under C, not A, and its inline cert still matches the
    # byte-identical restored delegate key.
    from tools.dashboard import link_serving_supervisor as supervisor

    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(node_b / "orgs"))
    monkeypatch.setenv("AUTONOMY_NETWORK_KEY_DIR", str(node_b / "network"))
    GraphDB.close_all_pooled()
    state = supervisor.serve_cert_state("autonomy")
    assert state["status"] == "ok"
    assert state["vault_key"] == expected["serve_vault_key"]
    assert state["child_pub"] == expected["serve_child_pub"]
    assert state["work_base"].startswith(str(node_b / "network"))
    assert not state["work_base"].startswith(str(node_a))

    # The key itself travels sealed inside the machine store, and opens on the
    # restored node once ITS vault is warm — byte-identical to A's. A cold
    # restored node holds the row and cannot open it, which is the design
    # (a reboot fails closed), so the warm step is explicit here.
    from tools.vault.personal_object import derive_delegate_audited_recipient
    recipient_private, _ = derive_delegate_audited_recipient(
        bytes.fromhex("34" * 32)
    )
    settings_ops.set_personal_delegate_audited_key(recipient_private)
    try:
        assert supervisor.serving_key_hex(state) == expected["serve_key_hex"]
    finally:
        settings_ops.set_personal_delegate_audited_key(None)

    materialized_cert = Path(supervisor._materialize_cert(
        state["work_base"], state["cert"]
    ))
    assert materialized_cert.parent == node_b / "network"
    assert materialized_cert.read_text() == state["cert"]


def test_snapshot_requires_explicit_quiescence_and_never_emits_partial_artifact(
    tmp_path,
):
    volume = tmp_path / "node"
    _seed_node(volume)
    artifact = tmp_path / "refused.tar.gz"
    with pytest.raises(PortabilityError, match="quiesced"):
        create_snapshot(volume, artifact, quiesced=False)
    assert not artifact.exists()


def test_snapshot_refuses_an_uninitialized_directory(tmp_path):
    volume = tmp_path / "empty"
    volume.mkdir()
    with pytest.raises(PortabilityError, match="not an initialized node"):
        create_snapshot(
            volume,
            tmp_path / "empty.tar.gz",
            quiesced=True,
        )


def test_torn_snapshot_is_detected_before_restore(tmp_path):
    volume = tmp_path / "node"
    _seed_node(volume)
    artifact = tmp_path / "good.tar.gz"
    create_snapshot(volume, artifact, quiesced=True)

    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    with tarfile.open(artifact, "r:gz") as archive:
        archive.extractall(unpacked, filter="data")
    (unpacked / "volume" / "tls.key").write_bytes(b"torn")
    torn = tmp_path / "torn.tar.gz"
    with tarfile.open(torn, "w:gz") as archive:
        archive.add(unpacked / SNAPSHOT_MANIFEST, arcname=SNAPSHOT_MANIFEST)
        archive.add(unpacked / "volume", arcname="volume")

    target = tmp_path / "target"
    with pytest.raises(PortabilityError, match="hash mismatch"):
        restore_snapshot(torn, target)
    assert not target.exists()


def test_restore_rejects_traversal_member_and_existing_target(tmp_path):
    malicious = tmp_path / "malicious.tar.gz"
    with tarfile.open(malicious, "w:gz") as archive:
        info = tarfile.TarInfo("../escape")
        body = b"escape"
        info.size = len(body)
        archive.addfile(info, io.BytesIO(body))
    with pytest.raises(PortabilityError, match="unsafe path"):
        restore_snapshot(malicious, tmp_path / "target")
    assert not (tmp_path / "escape").exists()

    volume = tmp_path / "node"
    _seed_node(volume)
    artifact = tmp_path / "good.tar.gz"
    create_snapshot(volume, artifact, quiesced=True)
    populated = tmp_path / "populated"
    populated.mkdir()
    (populated / "already-here").write_text("do not clobber me")
    with pytest.raises(PortabilityError, match="fresh and empty"):
        restore_snapshot(artifact, populated)

    # An existing but EMPTY target is fresh: that is what a container's mounted
    # data volume always looks like (the runtime creates the mount point before
    # anything runs), so refusing on mere existence made restore impossible in
    # the one environment it exists for.
    mount_point = tmp_path / "mounted-volume"
    mount_point.mkdir()
    restore_snapshot(artifact, mount_point)
    assert (mount_point / VOLUME_STAMP).exists()


def test_beads_declaration_and_restore_are_fail_closed(tmp_path):
    volume = tmp_path / "node"
    _seed_node(volume)
    with pytest.raises(PortabilityError, match="no consistent --beads-dump"):
        create_snapshot(
            volume,
            tmp_path / "missing.tar.gz",
            quiesced=True,
            beads_present=True,
        )

    dump = tmp_path / "beads.sql"
    dump.write_text("CREATE DATABASE auto;\n", encoding="utf-8")
    artifact = tmp_path / "with-beads.tar.gz"
    create_snapshot(
        volume,
        artifact,
        quiesced=True,
        beads_present=True,
        beads_dump=dump,
    )
    with pytest.raises(PortabilityError, match="--beads-output"):
        restore_snapshot(artifact, tmp_path / "without-beads")

    target = tmp_path / "restored"
    restored_dump = tmp_path / "restored-beads.sql"
    restore_snapshot(artifact, target, beads_output=restored_dump)
    assert restored_dump.read_bytes() == dump.read_bytes()
    _assert_same_node(target, {
        **_seed_expected_from_volume(volume),
    })


def test_beads_publish_failure_rolls_back_the_node_restore(tmp_path, monkeypatch):
    volume = tmp_path / "node"
    _seed_node(volume)
    dump = tmp_path / "beads.sql"
    dump.write_text("CREATE DATABASE auto;\n", encoding="utf-8")
    artifact = tmp_path / "with-beads.tar.gz"
    create_snapshot(
        volume,
        artifact,
        quiesced=True,
        beads_present=True,
        beads_dump=dump,
    )

    def refuse_publish(_source, _target):
        raise OSError("simulated destination-volume failure")

    monkeypatch.setattr(os, "link", refuse_publish)
    target = tmp_path / "restored"
    restored_dump = tmp_path / "dolt-volume" / "beads.sql"
    with pytest.raises(PortabilityError, match="could not publish the beads dump"):
        restore_snapshot(artifact, target, beads_output=restored_dump)
    assert not target.exists()
    assert not restored_dump.exists()
    assert not list(restored_dump.parent.glob(".beads.sql.*.tmp"))


def _seed_expected_from_volume(volume: Path) -> dict:
    with sqlite3.connect(volume / "personal.db") as conn:
        identity_payload = conn.execute(
            "SELECT payload FROM settings WHERE id='portable-identity'"
        ).fetchone()[0]
    with LedgerStore(volume / "orgs" / "autonomy.db") as store:
        state = store.fold()
        founder = next(iter(state.members))
        genesis = next(
            event.event_id for event in store.events()
            if event.payload["type"] == "genesis"
        )
    with sqlite3.connect(volume / "orgs" / "autonomy.db") as conn:
        org_key_payload = conn.execute(
            "SELECT payload FROM settings WHERE id='portable-org-key'"
        ).fetchone()[0]
    with sqlite3.connect(volume / "machine.db") as conn:
        serve_payload = json.loads(conn.execute(
            "SELECT payload FROM settings WHERE set_id='autonomy.machine.serve-cert'"
        ).fetchone()[0])
    return {
        "identity_payload": identity_payload,
        "org_key_payload": org_key_payload,
        "founder": founder,
        "genesis": genesis,
        "serve_child_pub": serve_payload["child_pub"],
        "serve_vault_key": serve_payload["vault_key"],
    }


def test_migrate_on_mount_upgrades_legacy_volume_and_stamps_it(tmp_path):
    volume = tmp_path / "legacy"
    volume.mkdir()
    (volume / "orgs").mkdir()
    sessions = volume / "dashboard_identity_sessions.db"
    with sqlite3.connect(sessions) as conn:
        conn.executescript(
            """
            CREATE TABLE identity_sessions (
                sid TEXT PRIMARY KEY,
                method TEXT NOT NULL,
                credential_id TEXT,
                created_at INTEGER NOT NULL,
                last_activity REAL NOT NULL,
                user_agent TEXT,
                source_ip TEXT,
                status TEXT NOT NULL,
                end_reason TEXT,
                ended_at REAL,
                expires_at INTEGER NOT NULL,
                grantee TEXT,
                scope_json TEXT
            );
            PRAGMA user_version = 1;
            """
        )
    assert not (volume / VOLUME_STAMP).exists()

    report = migrate_on_mount(volume, tls=False)
    assert report["from_version"] == 0
    assert report["to_version"] == VOLUME_SCHEMA_VERSION
    stamp = json.loads((volume / VOLUME_STAMP).read_text(encoding="utf-8"))
    assert stamp["schema_version"] == VOLUME_SCHEMA_VERSION
    with sqlite3.connect(sessions) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='dashboard_access_grants'"
        ).fetchone()
    assert (volume / "personal.db").is_file()


def test_newer_volume_is_refused_before_any_mutation(tmp_path):
    volume = tmp_path / "future"
    volume.mkdir()
    sentinel = volume / "sentinel"
    sentinel.write_bytes(b"unchanged")
    stamp = volume / VOLUME_STAMP
    stamp.write_text(
        json.dumps({
            "format": "autonomy-volume",
            "schema_version": VOLUME_SCHEMA_VERSION + 1,
        }),
        encoding="utf-8",
    )
    before = {
        path.relative_to(volume).as_posix(): path.read_bytes()
        for path in volume.rglob("*")
        if path.is_file()
    }
    with pytest.raises(PortabilityError, match="newer"):
        migrate_on_mount(volume, tls=False)
    after = {
        path.relative_to(volume).as_posix(): path.read_bytes()
        for path in volume.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_snapshot_refuses_store_resolved_outside_selected_volume(
    tmp_path, monkeypatch,
):
    volume = tmp_path / "node"
    _seed_node(volume)
    monkeypatch.setenv("DASHBOARD_DB", str(tmp_path / "outside.db"))
    with pytest.raises(PortabilityError, match="outside the selected volume"):
        create_snapshot(
            volume,
            tmp_path / "refused.tar.gz",
            quiesced=True,
        )


def test_deploy_entrypoint_migrates_volume_before_dashboard_start():
    entrypoint = (
        Path(__file__).resolve().parents[2] / "deploy" / "serve.sh"
    ).read_text(encoding="utf-8")
    migrate = "python3 -m tools.portability migrate-on-mount /app/data"
    assert migrate in entrypoint
    # Match the exec line by its shape, not by the server module of the day:
    # this pinned "exec python3 -m uvicorn" and went red on 2026-09-12 when
    # the script began exec'ing the reload wrapper instead.
    start = re.search(r"^exec python3 -m \S+", entrypoint, re.MULTILINE)
    assert start is not None, "serve.sh no longer execs a python module"
    assert entrypoint.index(migrate) < start.start()
