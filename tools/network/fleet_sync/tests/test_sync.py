from pathlib import Path
import json
import sqlite3

import pytest
import tools.network.fleet_sync.sync as sync_module

from tools.graph.db import GraphDB
from tools.network import fleet_roster
from tools.network.idkit import KeyPair, derive_machine_key
from tools.graph.schemas.fleet_roster import FLEET_ROSTER_REVISION
from tools.network.fleet_sync.sync import (
    ALPHA_VERSION,
    AlphaError,
    FleetSyncAlpha,
    install_checkpoint,
    transport_checkpoint_via_raptorq,
)
from tools.network.fleet_sync.compaction import WatermarkError
from tools.network.fleet_sync.streaming import StreamingCodecError


def _author_roster_entry(alpha, entry, *, timestamp_ns: int, tx: str) -> None:
    """Author a fleet-roster entry as an ordinary replicated Setting row.

    The roster is stored as ``raw`` Setting members keyed by each entry's
    content id, so it rides the mutation catalog exactly like any other synced
    row -- which is what lets a kick authored on one machine be preserved
    through another machine's checkpoint merge."""
    with alpha.author(timestamp_ns, tx):
        payload = json.dumps(
            fleet_roster._entry_payload(entry),
            sort_keys=True, separators=(",", ":"),
        )
        alpha.graph.conn.execute(
            "INSERT INTO settings(id,set_id,schema_revision,key,payload,"
            "publication_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entry.entry_id, fleet_roster.FLEET_ROSTER_SET_ID,
                FLEET_ROSTER_REVISION, entry.entry_id, payload, "raw",
                "2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z",
            ),
        )


def _resolved_roster(path: Path, anchor_root_pub: str) -> dict:
    """Read the roster Setting rows straight out of a published DB and resolve.

    Bypasses the org-scoped settings reader so a bare checkpoint-target DB can
    be inspected directly."""
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT payload FROM settings WHERE set_id=?",
            (fleet_roster.FLEET_ROSTER_SET_ID,),
        ).fetchall()
    finally:
        conn.close()
    entries = [
        fleet_roster._entry_from_payload(json.loads(row[0])) for row in rows
    ]
    return fleet_roster.resolve(entries, anchor_root_pub=anchor_root_pub)


def _source(conn, identity: str, title: str) -> None:
    conn.execute(
        "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
        "VALUES(?,?,?,?,?,?)",
        (identity, "note", title, "{}", "2026-08-19T00:00:00Z",
         "2026-08-19T00:00:00Z"),
    )


def _thought(conn, identity: str, source_id: str) -> None:
    conn.execute(
        "INSERT INTO thoughts(id,source_id,content,turn_number,created_at) "
        "VALUES(?,?,?,?,?)",
        (identity, source_id, "body", 0, "2026-08-19T00:00:00Z"),
    )


def _attachment(conn, identity: str, source_id: str) -> None:
    conn.execute(
        "INSERT INTO attachments"
        "(id,hash,filename,size_bytes,file_path,source_id,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (identity, "hash-" + identity, identity + ".png", 10,
         "data/attachments/xx/" + identity + ".png", source_id,
         "2026-08-19T00:00:00Z"),
    )


def _identity(path: Path, marker: str) -> None:
    """Seed a joiner's pre-sync local state: its own personal-identity Setting
    plus the local (never-synced) ``orgs`` row. On a checkpoint receiver the
    Setting is replaced by the checkpoint's own identity row while the local
    ``orgs`` row is preserved through ``_copy_local_state``."""
    graph = GraphDB(path)
    try:
        graph.conn.execute(
            "INSERT INTO settings(id,set_id,schema_revision,key,payload,"
            "publication_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (marker, "autonomy.identity.personal", 1, "root", '{}', "raw",
             "2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z"),
        )
        graph.conn.execute(
            "INSERT INTO orgs(id,slug,type,created_at) VALUES(?,?,?,?)",
            ("local-org", "personal", "personal", "2026-08-19T00:00:00Z"),
        )
        graph.conn.commit()
    finally:
        graph.close()


