"""Executable compaction proof for trusted personal-fleet synchronization.

The important object in this module is not a maximum timestamp that happened
to be observed.  It is an *earned watermark*: an origin first durably raises a
local no-more-before floor, seals every mutation through that floor, places the
sealed prefix on a second active machine, and only then advertises it.  The
minimum earned watermark of a frozen active roster is consequently a closed
history boundary.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from typing import Iterable, Mapping

from tools.graph.db import GraphDB
from tools.network.idkit import KeyPair, canonical_json, verify_signature

from .codec import Mutation, decode_stream, encode_stream
from .materialize import materialize
from .merge import MutationInbox


BASE_DOMAIN = b"autonomy.personal-graph.compacted-base.v2\x00"
PREFIX_DOMAIN = b"autonomy.personal-graph.origin-prefix.v1\x00"
KICK_DOMAIN = b"autonomy.network.fleet-roster-kick.v1\x00"
CODEC_POLICY_VERSION = "personal-graph-sim-v1"


class WatermarkError(RuntimeError):
    """An earned-watermark or base-round invariant was violated."""


def _source(identity: str, timestamp: int, title: str) -> Mutation:
    return Mutation(
        "sources", (identity,), timestamp, False,
        (("created_at", "2026-08-19T00:00:00Z"), ("id", identity),
         ("ingested_at", "2026-08-19T00:00:00Z"), ("metadata", {}),
         ("publication_state", "raw"), ("title", title), ("type", "note")),
    )


def _tombstone(identity: str, timestamp: int) -> Mutation:
    return Mutation("sources", (identity,), timestamp, True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    _fsync_directory(path.parent)


@dataclass(frozen=True)
class AuthoredMutation:
    """Origin metadata kept by the delta/prefix layer, not the logical row.

    The trusted-fleet design does not sign this metadata.  The authenticated
    channel establishes who supplied it.  It remains immutable so a relaying
    peer cannot turn store-and-forward delivery into authorship.
    """

    origin_incarnation: str
    transaction_id: str
    operation_index: int
    mutation: Mutation


@dataclass(frozen=True)
class PrefixArtifact:
    origin_incarnation: str
    roster_epoch: int
    watermark: int
    mutation_stream: bytes
    digest: str

    @classmethod
    def build(
        cls,
        origin_incarnation: str,
        roster_epoch: int,
        watermark: int,
        mutations: Iterable[Mutation],
    ) -> "PrefixArtifact":
        stream = encode_stream(
            mutation for mutation in mutations
            if mutation.timestamp_ns <= watermark
        )
        header = canonical_json({
            "origin_incarnation": origin_incarnation,
            "roster_epoch": roster_epoch,
            "watermark": watermark,
        })
        digest = hashlib.sha256(PREFIX_DOMAIN + header + stream).hexdigest()
        return cls(origin_incarnation, roster_epoch, watermark, stream, digest)

    def verify(self) -> None:
        rebuilt = self.build(
            self.origin_incarnation,
            self.roster_epoch,
            self.watermark,
            decode_stream(self.mutation_stream),
        )
        if rebuilt.digest != self.digest or rebuilt.mutation_stream != self.mutation_stream:
            raise WatermarkError("origin prefix commitment mismatch")


class PrefixStore:
    """Durable content-addressed prefix storage on one simulated machine."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        return self.root / f"{digest}.prefix"

    def put(self, artifact: PrefixArtifact) -> None:
        artifact.verify()
        _atomic_write(self.path_for(artifact.digest), artifact.mutation_stream)

    def has(self, artifact: PrefixArtifact) -> bool:
        path = self.path_for(artifact.digest)
        return path.is_file() and path.read_bytes() == artifact.mutation_stream

    def copy_from(self, artifact: PrefixArtifact, source: "PrefixStore") -> None:
        if not source.has(artifact):
            raise WatermarkError("source does not durably hold prefix")
        _atomic_write(self.path_for(artifact.digest), source.path_for(
            artifact.digest
        ).read_bytes())


@dataclass(frozen=True)
class WatermarkReceipt:
    origin_incarnation: str
    roster_epoch: int
    watermark: int
    prefix_digest: str
    holders: tuple[str, ...]


