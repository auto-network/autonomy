"""A settled fleet is silent (operator order, 2026-09-20; master 95b334d1).

The rule: a machine seals a write floor only at the end of a round in which
it wrote a row or applied a row from a peer since its last floor. A received
floor never causes one. Before 95b334d1 every machine sealed every round, so
every pull carried a floor frame per origin, every receiver claimed it,
every persona re-sealed and every advert fired, forever.

Two scenarios on the org acceptance harness (three members, two machines
each, six real sync workers, poll 0.1 s):

1. One write on one machine, then nothing. The floor tables of all six org
   stores are sampled every round until they hold still for SETTLE_ROUNDS.
   Each store must see each origin's floor move exactly once (the writer
   seals after writing, every other machine seals once after applying the
   row, and every store learns each new floor once), each persona floor must
   move at most three times, and after the last change the tables must not
   move again. The persona floor in every store must end at or above the
   written row's timestamp.
2. Negative control: no write at all. Sampled until every one of the six
   workers has completed IDLE_PULLS_PER_WORKER org pulls, with no floor
   change anywhere and no reply above the empty baseline.

Both waits are bounded by the CONDITION each is evidence for, never by a
round count. Waiting a fixed number of rounds is how this file previously
spent 41 s and 30 s per run, and a fixed count is also what made an earlier
version fail on a loaded machine while every property it asserted held.

The wire is read from each worker's own pull ledger (``bytes_received`` per
org-scope pull). A reply carrying no transaction and no control frame has a
fixed size; a write-floor frame adds hundreds of bytes. The baseline is the
smallest empty reply seen while the fleet was settled before the write, and
a reply more than 64 bytes above it carried a frame. After the last floor
change, no reply may carry one.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from tools.network.fleet_sync.harness.org import HarnessOrg

ROUND_S = 0.1          # the org harness poll interval
SETTLE_ROUNDS = 20     # unchanged samples before the fleet counts as settled
WINDOW_ROUNDS = 120    # the bound on the wait, not a deadline the fleet must meet
FRAME_SLACK = 64       # bytes above the empty-reply baseline that mean a frame rode along
#: Empty pulls PER WORKER that constitute the negative control's evidence.
#: The control is bounded by this, not by elapsed rounds: what proves silence
#: is that every worker polled and every reply was empty, and a worker that
#: polled ten times has demonstrated that as well as one that polled seven
#: hundred. Bounding by time instead cost 12 s of pure waiting per run and
#: proved nothing extra -- the idle test can never break out of _observe
#: early, because its break needs a change that by construction never comes.
IDLE_PULLS_PER_WORKER = 10


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)


def _floors(org: HarnessOrg, m: int, k: int) -> tuple[dict, dict]:
    """({origin12: machine write_floor_ns}, {persona12: persona write_floor_ns})."""
    path = org.members[m].fleet.org_db_path(k, org.slug)
    try:
        with _ro(path) as conn:
            machines = {
                str(r[0])[:12]: int(r[1]) for r in conn.execute(
                    "SELECT o.incarnation, f.write_floor_ns FROM fleet_sync_machine_write_floors f "
                    "JOIN fleet_sync_origins o ON o.id=f.origin_id"
                )
            }
            personas = {
                str(r[0])[:12]: int(r[1]) for r in conn.execute(
                    "SELECT persona, write_floor_ns FROM fleet_sync_persona_write_floors"
                )
            }
        return machines, personas
    except sqlite3.Error:
        return {}, {}


def _snapshot(org: HarnessOrg) -> dict:
    return {(m, k): _floors(org, m, k) for m in range(len(org.members)) for k in range(2)}


def _diff(before: dict, after: dict, round_index: int) -> list[tuple]:
    """Every (round, store, kind, key, old, new) that moved between two snapshots."""
    out = []
    for store, (mach_a, pers_a) in after.items():
        mach_b, pers_b = before.get(store, ({}, {}))
        for key in set(mach_a) | set(mach_b):
            if mach_a.get(key) != mach_b.get(key):
                out.append((round_index, store, "machine", key, mach_b.get(key), mach_a.get(key)))
        for key in set(pers_a) | set(pers_b):
            if pers_a.get(key) != pers_b.get(key):
                out.append((round_index, store, "persona", key, pers_b.get(key), pers_a.get(key)))
    return out


def _org_pulls(org: HarnessOrg, since_wall: float) -> list[dict]:
    """Successful org-scope pulls recorded by every worker at or after *since_wall*."""
    out = []
    for m, member in enumerate(org.members):
        for k in range(2):
            path = member.fleet.root_dir / f"machine-{k}-pulls.jsonl"
            try:
                lines = path.read_text().splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if (entry.get("scope") == org.slug and entry.get("direction") == "pull"
                        and entry.get("outcome") == "success" and float(entry.get("at", 0)) >= since_wall):
                    out.append({"store": (m, k), **entry})
    return out


def _complete(snapshot: dict, machines: int, personas: int) -> bool:
    """Every store holds a floor for every machine and every persona."""
    return all(
        len(mach) == machines and len(pers) == personas
        for mach, pers in snapshot.values()
    )


def _settle(org: HarnessOrg, *, timeout: float = 90.0) -> tuple[dict, float]:
    """Wait for the steady state: every store holds all six machine floors
    and all three persona floors, AND nothing has moved for SETTLE_ROUNDS.

    Quiet alone is not settled. Start-up has lulls: an earlier version
    took two quiet seconds as settled while one store still held no floor
    at all for one machine, and the rest of start-up then arrived during
    the observation window and failed the idle control (run
    at-0920-163346-ad8d, a floor appearing None -> value at round 12).
    """
    machines = 2 * len(org.members)
    personas = len(org.members)
    deadline = time.time() + timeout
    last = _snapshot(org)
    quiet_since = time.time()
    quiet = 0
    while quiet < SETTLE_ROUNDS or not _complete(last, machines, personas):
        time.sleep(ROUND_S)
        now = _snapshot(org)
        if _diff(last, now, 0):
            quiet, quiet_since = 0, time.time()
        else:
            quiet += 1
        last = now
        if time.time() > deadline:
            raise AssertionError(
                "the fleet never reached a complete, quiet steady state: "
                f"complete={_complete(last, machines, personas)} quiet={quiet}"
            )
    return last, quiet_since


def _observe(org: HarnessOrg, start: dict, rounds: int) -> tuple[list[tuple], list[float]]:
    """Sample every round until the floor tables hold still for
    ``SETTLE_ROUNDS`` consecutive rounds, then stop.

    The property under test is that the fleet goes quiet and stays quiet,
    not that it does so by a particular round. An earlier version asserted
    the last change landed by round 40 of 60; under a loaded machine
    propagation legitimately reached round 43 and the test failed on its
    own deadline while every count it cared about held (run
    at-0920-163144-585d). Waiting for the condition removes the deadline:
    if the fleet never goes quiet, ``rounds`` bounds the wait and the
    caller sees changes running to the end.
    """
    changes, stamps = [], []
    last = start
    quiet = 0
    for r in range(1, rounds + 1):
        time.sleep(ROUND_S)
        now = _snapshot(org)
        stamps.append(time.time())
        moved = _diff(last, now, r)
        changes.extend(moved)
        quiet = 0 if moved else quiet + 1
        last = now
        if changes and quiet >= SETTLE_ROUNDS:
            break
    return changes, stamps


def _observe_idle(org: HarnessOrg, start: dict, since_wall: float,
                  *, timeout: float = 60.0) -> tuple[list[tuple], list[dict]]:
    """Sample floors until every worker has pulled IDLE_PULLS_PER_WORKER times
    since *since_wall*, and return what moved plus the pulls observed.

    The evidence the negative control needs is "every worker polled and no
    reply carried a frame", so it waits for the POLLS, not for a clock. If a
    worker stops pulling entirely the wait ends at *timeout* and the caller's
    own assertion names it.
    """
    changes: list[tuple] = []
    last = start
    deadline = time.time() + timeout
    r = 0
    while True:
        time.sleep(ROUND_S)
        r += 1
        now = _snapshot(org)
        changes.extend(_diff(last, now, r))
        last = now
        pulls = _org_pulls(org, since_wall)
        counts: dict = {}
        for pull in pulls:
            counts[pull["store"]] = counts.get(pull["store"], 0) + 1
        stores = [(m, k) for m in range(len(org.members)) for k in range(2)]
        if all(counts.get(s, 0) >= IDLE_PULLS_PER_WORKER for s in stores):
            return changes, pulls
        if time.time() > deadline:
            return changes, pulls


def _baseline_bytes(pulls: list[dict]) -> int:
    empties = [int(p["bytes_received"]) for p in pulls if int(p.get("transactions") or 0) == 0]
    assert empties, "no empty org pulls observed while settled"
    return min(empties)


def _write_stamp(org: HarnessOrg, m: int, k: int) -> int:
    path = org.members[m].fleet.org_db_path(k, org.slug)
    own = org.machine_pubs(m)[k]
    with _ro(path) as conn:
        return int(conn.execute(
            "SELECT MAX(t.timestamp_ns) FROM fleet_sync_transactions t "
            "JOIN fleet_sync_origins o ON o.id=t.origin_id WHERE o.incarnation=?", (own,),
        ).fetchone()[0])


def _bring_up(tmp_path: Path) -> HarnessOrg:
    org = HarnessOrg(tmp_path / "org", members=3, machines_per_member=2).build()
    org.start_all()
    # Start-up traffic: every machine's reachability row reaches every store.
    all_machines = sorted(p for m in range(3) for p in org.machine_pubs(m))
    org.wait(lambda: all(sorted(org.reachability_rows(m, k)) == all_machines
                         for m in range(3) for k in range(2)),
             timeout=90.0, label="reachability rows on every machine")
    return org


#: Set by the write scenario before it writes. The negative control asserts it
#: is still empty, so the ordering the shared fleet depends on is ENFORCED
#: rather than assumed: a reorder fails loudly instead of quietly turning the
#: control into a test of a fleet that has already been written to.
_WRITTEN: list[str] = []


@pytest.fixture(scope="module")
def fleet(tmp_path_factory):
    """ONE six-machine fleet for both scenarios in this file.

    Bringing it up costs about five seconds -- three real personas, six
    machines, six sync workers and the reachability rows converging -- and
    both scenarios begin from the same state: settled, nothing written. The
    suite runs ``--dist loadfile``, so every test in this file is on one
    worker and this fixture is built once.

    The control runs FIRST, on a fleet that has had no write at all, which
    is exactly the precondition it needs; the write scenario then re-settles
    (a no-op here, since the control leaves the floors untouched) and writes.
    """
    org = _bring_up(tmp_path_factory.mktemp("settled-fleet"))
    try:
        yield org
    finally:
        org.shutdown()


def test_no_write_means_no_floor_and_no_frame(fleet: HarnessOrg, tmp_path: Path) -> None:
    org = fleet
    assert not _WRITTEN, (
        "the negative control must run before any write reaches the shared "
        f"fleet, but {_WRITTEN} was already written")
    settled, quiet_since = _settle(org)
    baseline = _baseline_bytes(_org_pulls(org, quiet_since))
    start_wall = time.time()
    changes, pulls = _observe_idle(org, settled, start_wall)
    by_store: dict = {}
    for pull in pulls:
        by_store[str(pull["store"])] = by_store.get(str(pull["store"]), 0) + 1
    (tmp_path / "silence-idle.json").write_text(json.dumps(
        {"baseline_bytes": baseline, "changes": changes, "pulls_per_store": by_store},
        indent=1, default=str))
    assert changes == [], ("floors moved with no write", changes[:8])
    # Every worker must have polled, or "no frame" would be satisfied by a
    # fleet that had simply stopped talking.
    short = {s: n for s, n in by_store.items() if n < IDLE_PULLS_PER_WORKER}
    assert len(by_store) == 6 and not short, (
        "some workers did not complete "
        f"{IDLE_PULLS_PER_WORKER} org pulls: {by_store}")
    carrying = [p for p in pulls if int(p["bytes_received"]) > baseline + FRAME_SLACK]
    assert carrying == [], (
        f"{len(carrying)} of {len(pulls)} idle org pulls carried more than an empty reply "
        f"(baseline {baseline} B): " + ", ".join(
            f"{p['store']}<-{p['peer']} {p['bytes_received']}B" for p in carrying[:8]))


def test_one_write_then_the_fleet_is_silent(fleet: HarnessOrg, tmp_path: Path) -> None:
    org = fleet
    _WRITTEN.append("settled-row")
    settled, quiet_since = _settle(org)
    baseline = _baseline_bytes(_org_pulls(org, quiet_since))
    for store, (machines, personas) in settled.items():
        assert len(machines) == 6, (store, machines)
        assert len(personas) == 3, (store, personas)

    written_wall = time.time()
    org.write_org(0, 0, "settled-row", "the only write after settling")
    changes, stamps = _observe(org, settled, WINDOW_ROUNDS)
    row_stamp = _write_stamp(org, 0, 0)
    final = _snapshot(org)
    evidence = {"baseline_bytes": baseline, "row_stamp": row_stamp, "changes": changes}
    (tmp_path / "silence-after-write.json").write_text(json.dumps(evidence, indent=1, default=str))

    # Every store sees each origin's floor move exactly once, and each
    # persona floor at most twice.
    per_store_origin: dict = {}
    per_store_persona: dict = {}
    for r, store, kind, key, old, new in changes:
        bucket = per_store_origin if kind == "machine" else per_store_persona
        bucket.setdefault(store, {}).setdefault(key, []).append(r)
    for store in settled:
        origins = per_store_origin.get(store, {})
        assert len(origins) == 6, (store, "origins that moved", sorted(origins))
        for key, rounds in origins.items():
            assert len(rounds) == 1, (store, key, "moved in rounds", rounds)
        # A persona floor is the minimum over its machines' floors and is
        # re-sealed as each machine's new floor reaches the sealer, so a
        # store can see it step up once per machine plus once for the
        # sealer's own view (observed: up to 3 steps for a two-machine
        # persona, intermediate values below the row, final at or above
        # it). More steps than that would be a floor sealed by something
        # other than a machine floor arriving.
        for key, rounds in per_store_persona.get(store, {}).items():
            assert len(rounds) <= 1 + 2, (store, key, "persona floor moved in rounds", rounds)
    # After the last change the tables are static for the rest of the window.
    last_change_round = max(r for r, *_ in changes)
    quiet_rounds = len(stamps) - last_change_round
    assert quiet_rounds >= SETTLE_ROUNDS, (
        f"the floors never held still: last change at round "
        f"{last_change_round}, only {quiet_rounds} quiet round(s) in "
        f"{len(stamps)} observed", changes[-6:])
    # The persona floors cover the write and did not move afterwards.
    for store, (_machines, personas) in final.items():
        for persona, floor in personas.items():
            assert floor >= row_stamp, (store, persona, floor, row_stamp)
    # The wire: replies that carried a frame all precede the last floor
    # change (they are what carried it); after it, none may.
    settle_wall = stamps[last_change_round - 1]
    window = _org_pulls(org, written_wall)
    carrying = [p for p in window if int(p["bytes_received"]) > baseline + FRAME_SLACK]
    evidence["pulls_in_window"] = len(window)
    evidence["replies_carrying_frames"] = len(carrying)
    (tmp_path / "silence-after-write.json").write_text(json.dumps(evidence, indent=1, default=str))
    late = [p for p in carrying if float(p["at"]) > settle_wall]
    assert late == [], (
        f"{len(late)} org pulls after the last floor change carried more than an empty reply "
        f"(baseline {baseline} B): " + ", ".join(
            f"{p['store']}<-{p['peer']} {p['bytes_received']}B at +{p['at'] - written_wall:.1f}s"
            for p in late[:8]))