def _author_identity(origin, marker: str, timestamp_ns: int) -> None:
    """Author a personal-identity Setting through the catalog on *origin*.

    Personal identity is now an ordinary replicated Setting, so its checkpoint
    carry goes through the authored path — exactly like any other synced row
    (see ``test_checkpoint_carries_personal_vault_key_records``). A row merely
    present before activation would be an untracked logical row and would fail
    the checkpoint closed (see
    ``test_preexisting_untracked_rows_fail_checkpoint_closed``)."""
    with origin.author(timestamp_ns, f"identity-{marker}"):
        origin.graph.conn.execute(
            "INSERT INTO settings(id,set_id,schema_revision,key,payload,"
            "publication_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (marker, "autonomy.identity.personal", 1, "root", '{}', "raw",
             "2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z"),
        )


def test_alpha_checkpoint_installs_atomically_and_restarts(tmp_path: Path) -> None:
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target.db"
    _identity(target_path, "target-secret")
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        _author_identity(origin, "origin-secret", 90)
        with origin.author(100, "tx-1"):
            _source(origin.graph.conn, "live", "carried")
            _source(origin.graph.conn, "gone", "temporary")
        with origin.author(110, "tx-2"):
            origin.graph.conn.execute("DELETE FROM sources WHERE id='gone'")
        checkpoint = origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=7,
            active_roster=("machine-a", "machine-b"), target_chunk_bytes=4096,
        )
    assert checkpoint.alpha_version == ALPHA_VERSION
    assert checkpoint.watermark == 110
    # Full checkpoints carry one current winner/tombstone per address, not
    # retired transaction history (identity Setting + live + gone tombstone).
    assert checkpoint.winner_records == 3

    installed = install_checkpoint(
        tmp_path / "checkpoint", target_path,
        target_origin_incarnation="machine-b", expected_roster_epoch=7,
        expected_active_roster=("machine-a", "machine-b"),
    )
    assert installed.manifest_sha256 == checkpoint.manifest_sha256
    with FleetSyncAlpha(target_path, "machine-b") as target:
        assert [tuple(row) for row in target.graph.conn.execute(
            "SELECT id,title FROM sources"
        ).fetchall()] == [("live", "carried")]
        assert [tuple(row) for row in target.graph.conn.execute(
            "SELECT id FROM settings WHERE set_id='autonomy.identity.personal'"
        ).fetchall()] == [("origin-secret",)]
        assert [tuple(row) for row in target.graph.conn.execute(
            "SELECT slug,type FROM orgs"
        ).fetchall()] == [("personal", "personal")]
        forwarded = list(target.catalog.iter_mutations())
        assert {item.origin_incarnation for item in forwarded} == {"machine-a"}
        assert {item.mutation.address for item in forwarded} == {
            ("live",), ("gone",),
            ("autonomy.identity.personal", 1, "root", "raw", "base"),
        }
        with pytest.raises(WatermarkError, match="write refused"):
            with target.author(110, "too-old"):
                pass
        with target.author(111, "after-restart"):
            target.graph.conn.execute(
                "UPDATE sources SET title='new local write' WHERE id='live'"
            )


def test_checkpoint_survives_deprecated_settings_base(tmp_path: Path) -> None:
    """A base Setting leaving the live set must checkpoint as a tombstone.

    The journal-replay test in ``test_catalog`` proves that a tombstone can be
    applied remotely.  The production failure in auto-mnm2a was one boundary
    later: checkpoint construction resolves every non-tombstoned catalog
    winner through ``_live_row``.  If deprecation updates the physical row but
    leaves a live catalog winner behind, this call fails with ``catalog points
    to missing live row: settings`` before any checkpoint can be published.
    """
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target.db"
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with origin.author(100, "create-setting"):
            origin.graph.conn.execute(
                "INSERT INTO settings(id,set_id,schema_revision,key,payload,"
                "publication_state,deprecated,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "setting-a", "dashboard.example", 1, "default",
                    '{"enabled":true}', "raw", 0,
                    "2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z",
                ),
            )
        with origin.author(110, "deprecate-setting"):
            origin.graph.conn.execute(
                "UPDATE settings SET deprecated=1 WHERE id='setting-a'"
            )

        checkpoint = origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=1,
            active_roster=("machine-a", "machine-b"),
            target_chunk_bytes=4096,
        )

    assert checkpoint.winner_records == 1
    install_checkpoint(
        tmp_path / "checkpoint", target_path,
        target_origin_incarnation="machine-b", expected_roster_epoch=1,
        expected_active_roster=("machine-a", "machine-b"),
    )
    with FleetSyncAlpha(target_path, "machine-b") as target:
        assert target.graph.conn.execute(
            "SELECT COUNT(*) FROM settings "
            "WHERE set_id='dashboard.example' AND deprecated=0"
        ).fetchone()[0] == 0
        winners = list(target.catalog.iter_mutations())
        assert len(winners) == 1
        assert winners[0].mutation.address == (
            "dashboard.example", 1, "default", "raw", "base",
        )
        assert winners[0].mutation.tombstone


