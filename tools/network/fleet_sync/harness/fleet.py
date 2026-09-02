"""N-machine fleet construction and scenario driving for sync acceptance.

Generalizes the two-process acceptance probe (process_dashboard_sync.py):
real scheduler processes with independent databases, every inter-machine
dial routed through a per-direction FaultyLink, and a declarative timeline
runner. The parent stays synchronous; evidence accumulates into one bounded
JSON-serializable record.
"""

from __future__ import annotations

import hashlib
import json
import os
import select
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from tools.graph.db import GraphDB
from tools.graph.models import Source
from tools.network.fleet_roster import enroll
from tools.network.idkit import KeyPair, canonical_json

from .proxy import ProxyHub

_WORKER_MODULE = "tools.network.fleet_sync.process_dashboard_sync"
_REPO_ROOT = Path(__file__).resolve().parents[4]


@dataclass
class Machine:
    index: int
    key: KeyPair
    db_path: Path
    config_path: Path
    process: subprocess.Popen | None = None
    port: int | None = None
    restarts: int = 0


@dataclass
class Step:
    """One timeline event: ``at`` seconds after timeline start."""

    at: float
    action: Callable[["HarnessFleet"], None]
    label: str = ""


class HarnessFleet:
    """Build, fault, and observe a local fleet of real sync processes."""

    def __init__(
        self, root_dir: Path, size: int, *, seed: int = 7,
        org_scopes: tuple[str, ...] = (),
        org_customize: "Callable[[int, str, Path], None] | None" = None,
    ) -> None:
        if size < 2:
            raise ValueError("a fleet needs at least two machines")
        self.root_dir = Path(root_dir)
        self.size = size
        self.org_scopes = org_scopes
        self.org_customize = org_customize
        self.hub = ProxyHub(seed=seed)
        self.machines: list[Machine] = []
        self.evidence: dict = {"faults": [], "restarts": [], "writes": 0}
        self._root_key = KeyPair.generate()
        self._entries: list = []

    # -- construction -----------------------------------------------------

    def build(self) -> "HarnessFleet":
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.hub.start()
        for index in range(self.size):
            key = KeyPair.generate()
            self._entries.append(
                enroll(self._root_key, machine_pub=key.public_hex)
            )
            db_path = self.root_dir / f"machine-{index}.db"
            graph = GraphDB(db_path)
            try:
                graph.activate_fleet_sync_writers(key.public_hex)
            finally:
                graph.close()
            for slug in self.org_scopes:
                org_path = self.org_db_path(index, slug)
                GraphDB(org_path).close()
                # Customization runs before writer activation so a scenario
                # can diverge one machine's schema while leaving that
                # machine self-consistent (its capture triggers match its
                # own schema; only the cross-machine digest differs).
                if self.org_customize is not None:
                    self.org_customize(index, slug, org_path)
                org_graph = GraphDB(org_path)
                try:
                    org_graph.activate_fleet_sync_writers(key.public_hex)
                finally:
                    org_graph.close()
            self.machines.append(Machine(
                index, key, db_path, self.root_dir / f"machine-{index}.json",
            ))
        for dialer in range(self.size):
            for target in range(self.size):
                if dialer != target:
                    self.hub.create_link(self._link_name(dialer, target))
        return self

    @staticmethod
    def _link_name(dialer: int, target: int) -> str:
        return f"{dialer}->{target}"

    def _write_config(self, machine: Machine) -> None:
        peer_addresses = {}
        for other in self.machines:
            if other.index == machine.index:
                continue
            link = self.hub.link(self._link_name(machine.index, other.index))
            peer_addresses[other.key.public_hex] = [
                f"ws://{link.host}:{link.port}"
            ]
        machine.config_path.write_text(json.dumps({
            "personal_root_pub": self._root_key.public_hex,
            "roster_entries": [entry.to_dict() for entry in self._entries],
            "machine_private": machine.key.private_hex,
            "personal_db_path": str(machine.db_path),
            "peer_addresses": peer_addresses,
            "sync_scopes": {
                slug: str(self.org_db_path(machine.index, slug))
                for slug in self.org_scopes
            },
        }, sort_keys=True))

    # -- lifecycle --------------------------------------------------------

    def start(self, index: int) -> None:
        machine = self.machines[index]
        self._write_config(machine)
        process = subprocess.Popen(
            [sys.executable, "-m", _WORKER_MODULE, "--worker",
             str(machine.config_path)],
            cwd=_REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        assert process.stdout is not None
        ready, _, _ = select.select([process.stdout], [], [], 15.0)
        if not ready:
            process.kill()
            _out, err = process.communicate()
            raise RuntimeError(f"machine {index} did not start: {err[-2000:]}")
        message = json.loads(process.stdout.readline())
        if message.get("kind") != "ready":
            raise RuntimeError(f"machine {index} unexpected startup: {message}")
        machine.process = process
        machine.port = int(message["port"])
        for other in self.machines:
            if other.index != index:
                self.hub.set_target(
                    self._link_name(other.index, index),
                    "127.0.0.1", machine.port,
                )

    def start_all(self) -> None:
        for index in range(self.size):
            self.start(index)

    def stop(self, index: int, *, kill: bool = False) -> None:
        machine = self.machines[index]
        process = machine.process
        if process is None:
            return
        machine.process = None
        if kill:
            process.kill()
            process.communicate()
            return
        process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()

    def restart(self, index: int, *, kill: bool = True) -> None:
        self.stop(index, kill=kill)
        self.machines[index].restarts += 1
        self.evidence["restarts"].append(
            {"machine": index, "at": time.monotonic()}
        )
        self.start(index)

    def shutdown(self) -> None:
        for machine in self.machines:
            self.stop(machine.index, kill=True)
        self.hub.stop()

    # -- faults -----------------------------------------------------------

    def set_link_faults(self, dialer: int, target: int, **changes) -> None:
        name = self._link_name(dialer, target)
        self.hub.set_faults(name, **changes)
        self.evidence["faults"].append({"link": name, **{
            key: value for key, value in changes.items()
        }})

    def set_pair_faults(self, a: int, b: int, **changes) -> None:
        self.set_link_faults(a, b, **changes)
        self.set_link_faults(b, a, **changes)

    def partition(self, a: int, b: int) -> None:
        self.set_pair_faults(a, b, partitioned=True)

    def heal(self, a: int, b: int) -> None:
        self.set_pair_faults(a, b, partitioned=False)

    # -- workload and observation ----------------------------------------

    def org_db_path(self, index: int, slug: str) -> Path:
        org_dir = self.root_dir / f"machine-{index}-orgs"
        org_dir.mkdir(parents=True, exist_ok=True)
        return org_dir / f"{slug}.db"

    def write_org(
        self, index: int, slug: str, source_id: str, title: str
    ) -> None:
        graph = GraphDB(self.org_db_path(index, slug))
        try:
            graph.insert_source(Source(id=source_id, type="note", title=title))
        finally:
            graph.close()
        self.evidence["writes"] += 1

    def has_org(self, index: int, slug: str, source_id: str) -> bool:
        with sqlite3.connect(self.org_db_path(index, slug)) as conn:
            return conn.execute(
                "SELECT 1 FROM sources WHERE id=?", (source_id,)
            ).fetchone() is not None

    def write(self, index: int, source_id: str, title: str) -> None:
        graph = GraphDB(self.machines[index].db_path)
        try:
            graph.insert_source(Source(id=source_id, type="note", title=title))
        finally:
            graph.close()
        self.evidence["writes"] += 1

    def has(self, index: int, source_id: str) -> bool:
        with sqlite3.connect(self.machines[index].db_path) as conn:
            return conn.execute(
                "SELECT 1 FROM sources WHERE id=?", (source_id,)
            ).fetchone() is not None

    def digest(self, index: int) -> str:
        with sqlite3.connect(self.machines[index].db_path) as conn:
            rows = conn.execute(
                "SELECT id,type,title,metadata,created_at,ingested_at "
                "FROM sources ORDER BY id"
            ).fetchall()
        return hashlib.sha256(
            canonical_json([list(row) for row in rows])
        ).hexdigest()

    def converged(self) -> bool:
        digests = {self.digest(machine.index) for machine in self.machines}
        return len(digests) == 1

    def wait(
        self, predicate: Callable[[], bool], *, timeout: float,
        interval: float = 0.05, label: str = "condition",
    ) -> float:
        started = time.monotonic()
        deadline = started + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"harness {label} not met within {timeout:.1f}s"
                )
            time.sleep(interval)
        return time.monotonic() - started

    def wait_converged(self, *, timeout: float) -> float:
        elapsed = self.wait(
            self.converged, timeout=timeout, label="convergence"
        )
        self.evidence["converged_after_s"] = round(elapsed, 3)
        self.evidence["final_digest"] = self.digest(0)
        return elapsed

    # -- timeline ---------------------------------------------------------

    def run_timeline(self, steps: list[Step]) -> None:
        """Execute actions at their offsets from now, in order."""
        origin = time.monotonic()
        for step in sorted(steps, key=lambda item: item.at):
            delay = origin + step.at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            step.action(self)
            if step.label:
                self.evidence.setdefault("timeline", []).append(
                    {"at": step.at, "label": step.label}
                )

    def write_evidence(self, path: Path) -> None:
        record = dict(self.evidence)
        record["machines"] = [
            {
                "index": machine.index,
                "restarts": machine.restarts,
                "digest": self.digest(machine.index),
            }
            for machine in self.machines
        ]
        path.write_text(json.dumps(record, indent=1, sort_keys=True))
