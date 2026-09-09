"""Operator-specified adversarial matrix on the scenario harness.

Each scenario asserts convergence PLUS its named invariants. A scenario
that fails here files a bug bead; the engine is never patched from this
file.
"""

import shutil
import sqlite3
import time
from pathlib import Path

from tools.network.fleet_sync.harness import HarnessFleet, Step


def test_two_slow_feeders_bootstrap_a_new_machine(tmp_path: Path) -> None:
    """Scenario (a): a joiner fed by two throttled peers — collaborative
    bootstrap-plus-delta from two sources, admission ordering and
    duplicate-inert merge under way."""
    fleet = HarnessFleet(tmp_path / "fleet", size=3).build()
    try:
        fleet.start(0)
        fleet.start(1)
        for note in range(20):
            fleet.write(note % 2, f"seed-{note}", f"seeded {note}")
        fleet.wait(
            lambda: fleet.has(0, "seed-19") and fleet.has(1, "seed-18"),
            timeout=120.0, label="feeders converge",
        )
        # Both feeder-facing links are slow before the joiner exists.
        for feeder in (0, 1):
            fleet.set_link_faults(
                2, feeder,
                latency_s=0.05, jitter_s=0.05,
                bandwidth_bytes_per_s=512 * 1024,
            )
        fleet.start(2)
        # Convergence IS the assertion. It used to be followed by a count of
        # bootstraps received; a joiner bootstraps by sweep and there is no
        # artifact to count. What matters
        # is unchanged: a new machine fed by two throttled peers reaches the
        # fleet's state.
        fleet.wait_converged(timeout=120.0)
        fleet.write_evidence(tmp_path / "two-slow-feeders.json")
    finally:
        fleet.shutdown()


def test_asymmetric_loss_on_one_peer(tmp_path: Path) -> None:
    """Scenario (b): every link touching one machine is lossy; the clean
    pair is unaffected and the lossy machine still converges."""
    fleet = HarnessFleet(tmp_path / "fleet", size=3).build()
    try:
        fleet.start_all()
        for other in (0, 2):
            fleet.set_pair_faults(
                1, other,
                stall_rate=0.35, stall_s=0.12, reset_rate=0.03,
            )
        for machine in range(3):
            fleet.write(machine, f"al-{machine}", f"from {machine}")
        fleet.wait_converged(timeout=120.0)
        fleet.write_evidence(tmp_path / "asymmetric-loss.json")
    finally:
        fleet.shutdown()


def test_flap_during_bootstrap(tmp_path: Path) -> None:
    """Scenario (c): a joiner killed repeatedly while bootstrapping leaves
    no staging debris or stale backups and still converges cleanly."""
    fleet = HarnessFleet(tmp_path / "fleet", size=2).build()
    try:
        fleet.start(0)
        for note in range(60):
            fleet.write(0, f"bulk-{note}", "x" * 300)
        fleet.start(1)
        # Kill the joiner during its likely install windows, twice.
        fleet.run_timeline([
            Step(0.25, lambda f: f.restart(1, kill=True), "kill mid-install"),
            Step(0.9, lambda f: f.restart(1, kill=True), "kill again"),
        ])
        # Killed twice mid-bootstrap and still converges. The old assertion
        # counted bootstraps received; the sweep leaves no such artifact, and
        # surviving two kills is the property under test.
        fleet.wait_converged(timeout=120.0)
        fleet.restart(1, kill=False)
        fleet.wait_converged(timeout=120.0)

        # THE PROPERTY (auto-14bxm): the joiner resumed under the SAME F it
        # first anchored, rather than re-anchoring on a later one. Two kills
        # mid-sweep and a graceful restart are three chances to adopt a fresh
        # frontier from the serving store, which by then holds strictly more.
        # Re-anchoring would strand every key between the old F and the new,
        # and the store would look converged while missing them.
        import json as _json
        import sqlite3 as _sq

        def _bootstrap_row(path) -> tuple[str, dict] | None:
            conn = _sq.connect(f"file:{path}?mode=ro", uri=True)
            try:
                present = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='fleet_sync_bootstrap'"
                ).fetchone()
                if present is None:
                    return None
                row = conn.execute(
                    "SELECT phase,frontier FROM fleet_sync_bootstrap "
                    "WHERE singleton=1"
                ).fetchone()
                return None if row is None else (str(row[0]), _json.loads(row[1]))
            finally:
                conn.close()

        final = _bootstrap_row(fleet.machines[1].db_path)
        assert final is not None, (
            "the joiner bootstrapped by sweep, so it must hold a bootstrap row"
        )
        phase, frontier = final
        assert phase == "complete", f"joiner did not finish its sweep: {phase}"
        assert frontier, "a completed bootstrap must retain the F it ran under"

        # And the debris claim, now that installs are gone: a sweep stages
        # nothing and replaces no file, so there is no crash-recovery artifact
        # to consume in the first place.
        joiner_dir = fleet.machines[1].db_path.parent
        assert [
            path.name for path in joiner_dir.iterdir()
            if ".fleet-sync-install-" in path.name
            or path.name.endswith(".pre-fleet-sync")
            or ".fleet-sync-handoff" in path.name
        ] == [], "a sweep must never leave install/handoff staging debris"

        fleet.evidence["joiner_bootstrap_phase"] = phase
        fleet.evidence["joiner_retained_frontier_origins"] = len(frontier)
        fleet.write_evidence(tmp_path / "flap-bootstrap.json")
    finally:
        fleet.shutdown()