def test_checkpoint_carries_personal_vault_key_records(tmp_path: Path) -> None:
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target.db"
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with origin.author(100, "vault-records"):
            origin.graph.conn.execute(
                "INSERT INTO policy_classes(class_id,wire) VALUES(?,?)",
                ("class-1", '{"class":1}'),
            )
            origin.graph.conn.execute(
                "INSERT INTO vault_factors(factor_id,factor_type,public_key,armor) "
                "VALUES(?,?,?,?)",
                ("factor-1", "password", "public", "armor"),
            )
            origin.graph.conn.execute(
                "INSERT INTO root_anchors(anchor_id,wire) VALUES(?,?)",
                ("anchor-1", '{"anchor":1}'),
            )
            origin.graph.conn.execute(
                "INSERT INTO vault_secrets(setting_name,wire) VALUES(?,?)",
                ("secret-1", '{"secret":1}'),
            )
        origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=1,
            active_roster=("machine-a", "machine-b"),
        )

    install_checkpoint(
        tmp_path / "checkpoint", target_path,
        target_origin_incarnation="machine-b", expected_roster_epoch=1,
        expected_active_roster=("machine-a", "machine-b"),
    )
    with GraphDB(target_path) as target:
        assert target.conn.execute(
            "SELECT wire FROM policy_classes WHERE class_id='class-1'"
        ).fetchone()[0] == '{"class":1}'
        assert tuple(target.conn.execute(
            "SELECT factor_type,public_key,armor FROM vault_factors "
            "WHERE factor_id='factor-1'"
        ).fetchone()) == ("password", "public", "armor")
        assert target.conn.execute(
            "SELECT wire FROM root_anchors WHERE anchor_id='anchor-1'"
        ).fetchone()[0] == '{"anchor":1}'
        assert target.conn.execute(
            "SELECT wire FROM vault_secrets WHERE setting_name='secret-1'"
        ).fetchone()[0] == '{"secret":1}'


def test_alpha_installs_when_roster_epoch_differs(tmp_path: Path) -> None:
    """A checkpoint whose roster epoch differs from the receiver's installs.

    Roster-epoch equality is a canonical-base-round property, not a checkpoint
    admission rule (auto graph 1155b8f4-8cf): pinning one exact epoch belongs on
    the exact-base barrier, not here. Enforcing it on ordinary checkpoint
    install was the defect that left a fresh joiner -- which holds a smaller
    roster and therefore a different epoch -- unable to ever complete a first
    sync, because the roster that would fix its epoch lives inside the very
    checkpoint being refused.
    """
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target.db"
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with origin.author(1, "tx"):
            _source(origin.graph.conn, "s1", "one")
        checkpoint = origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=3,
            active_roster=("machine-a", "machine-b"),
        )
    installed = install_checkpoint(
        tmp_path / "checkpoint", target_path,
        target_origin_incarnation="machine-b", expected_roster_epoch=4,
        expected_active_roster=("machine-a", "machine-b"),
    )
    assert installed.manifest_sha256 == checkpoint.manifest_sha256
    # The install reports the checkpoint's OWN roster epoch, not the receiver's
    # expected 4 -- the two now legitimately differ.
    assert installed.roster_epoch == 3
    with FleetSyncAlpha(target_path, "machine-b") as target:
        assert target.graph.conn.execute(
            "SELECT title FROM sources WHERE id='s1'"
        ).fetchone()[0] == "one"


def test_two_entry_receiver_installs_four_entry_checkpoint(
    tmp_path: Path,
) -> None:
    """A receiver holding two roster entries installs a checkpoint built with
    four, and the published DB is readable and carries the checkpoint's rows."""
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target.db"
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with origin.author(5, "tx"):
            _source(origin.graph.conn, "wide", "built with four")
        origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=9,
            active_roster=(
                "machine-a", "machine-b", "machine-c", "machine-d",
            ),
        )
    install_checkpoint(
        tmp_path / "checkpoint", target_path,
        target_origin_incarnation="machine-b", expected_roster_epoch=1,
        expected_active_roster=("machine-a", "machine-b"),
    )
    conn = sqlite3.connect(target_path)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute(
            "SELECT title FROM sources WHERE id='wide'"
        ).fetchone()[0] == "built with four"
    finally:
        conn.close()


