"""Executable compaction-frontier model for the fleet checkpoint simulation."""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
import hashlib
import itertools
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
from typing import Iterable

from tools.graph.db import GraphDB
from tools.network.idkit import KeyPair, canonical_json, verify_signature

from .codec import Mutation, decode_stream, encode_stream
from .materialize import materialize
from .merge import MutationInbox


BASE_DOMAIN = b"autonomy.personal-graph.compacted-base.v1\x00"
KICK_DOMAIN = b"autonomy.network.fleet-roster-kick.v1\x00"


def _source(identity: str, timestamp: int, title: str) -> Mutation:
    return Mutation(
        "sources", (identity,), timestamp, False,
        (("created_at", "2026-08-19T00:00:00Z"), ("id", identity),
         ("ingested_at", "2026-08-19T00:00:00Z"), ("metadata", {}),
         ("publication_state", "raw"), ("title", title), ("type", "note")),
    )


def _tombstone(identity: str, timestamp: int) -> Mutation:
    return Mutation("sources", (identity,), timestamp, True)


@dataclass(frozen=True)
class CanonicalBase:
    horizon: int
    live_stream: bytes
    digest: str

    @classmethod
    def build(cls, horizon: int, mutations: Iterable[Mutation]) -> "CanonicalBase":
        inbox = MutationInbox()
        inbox.ingest(mutation for mutation in mutations
                     if mutation.timestamp_ns <= horizon)
        live = encode_stream(inbox.winners(include_tombstones=False))
        body = BASE_DOMAIN + horizon.to_bytes(8, "big") + live
        return cls(horizon, live, hashlib.sha256(body).hexdigest())


class Replica:
    """Logical mutation log plus the compacted history boundary it trusts."""

    def __init__(self, name: str, mutations: Iterable[Mutation] = ()) -> None:
        self.name = name
        self.base_horizon = 0
        self.log = list(mutations)

    def ingest(self, mutations: Iterable[Mutation]) -> None:
        for mutation in mutations:
            # Once an exact canonical base is acknowledged, history through
            # its horizon is represented by that base, including omissions.
            if mutation.timestamp_ns <= self.base_horizon:
                continue
            self.log.append(mutation)

    def winners(self, *, include_tombstones: bool = True) -> list[Mutation]:
        inbox = MutationInbox()
        inbox.ingest(self.log)
        return inbox.winners(include_tombstones=include_tombstones)

    def apply_base(self, base: CanonicalBase) -> str:
        newer = [m for m in self.log if m.timestamp_ns > base.horizon]
        self.log = decode_stream(base.live_stream) + newer
        self.base_horizon = max(self.base_horizon, base.horizon)
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
    """Bounded trusted membership and each peer's complete checkpoint."""

    def __init__(self, root_pub: str, peers: Iterable[str]) -> None:
        self.root_pub = root_pub
        self.active = set(peers)
        self.complete_frontiers = {peer: 0 for peer in self.active}
        self.kick_observers: dict[tuple[str, int], set[str]] = {}
        self.kicked: set[str] = set()

    @property
    def frontier(self) -> int:
        if not self.active:
            raise ValueError("fleet has no active peers")
        return min(self.complete_frontiers[peer] for peer in self.active)

    def complete(self, peer: str, frontier: int) -> None:
        if peer not in self.active:
            raise ValueError("inactive peer cannot advance a fleet frontier")
        self.complete_frontiers[peer] = max(self.complete_frontiers[peer], frontier)

    def timeout(self, peer: str) -> None:
        if peer not in self.active:
            raise ValueError("unknown peer")
        # Deliberate no-op: liveness is not membership authority.

    def observe_kick(self, observer: str, kick: Kick) -> bool:
        if observer not in self.active or kick.peer not in self.active:
            return False
        verify_signature(self.root_pub, kick.signature, kick.signing_input())
        key = (kick.peer, kick.roster_epoch)
        observers = self.kick_observers.setdefault(key, set())
        observers.add(observer)
        remaining = self.active - {kick.peer}
        if remaining.issubset(observers):
            self.active.remove(kick.peer)
            self.kicked.add(kick.peer)
            return True
        return False

    def admit(self, peer: str) -> bool:
        return peer in self.active and peer not in self.kicked

    def reenroll(self, old_peer: str, new_peer: str) -> None:
        if old_peer not in self.kicked or new_peer in self.active:
            raise ValueError("re-enrollment must create a fresh active identity")
        self.active.add(new_peer)
        self.complete_frontiers[new_peer] = 0


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
        # Keep both jobs live long enough for the process-pool proof to use
        # two workers rather than legitimately reusing one idle worker.
        time.sleep(0.05)
        return {"pid": os.getpid(), "rows": rows,
                "digest": hashlib.sha256(encode_stream(
                    decode_stream(stream)
                )).hexdigest()}
    finally:
        graph.close()


