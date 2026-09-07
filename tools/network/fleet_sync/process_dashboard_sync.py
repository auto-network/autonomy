"""Real-process acceptance probe for the Dashboard fleet-sync scheduler.

The parent launches independent scheduler processes against independent
personal databases, forces one failed dial, transfers a note, restarts the
receiver, transfers another note, and emits one bounded JSON evidence record.
It is an acceptance instrument, not a production daemon.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import select
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from tools.graph.db import GraphDB
from tools.graph.models import Source
from tools.network.fleet_roster import RosterEntry, enroll
from tools.network.fleet_sync_scheduler import (
    FleetSyncRuntimeConfig,
    FleetSyncScheduler,
)
from tools.network.idkit import KeyPair, canonical_json

_MODULE = "tools.network.fleet_sync.process_dashboard_sync"


def _entry_dict(entry: RosterEntry) -> dict:
    return entry.to_dict()


def _entry(payload: dict) -> RosterEntry:
    return RosterEntry.from_dict(payload)


def _peer_report(path: Path) -> dict:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(bytes_sent),0),COALESCE(SUM(bytes_received),0),"
            "COALESCE(SUM(retries),0),COALESCE(SUM(transactions_applied),0),"
            "COALESCE(SUM(acknowledgements),0),COALESCE(MAX(online),0) "
            "FROM fleet_sync_peer_state"
        ).fetchone()
    return {
        "bytes_sent": int(row[0]),
        "bytes_received": int(row[1]),
        "retries": int(row[2]),
        "transactions_applied": int(row[3]),
        "acknowledgements": int(row[4]),
        "online": int(row[5]),
    }


class _OrgChannels:
    """The worker's org channels (auto-coea3), built from the config's
    ``org_channels`` block and refreshed when the parent rewrites the
    config file -- the harness's stand-in for the membership half
    updating a node's adopted checkpoint (a removal, a rekey, a join).

    Per scope: ``{"org", "persona_cert", "adopted": {seq: [persona pubs]},
    "seq"}``. The rider is computed from the member list of ``seq`` with
    membership_commitment.inclusion_proof; the adopted checkpoint record
    carries the members_root of that list; a newer ``seq`` on reload calls
    note_adoption with the 5 s production deadline; a changed persona
    certificate (a rekey) rebuilds that scope's authenticator.
    """

    def __init__(self, config_path: Path, payload: dict, addresses, machine_key) -> None:
        self._path = config_path
        self._addresses = addresses
        self._machine_key = machine_key
        self._mtime = config_path.stat().st_mtime_ns
        self._channels: dict = {}
        self._specs: dict = {}
        self.org_peers: dict = dict(payload.get("org_peer_addresses") or {})
        self._apply(payload.get("org_channels") or {})

    def _build(self, scope: str, spec: dict, machine_key):
        from tools.network.fleet_org_channel import OrgFleetAuthenticator
        from tools.network.idkit import DelegationCert
        from tools.network.ledger import membership_commitment as mc

        state = {"seq": int(spec["seq"]), "adopted": {
            int(k): list(v) for k, v in spec["adopted"].items()
        }}
        cert = DelegationCert.from_dict(spec["persona_cert"])
        persona = str(cert.subject.id)

        def rider():
            members = state["adopted"][state["seq"]]
            try:
                index, path = mc.inclusion_proof(members, persona)
            except mc.MembershipCommitmentError:
                index, path = 0, []
            return {"v": 1, "checkpoint_seq": state["seq"], "index": index, "path": path}

        def adopted_for(seq):
            members = state["adopted"].get(int(seq))
            if members is None:
                return None
            return {"seq": int(seq), "members_root": mc.compute_root(members)}

        channel = OrgFleetAuthenticator(
            machine_key, org=str(spec["org"]), persona_cert=cert,
            membership_proof_for=rider, adopted_checkpoint_for=adopted_for,
            newest_adopted_seq=lambda: max(state["adopted"]),
            adopted_members_for=lambda seq: state["adopted"].get(int(seq)),
            advertised_addresses=self._addresses,
        )
        return channel, state

    def _apply(self, specs: dict) -> None:
        for scope, spec in specs.items():
            machine_key = self._machine_key
            previous = self._specs.get(scope)
            if previous is None or previous["persona_cert"] != spec["persona_cert"] \
                    or previous["org"] != spec["org"]:
                self._channels[scope] = self._build(scope, spec, machine_key)
            else:
                channel, state = self._channels[scope]
                newest_before = max(state["adopted"])
                state["adopted"] = {int(k): list(v) for k, v in spec["adopted"].items()}
                state["seq"] = int(spec["seq"])
                newest = max(state["adopted"])
                if newest > newest_before:
                    channel.note_adoption(newest)
            self._specs[scope] = dict(spec)
        for scope in list(self._channels):
            if scope not in specs:
                del self._channels[scope]
                self._specs.pop(scope, None)

    def refresh(self) -> None:
        try:
            mtime = self._path.stat().st_mtime_ns
        except OSError:
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            payload = json.loads(self._path.read_text())
        except (OSError, ValueError):
            return
        self.org_peers = dict(payload.get("org_peer_addresses") or {})
        self._apply(payload.get("org_channels") or {})

    def channels(self) -> dict:
        self.refresh()
        return {scope: pair[0] for scope, pair in self._channels.items()}

    def peers(self) -> dict:
        self.refresh()
        return dict(self.org_peers)


async def _worker(config_path: Path) -> int:
    payload = json.loads(config_path.read_text())
    entries = tuple(_entry(item) for item in payload["roster_entries"])
    machine_key = KeyPair.from_private_hex(payload["machine_private"])
    # In-memory resume trails, as production keeps durably: without a trail
    # every pull presents position zero, and against a gap-pruned journal
    # the continuity decision would re-checkpoint on every poll. Losing the
    # trail on process death is correct — the restarted worker re-bootstraps
    # once and resumes.
    acknowledged: dict[tuple[str, str], list] = {}

    pull_log = os.environ.get("AUTONOMY_HARNESS_PULL_LOG")
    if pull_log:
        # The engine's warnings (a failed attachment drain, a refused
        # install) are otherwise lost with the worker's stderr pipe; a
        # harness run wants them next to its ledger.
        import logging

        logging.basicConfig(
            level=logging.INFO, filename=f"{pull_log}.log",
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )

    def record(peer, **values):
        breadcrumb = values.get("acknowledged_breadcrumb")
        if breadcrumb is not None and values.get("outcome") == "success":
            acknowledged[(peer, values.get("scope", "personal"))] = [(
                breadcrumb["origin"],
                breadcrumb["transaction"],
                breadcrumb["timestamp"],
            )]
        if pull_log:
            # One JSON line per terminal pull attempt: the scale driver
            # aggregates frames/bytes per pull to separate payload
            # duplication from protocol overhead.
            entry = {k: v for k, v in values.items()
                     if isinstance(v, (int, float, str, list)) or v is None}
            entry["peer"] = peer[:12]
            entry["at"] = time.time()
            with open(pull_log, "a") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")

    # Org channels (auto-coea3): present only when the config carries them;
    # a worker without them is the personal-fleet worker exactly as before.
    advertised: list[str] = []
    org: _OrgChannels | None = None
    if payload.get("org_channels"):
        org = _OrgChannels(config_path, payload, lambda: list(advertised), machine_key)

    scheduler = FleetSyncScheduler(
        FleetSyncRuntimeConfig(
            machine_key=machine_key,
            personal_root_pub=payload["personal_root_pub"],
            roster_entries=lambda: entries,
            peer_addresses=lambda: payload["peer_addresses"],
            personal_db_path=Path(payload["personal_db_path"]),
            telemetry_recorder=record,
            resume_cursor=lambda peer, scope="personal": acknowledged.get(
                (peer, scope), []
            ),
            sync_scopes=(
                (lambda scopes: (lambda: {
                    slug: Path(path) for slug, path in scopes.items()
                }))(payload["sync_scopes"])
                if payload.get("sync_scopes") else None
            ),
            connect_timeout=3.0,
            min_backoff=0.02,
            max_backoff=0.08,
            # Honor the fleet's settings: until 2026-09-07 these two were
            # ignored, so every harness run polled at the production default
            # (10 s) with 3 concurrent pulls whatever the scenario asked for.
            poll_interval=float(payload.get("poll_interval", 10.0)),
            max_concurrent_pulls=int(payload.get("max_concurrent_pulls", 3)),
            org_channels=(org.channels if org is not None else None),
            org_peer_addresses=(org.peers if org is not None else None),
            advertised_addresses=(
                (lambda: list(advertised)) if org is not None else None
            ),
        )
    )
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopped.set)

    async def parent_watch(initial_parent: int = os.getppid()) -> None:
        # A worker must never outlive its parent: an orphan keeps the test
        # runner's process group open and wedges its supervisor (observed
        # live 2026-09-02). Reparenting to init/reaper means the parent died.
        while not stopped.is_set():
            if os.getppid() != initial_parent:
                stopped.set()
                return
            await asyncio.sleep(0.5)

    watcher = asyncio.ensure_future(parent_watch())
    await scheduler.start()
    if org is not None:
        # The listener's port is known only now; it is what this machine
        # introduces in its org hello and publishes as its reachability row.
        advertised.append(f"ws://127.0.0.1:{scheduler.port}")
    print(json.dumps({"kind": "ready", "port": scheduler.port}), flush=True)
    await stopped.wait()
    watcher.cancel()
    await scheduler.stop()
    print(json.dumps({
        "kind": "stopped",
        "connections": scheduler.server.connection_count,
        "peer_state": _peer_report(Path(payload["personal_db_path"])),
    }), flush=True)
    return 0


def _prepare(path: Path, machine: KeyPair) -> None:
    db = GraphDB(path)
    try:
        db.activate_fleet_sync_writers(machine.public_hex)
    finally:
        db.close()


def _insert(path: Path, source_id: str, title: str) -> None:
    db = GraphDB(path)
    try:
        db.insert_source(Source(id=source_id, type="note", title=title))
    finally:
        db.close()


def _delete(path: Path, source_id: str) -> None:
    db = GraphDB(path)
    try:
        db.conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
        db.conn.commit()
    finally:
        db.close()


def _has(path: Path, source_id: str) -> bool:
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT 1 FROM sources WHERE id=?", (source_id,)
        ).fetchone() is not None


def _digest(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT id,type,title,metadata,created_at,ingested_at "
            "FROM sources ORDER BY id"
        ).fetchall()
    return hashlib.sha256(canonical_json([list(row) for row in rows])).hexdigest()


def _write_config(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, sort_keys=True))


def _start_worker(config: Path) -> tuple[subprocess.Popen, int]:
    process = subprocess.Popen(
        [sys.executable, "-m", _MODULE, "--worker", str(config)],
        cwd=Path(__file__).resolve().parents[3],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    assert process.stdout is not None
    ready, _, _ = select.select([process.stdout], [], [], 10.0)
    if not ready:
        process.kill()
        _out, err = process.communicate()
        raise RuntimeError(f"fleet worker did not start: {err[-2000:]}")
    line = process.stdout.readline()
    message = json.loads(line)
    if message.get("kind") != "ready":
        raise RuntimeError(f"fleet worker returned unexpected startup: {message}")
    return process, int(message["port"])


def _stop_worker(process: subprocess.Popen) -> dict:
    process.terminate()
    try:
        stdout, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise RuntimeError(f"fleet worker did not stop: {stderr[-2000:]}")
    if process.returncode != 0:
        raise RuntimeError(f"fleet worker failed: {stderr[-2000:]}")
    messages = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    if not messages or messages[-1].get("kind") != "stopped":
        raise RuntimeError(f"fleet worker omitted shutdown evidence: {stdout[-2000:]}")
    return messages[-1]


def _wait(predicate, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise RuntimeError("fleet process acceptance condition timed out")
        time.sleep(0.03)


def run_process_acceptance(root_dir: Path) -> dict:
    root_dir.mkdir(parents=True, exist_ok=False)
    personal_root = KeyPair.generate()
    left_key = KeyPair.generate()
    right_key = KeyPair.generate()
    entries = [
        enroll(personal_root, machine_pub=left_key.public_hex),
        enroll(personal_root, machine_pub=right_key.public_hex),
    ]
    left_db = root_dir / "left-personal.db"
    right_db = root_dir / "right-personal.db"
    _prepare(left_db, left_key)
    _prepare(right_db, right_key)
    _insert(right_db, "process-first", "first process crossing")
    # Authored-then-deleted local state keeps the probe on the delta path
    # it exists to prove (checkpoint bootstrap has its own harness coverage)
    # without a live row that would diverge the final digests.
    _insert(left_db, "left-local-seed", "keeps the delta path")
    _delete(left_db, "left-local-seed")

    base = {
        "personal_root_pub": personal_root.public_hex,
        "roster_entries": [_entry_dict(item) for item in entries],
    }
    right_config = root_dir / "right.json"
    _write_config(right_config, {
        **base,
        "machine_private": right_key.private_hex,
        "personal_db_path": str(right_db),
        "peer_addresses": {},
    })
    right_process, right_port = _start_worker(right_config)
    live_processes = [right_process]
    reports: list[dict] = []
    try:
        left_config = root_dir / "left.json"
        _write_config(left_config, {
            **base,
            "machine_private": left_key.private_hex,
            "personal_db_path": str(left_db),
            "peer_addresses": {right_key.public_hex: ["ws://127.0.0.1:1"]},
        })
        failed_process, _ = _start_worker(left_config)
        live_processes.append(failed_process)
        _wait(lambda: _peer_report(left_db)["retries"] >= 1)
        reports.append(_stop_worker(failed_process))
        live_processes.remove(failed_process)

        _write_config(left_config, {
            **base,
            "machine_private": left_key.private_hex,
            "personal_db_path": str(left_db),
            "peer_addresses": {
                right_key.public_hex: [f"ws://127.0.0.1:{right_port}"]
            },
        })
        first_process, _ = _start_worker(left_config)
        live_processes.append(first_process)
        _wait(lambda: _has(left_db, "process-first"))
        _wait(lambda: _peer_report(left_db)["transactions_applied"] >= 1)
        # The success record (acknowledgements) lands after the stream and
        # drain complete; stopping on data-arrival alone races it.
        _wait(lambda: _peer_report(left_db)["acknowledgements"] >= 1)
        reports.append(_stop_worker(first_process))
        live_processes.remove(first_process)

        _insert(right_db, "process-reconnect", "second process crossing")
        second_process, _ = _start_worker(left_config)
        live_processes.append(second_process)
        _wait(lambda: _has(left_db, "process-reconnect"))
        _wait(lambda: _peer_report(left_db)["transactions_applied"] >= 2)
        _wait(lambda: _peer_report(left_db)["acknowledgements"] >= 2)
        reports.append(_stop_worker(second_process))
        live_processes.remove(second_process)
    finally:
        right_report = None
        stop_errors: list[Exception] = []
        for process in reversed(live_processes):
            try:
                report = _stop_worker(process)
                if process is right_process:
                    right_report = report
            except Exception as exc:
                stop_errors.append(exc)
        if stop_errors:
            raise stop_errors[0]

    assert right_report is not None

    left_digest = _digest(left_db)
    right_digest = _digest(right_db)
    peer_state = _peer_report(left_db)
    left_config.unlink(missing_ok=True)
    right_config.unlink(missing_ok=True)
    return {
        "peer_ids": [left_key.public_hex, right_key.public_hex],
        "bytes_sent": peer_state["bytes_sent"],
        "bytes_received": peer_state["bytes_received"],
        "retries": peer_state["retries"],
        "transactions_applied": peer_state["transactions_applied"],
        "acknowledgements": peer_state["acknowledgements"],
        "final_digests": {"left": left_digest, "right": right_digest},
        "digests_match": left_digest == right_digest,
        "reconnected": len(reports) == 3,
        "connections_closed": all(
            report["connections"] == 0 for report in [*reports, right_report]
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    if args.worker is not None:
        return asyncio.run(_worker(args.worker))
    if args.output_dir is None:
        parser.error("--output-dir is required")
    print(json.dumps(run_process_acceptance(args.output_dir), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