def test_older_checkpoint_does_not_resurrect_third_machine_kick(
    tmp_path: Path,
) -> None:
    """A kick a receiver already holds survives an older checkpoint's merge.

    This pins the property the deleted roster-equality checks were wrongly
    believed to protect. A kick is an immutable tombstone at its own entry_id
    address; an older checkpoint built before the kick cannot carry a competing
    newer row at that address, and the merge (``merge_existing=True``) re-applies
    the receiver's whole held catalog by last-writer-wins, so a kick authored by
    a THIRD machine is preserved regardless of the checkpoint's origin or epoch.
    """
    root = KeyPair.generate()
    kicked_id = "aa" * 32
    kicked_key = derive_machine_key(bytes.fromhex(root.private_hex), kicked_id)
    enroll = fleet_roster.enroll(
        root, machine_id=kicked_id, machine_pub=kicked_key.public_hex, seq=0
    )
    kick = fleet_roster.kick(
        root, machine_id=kicked_id, machine_pub=kicked_key.public_hex, seq=1
    )
    # Sanity: the enroll alone resolves the machine as active, so only the kick
    # keeps it out of the roster.
    assert kicked_key.public_hex in fleet_roster.resolve(
        [enroll], anchor_root_pub=root.public_hex
    )

    # A THIRD machine (machine-c, not the receiver) authors both the enroll and
    # the later kick and ships them in a checkpoint the receiver installs.
    kick_checkpoint = tmp_path / "kick-checkpoint"
    with FleetSyncAlpha(tmp_path / "machine-c.db", "machine-c") as mc:
        _author_roster_entry(mc, enroll, timestamp_ns=100, tx="c-enroll")
        _author_roster_entry(mc, kick, timestamp_ns=200, tx="c-kick")
        mc.checkpoint(
            kick_checkpoint, roster_epoch=2,
            active_roster=("machine-c", "machine-b"),
        )
    target_path = tmp_path / "target.db"
    install_checkpoint(
        kick_checkpoint, target_path,
        target_origin_incarnation="machine-b", expected_roster_epoch=2,
        expected_active_roster=("machine-c", "machine-b"),
    )
    assert kicked_key.public_hex not in _resolved_roster(
        target_path, root.public_hex
    )

    # machine-a built an OLDER checkpoint before the kick existed: it carries
    # the enroll but not the kick.
    old_checkpoint = tmp_path / "old-checkpoint"
    with FleetSyncAlpha(tmp_path / "machine-a.db", "machine-a") as ma:
        _author_roster_entry(ma, enroll, timestamp_ns=100, tx="a-enroll")
        ma.checkpoint(
            old_checkpoint, roster_epoch=1,
            active_roster=("machine-a", "machine-b"),
        )
    # Installing the older checkpoint MERGES; the receiver's held kick is not
    # erased, so the machine still resolves as kicked.
    install_checkpoint(
        old_checkpoint, target_path,
        target_origin_incarnation="machine-b", expected_roster_epoch=1,
        expected_active_roster=("machine-a", "machine-b"),
        merge_existing=True,
    )
    assert kicked_key.public_hex not in _resolved_roster(
        target_path, root.public_hex
    )
    conn = sqlite3.connect(target_path)
    try:
        keys = {
            row[0] for row in conn.execute(
                "SELECT key FROM settings WHERE set_id=?",
                (fleet_roster.FLEET_ROSTER_SET_ID,),
            )
        }
    finally:
        conn.close()
    assert {enroll.entry_id, kick.entry_id} <= keys


def test_full_checkpoint_survives_retired_transaction_journal(
    tmp_path: Path,
) -> None:
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target.db"
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with origin.author(10, "old-history"):
            _source(origin.graph.conn, "stable", "current winner")
        assert origin.catalog.prune_journal(10) == 1
        checkpoint = origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=2,
            active_roster=("machine-a", "machine-b"), target_chunk_bytes=4096,
        )
        assert checkpoint.winner_records == 1
    install_checkpoint(
        tmp_path / "checkpoint", target_path,
        target_origin_incarnation="machine-b", expected_roster_epoch=2,
        expected_active_roster=("machine-a", "machine-b"),
    )
    with FleetSyncAlpha(target_path, "machine-b") as target:
        assert target.graph.conn.execute(
            "SELECT title FROM sources WHERE id='stable'"
        ).fetchone()[0] == "current winner"
        relayed = list(target.catalog.iter_mutations())
        assert len(relayed) == 1
        assert relayed[0].mutation.timestamp_ns == 10