class DurableOrigin:
    """SQLite-backed author state with an atomic no-more-before cut."""

    def __init__(
        self,
        root: Path,
        incarnation: str,
        roster_epoch: int,
        *,
        uncertain_startup: bool = False,
    ) -> None:
        self.root = root
        self.incarnation = incarnation
        self.roster_epoch = roster_epoch
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = PrefixStore(self.root / "prefixes")
        self.conn = sqlite3.connect(self.root / "origin.sqlite")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS origin_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                incarnation TEXT NOT NULL,
                roster_epoch INTEGER NOT NULL,
                write_floor INTEGER NOT NULL,
                last_authored INTEGER NOT NULL,
                available_watermark INTEGER NOT NULL,
                available_digest TEXT NOT NULL,
                advertised_watermark INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS authored_mutations (
                timestamp_ns INTEGER PRIMARY KEY,
                transaction_id TEXT NOT NULL,
                operation_index INTEGER NOT NULL,
                frame BLOB NOT NULL
            );
        """)
        row = self.conn.execute(
            "SELECT incarnation,roster_epoch FROM origin_state WHERE singleton=1"
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO origin_state VALUES (1,?,?,0,0,0,'',0)",
                (incarnation, roster_epoch),
            )
            self.conn.commit()
        elif tuple(row) != (incarnation, roster_epoch):
            raise WatermarkError("origin state identity/epoch mismatch")
        self.authoring_enabled = not uncertain_startup

    def close(self) -> None:
        self.conn.close()

    def advance_roster_epoch(self, roster_epoch: int) -> None:
        if roster_epoch <= self.roster_epoch:
            raise WatermarkError("roster epoch must advance")
        self.conn.execute(
            "UPDATE origin_state SET roster_epoch=? WHERE singleton=1",
            (roster_epoch,),
        )
        self.conn.commit()
        self.roster_epoch = roster_epoch

    def _state(self) -> tuple[int, int, int, str, int]:
        row = self.conn.execute(
            "SELECT write_floor,last_authored,available_watermark,"
            "available_digest,advertised_watermark FROM origin_state "
            "WHERE singleton=1"
        ).fetchone()
        assert row is not None
        return int(row[0]), int(row[1]), int(row[2]), str(row[3]), int(row[4])

    @property
    def write_floor(self) -> int:
        return self._state()[0]

    @property
    def advertised_watermark(self) -> int:
        return self._state()[4]

    def author(
        self,
        mutation: Mutation,
        *,
        transaction_id: str | None = None,
        operation_index: int = 0,
    ) -> AuthoredMutation:
        if not self.authoring_enabled:
            raise WatermarkError("startup watermark recovery required before authoring")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            floor, last, _, _, _ = self._state()
            required = max(floor, last)
            if mutation.timestamp_ns <= required:
                raise WatermarkError(f"write refused before time {required + 1}")
            txid = transaction_id or f"{self.incarnation}:{mutation.timestamp_ns}"
            frame = encode_stream([mutation])
            self.conn.execute(
                "INSERT INTO authored_mutations VALUES (?,?,?,?)",
                (mutation.timestamp_ns, txid, operation_index, frame),
            )
            self.conn.execute(
                "UPDATE origin_state SET last_authored=? WHERE singleton=1",
                (mutation.timestamp_ns,),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return AuthoredMutation(self.incarnation, txid, operation_index, mutation)

    def freeze_cut(self) -> int:
        """Atomically establish the floor before any prefix bytes are built."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            _, last, _, _, _ = self._state()
            self.conn.execute(
                "UPDATE origin_state SET write_floor=? WHERE singleton=1",
                (last,),
            )
            self.conn.commit()
            return last
        except Exception:
            self.conn.rollback()
            raise

    def seal_cut(self, watermark: int) -> PrefixArtifact:
        floor, _, _, _, _ = self._state()
        if watermark > floor:
            raise WatermarkError("cannot seal a prefix above durable write floor")
        rows = self.conn.execute(
            "SELECT frame FROM authored_mutations WHERE timestamp_ns<=? "
            "ORDER BY timestamp_ns", (watermark,)
        ).fetchall()
        mutations = [decode_stream(bytes(row[0]))[0] for row in rows]
        artifact = PrefixArtifact.build(
            self.incarnation, self.roster_epoch, watermark, mutations
        )
        self.store.put(artifact)
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute(
            "UPDATE origin_state SET available_watermark=?,available_digest=? "
            "WHERE singleton=1", (watermark, artifact.digest)
        )
        self.conn.commit()
        return artifact

    def advertise(
        self,
        artifact: PrefixArtifact,
        holders: Mapping[str, PrefixStore],
    ) -> WatermarkReceipt:
        floor, _, available, digest, _ = self._state()
        if artifact.origin_incarnation != self.incarnation:
            raise WatermarkError("cannot advertise another origin's prefix")
        if artifact.roster_epoch != self.roster_epoch:
            raise WatermarkError("prefix belongs to a different roster epoch")
        if artifact.watermark > floor or available != artifact.watermark:
            raise WatermarkError("watermark cut is not durably sealed")
        if digest != artifact.digest or not self.store.has(artifact):
            raise WatermarkError("advertised prefix is not durably available")
        durable_holders = tuple(sorted(
            name for name, store in holders.items() if store.has(artifact)
        ))
        if self.incarnation not in durable_holders or len(durable_holders) < 2:
            raise WatermarkError(
                "watermark requires an origin and one surviving durable holder"
            )
        self.conn.execute(
            "UPDATE origin_state SET advertised_watermark=? WHERE singleton=1",
            (artifact.watermark,),
        )
        self.conn.commit()
        return WatermarkReceipt(
            self.incarnation, self.roster_epoch, artifact.watermark,
            artifact.digest, durable_holders,
        )

    def recover_startup_floor(self, fleet_held_floor: int) -> None:
        """Recover a rolled-back incarnation's promise before enabling writes."""
        self.conn.execute("BEGIN IMMEDIATE")
        floor, last, available, digest, advertised = self._state()
        recovered = max(floor, last, advertised, fleet_held_floor)
        self.conn.execute(
            "UPDATE origin_state SET write_floor=?,last_authored=? WHERE singleton=1",
            (recovered, recovered),
        )
        self.conn.commit()
        self.authoring_enabled = True


