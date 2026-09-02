"""End-to-end 1.0-alpha checkpoint lifecycle for personal GraphDB sync.

This is the executable engine boundary: authored SQLite writes, one atomic WAL
cut, independently coded base/winner and hot-delta chunks, bounded staging
realization, and atomic publication. Roster enrollment, live scheduling, and
RelayKit channel establishment are supplied by production components; address
and checkpoint-offer discovery remain injected boundaries. This module is the
format and bounded checkpoint/delta engine beneath them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from typing import Iterator

from tools.graph.db import GraphDB
from raptorq import Decoder
from tools.network.swarmkit.fountain import FountainStore, source_symbols

from .catalog import MutationCatalog, ensure_quarantine_table, quarantine_unrealized
from .codec import encode_value
from .compaction import WatermarkError
from .delta import DeltaCatalog, read_delta_catalog, stream_delta_to_chunks
from .materialize import ContentAddressedBlobStore
from .policies import PolicyKind, TABLE_POLICIES
from .streaming import (
    BaseCatalog,
    ensure_streaming_indexes,
    materialize_catalog,
    stream_snapshot_to_chunks,
)
from .winners import (
    WinnerCatalog, WinnerChunkEntry, install_winner_catalog,
    stream_winners_to_chunks,
)


ALPHA_VERSION = "1.0-alpha.1"

_log = logging.getLogger(__name__)


class AlphaError(RuntimeError):
    pass


def _strict_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AlphaError(f"{label} must be a non-negative integer")
    return value


def _strict_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AlphaError(f"{label} must be non-empty text")
    return value


def _strict_roster_epoch(value: object) -> int | str:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if (
        isinstance(value, str)
        and len(value) == 64
        and all(ch in "0123456789abcdef" for ch in value)
    ):
        return value
    raise AlphaError("roster epoch must be a non-negative integer or digest")


@dataclass(frozen=True)
class AlphaCheckpoint:
    alpha_version: str
    roster_epoch: int | str
    roster_hash: str
    origin_incarnation: str
    watermark: int
    base_root: str
    winner_root: str
    base_records: int
    winner_records: int
    manifest_sha256: str


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_manifest(directory: Path, body: dict[str, object]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    temporary = directory / ".alpha-manifest.tmp"
    with temporary.open("wb") as handle:
        handle.write(canonical)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(directory / "alpha-manifest.json")
    _fsync_directory(directory)
    return digest


def _roster_hash(
    roster_epoch: int | str, active_roster: tuple[str, ...]
) -> str:
    active = tuple(sorted(active_roster))
    if not active or len(active) != len(set(active)) or any(not item for item in active):
        raise AlphaError("active roster must contain unique non-empty machine ids")
    body = json.dumps(
        {"active": active, "epoch": roster_epoch},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    return hashlib.sha256(body).hexdigest()


def _read_manifest(directory: Path) -> tuple[dict[str, object], str]:
    try:
        body = (directory / "alpha-manifest.json").read_bytes()
        value = json.loads(body)
    except (OSError, json.JSONDecodeError) as exc:
        raise AlphaError("alpha manifest is unreadable") from exc
    if not isinstance(value, dict):
        raise AlphaError("alpha manifest must be an object")
    if json.dumps(value, sort_keys=True, separators=(",", ":")).encode() != body:
        raise AlphaError("alpha manifest is not canonical JSON")
    return value, hashlib.sha256(body).hexdigest()


def _raptorq_copy(source: Path, target: Path, symbol_size: int) -> None:
    payload = source.read_bytes()
    store = FountainStore(stripe=0, n_stripes=1)
    artifact = store.add_object(payload, symbol_size=symbol_size)
    manifest = store.manifest(artifact)
    if manifest is None:
        raise AlphaError("RaptorQ manifest disappeared")
    decoder = Decoder.with_defaults(manifest["size"], manifest["symbol_size"])
    decoded = None
    # Asking the current Python wrapper for an arbitrary huge batch forces it
    # to materialize that whole repair prefix before the decoder sees packet
    # one.  Systematic K plus a small bounded repair allowance is sufficient
    # for this lossless in-process transport and keeps work proportional.
    packet_budget = source_symbols(manifest) + 16
    for packet in store.serve(artifact, packet_budget, []):
        decoded = decoder.decode(packet)
        if decoded is not None:
            break
    if decoded is None or bytes(decoded) != payload:
        raise AlphaError("RaptorQ checkpoint object did not reconstruct")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name("." + target.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(decoded)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(target)


def transport_checkpoint_via_raptorq(
    source: Path, target: Path, *, symbol_size: int = 8192
) -> None:
    """Reconstruct every immutable alpha artifact through real RaptorQ."""
    if target.exists():
        raise AlphaError("RaptorQ target already exists")
    manifest, _ = _read_manifest(source)
    base = _base_catalog(manifest.get("base"))
    winners = _winner_catalog(manifest.get("winners"))
    _verify_catalog_file(
        source / "base" / "catalog.json", manifest.get("base"), "base"
    )
    _verify_catalog_file(
        source / "winners" / "winner-catalog.json",
        manifest.get("winners"), "winner",
    )
    files = [
        "alpha-manifest.json", "base/catalog.json",
        "winners/winner-catalog.json",
    ]
    files.extend(f"base/{entry.filename}" for entry in base.chunks)
    files.extend(f"winners/{entry.filename}" for entry in winners.chunks)
    _transport_files(source, target, files, symbol_size=symbol_size)


def transport_delta_via_raptorq(
    source: Path, target: Path, *, symbol_size: int = 8192
) -> None:
    if target.exists():
        raise AlphaError("RaptorQ delta target already exists")
    catalog = read_delta_catalog(source)
    files = ["delta-catalog.json"] + _delta_filenames(catalog)
    _transport_files(source, target, files, symbol_size=symbol_size)


def transport_delta_reliable(source: Path, target: Path) -> None:
    """Move a small hot delta over the reliable-channel shape, without RaptorQ."""
    if target.exists():
        raise AlphaError("delta target already exists")
    catalog = read_delta_catalog(source)
    files = ["delta-catalog.json"] + _delta_filenames(catalog)
    _transport_files(source, target, files, symbol_size=None)


def _transport_files(
    source: Path, target: Path, files: list[str], *, symbol_size: int | None
) -> None:
    """Stage a complete artifact set and publish no partial destination."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(tempfile.mkdtemp(
        prefix=".fleet-sync-receive-", dir=target.parent
    ))
    stage = temporary_root / target.name
    stage.mkdir()
    try:
        for filename in files:
            destination = stage / filename
            if symbol_size is not None:
                _raptorq_copy(source / filename, destination, symbol_size)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name("." + destination.name + ".tmp")
            with (source / filename).open("rb") as incoming, temporary.open("wb") as outgoing:
                for block in iter(lambda: incoming.read(1024 * 1024), b""):
                    outgoing.write(block)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            temporary.replace(destination)
        _fsync_directory(stage)
        os.replace(stage, target)
        _fsync_directory(target.parent)
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def _delta_filenames(catalog: DeltaCatalog) -> list[str]:
    names: list[str] = []
    for entry in catalog.chunks:
        if entry.filename != f"{entry.sequence:08d}-{entry.sha256}.delta":
            raise AlphaError("delta artifact filename is not canonical")
        names.append(entry.filename)
    return names