def test_checkpoint_installs_despite_active_roster_difference(
    tmp_path: Path,
) -> None:
    """A checkpoint built under a different active set installs. The active-set
    equality gate was a base-round rule mis-applied to checkpoint install; a
    receiver whose roster differs from the checkpoint's is not an admission
    fault, only a base-acknowledgment one (auto graph 1155b8f4-8cf)."""
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target.db"
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with origin.author(1, "tx"):
            _source(origin.graph.conn, "s1", "one")
        origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=3,
            active_roster=("machine-a", "machine-b"),
        )
    install_checkpoint(
        tmp_path / "checkpoint", target_path,
        target_origin_incarnation="machine-c", expected_roster_epoch=3,
        expected_active_roster=("machine-a", "machine-c"),
    )
    with FleetSyncAlpha(target_path, "machine-c") as target:
        assert target.graph.conn.execute(
            "SELECT title FROM sources WHERE id='s1'"
        ).fetchone()[0] == "one"


def test_failed_install_preserves_target_and_removes_staging(
    tmp_path: Path,
) -> None:
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target.db"
    _identity(target_path, "target-secret")
    before = target_path.read_bytes()
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with origin.author(1, "tx"):
            _source(origin.graph.conn, "s1", "one")
        origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=3,
            active_roster=("machine-a", "machine-b"),
            target_chunk_bytes=4096,
        )
    chunk = next((tmp_path / "checkpoint" / "base").glob("*.base"))
    damaged = bytearray(chunk.read_bytes())
    damaged[-1] ^= 1
    chunk.write_bytes(damaged)
    with pytest.raises(StreamingCodecError, match="digest"):
        install_checkpoint(
            tmp_path / "checkpoint", target_path,
            target_origin_incarnation="machine-b", expected_roster_epoch=3,
            expected_active_roster=("machine-a", "machine-b"),
        )
    assert target_path.read_bytes() == before
    assert not list(tmp_path.glob(".fleet-sync-install-*"))


def test_transport_refuses_noncanonical_artifact_path_without_partial_target(
    tmp_path: Path,
) -> None:
    origin_path = tmp_path / "origin.db"
    checkpoint_path = tmp_path / "checkpoint"
    target = tmp_path / "received"
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with origin.author(1, "tx"):
            _source(origin.graph.conn, "s1", "one")
        origin.checkpoint(
            checkpoint_path, roster_epoch=3,
            active_roster=("machine-a", "machine-b"),
            target_chunk_bytes=4096,
        )
    manifest_path = checkpoint_path / "alpha-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["base"]["chunks"][0]["filename"] = "../../outside"
    manifest_path.write_text(json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ))
    with pytest.raises(AlphaError, match="filename"):
        transport_checkpoint_via_raptorq(
            checkpoint_path, target, symbol_size=256
        )
    assert not target.exists()
    assert not (tmp_path / "outside").exists()


def test_failed_checkpoint_build_publishes_no_partial_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin_path = tmp_path / "origin.db"
    checkpoint = tmp_path / "checkpoint"
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with origin.author(1, "tx"):
            _source(origin.graph.conn, "s1", "one")

        def fail(*_args, **_kwargs):
            raise RuntimeError("injected winner failure")

        monkeypatch.setattr(sync_module, "stream_winners_to_chunks", fail)
        with pytest.raises(RuntimeError, match="injected winner failure"):
            origin.checkpoint(
                checkpoint, roster_epoch=3,
                active_roster=("machine-a", "machine-b"),
                target_chunk_bytes=4096,
            )
    assert not checkpoint.exists()
    assert not list(tmp_path.glob(".fleet-sync-checkpoint-*"))