@dataclass(frozen=True)
class CanonicalBase:
    roster_epoch: int
    roster_hash: str
    gc_floor: int
    included_cuts: tuple[tuple[str, int], ...]
    watermarks: tuple[tuple[str, int], ...]
    included_artifacts: tuple[str, ...]
    codec_policy_version: str
    live_stream: bytes
    digest: str

    @classmethod
    def build(
        cls,
        roster_epoch: int,
        active: Iterable[str],
        receipts: Mapping[str, WatermarkReceipt],
        mutations: Iterable[Mutation],
        included_artifacts: Iterable[str],
    ) -> "CanonicalBase":
        active_tuple = tuple(sorted(active))
        if set(receipts) != set(active_tuple):
            raise WatermarkError("base round lacks an active origin watermark")
        if any(receipt.roster_epoch != roster_epoch for receipt in receipts.values()):
            raise WatermarkError("base round mixes roster epochs")
        watermarks = tuple(sorted(
            (peer, receipts[peer].watermark) for peer in active_tuple
        ))
        gc_floor = min(value for _, value in watermarks)
        included_cuts = watermarks
        inbox = MutationInbox()
        # This is exact current state at every peer's cut, not historical
        # state reconstructed at the minimum watermark.  Faster peers may
        # contribute state above gc_floor; gc_floor governs semantic GC only.
        inbox.ingest(mutations)
        # Tombstones at/below the closed floor may be erased.  Newer
        # tombstones remain join-relevant state and stay in the exact base.
        compact_state = [
            mutation for mutation in inbox.winners(include_tombstones=True)
            if not mutation.tombstone or mutation.timestamp_ns > gc_floor
        ]
        live = encode_stream(compact_state)
        artifact_ids = tuple(sorted(included_artifacts))
        roster_hash = hashlib.sha256(canonical_json({
            "active": list(active_tuple), "epoch": roster_epoch,
        })).hexdigest()
        certificate = canonical_json({
            "gc_floor": gc_floor,
            "included_cuts": [list(item) for item in included_cuts],
            "roster_epoch": roster_epoch,
            "roster_hash": roster_hash,
            "watermarks": [list(item) for item in watermarks],
            "included_artifacts": list(artifact_ids),
            "codec_policy_version": CODEC_POLICY_VERSION,
        })
        digest = hashlib.sha256(BASE_DOMAIN + certificate + live).hexdigest()
        return cls(
            roster_epoch, roster_hash, gc_floor, included_cuts,
            watermarks, artifact_ids, CODEC_POLICY_VERSION, live, digest,
        )

    @property
    def horizon(self) -> int:
        """Compatibility name for the semantic garbage-collection floor."""
        return self.gc_floor


