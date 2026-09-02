"""Seed scenarios proving the fault-injection harness end to end."""

import json
from pathlib import Path

from tools.network.fleet_sync.harness import HarnessFleet, Step


def test_three_way_convergence(tmp_path: Path) -> None:
    fleet = HarnessFleet(tmp_path / "fleet", size=3).build()
    fleet.start_all()
    try:
        for machine in range(3):
            for note in range(5):
                fleet.write(
                    machine, f"m{machine}-n{note}", f"note {note} from {machine}"
                )
        fleet.wait_converged(timeout=25.0)
        assert all(
            fleet.has(machine, "m2-n4") for machine in range(3)
        )
        fleet.write_evidence(tmp_path / "three-way.json")
    finally:
        fleet.shutdown()
    evidence = json.loads((tmp_path / "three-way.json").read_text())
    assert evidence["writes"] == 15
    assert len({m["digest"] for m in evidence["machines"]}) == 1


def test_flapping_peer_converges_after_stabilizing(tmp_path: Path) -> None:
    fleet = HarnessFleet(tmp_path / "fleet", size=3).build()
    fleet.start_all()
    try:
        fleet.run_timeline([
            Step(0.0, lambda f: f.write(0, "w-0", "before flapping"), "write"),
            Step(0.2, lambda f: f.restart(2, kill=True), "flap 1"),
            Step(0.5, lambda f: f.write(1, "w-1", "during flapping"), "write"),
            Step(0.7, lambda f: f.restart(2, kill=True), "flap 2"),
            Step(1.0, lambda f: f.write(0, "w-2", "still flapping"), "write"),
            Step(1.2, lambda f: f.restart(2, kill=True), "flap 3"),
        ])
        fleet.wait_converged(timeout=30.0)
        assert fleet.machines[2].restarts == 3
        assert all(fleet.has(2, f"w-{n}") for n in range(3))
        fleet.write_evidence(tmp_path / "flap.json")
    finally:
        fleet.shutdown()


def test_lossy_link_converges_without_divergence(tmp_path: Path) -> None:
    fleet = HarnessFleet(tmp_path / "fleet", size=2).build()
    # Both directions degraded before anything syncs: 30% retransmit-like
    # stalls, occasional mid-stream resets, and real added latency.
    fleet.start_all()
    try:
        fleet.set_pair_faults(
            0, 1,
            stall_rate=0.3, stall_s=0.15, reset_rate=0.02,
            latency_s=0.01, jitter_s=0.02,
        )
        for note in range(10):
            fleet.write(0, f"a-{note}", f"lossy a {note}")
            fleet.write(1, f"b-{note}", f"lossy b {note}")
        fleet.wait_converged(timeout=60.0)
        assert fleet.has(1, "a-9") and fleet.has(0, "b-9")
        fleet.write_evidence(tmp_path / "lossy.json")
    finally:
        fleet.shutdown()
    evidence = json.loads((tmp_path / "lossy.json").read_text())
    assert evidence["converged_after_s"] >= 0.0
    assert len({m["digest"] for m in evidence["machines"]}) == 1