def test_checkpoint_install_skips_and_quarantines_foreign_key_orphans(
    tmp_path: Path,
) -> None:
    """A thought whose source was deleted long ago without cascade — legacy
    foreign-key-off debris — must not abort the whole checkpoint. The receiver
    skips it, keeps the catalog exactly consistent with the rows that landed,
    and quarantines it for later repair."""
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target.db"
    # This test isolates the quarantine path, so the origin carries only the
    # authored rows counted below — no identity Setting rides this checkpoint.
    _identity(target_path, "target-secret")
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        conn = origin.graph.conn
        with origin.author(100, "tx-good"):
            _source(conn, "src-live", "kept")
            _thought(conn, "t-good", "src-live")
        # Simulate historical debris: a thought whose parent source is absent,
        # still tracked by the catalog (SQLite shipped with foreign keys off, so
        # such orphans accumulated unnoticed and now ride the checkpoint).
        conn.execute("PRAGMA foreign_keys=OFF")
        with origin.author(105, "tx-orphan"):
            _thought(conn, "t-orphan", "src-missing")
        conn.execute("PRAGMA foreign_keys=ON")
        checkpoint = origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=7,
            active_roster=("machine-a", "machine-b"), target_chunk_bytes=4096,
        )
    # The origin tracked the orphan, so it is part of the checkpoint's base.
    assert checkpoint.winner_records == 3  # src-live, t-good, t-orphan

    installed = install_checkpoint(
        tmp_path / "checkpoint", target_path,
        target_origin_incarnation="machine-b", expected_roster_epoch=7,
        expected_active_roster=("machine-a", "machine-b"),
    )
    assert installed.manifest_sha256 == checkpoint.manifest_sha256
    with FleetSyncAlpha(target_path, "machine-b") as target:
        tconn = target.graph.conn
        # The orphan was skipped; the representable rows landed intact.
        assert [r[0] for r in tconn.execute(
            "SELECT id FROM thoughts ORDER BY id"
        )] == ["t-good"]
        assert [r[0] for r in tconn.execute(
            "SELECT id FROM sources ORDER BY id"
        )] == ["src-live"]
        # The catalog is exactly consistent with what landed — the orphan is
        # neither a data row nor a live catalog entry.
        live = tconn.execute(
            "SELECT COUNT(*) FROM fleet_sync_catalog WHERE tombstone=0"
        ).fetchone()[0]
        assert live == 2  # src-live + t-good
        # The retained skip delta is the quarantine row count — a later canary
        # reads COUNT(*) here instead of re-scanning foreign keys.
        quarantined = [tuple(r) for r in tconn.execute(
            "SELECT table_name,logical_address,reason FROM fleet_sync_quarantine"
        )]
        assert quarantined == [
            ("thoughts", json.dumps(["t-orphan"]), "fk_orphan")
        ]


def test_checkpoint_install_quarantines_attachments_without_blob_bytes(
    tmp_path: Path,
) -> None:
    """When blob transfer is not wired (blob_store is None), an attachment's
    external bytes are unavailable and its NOT-NULL file_path cannot be filled.
    The install must not abort: the attachment row is skipped and quarantined
    for a later fetch, while the representable rows land and the catalog stays
    consistent."""
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target.db"
    # This test isolates the quarantine path, so the origin carries only the
    # authored rows counted below — no identity Setting rides this checkpoint.
    _identity(target_path, "target-secret")
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        conn = origin.graph.conn
        with origin.author(100, "tx-1"):
            _source(conn, "src", "kept")
            _thought(conn, "t1", "src")
            _attachment(conn, "att1", "src")
        checkpoint = origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=7,
            active_roster=("machine-a", "machine-b"), target_chunk_bytes=4096,
        )
    assert checkpoint.winner_records == 3  # src, t1, att1

    # No blob_store: attachment bytes are unavailable on the receiver.
    install_checkpoint(
        tmp_path / "checkpoint", target_path,
        target_origin_incarnation="machine-b", expected_roster_epoch=7,
        expected_active_roster=("machine-a", "machine-b"),
    )
    with FleetSyncAlpha(target_path, "machine-b") as target:
        tconn = target.graph.conn
        assert [r[0] for r in tconn.execute("SELECT id FROM sources")] == ["src"]
        assert [r[0] for r in tconn.execute("SELECT id FROM thoughts")] == ["t1"]
        # The attachment row was skipped (bytes unavailable), not installed.
        assert tconn.execute(
            "SELECT COUNT(*) FROM attachments"
        ).fetchone()[0] == 0
        # Catalog stays consistent with what landed: src + t1 live, att1 out.
        assert tconn.execute(
            "SELECT COUNT(*) FROM fleet_sync_catalog WHERE tombstone=0"
        ).fetchone()[0] == 2
        quarantined = [tuple(r) for r in tconn.execute(
            "SELECT table_name,logical_address,reason FROM fleet_sync_quarantine"
        )]
        assert quarantined == [
            ("attachments", json.dumps(["att1"]),
             "attachment_bytes_unavailable")
        ]