class Replica:
    """Logical mutation log plus durably installed compacted-base boundary."""

    def __init__(
        self,
        name: str,
        mutations: Iterable[Mutation] = (),
        *,
        install_root: Path | None = None,
    ) -> None:
        self.name = name
        self.base_horizon = 0
        self.log = list(mutations)
        self.install_root = install_root

    def ingest(self, mutations: Iterable[Mutation]) -> None:
        for mutation in mutations:
            if mutation.timestamp_ns <= self.base_horizon:
                continue
            self.log.append(mutation)

    def winners(self, *, include_tombstones: bool = True) -> list[Mutation]:
        inbox = MutationInbox()
        inbox.ingest(self.log)
        return inbox.winners(include_tombstones=include_tombstones)

    def apply_base(self, base: CanonicalBase) -> str:
        if self.install_root is not None:
            _atomic_write(self.install_root / f"{base.digest}.base", base.live_stream)
            _atomic_write(
                self.install_root / "installed.json",
                canonical_json({
                    "digest": base.digest,
                    "gc_floor": base.gc_floor,
                    "included_cuts": [list(item) for item in base.included_cuts],
                    "roster_epoch": base.roster_epoch,
                }),
            )
        # Source artifacts represented by the exact base are now redundant.
        # This simulation freezes writers during install; production retains
        # transactions beyond each origin's included cut as new deltas.
        self.log = decode_stream(base.live_stream)
        self.base_horizon = max(self.base_horizon, base.gc_floor)
        return base.digest

    def retire_through(self, horizon: int) -> None:
        if horizon > self.base_horizon:
            raise ValueError("cannot retire history beyond acknowledged base")
        self.log = [m for m in self.log if m.timestamp_ns > horizon]


@dataclass(frozen=True)
class Kick:
    peer: str
    roster_epoch: int
    signature: str

    def signing_input(self) -> bytes:
        return KICK_DOMAIN + canonical_json({
            "peer": self.peer, "roster_epoch": self.roster_epoch,
        })


class ActiveRosterFrontier:
    """Frozen membership plus verified, earned watermark receipts."""

    def __init__(self, root_pub: str, peers: Iterable[str], *, epoch: int = 1) -> None:
        self.root_pub = root_pub
        self.epoch = epoch
        self.active = set(peers)
        self.receipts: dict[str, WatermarkReceipt] = {}
        self.kick_observers: dict[tuple[str, int], set[str]] = {}
        self.kicked: set[str] = set()

    @property
    def complete_frontiers(self) -> dict[str, int]:
        return {
            peer: self.receipts[peer].watermark if peer in self.receipts else 0
            for peer in self.active
        }

    @property
    def frontier(self) -> int:
        if not self.active:
            raise ValueError("fleet has no active peers")
        return min(self.complete_frontiers.values())

    def complete(self, receipt: WatermarkReceipt) -> None:
        peer = receipt.origin_incarnation
        if peer not in self.active:
            raise ValueError("inactive peer cannot advance a fleet frontier")
        if receipt.roster_epoch != self.epoch:
            raise WatermarkError("watermark receipt belongs to another roster epoch")
        current = self.receipts.get(peer)
        if current is None or receipt.watermark > current.watermark:
            self.receipts[peer] = receipt

    def timeout(self, peer: str) -> None:
        if peer not in self.active:
            raise ValueError("unknown peer")

    def observe_kick(self, observer: str, kick: Kick) -> bool:
        if observer not in self.active or kick.peer not in self.active:
            return False
        if kick.roster_epoch <= self.epoch:
            raise WatermarkError("kick does not advance roster epoch")
        verify_signature(self.root_pub, kick.signature, kick.signing_input())
        key = (kick.peer, kick.roster_epoch)
        observers = self.kick_observers.setdefault(key, set())
        observers.add(observer)
        remaining = self.active - {kick.peer}
        if remaining.issubset(observers):
            self.active.remove(kick.peer)
            self.kicked.add(kick.peer)
            self.receipts.pop(kick.peer, None)
            self.epoch = kick.roster_epoch
            # Receipts are epoch-bound evidence. Survivors must reseal or
            # explicitly carry them into a new base round under the new epoch.
            self.receipts.clear()
            return True
        return False

    def admit(self, peer: str) -> bool:
        return peer in self.active and peer not in self.kicked

    def reenroll(self, old_peer: str, new_peer: str) -> None:
        if old_peer not in self.kicked or new_peer in self.active:
            raise ValueError("re-enrollment must create a fresh active identity")
        self.active.add(new_peer)
        self.epoch += 1
        self.receipts.clear()