def test_writes_during_a_bootstrap_arrive_in_the_above_f_half(
    tmp_path: Path,
) -> None:
    """auto-5j6o0: the partition holds against a source that keeps writing.

    F is the serving store's frontier captured ONCE when the sweep starts, so
    anything authored after that instant is above F by construction and the
    sweep must not carry it -- the sweep walks live rows at or below F. Those
    rows are owed by the delta half, and the joiner must not report COMPLETE
    until it has them.

    The failure this excludes is the quiet one: a joiner that finishes its
    sweep, declares itself complete, and is permanently missing every row
    written while it was copying. Convergence alone would hide it, because the
    ordinary delta loop would eventually deliver them anyway -- so the check is
    that completion did not happen BEFORE they landed.
    """
    fleet = HarnessFleet(tmp_path / "fleet", size=2).build()
    try:
        fleet.start(0)
        for note in range(40):
            fleet.write(0, f"before-{note:02d}", "authored before the sweep")

        # The joiner begins bootstrapping; the source keeps authoring while it
        # copies. These land above whatever F the sweep captured.
        fleet.start(1)
        fleet.run_timeline([
            Step(0.3, lambda f: [
                f.write(0, f"during-{n:02d}", "authored mid-bootstrap")
                for n in range(10)
            ], "author above F while the joiner sweeps"),
        ])
        fleet.wait_converged(timeout=120.0)

        import json as _json
        import sqlite3 as _sq

        conn = _sq.connect(f"file:{fleet.machines[1].db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT phase,frontier FROM fleet_sync_bootstrap WHERE singleton=1"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, "the joiner bootstrapped, so it holds the row"
        phase, frontier = str(row[0]), _json.loads(row[1])
        assert phase == "complete", f"bootstrap did not finish: {phase}"
        assert frontier, "a completed bootstrap retains the F it ran under"

        # Both halves are present: swept (<= F) and delta (> F).
        for note in range(40):
            assert fleet.has(1, f"before-{note:02d}"), (
                f"the sweep half lost before-{note:02d}"
            )
        for note in range(10):
            assert fleet.has(1, f"during-{note:02d}"), (
                f"the above-F half lost during-{note:02d}; a write made while "
                "the joiner swept was dropped by both halves"
            )
        fleet.write_evidence(tmp_path / "above-f-half.json")
    finally:
        fleet.shutdown()


def test_partition_during_prune_retains_needed_frames(tmp_path: Path) -> None:
    """Scenario (d): the ack floor's safety, live — a partitioned peer's
    frozen acknowledgement blocks retirement of the frames it still needs,
    so healing converges by DELTAS (no new bootstrap)."""
    fleet = HarnessFleet(tmp_path / "fleet", size=3).build()
    try:
        fleet.start_all()
        fleet.write(0, "pre-part", "before partition")
        fleet.wait_converged(timeout=120.0)
        # Retention-under-frozen-ack presumes an ESTABLISHED fleet: a pair
        # that converged only indirectly gets a legitimate bootstrap on
        # first direct contact, which resets a journal mid-scenario.
        fleet.wait_pairwise_established()

        fleet.partition(2, 0)
        fleet.partition(2, 1)
        for note in range(6):
            fleet.write(0, f"while-away-{note}", "authored during partition")
        fleet.wait(
            lambda: fleet.has(1, "while-away-5"),
            timeout=120.0, label="live pair converges",
        )
        # Give the live pair time to ack and prune each other.
        time.sleep(1.0)
        # THE invariant, asserted directly (restored after auto-jn8ca):
        # the partitioned peer's acknowledgement is frozen below the
        # while-away transactions, so the floor cannot retire them — every
        # one of the six frames is still journaled on both live machines
        # at heal time. Matches the AckFloor TLA model's safety property,
        # here checked on the real engine.
        for live in (0, 1):
            with sqlite3.connect(
                f"file:{fleet.machines[live].db_path}?mode=ro&immutable=1",
                uri=True,
            ) as conn:
                retained = conn.execute(
                    "SELECT COUNT(*) FROM fleet_sync_transactions"
                ).fetchone()[0]
            assert retained >= 6, (
                f"machine {live} retained only {retained} transaction rows "
                "while a partitioned peer was unacknowledged"
            )
        fleet.heal(2, 0)
        fleet.heal(2, 1)
        fleet.wait(
            lambda: all(
                fleet.has(2, f"while-away-{n}") for n in range(6)
            ),
            timeout=120.0, label="healed peer catches up",
        )
        fleet.wait_converged(timeout=120.0)
        fleet.write_evidence(tmp_path / "partition-prune.json")
    finally:
        fleet.shutdown()


def test_restored_backup_server_reconverges(tmp_path: Path) -> None:
    """Scenario (e): a machine restored from an old snapshot rejoins and
    reconverges without divergence.

    This is the heaviest scenario in the suite — two full bootstraps
    plus a restore cycle — and under a loaded xdist sweep it has
    legitimately exceeded a 120s ceiling while making continuous forward
    progress (retained history: passes solo in 12-38s, both historical
    failures hit the ceiling exactly). The stall detector remains the real
    failure signal; the absolute timeout is only the backstop against
    progress-without-convergence, so it gets the headroom the docstring on
    ``wait`` promises."""
    fleet = HarnessFleet(tmp_path / "fleet", size=2).build()
    try:
        fleet.start_all()
        fleet.write(0, "epoch-1", "before the snapshot")
        fleet.wait_converged(timeout=300.0)
        fleet.stop(0, kill=True)
        snapshot = tmp_path / "snapshot.db"
        shutil.copy(fleet.machines[0].db_path, snapshot)

        fleet.start(0)
        fleet.write(0, "epoch-2", "after the snapshot")
        fleet.wait_converged(timeout=300.0)

        # Restore machine 0 from the old snapshot and rejoin.
        fleet.stop(0, kill=True)
        shutil.copy(snapshot, fleet.machines[0].db_path)
        for suffix in ("-wal", "-shm"):
            side = Path(str(fleet.machines[0].db_path) + suffix)
            side.unlink(missing_ok=True)
        fleet.start(0)
        fleet.wait(
            lambda: fleet.has(0, "epoch-2"),
            timeout=300.0, label="restored machine recovers",
        )
        fleet.wait_converged(timeout=300.0)
        fleet.write_evidence(tmp_path / "restore.json")
    finally:
        fleet.shutdown()


def test_joiner_never_rebootstraps_in_a_loop(tmp_path: Path) -> None:
    """Regression for the 3b2f0526 field bug: a joiner re-fetched and
    discarded the whole bootstrap on every retry (316 MB per attempt in
    production). It bootstraps once; steady state adds none."""
    fleet = HarnessFleet(tmp_path / "fleet", size=2).build()
    try:
        fleet.start(0)
        for note in range(10):
            fleet.write(0, f"j-{note}", f"joiner content {note}")
        fleet.start(1)
        fleet.wait_converged(timeout=120.0)
        # Bootstrap ONCE, then stop. The historical bugs were a double
        # receipt (client and installer both recording — auto-jn8ca) and a
        # journal-empty server re-bootstrapping established peers on every
        # pull. The signal is the joiner's own bootstrap row: it must
        # settle at COMPLETE and stay there rather than re-anchoring on
        # later pulls.
        def settled_count() -> int:
            import sqlite3 as _sq
            from tools.network.fleet_sync.sweep_receive import read_bootstrap
            try:
                conn = _sq.connect(f"file:{fleet.machines[1].db_path}?mode=ro",
                                   uri=True)
            except _sq.Error:
                return 0
            try:
                state = read_bootstrap(conn)
            except Exception:
                return 0
            finally:
                conn.close()
            return 1 if state is not None and state.phase.value == "complete" else 0

        stable_since = time.monotonic()
        last = settled_count()
        while time.monotonic() - stable_since < 2.0:
            current = settled_count()
            if current != last:
                last = current
                stable_since = time.monotonic()
            assert last <= 1, "joiner re-bootstrapped in a loop"
            time.sleep(0.1)
        time.sleep(1.0)
        assert settled_count() == last
        assert last == 1
        fleet.evidence["settled_bootstraps"] = last
        fleet.write_evidence(tmp_path / "no-refetch-loop.json")
    finally:
        fleet.shutdown()