def test_preexisting_untracked_rows_fail_checkpoint_closed(tmp_path: Path) -> None:
    origin_path = tmp_path / "origin.db"
    graph = GraphDB(origin_path)
    try:
        _source(graph.conn, "legacy", "predates catalog")
        graph.conn.commit()
    finally:
        graph.close()

    checkpoint = tmp_path / "checkpoint"
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with pytest.raises(AlphaError, match="untracked logical rows"):
            origin.checkpoint(
                checkpoint, roster_epoch=3,
                active_roster=("machine-a", "machine-b"),
                target_chunk_bytes=4096,
            )
    assert not checkpoint.exists()


def test_checkpoint_abort_leaves_no_artifact_or_staging(tmp_path: Path) -> None:
    """A withdrawn consumer stops the build cooperatively: CheckpointAborted
    surfaces, no checkpoint directory appears, no staging dir leaks."""
    from tools.network.fleet_sync.sync import CheckpointAborted

    with FleetSyncAlpha(tmp_path / "origin.db", "machine-a") as origin:
        with origin.author(100, "tx-1"):
            _source(origin.graph.conn, "row-1", "one")
            _source(origin.graph.conn, "row-2", "two")
        with pytest.raises(CheckpointAborted):
            origin.checkpoint(
                tmp_path / "checkpoint", roster_epoch=7,
                active_roster=("machine-a",), should_abort=lambda: True,
            )
    assert not (tmp_path / "checkpoint").exists()
    assert not [
        p for p in tmp_path.iterdir()
        if p.name.startswith(".fleet-sync-checkpoint-")
    ]


def test_checkpoint_quiet_abort_callable_builds_normally(tmp_path: Path) -> None:
    """should_abort that never fires must not change the build's result."""
    with FleetSyncAlpha(tmp_path / "origin.db", "machine-a") as origin:
        with origin.author(100, "tx-1"):
            _source(origin.graph.conn, "live", "carried")
        checkpoint = origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=7,
            active_roster=("machine-a",), should_abort=lambda: False,
        )
    assert checkpoint.watermark == 100
    assert (tmp_path / "checkpoint").exists()


def test_checkpoint_tolerates_duplicate_key_rows(tmp_path: Path) -> None:
    """note_versions' policy key (source_id, created_at, content_hash) is not
    the table's uniqueness (source_id, version): two versions with identical
    content and timestamp share one catalog address. The base streams both
    rows, the catalog holds one winner, and the coverage guard must net the
    duplicate out instead of refusing the whole store (live 2026-09-06: 3
    such March-era rows made a 2.8M-row checkpoint fail on every build)."""
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target.db"
    _identity(target_path, "target-secret")
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        _author_identity(origin, "origin-secret", 90)
        with origin.author(100, "tx-1"):
            _source(origin.graph.conn, "note-1", "a note")
        for version in (1, 2, 3):
            with origin.author(100 + version, f"tx-v{version}"):
                origin.graph.conn.execute(
                    "INSERT INTO note_versions(source_id,version,content,"
                    "created_at) VALUES(?,?,?,?)",
                    ("note-1", version, "same body", "2026-03-30T16:07:29Z"),
                )
        checkpoint = origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=7,
            active_roster=("machine-a", "machine-b"), target_chunk_bytes=4096,
        )
    # identity + source + 3 version rows streamed; 2 of them duplicate-key
    assert checkpoint.base_records == 5
    installed = install_checkpoint(
        tmp_path / "checkpoint", target_path,
        target_origin_incarnation="machine-b", expected_roster_epoch=7,
        expected_active_roster=("machine-a", "machine-b"),
    )
    assert installed.manifest_sha256 == checkpoint.manifest_sha256
    with sqlite3.connect(target_path) as conn:
        # The receiver realizes ONE row per logical address: duplicate-key
        # source rows (identical content and timestamp) collapse to their
        # logical row, exactly one winner tracks it, and nothing is lost —
        # the source keeps its redundant copies, which are inert.
        rows = conn.execute(
            "SELECT content FROM note_versions WHERE source_id='note-1'"
        ).fetchall()
        assert [r[0] for r in rows] == ["same body"]
        assert conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_catalog WHERE tombstone=0"
        ).fetchone()[0] == 3  # identity + source + the one version address
