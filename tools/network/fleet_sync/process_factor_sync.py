"""Real-process live proof: two Dashboards syncing a personal vault *factor
change* over the roster-authenticated fleet-sync channel after bootstrap.

This is the witness named in bead auto-cf13m's final acceptance criterion —
"two Dashboards syncing a factor change after bootstrap". It reuses the exact
real-process scheduler harness that :mod:`process_dashboard_sync` uses for
notes, but the payload that crosses the wire is a ``vault_factors`` write
authored through :class:`~tools.vault.store.VaultStore` — the ordinary
authored-catalog path, no identity-specific sync code.

The flow, all with genuine OS processes and independent personal databases:

1. Bootstrap. A personal-root key enrolls two machine keys, producing the
   roster entries that authenticate the sync channel. Both joiners open a
   personal database and activate the ordinary fleet-sync writers — the state
   a machine is in *after* it has completed enrollment.
2. Initial factor. The source Dashboard writes a password factor through
   VaultStore. The receiver dials in and the factor converges.
3. Factor change. The source Dashboard rotates that same factor's armor (an
   ordinary last-writer-wins update). The change converges on the receiver.
4. The two ``vault_factors`` tables are compared byte-for-byte.

It is an acceptance instrument, not a production daemon.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from tools.network.fleet_roster import enroll
from tools.network.idkit import KeyPair, canonical_json
from tools.vault.store import VaultStore

from tools.network.fleet_sync.process_dashboard_sync import (
    _entry_dict,
    _peer_report,
    _delete,
    _insert,
    _prepare,
    _start_worker,
    _stop_worker,
    _wait,
    _write_config,
)


def _put_password_factor(
    path: Path, factor_id: str, public_key: str, armor: str
) -> None:
    """Author a password-factor write on *path* through the ordinary VaultStore
    seam, so it becomes a normal authored catalog mutation."""
    with VaultStore(path) as vault:
        vault.put_password_factor(factor_id, public_key, armor)


def _factor_rows(path: Path) -> list[list]:
    with sqlite3.connect(path) as conn:
        return [
            list(row)
            for row in conn.execute(
                "SELECT factor_id,factor_type,public_key,armor "
                "FROM vault_factors ORDER BY factor_id"
            ).fetchall()
        ]


def _factor_armor(path: Path, factor_id: str) -> str | None:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT armor FROM vault_factors WHERE factor_id=?", (factor_id,)
        ).fetchone()
    return None if row is None else row[0]


def _factor_digest(path: Path) -> str:
    import hashlib

    return hashlib.sha256(canonical_json(_factor_rows(path))).hexdigest()


def run_factor_process_acceptance(root_dir: Path) -> dict:
    """Bootstrap two Dashboards and prove a factor change crosses between them.

    Returns one bounded JSON-serializable evidence record.
    """
    root_dir.mkdir(parents=True, exist_ok=False)
    personal_root = KeyPair.generate()
    source_key = KeyPair.generate()
    receiver_key = KeyPair.generate()

    # (1) Bootstrap: roster entries authenticate the channel; both machines
    #     activate the ordinary fleet-sync writers on their personal db.
    entries = [
        enroll(personal_root, machine_pub=source_key.public_hex),
        enroll(personal_root, machine_pub=receiver_key.public_hex),
    ]
    source_db = root_dir / "source-personal.db"
    receiver_db = root_dir / "receiver-personal.db"
    _prepare(source_db, source_key)
    _prepare(receiver_db, receiver_key)
    # Authored-then-deleted local state keeps the receiver on the delta path
    # this probe asserts (transactions_applied counters); sweep bootstrap
    # has its own harness coverage.
    _insert(receiver_db, "receiver-local-seed", "keeps the delta path")
    _delete(receiver_db, "receiver-local-seed")

    factor_id = "personal-root-password"
    public_key = "f" * 64
    initial_armor = json.dumps({"v": 2, "gen": 1, "note": "initial armor"})
    rotated_armor = json.dumps({"v": 2, "gen": 2, "note": "rotated armor"})

    # (2) Initial factor authored on the source Dashboard, before the receiver
    #     connects, so the first crossing carries it.
    _put_password_factor(source_db, factor_id, public_key, initial_armor)

    base = {
        "personal_root_pub": personal_root.public_hex,
        "roster_entries": [_entry_dict(item) for item in entries],
    }
    source_config = root_dir / "source.json"
    _write_config(
        source_config,
        {
            **base,
            "machine_private": source_key.private_hex,
            "personal_db_path": str(source_db),
            "peer_addresses": {},
        },
    )
    source_process, source_port = _start_worker(source_config)
    live = [source_process]
    reports: list[dict] = []
    try:
        receiver_config = root_dir / "receiver.json"
        _write_config(
            receiver_config,
            {
                **base,
                "machine_private": receiver_key.private_hex,
                "personal_db_path": str(receiver_db),
                "peer_addresses": {
                    source_key.public_hex: [f"ws://127.0.0.1:{source_port}"]
                },
            },
        )
        first_process, _ = _start_worker(receiver_config)
        live.append(first_process)
        _wait(lambda: _factor_armor(receiver_db, factor_id) == initial_armor)
        _wait(lambda: _peer_report(receiver_db)["transactions_applied"] >= 1)
        initial_synced = _factor_armor(receiver_db, factor_id) == initial_armor
        reports.append(_stop_worker(first_process))
        live.remove(first_process)

        # (3) Factor change: rotate the SAME factor's armor while the receiver
        #     is offline, then reconnect and prove the newer value wins.
        _put_password_factor(source_db, factor_id, public_key, rotated_armor)
        second_process, _ = _start_worker(receiver_config)
        live.append(second_process)
        _wait(lambda: _factor_armor(receiver_db, factor_id) == rotated_armor)
        _wait(lambda: _peer_report(receiver_db)["transactions_applied"] >= 2)
        change_synced = _factor_armor(receiver_db, factor_id) == rotated_armor
        reports.append(_stop_worker(second_process))
        live.remove(second_process)
    finally:
        source_report = None
        stop_errors: list[Exception] = []
        for process in reversed(live):
            try:
                report = _stop_worker(process)
                if process is source_process:
                    source_report = report
            except Exception as exc:  # pragma: no cover - shutdown best effort
                stop_errors.append(exc)
        if stop_errors:
            raise stop_errors[0]

    assert source_report is not None

    source_digest = _factor_digest(source_db)
    receiver_digest = _factor_digest(receiver_db)
    peer_state = _peer_report(receiver_db)
    source_config.unlink(missing_ok=True)
    receiver_config.unlink(missing_ok=True)
    return {
        "peer_ids": [source_key.public_hex, receiver_key.public_hex],
        "factor_id": factor_id,
        "initial_factor_synced": bool(initial_synced),
        "factor_change_synced": bool(change_synced),
        "receiver_final_armor": _factor_armor(receiver_db, factor_id),
        "expected_final_armor": rotated_armor,
        "bytes_sent": peer_state["bytes_sent"],
        "bytes_received": peer_state["bytes_received"],
        "transactions_applied": peer_state["transactions_applied"],
        "acknowledgements": peer_state["acknowledgements"],
        "final_factor_digests": {
            "source": source_digest,
            "receiver": receiver_digest,
        },
        "factor_tables_identical": source_digest == receiver_digest,
        "reconnected": len(reports) == 2,
        "connections_closed": all(
            report["connections"] == 0 for report in [*reports, source_report]
        ),
    }


def _transcript(evidence: dict) -> str:
    lines = [
        "two-Dashboard factor-change sync — live proof (auto-cf13m)",
        f"  source machine:   {evidence['peer_ids'][0]}",
        f"  receiver machine: {evidence['peer_ids'][1]}",
        f"  factor:           {evidence['factor_id']}",
        f"  [1] initial factor crossed:  {evidence['initial_factor_synced']}",
        f"  [2] factor CHANGE crossed:   {evidence['factor_change_synced']}",
        f"      receiver final armor:    {evidence['receiver_final_armor']}",
        f"      expected final armor:    {evidence['expected_final_armor']}",
        f"  transactions applied:        {evidence['transactions_applied']}",
        f"  acknowledgements:            {evidence['acknowledgements']}",
        f"  vault_factors byte-identical: {evidence['factor_tables_identical']}",
        f"  reconnected across restart:  {evidence['reconnected']}",
        f"  all connections closed:      {evidence['connections_closed']}",
    ]
    return "\n".join(lines)


def _proven(evidence: dict) -> bool:
    return bool(
        evidence["initial_factor_synced"]
        and evidence["factor_change_synced"]
        and evidence["receiver_final_armor"] == evidence["expected_final_armor"]
        and evidence["factor_tables_identical"]
        and evidence["transactions_applied"] >= 2
        and evidence["reconnected"]
        and evidence["connections_closed"]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    evidence = run_factor_process_acceptance(args.output_dir / "factor-fleet")
    print(_transcript(evidence))
    print(json.dumps(evidence, sort_keys=True))
    proven = _proven(evidence)
    print("RESULT: PROVEN" if proven else "RESULT: NOT PROVEN")
    return 0 if proven else 1


if __name__ == "__main__":
    raise SystemExit(main())
