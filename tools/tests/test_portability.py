"""End-to-end acceptance for portable node-volume snapshot and restore."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tarfile
import time
from pathlib import Path

import pytest

from tools.data_paths import STORE_MANIFEST
from tools.graph.db import GraphDB, _SCHEMA_USER_VERSION
from tools.graph.models import Source
from tools.network.idkit import (
    KeyPair,
    Subject,
    derive_persona,
    issue_cert,
    verify_signature,
)
from tools.network.idkit.armor import decrypt_root_key, encrypt_root_key
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
        "armored_private_key": encrypt_root_key(
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
        "armored_private_key": encrypt_root_key(
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

    # The tunnel delegate is a filesystem secret plus an inline public cert.
    # Only the portable basename enters Settings.
    delegate = KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(
        org_root,
        delegate.public_hex,
        scope=("tunnel:serve",),
        org=PORTABLE_ORG_UUID,
        # serve-cert v2 requires a canonical org persona subject (not an
        # operator label) — use the founded org's persona.
        subject=Subject("persona", founded.founder_persona_pub),
        not_before=now - 60,
        not_after=now + 86_400,
    )
    # v2 also requires an identity-neutral viewer cert: same delegate key,
    # org and validity window as the serve cert, subject {operator, child_pub}.
    viewer_cert = issue_cert(
        org_root,
        delegate.public_hex,
        scope=("tunnel:serve",),
        org=PORTABLE_ORG_UUID,
        subject=Subject("operator", delegate.public_hex),
        not_before=now - 60,
        not_after=now + 86_400,
    )
    key_file = f"serve-{PORTABLE_ORG_UUID}.key"
    key_path = volume / "network" / key_file
    key_path.parent.mkdir(mode=0o700)
    key_path.write_text(delegate.private_hex, encoding="ascii")
    key_path.chmod(0o600)
    serve_payload = json.dumps({
        "cert": cert.to_json().decode("ascii"),
        "viewer_cert": viewer_cert.to_json().decode("ascii"),
        "key_path": key_file,
        "root_pub": org_root.public_hex,
        "not_after": cert.not_after,
    }, sort_keys=True)
    with sqlite3.connect(org_db) as conn:
        conn.execute(
            "INSERT INTO settings("
            "id,set_id,schema_revision,key,payload,created_at,updated_at,"
            "publication_state"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (
                "portable-serving-key",
                "autonomy.network.serve-cert",
                2,  # NETWORK_SERVE_CERT_REVISION — read_set filters by the
                    # current revision, so a stale rev-1 row reads as missing
                "default",
                serve_payload,
                "2026-07-26T00:00:00Z",
                "2026-07-26T00:00:00Z",
            "canonical",
            ),
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
        "serve_key_file": key_file,
        "serve_key_bytes": delegate.private_hex.encode("ascii"),
    }


def _assert_same_node(volume: Path, expected: dict) -> None:
    with sqlite3.connect(volume / "personal.db") as conn:
        row = conn.execute(
            "SELECT payload FROM settings WHERE id='portable-identity'"
        ).fetchone()
    assert row == (expected["identity_payload"],)
    identity = json.loads(row[0])
    personal_root = decrypt_root_key(
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
    org_root = decrypt_root_key(
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
    restored_serve_key = volume / "network" / expected["serve_key_file"]
    assert restored_serve_key.read_bytes() == expected["serve_key_bytes"]
    assert (restored_serve_key.stat().st_mode & 0o777) == 0o600


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
    assert state["key_path"] == str(
        node_b / "network" / expected["serve_key_file"]
    )
    assert not state["key_path"].startswith(str(node_a))
    assert supervisor._verify_key_matches(
        state["cert"], state["viewer_cert"], state["key_path"]
    ) == (True, "ok")
    materialized_cert = Path(supervisor._materialize_cert(
        state["key_path"], state["cert"]
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
        serve_payload = json.loads(conn.execute(
            "SELECT payload FROM settings WHERE id='portable-serving-key'"
        ).fetchone()[0])
    serve_key_file = serve_payload["key_path"]
    return {
        "identity_payload": identity_payload,
        "org_key_payload": org_key_payload,
        "founder": founder,
        "genesis": genesis,
        "serve_key_file": serve_key_file,
        "serve_key_bytes": (
            volume / "network" / serve_key_file
        ).read_bytes(),
    }


def test_migrate_on_mount_upgrades_legacy_volume_and_stamps_it(tmp_path):
    volume = tmp_path / "legacy"
    volume.mkdir()
    (volume / "orgs").mkdir()
    other_org = volume / "orgs" / "second-org.db"
    GraphDB(other_org).close()
    with sqlite3.connect(other_org) as conn:
        conn.execute("PRAGMA user_version = 0")
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
    with sqlite3.connect(other_org) as conn:
        # A graph org DB heals to the CURRENT graph schema version on
        # open (crypto's fleet-wide self-heal), not the version this
        # test was written against.
        assert conn.execute("PRAGMA user_version").fetchone()[0] == _SCHEMA_USER_VERSION


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
        Path(__file__).resolve().parents[2] / "deploy" / "entrypoint.sh"
    ).read_text(encoding="utf-8")
    migrate = "python3 -m tools.portability migrate-on-mount /app/data"
    assert migrate in entrypoint
    assert entrypoint.index(migrate) < entrypoint.index("exec python3 -m uvicorn")