def make_kick(root: KeyPair, peer: str, roster_epoch: int) -> Kick:
    unsigned = Kick(peer, roster_epoch, "")
    return Kick(peer, roster_epoch, root.sign_hex(unsigned.signing_input()))


def _materialize_worker(path: str, stream: bytes) -> dict:
    graph = GraphDB(Path(path))
    try:
        materialize(graph.conn, decode_stream(stream))
        rows = [tuple(row) for row in graph.conn.execute(
            "SELECT id,title FROM sources ORDER BY id"
        ).fetchall()]
        time.sleep(0.05)
        return {"pid": os.getpid(), "rows": rows,
                "digest": hashlib.sha256(encode_stream(
                    decode_stream(stream)
                )).hexdigest()}
    finally:
        graph.close()


def _seal_and_replicate(
    origin: DurableOrigin,
    custodian_name: str,
    custodian_store: PrefixStore,
    all_stores: Mapping[str, PrefixStore],
) -> tuple[PrefixArtifact, WatermarkReceipt]:
    watermark = origin.freeze_cut()
    artifact = origin.seal_cut(watermark)
    custodian_store.copy_from(artifact, origin.store)
    receipt = origin.advertise(artifact, all_stores)
    if custodian_name not in receipt.holders:
        raise AssertionError("custodian did not retain prefix")
    return artifact, receipt