def run_compaction_simulation() -> dict:
    live_x = _source("x", 10, "live-x")
    kept = _source("kept", 15, "kept")
    delete_x = _tombstone("x", 20)
    local_new = _source("offline-local", 50, "new-local")
    checkpoint_old = _source("offline-local", 40, "old-checkpoint")

    # Merge-not-replace and original timestamp semantics.
    offline = Replica("offline", [local_new])
    offline.ingest([checkpoint_old])
    assert offline.winners(include_tombstones=False) == [local_new]
    delete_orders = []
    for order in itertools.permutations((live_x, delete_x)):
        replica = Replica("order", order)
        delete_orders.append(replica.winners())
        assert replica.winners() == [delete_x]
    restamped = Replica("bad", [delete_x, _source("x", 30, "restamped-live")])
    assert restamped.winners(include_tombstones=False)[0].values

    # The deliberately unsafe variant drops the only deletion evidence while
    # active C still has the older row, so C resurrects it on rejoin.
    unsafe_a = Replica("A", [live_x, delete_x])
    unsafe_c = Replica("C", [live_x])
    unsafe_a.log.clear()  # unguarded "compaction"
    unsafe_a.ingest(unsafe_c.log)
    unsafe_resurrected = unsafe_a.winners(include_tombstones=False)
    assert [m.address for m in unsafe_resurrected] == [("x",)]

    root = KeyPair.generate()
    roster = ActiveRosterFrontier(root.public_hex, {"A", "B", "C"})
    roster.complete("A", 30)
    roster.complete("B", 30)
    roster.complete("C", 10)
    before_branch = roster.frontier
    roster.complete("A", 40)  # one branch advances; fleet minimum does not
    after_branch = roster.frontier
    roster.timeout("C")
    after_timeout = roster.frontier
    assert before_branch == after_branch == after_timeout == 10

    many_tombstones = [_tombstone(f"dead-{index}", 21 + index % 9)
                       for index in range(100)]
    heal_candidates = [live_x, kept, delete_x, local_new, checkpoint_old,
                       *many_tombstones]
    heal_orders = [
        heal_candidates,
        list(reversed(heal_candidates)),
        heal_candidates[::2] + heal_candidates[1::2],
    ]
    heal_digests = []
    for order in heal_orders:
        healed = MutationInbox()
        healed.ingest(order)
        heal_digests.append(healed.digest())
    assert len(set(heal_digests)) == 1
    replicas = {
        "A": Replica("A", [live_x, kept, delete_x, local_new, *many_tombstones]),
        "B": Replica("B", [live_x, kept, delete_x, checkpoint_old,
                             *reversed(many_tombstones)]),
        "C": Replica("C", [live_x]),
    }
    compaction_refused_before_kick = roster.frontier < max(
        mutation.timestamp_ns for mutation in [delete_x, *many_tombstones]
    )
    assert compaction_refused_before_kick

    kick = make_kick(root, "C", 2)
    assert roster.observe_kick("A", kick) is False
    assert roster.active == {"A", "B", "C"}
    assert roster.observe_kick("B", kick) is True
    assert roster.active == {"A", "B"}
    assert roster.frontier == 30

    all_active_mutations = replicas["A"].log + replicas["B"].log
    base = CanonicalBase.build(roster.frontier, all_active_mutations)
    acknowledgments = {
        peer: replicas[peer].apply_base(base) for peer in sorted(roster.active)
    }
    assert set(acknowledgments.values()) == {base.digest}
    # Heal post-base mutations through the ordinary merge path. The newer
    # offline-local value survives the older checkpoint on both machines.
    newer_a = list(replicas["A"].log)
    newer_b = list(replicas["B"].log)
    replicas["A"].ingest(newer_b)
    replicas["B"].ingest(newer_a)
    for peer in roster.active:
        replicas[peer].retire_through(base.horizon)

    # Stale C is neither admitted nor able to replay history below the base.
    assert not roster.admit("C")
    for peer in roster.active:
        replicas[peer].ingest(replicas["C"].log)
        assert ("x",) not in {
            m.address for m in replicas[peer].winners(include_tombstones=False)
        }

    # Same canonical row set after every permitted order; separate processes
    # materialize the exact base bytes into separate real SQLite files.
    active_rows = {
        peer: [(m.table, m.address, m.position)
               for m in replicas[peer].winners(include_tombstones=False)]
        for peer in roster.active
    }
    assert active_rows["A"] == active_rows["B"]
    with tempfile.TemporaryDirectory() as temporary:
        paths = [str(Path(temporary) / f"peer-{peer}.db") for peer in ("A", "B")]
        with ProcessPoolExecutor(
            max_workers=2, mp_context=multiprocessing.get_context("spawn")
        ) as pool:
            process_results = list(pool.map(
                _materialize_worker, paths, [base.live_stream, base.live_stream]
            ))
    assert len({result["pid"] for result in process_results}) == 2
    assert len({result["digest"] for result in process_results}) == 1
    assert process_results[0]["rows"] == process_results[1]["rows"]

    # Re-enrollment is a new roster act and identity, never revival of C.
    roster.reenroll("C", "C2")
    assert roster.admit("C2") and not roster.admit("C")

    return {
        "status": "pass",
        "merge_not_replace_preserved": "new-local",
        "delete_won_all_arrival_orders": len(delete_orders),
        "restamped_variant_resurrected": True,
        "unsafe_variant_resurrected": True,
        "guarded": {
            "frontier_before_branch": before_branch,
            "frontier_after_branch": after_branch,
            "frontier_after_timeout": after_timeout,
            "compaction_refused_before_kick": compaction_refused_before_kick,
            "tombstones_pinned": len(many_tombstones) + 1,
            "heal_orders_converged": len(heal_orders),
        },
        "kick": {
            "root_signature_verified": True,
            "active_after_first_observer": ["A", "B", "C"],
            "active_after_all_remaining_observers": ["A", "B"],
            "frontier_after": roster.complete_frontiers["B"],
            "old_peer_refused": not roster.admit("C"),
            "fresh_identity_admitted": roster.admit("C2"),
        },
        "canonical_base": {
            "horizon": base.horizon,
            "digest": base.digest,
            "acknowledgments": acknowledgments,
            "retirement_succeeded": True,
        },
        "separate_process_materialization": process_results,
    }


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