def _base_catalog(value: object) -> BaseCatalog:
    if not isinstance(value, dict):
        raise AlphaError("base catalog has wrong shape")
    from .streaming import ChunkEntry
    try:
        catalog = BaseCatalog(
            version=_strict_int(value["version"], "base version"),
            policy_digest=_strict_text(value["policy_digest"], "base policy"),
            target_chunk_bytes=_strict_int(value["target_chunk_bytes"], "base chunk size"),
            total_records=_strict_int(value["total_records"], "base record count"),
            total_bytes=_strict_int(value["total_bytes"], "base byte count"),
            root_sha256=_strict_text(value["root_sha256"], "base root"),
            chunks=tuple(ChunkEntry(**entry) for entry in value["chunks"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AlphaError("base catalog has wrong shape") from exc
    for entry in catalog.chunks:
        if entry.filename != f"{entry.sequence:08d}-{entry.sha256}.base":
            raise AlphaError("base artifact filename is not canonical")
    return catalog


def _winner_catalog(value: object) -> WinnerCatalog:
    if not isinstance(value, dict):
        raise AlphaError("winner catalog has wrong shape")
    try:
        catalog = WinnerCatalog(
            version=_strict_int(value["version"], "winner version"),
            policy_digest=_strict_text(value["policy_digest"], "winner policy"),
            through_watermark=_strict_int(value["through_watermark"], "winner watermark"),
            target_chunk_bytes=_strict_int(value["target_chunk_bytes"], "winner chunk size"),
            total_records=_strict_int(value["total_records"], "winner record count"),
            total_bytes=_strict_int(value["total_bytes"], "winner byte count"),
            root_sha256=_strict_text(value["root_sha256"], "winner root"),
            chunks=tuple(WinnerChunkEntry(**entry) for entry in value["chunks"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AlphaError("winner catalog has wrong shape") from exc
    for entry in catalog.chunks:
        if entry.filename != f"{entry.sequence:08d}-{entry.sha256}.winner":
            raise AlphaError("winner artifact filename is not canonical")
    return catalog


def _verify_catalog_file(path: Path, embedded: object, label: str) -> None:
    try:
        body = path.read_bytes()
        value = json.loads(body)
    except (OSError, json.JSONDecodeError) as exc:
        raise AlphaError(f"{label} catalog is unreadable") from exc
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if body != canonical or value != embedded:
        raise AlphaError(f"{label} catalog does not match alpha manifest")


class FleetSyncAlpha:
    def __init__(self, path: Path, origin_incarnation: str) -> None:
        self.path = path
        # Alpha owns an explicit caller-supplied authored context. Production
        # GraphDB opens auto-attach the connection-bound writer hook instead.
        self.graph = GraphDB(path, attach_fleet_sync=False)
        self.catalog = MutationCatalog(self.graph.conn, origin_incarnation)
        self.catalog.install()
        ensure_streaming_indexes(self.graph.conn)

    def close(self) -> None:
        self.graph.close()

    def __enter__(self) -> "FleetSyncAlpha":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def author(self, timestamp_ns: int, transaction_id: str):
        return self.catalog.transaction(timestamp_ns, transaction_id)

    def checkpoint(
        self,
        directory: Path,
        *,
        roster_epoch: int | str,
        active_roster: tuple[str, ...],
        target_chunk_bytes: int = 4 * 1024 * 1024,
    ) -> AlphaCheckpoint:
        if directory.exists():
            raise AlphaError("checkpoint directory already exists")
        roster_hash = _roster_hash(roster_epoch, active_roster)
        if self.catalog.origin_incarnation not in active_roster:
            raise AlphaError("checkpoint origin is absent from active roster")
        directory.parent.mkdir(parents=True, exist_ok=True)
        temporary_root = Path(tempfile.mkdtemp(
            prefix=".fleet-sync-checkpoint-", dir=directory.parent
        ))
        stage = temporary_root / directory.name
        stage.mkdir()
        try:
            with self.catalog.freeze_cut() as cut:
                base = stream_snapshot_to_chunks(
                    cut.reader, stage / "base",
                    target_chunk_bytes=target_chunk_bytes,
                )
                winners = stream_winners_to_chunks(
                    # Payload is already in the key-ordered base.  This
                    # carries only replication metadata and tombstones.
                    self.catalog.iter_winner_metadata(cut), stage / "winners",
                    through_watermark=cut.watermark,
                    target_chunk_bytes=target_chunk_bytes,
                )
                tracked_live = int(cut.reader.execute(
                    "SELECT COUNT(*) FROM fleet_sync_catalog "
                    "WHERE tombstone=0 AND timestamp_ns<=?",
                    (cut.watermark,),
                ).fetchone()[0])
                if tracked_live != base.total_records:
                    raise AlphaError(
                        "checkpoint contains untracked logical rows; bootstrap "
                        "the mutation catalog before checkpointing"
                    )
            body: dict[str, object] = {
                "alpha_version": ALPHA_VERSION,
                "roster_epoch": roster_epoch,
                "roster_hash": roster_hash,
                "origin_incarnation": self.catalog.origin_incarnation,
                "watermark": cut.watermark,
                "base": {
                    **asdict(base), "chunks": [asdict(c) for c in base.chunks]
                },
                "winners": {
                    **asdict(winners),
                    "chunks": [asdict(c) for c in winners.chunks],
                },
            }
            digest = _write_manifest(stage, body)
            os.replace(stage, directory)
            _fsync_directory(directory.parent)
            return AlphaCheckpoint(
                ALPHA_VERSION, roster_epoch, roster_hash,
                self.catalog.origin_incarnation,
                cut.watermark, base.root_sha256, winners.root_sha256,
                base.total_records, winners.total_records, digest,
            )
        finally:
            shutil.rmtree(temporary_root, ignore_errors=True)

    def delta(
        self,
        directory: Path,
        *,
        after_watermark: int,
        target_chunk_bytes: int = 4 * 1024 * 1024,
    ) -> DeltaCatalog:
        if directory.exists():
            raise AlphaError("delta directory already exists")
        directory.parent.mkdir(parents=True, exist_ok=True)
        temporary_root = Path(tempfile.mkdtemp(
            prefix=".fleet-sync-delta-", dir=directory.parent
        ))
        stage = temporary_root / directory.name
        stage.mkdir()
        try:
            with self.catalog.freeze_cut() as cut:
                catalog = stream_delta_to_chunks(
                    self.catalog.iter_journal(
                        cut, after_watermark=after_watermark
                    ),
                    stage, through_watermark=cut.watermark,
                    target_chunk_bytes=target_chunk_bytes,
                )
            os.replace(stage, directory)
            _fsync_directory(directory.parent)
            return catalog
        finally:
            shutil.rmtree(temporary_root, ignore_errors=True)


def _copy_local_state(source: Path, target: sqlite3.Connection) -> None:
    if not source.exists():
        return
    local = sqlite3.connect(source)
    local.row_factory = sqlite3.Row
    try:
        for raw in local.execute("SELECT * FROM orgs"):
            row = dict(raw)
            columns = sorted(row)
            target.execute(
                "INSERT INTO orgs(" + ",".join(f'\"{c}\"' for c in columns)
                + ") VALUES(" + ",".join("?" for _ in columns) + ")",
                [row[column] for column in columns],
            )
        local_tables = {
            str(row[0]) for row in local.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table in (
            "keycontrol_meta", "keycontrol_pending", "keycontrol_pending_usage"
        ):
            if table not in local_tables:
                continue
            for raw in local.execute(f'SELECT * FROM "{table}"'):
                row = dict(raw)
                columns = sorted(row)
                target.execute(
                    f'INSERT INTO "{table}"('
                    + ",".join(f'"{column}"' for column in columns)
                    + ") VALUES("
                    + ",".join("?" for _ in columns)
                    + ")",
                    [row[column] for column in columns],
                )
        target.commit()
        local.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        local.close()


def _copy_peer_state(source: Path, target: sqlite3.Connection) -> None:
    """Carry machine-local scheduler evidence across database publication."""
    if not source.exists():
        return
    local = sqlite3.connect(source)
    local.row_factory = sqlite3.Row
    try:
        exists = local.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='fleet_sync_peer_state'"
        ).fetchone()
        if exists is None:
            return
        for raw in local.execute("SELECT * FROM fleet_sync_peer_state"):
            row = dict(raw)
            # Served acknowledgements are local journal transaction row ids,
            # and the staging database renumbers them; a stale ref could
            # authorize pruning frames a peer never consumed. Peers
            # re-establish their acknowledgement on their next pull.
            if "local_watermark" in row:
                row["local_watermark"] = None
            columns = sorted(row)
            target.execute(
                "INSERT INTO fleet_sync_peer_state("
                + ",".join(f'\"{column}\"' for column in columns)
                + ") VALUES("
                + ",".join("?" for _ in columns)
                + ")",
                [row[column] for column in columns],
            )
        target.commit()
    finally:
        local.close()


def _copy_quarantine(source: Path, catalog: MutationCatalog) -> int:
    """Carry the unrealized-row backlog across database publication.

    The staging database only records what THIS install could not realize; the
    outgoing database's backlog — delta-deferred attachments awaiting bytes,
    prior orphans — would otherwise vanish with the swapped-out file. Each
    prior entry is carried unless this install already resolved it: a fresh
    same-address entry supersedes it, and an address whose row now exists
    realized in staging is satisfied and dropped. Returns the carried count.
    """
    if not source.exists():
        return 0
    local = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    local.row_factory = sqlite3.Row
    try:
        exists = local.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='fleet_sync_quarantine'"
        ).fetchone()
        if exists is None:
            return 0
        ensure_quarantine_table(catalog.conn)
        carried = 0
        for raw in local.execute("SELECT * FROM fleet_sync_quarantine"):
            row = dict(raw)
            fresh = catalog.conn.execute(
                "SELECT 1 FROM fleet_sync_quarantine WHERE address=?",
                (row["address"],),
            ).fetchone()
            if fresh is not None:
                continue
            table = str(row["table_name"])
            address = tuple(json.loads(str(row["logical_address"])))
            live = catalog.conn.execute(
                "SELECT 1 FROM fleet_sync_catalog WHERE address=? "
                "AND tombstone=0",
                (row["address"],),
            ).fetchone()
            if live is not None:
                try:
                    catalog._live_row(catalog.conn, table, address)
                    continue  # realized by this install: satisfied.
                except WatermarkError:
                    pass
            catalog.conn.execute(
                "INSERT INTO fleet_sync_quarantine VALUES(?,?,?,?,?,?,?)",
                (
                    row["address"], table, row["logical_address"],
                    row["reason"], row["watermark"],
                    row["quarantined_at_ns"], row.get("frame"),
                ),
            )
            carried += 1
        catalog.conn.commit()
        return carried
    finally:
        local.close()


def _existing_replicated_rows(conn: sqlite3.Connection) -> int:
    tables = {
        str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    return sum(
        int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table, policy in TABLE_POLICIES.items()
        if table in tables
        and policy.kind not in {PolicyKind.LOCAL, PolicyKind.DERIVED}
    )


def _merge_existing_winners(
    source: Path,
    target: MutationCatalog,
    *,
    expected_origin: str,
) -> int:
    """Merge a quiesced target's authored winners into the staged checkpoint."""
    if not source.exists():
        return 0
    local = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    local.row_factory = sqlite3.Row
    try:
        has_state = local.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='fleet_sync_state'"
        ).fetchone()
        state = (
            local.execute(
                "SELECT origin_incarnation FROM fleet_sync_state "
                "WHERE singleton=1"
            ).fetchone()
            if has_state is not None
            else None
        )
        if state is None:
            if _existing_replicated_rows(local):
                raise AlphaError(
                    "existing logical rows have no authoritative mutation catalog"
                )
            return 0
        origin = str(state[0])
        if origin != expected_origin:
            raise AlphaError("target mutation identity changed during handoff")
        catalog = MutationCatalog(local, origin)
        if not catalog.triggers_active():
            raise AlphaError("existing target does not have active writer guards")

        merged = 0
        group = []
        identity: tuple[str, str, int] | None = None
        for item in catalog.iter_mutations():
            item_identity = (
                item.origin_incarnation,
                item.transaction_id,
                item.mutation.timestamp_ns,
            )
            if group and item_identity != identity:
                won, _ignored = target.apply_remote_batch(group)
                merged += won
                group.clear()
            identity = item_identity
            group.append(item)
        if group:
            won, _ignored = target.apply_remote_batch(group)
            merged += won
        return merged
    finally:
        local.close()


def _record_checkpoint_receipt(
    conn: sqlite3.Connection,
    *,
    source_machine_pub: str,
    roster_epoch: int | str,
    watermark: int,
) -> None:
    now = time.time_ns()
    conn.execute(
        "INSERT INTO fleet_sync_peer_state("
        "machine_public_key,roster_epoch,online,last_success_ns,peer_watermark,"
        "checkpoints_received,updated_at_ns) VALUES(?,?,0,?,?,1,?) "
        "ON CONFLICT(machine_public_key,roster_epoch) DO UPDATE SET "
        "online=0,last_success_ns=excluded.last_success_ns,"
        "peer_watermark=CASE "
        "WHEN fleet_sync_peer_state.peer_watermark IS NULL OR "
        "excluded.peer_watermark>fleet_sync_peer_state.peer_watermark "
        "THEN excluded.peer_watermark ELSE fleet_sync_peer_state.peer_watermark END,"
        "checkpoints_received=fleet_sync_peer_state.checkpoints_received+1,"
        "last_error_code=NULL,updated_at_ns=excluded.updated_at_ns",
        (source_machine_pub, str(roster_epoch), now, watermark, now),
    )
    conn.commit()


def install_checkpoint(
    checkpoint_directory: Path,
    target_path: Path,
    *,
    target_origin_incarnation: str,
    expected_roster_epoch: int | str,
    expected_active_roster: tuple[str, ...],
    blob_store: ContentAddressedBlobStore | None = None,
    merge_existing: bool = False,
    checkpoint_source_machine: str | None = None,
) -> AlphaCheckpoint:
    """Validate and realize a checkpoint, then atomically publish its DB file.

    ``expected_active_roster`` is retained on the signature for caller symmetry
    (every caller resolves and passes its own active roster), but it no longer
    gates admission: checkpoint installation is a merge, not a base round, so it
    does not require the receiver's exact active set. ``expected_roster_epoch``
    is still recorded on the peer-state receipt, whose primary key includes it.
    """
    body, manifest_digest = _read_manifest(checkpoint_directory)
    if body.get("alpha_version") != ALPHA_VERSION:
        raise AlphaError("unsupported alpha checkpoint version")
    # No roster-epoch or roster-hash equality gate here. Pinning one exact
    # roster epoch is a property of a canonical BASE round (auto graph
    # 1155b8f4-8cf): a base means one exact active set agreed one exact result,
    # so every active peer must install and acknowledge it under that same set.
    # An ordinary checkpoint is not a base round -- the same design states
    # checkpoints "need not be current" and several branches may coexist -- and
    # its install is a merge, not a replacement (`merge_existing=True` re-applies
    # the receiver's entire held catalog by last-writer-wins), so an epoch or
    # active-set difference is not an admission fault. Enforcing base-round
    # equality here was the defect that left a joiner unable to ever complete a
    # first sync: enrollment gives it two roster entries, its epoch never matches
    # a larger fleet, and the roster that would fix it lives inside the very
    # checkpoint being refused. The equality test belongs on the exact-base
    # barrier that gates base acknowledgment, not on checkpoint installation.
    # The shape of `roster_epoch` is still validated below when it is recorded.
    _strict_roster_epoch(body.get("roster_epoch"))
    base = _base_catalog(body.get("base"))
    winners = _winner_catalog(body.get("winners"))
    _verify_catalog_file(
        checkpoint_directory / "base" / "catalog.json", body.get("base"), "base"
    )
    _verify_catalog_file(
        checkpoint_directory / "winners" / "winner-catalog.json",
        body.get("winners"), "winner",
    )
    if _strict_int(body.get("watermark"), "watermark") != winners.through_watermark:
        raise AlphaError("manifest/winner watermark mismatch")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(tempfile.mkdtemp(
        prefix=f".fleet-sync-install-{target_path.name}-",
        dir=target_path.parent,
    ))
    stage_path = temporary_root / target_path.name
    try:
        stage = GraphDB(stage_path)
        try:
            _copy_local_state(target_path, stage.conn)
            report = materialize_catalog(
                stage.conn, checkpoint_directory / "base", base,
                batch_records=1024, blob_store=blob_store,
            )
            # Rows the receiver cannot fully realize right now are skipped
            # rather than aborting the whole checkpoint, then quarantined. Two
            # kinds today, both excluded from the winner catalog and from both
            # count invariants so the catalog stays exactly consistent with the
            # rows that landed:
            #   * foreign-key orphans — a NOT-NULL parent absent from the whole
            #     checkpoint (legacy foreign-key-off debris). Permanently
            #     unrepresentable; the origin keeps its own copy and the fleet
            #     does not carry it.
            #   * attachments whose external bytes are unavailable — the blob is
            #     content-addressed and fetched separately, and file_path is
            #     NOT NULL, so the row cannot be stored until the bytes arrive.
            #     Until blob transfer is wired (blob_store is None today), every
            #     attachment waits here; the quarantine is the fetch backlog.
            unrealized = [
                (table, address, "fk_orphan")
                for table, address in report.skipped_orphans
            ]
            unrealized += [
                ("attachments", (attachment_id,), "attachment_bytes_unavailable")
                for attachment_id in report.pending_attachments
            ]
            skip_count = len(unrealized)
            skip_blobs = frozenset(
                encode_value([table, list(address)])
                for table, address, _reason in unrealized
            )
            catalog = MutationCatalog(stage.conn, target_origin_incarnation)
            catalog.install()
            # The winner merge below replays the receiver's held attachment
            # rows through apply_remote_batch; without the store they would
            # all defer to quarantine on every install.
            catalog.blob_store = blob_store
            installed_winners = install_winner_catalog(
                catalog, checkpoint_directory / "winners", winners,
                skip_addresses=skip_blobs,
            )
            if installed_winners != winners.total_records - skip_count:
                raise AlphaError("winner installation count mismatch")
            installed_live = int(stage.conn.execute(
                "SELECT COUNT(*) FROM fleet_sync_catalog WHERE tombstone=0"
            ).fetchone()[0])
            if installed_live != base.total_records - skip_count:
                raise AlphaError("winner metadata does not cover the exact base")
            if unrealized:
                quarantine_unrealized(
                    stage.conn, unrealized,
                    watermark=winners.through_watermark,
                )
                _log.warning(
                    "fleet checkpoint install skipped %d row(s) it cannot yet "
                    "realize (%d foreign-key orphan(s), %d attachment(s) awaiting "
                    "bytes); quarantined in fleet_sync_quarantine",
                    skip_count,
                    len(report.skipped_orphans),
                    len(report.pending_attachments),
                )
            _copy_peer_state(target_path, stage.conn)
            if merge_existing:
                _merge_existing_winners(
                    target_path,
                    catalog,
                    expected_origin=target_origin_incarnation,
                )
            _copy_quarantine(target_path, catalog)
            if checkpoint_source_machine is not None:
                _record_checkpoint_receipt(
                    stage.conn,
                    source_machine_pub=checkpoint_source_machine,
                    roster_epoch=expected_roster_epoch,
                    watermark=winners.through_watermark,
                )
            stage.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            stage.conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            stage.close()

        with stage_path.open("rb") as handle:
            os.fsync(handle.fileno())
        backup = target_path.with_name("." + target_path.name + ".pre-fleet-sync")
        if backup.exists():
            raise AlphaError(f"unresolved prior install backup: {backup}")
        try:
            if target_path.exists():
                os.replace(target_path, backup)
                _fsync_directory(target_path.parent)
            os.replace(stage_path, target_path)
            _fsync_directory(target_path.parent)
            validation = sqlite3.connect(target_path)
            try:
                if validation.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise AlphaError("published checkpoint fails SQLite integrity check")
            finally:
                validation.close()
            backup.unlink(missing_ok=True)
            _fsync_directory(target_path.parent)
        except Exception:
            if backup.exists():
                if target_path.exists():
                    target_path.unlink()
                os.replace(backup, target_path)
                _fsync_directory(target_path.parent)
            raise
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)
    # Describe the checkpoint that was actually installed, carrying its own
    # roster identity rather than the receiver's expected epoch -- the two may
    # now legitimately differ, since the equality gate above is gone.
    return AlphaCheckpoint(
        ALPHA_VERSION,
        _strict_roster_epoch(body.get("roster_epoch")),
        _strict_text(body.get("roster_hash"), "roster hash"),
        _strict_text(body["origin_incarnation"], "origin incarnation"),
        _strict_int(body["watermark"], "watermark"),
        base.root_sha256, winners.root_sha256, base.total_records,
        winners.total_records, manifest_digest,
    )