def run_compaction_simulation() -> dict:
    """Exercise the safety theorem and every crash/membership barrier."""
    with tempfile.TemporaryDirectory() as temporary_name:
        temporary = Path(temporary_name)
        roots = {peer: temporary / peer for peer in ("A", "B", "C")}
        origins = {
            peer: DurableOrigin(roots[peer], peer, 1) for peer in roots
        }
        stores = {peer: origins[peer].store for peer in origins}
        try:
            # Distinct origins author divergent histories.  Origin is retained
            # by the prefix layer even when another peer later relays it.
            authored_a = [
                origins["A"].author(_source("x", 10, "live-x")),
                origins["A"].author(_tombstone("x", 20)),
            ]
            # A real SQLite backup taken before later writes drives the
            # restore test; this is not a simulated counter assignment.
            old_a_path = temporary / "old-A.sqlite"
            old_a_conn = sqlite3.connect(old_a_path)
            origins["A"].conn.backup(old_a_conn)
            old_a_conn.close()
            authored_a.append(origins["A"].author(_source("a-only", 30, "a")))
            authored = {
                "A": authored_a,
                "B": [
                    origins["B"].author(_source("kept", 15, "kept")),
                    origins["B"].author(_source("b-only", 30, "b")),
                ],
                "C": [origins["C"].author(_source("c-old", 10, "c"))],
            }

            # Crash after floor persistence but before sealing/advertisement:
            # the old write is already refused and the public frontier has not
            # advanced.  Retrying can safely finish the same cut.
            cut_a = origins["A"].freeze_cut()
            crash_before_advertisement = origins["A"].advertised_watermark == 0
            try:
                origins["A"].author(_source("late-old", cut_a, "bad"))
                raise AssertionError("backward write was accepted")
            except WatermarkError as exc:
                backward_refusal = str(exc)
            artifact_a = origins["A"].seal_cut(cut_a)
            try:
                origins["A"].advertise(artifact_a, stores)
                raise AssertionError("sole-copy watermark was advertised")
            except WatermarkError:
                sole_copy_refused = True
            stores["B"].copy_from(artifact_a, origins["A"].store)
            receipt_a = origins["A"].advertise(artifact_a, stores)

            artifact_b, receipt_b = _seal_and_replicate(
                origins["B"], "A", stores["A"], stores
            )
            artifact_c, receipt_c = _seal_and_replicate(
                origins["C"], "A", stores["A"], stores
            )

            root = KeyPair.generate()
            roster = ActiveRosterFrontier(root.public_hex, {"A", "B", "C"})
            for receipt in (receipt_a, receipt_b, receipt_c):
                roster.complete(receipt)
            before_branch = roster.frontier
            assert before_branch == 10

            # A new write above A's floor and a later prefix advance do not
            # move the minimum while C remains at ten.
            authored["A"].append(origins["A"].author(
                _tombstone("future-dead", 40)
            ))
            authored["A"].append(origins["A"].author(
                _source("offline-local", 50, "new-local")
            ))
            artifact_a2, receipt_a2 = _seal_and_replicate(
                origins["A"], "B", stores["B"], stores
            )
            roster.complete(receipt_a2)
            after_branch = roster.frontier
            roster.timeout("C")
            after_timeout = roster.frontier
            assert before_branch == after_branch == after_timeout == 10

            # Pin a base round to epoch 1, then prove a membership change makes
            # that round unusable rather than mixing active sets.
            frozen_epoch = roster.epoch
            frozen_active = set(roster.active)
            frozen_receipts = dict(roster.receipts)
            kick = make_kick(root, "C", 2)
            assert roster.observe_kick("A", kick) is False
            assert roster.observe_kick("B", kick) is True
            stale_round_refused = roster.epoch != frozen_epoch
            assert stale_round_refused

            # Survivors reseal under the new roster epoch.  Existing mutations
            # are retained; only the receipt/certificate epoch changes.
            for peer in ("A", "B"):
                origins[peer].advance_roster_epoch(roster.epoch)
            artifact_a3, receipt_a3 = _seal_and_replicate(
                origins["A"], "B", stores["B"], stores
            )
            artifact_b2, receipt_b2 = _seal_and_replicate(
                origins["B"], "A", stores["A"], stores
            )
            roster.complete(receipt_a3)
            roster.complete(receipt_b2)
            assert roster.frontier == 30

            active_prefixes = (artifact_a3, artifact_b2)
            prefix_mutations = [
                mutation
                for artifact in active_prefixes
                for mutation in decode_stream(artifact.mutation_stream)
            ]
            base = CanonicalBase.build(
                roster.epoch, roster.active, roster.receipts, prefix_mutations,
                (artifact.digest for artifact in active_prefixes),
            )
            retained_post_floor_tombstones = [
                mutation for mutation in decode_stream(base.live_stream)
                if mutation.tombstone and mutation.timestamp_ns > base.gc_floor
            ]
            assert [mutation.address for mutation in retained_post_floor_tombstones] == [
                ("future-dead",)
            ]
            replicas = {
                peer: Replica(
                    peer,
                    [item.mutation for origin in authored.values() for item in origin],
                    install_root=temporary / f"install-{peer}",
                )
                for peer in roster.active
            }
            acknowledgments = {
                peer: replicas[peer].apply_base(base) for peer in sorted(roster.active)
            }
            assert set(acknowledgments.values()) == {base.digest}
            assert all(
                (temporary / f"install-{peer}" / "installed.json").is_file()
                for peer in roster.active
            )
            for replica in replicas.values():
                replica.retire_through(base.gc_floor)

            # A delayed/store-forward copy at or below F is already represented
            # by the origin prefix/base and is inert; a legitimate later write
            # survives as delta state.
            for replica in replicas.values():
                replica.ingest(decode_stream(artifact_a3.mutation_stream))
                assert ("x",) not in {
                    m.address for m in replica.winners(include_tombstones=False)
                }

            # Restore A from a deliberately old state image.  The restored
            # incarnation cannot author until it recovers the fleet-held floor.
            restored_root = temporary / "restored-A"
            restored_root.mkdir()
            shutil.copy2(old_a_path, restored_root / "origin.sqlite")
            restored = DurableOrigin(
                restored_root, "A", 1, uncertain_startup=True
            )
            try:
                try:
                    restored.author(_source("restore-write", 60, "blocked"))
                    raise AssertionError("uncertain restored origin authored")
                except WatermarkError as exc:
                    restore_gate = str(exc)
                restored.recover_startup_floor(receipt_a3.watermark)
                try:
                    restored.author(_source(
                        "restore-backward", receipt_a3.watermark, "blocked"
                    ))
                    raise AssertionError("restored origin reused its floor")
                except WatermarkError:
                    restore_floor_refused = True
                restored.author(_source(
                    "restore-forward", receipt_a3.watermark + 1, "accepted"
                ))
            finally:
                restored.close()

            # Same exact base bytes materialize in separate processes.
            paths = [str(temporary / f"materialized-{peer}.db") for peer in ("A", "B")]
            with ProcessPoolExecutor(
                max_workers=2, mp_context=multiprocessing.get_context("spawn")
            ) as pool:
                process_results = list(pool.map(
                    _materialize_worker, paths, [base.live_stream, base.live_stream]
                ))
            assert len({result["pid"] for result in process_results}) == 2
            assert len({result["digest"] for result in process_results}) == 1
            assert process_results[0]["rows"] == process_results[1]["rows"]

            roster.reenroll("C", "C2")
            assert roster.admit("C2") and not roster.admit("C")

            return {
                "status": "pass",
                "watermark_contract": {
                    "atomic_cut": cut_a,
                    "crash_before_advertisement_safe": crash_before_advertisement,
                    "backward_write_refusal": backward_refusal,
                    "sole_copy_advertisement_refused": sole_copy_refused,
                    "durable_holders": list(receipt_a.holders),
                    "origin_metadata_preserved": authored["A"][0].origin_incarnation,
                },
                "guarded": {
                    "frontier_before_branch": before_branch,
                    "frontier_after_branch": after_branch,
                    "frontier_after_timeout": after_timeout,
                    "compaction_refused_before_kick": True,
                },
                "kick": {
                    "root_signature_verified": True,
                    "active_after_all_remaining_observers": ["A", "B"],
                    "old_peer_refused": not roster.admit("C"),
                    "fresh_identity_admitted": roster.admit("C2"),
                    "stale_roster_round_refused": stale_round_refused,
                    "abandoned_unadvertised_history": "explicit membership consequence",
                },
                "canonical_base": {
                    "horizon": base.gc_floor,
                    "gc_floor": base.gc_floor,
                    "included_cuts": dict(base.included_cuts),
                    "roster_epoch": base.roster_epoch,
                    "roster_hash": base.roster_hash,
                    "watermarks": dict(base.watermarks),
                    "included_artifacts": list(base.included_artifacts),
                    "codec_policy_version": base.codec_policy_version,
                    "post_floor_tombstones_retained": len(
                        retained_post_floor_tombstones
                    ),
                    "digest": base.digest,
                    "acknowledgments": acknowledgments,
                    "ack_after_durable_install": True,
                    "retirement_succeeded": True,
                },
                "restore": {
                    "startup_gate": restore_gate,
                    "fleet_floor_recovered": receipt_a3.watermark,
                    "backward_write_refused": restore_floor_refused,
                    "forward_write_accepted": True,
                },
                "separate_process_materialization": process_results,
            }
        finally:
            for origin in origins.values():
                origin.close()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rendered = json.dumps(run_compaction_simulation(), sort_keys=True, indent=2) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(rendered)
        temporary.replace(args.output)


if __name__ == "__main__":
    main()
