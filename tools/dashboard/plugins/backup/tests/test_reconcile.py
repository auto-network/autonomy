"""Run-report ingestion: bounded probes, idempotence, retention.

The store side is faked with an in-memory dict whose add path runs the
REAL validate_payload, so a report the schema would refuse fails here
too. The filesystem side uses real files in tmp_path — plus a FIFO to
prove a blocking read marks the tier probe-stale inside the timeout
instead of hanging the caller (driver S7).
"""
from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest

from tools.dashboard.plugins.backup import reconcile as R
from tools.dashboard.plugins.backup.entrypoints.schemas import (
    RUN_SET_ID,
    SCHEMA_REVISION,
)
from tools.graph.schemas.registry import validate_payload


def _report(tier="hourly", stamp="20260906-001110", verdict="complete",
            **kw) -> dict:
    base = {
        "tier": tier, "stamp": stamp, "verdict": verdict,
        "started_at": "2026-09-06T00:11:10+00:00",
        "finished_at": "2026-09-06T00:12:40+00:00",
        "duration_seconds": 90.0, "origin": "host",
        "data_root": "/opt/autonomy/data",
        "stores": [{"name": "orgs/autonomy.db", "action": "sqlite",
                    "status": "ok", "bytes": 1557696512}],
        "store_count": 24, "beads_databases": 3,
        "total_bytes": 1557696512, "offsite": "complete",
        "failures": [] if verdict == "complete" else ["auth: MISSING"],
        "exit_code": 0 if verdict == "complete" else 1,
    }
    base.update(kw)
    return base


@pytest.fixture
def store(monkeypatch):
    """In-memory settings store with the real schema validation."""
    rows: dict[str, dict] = {}
    counter = iter(range(10_000))

    def write_by_key(set_id, rev, key, payload, *, org, **kw):
        assert set_id == RUN_SET_ID and rev == SCHEMA_REVISION
        assert org == "machine"
        validate_payload(set_id, rev, payload)
        rows[key] = {"id": rows.get(key, {}).get("id") or f"s{next(counter)}",
                     "payload": payload}

    def read_set_key(set_id, key, *, org, peers=None):
        row = rows.get(key)
        return {"payload": row["payload"]} if row else None

    def read_set(set_id, *, org, peers=None, **kw):
        return [SimpleNamespace(key=key, id=row["id"], payload=row["payload"])
                for key, row in rows.items()]

    def remove_setting(setting_id, *, org):
        for key, row in list(rows.items()):
            if row["id"] == setting_id:
                del rows[key]
                return
        raise KeyError(setting_id)

    import tools.graph.settings_ops as settings_ops
    monkeypatch.setattr(settings_ops, "write_by_key", write_by_key)
    monkeypatch.setattr(settings_ops, "read_set_key", read_set_key)
    monkeypatch.setattr(settings_ops, "read_set", read_set)
    monkeypatch.setattr(settings_ops, "remove_setting", remove_setting)
    monkeypatch.setattr(R, "_run_retention", lambda: 3)
    return rows


def _write(root, tier, report):
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{tier}-latest.json").write_text(json.dumps(report))


def test_ingests_and_strips_key_segments(store, tmp_path):
    _write(tmp_path, "hourly", _report())
    result = R.reconcile(tmp_path)
    assert result["ingested"] == ["hourly:20260906-001110"]
    payload = store["hourly:20260906-001110"]["payload"]
    assert "tier" not in payload and "stamp" not in payload
    assert payload["verdict"] == "complete"


def test_idempotent_until_the_report_changes(store, tmp_path):
    _write(tmp_path, "hourly", _report(offsite="unknown"))
    assert R.reconcile(tmp_path)["ingested"] == ["hourly:20260906-001110"]
    assert R.reconcile(tmp_path)["ingested"] == []
    # The offsite verdict lands after the first write — same key, new
    # payload, re-upserted.
    _write(tmp_path, "hourly", _report(offsite="complete"))
    assert R.reconcile(tmp_path)["ingested"] == ["hourly:20260906-001110"]
    assert store["hourly:20260906-001110"]["payload"]["offsite"] == "complete"


def test_failed_run_reports_ingest_too(store, tmp_path):
    _write(tmp_path, "hourly", _report(verdict="failed"))
    result = R.reconcile(tmp_path)
    assert result["ingested"] == ["hourly:20260906-001110"]
    assert store["hourly:20260906-001110"]["payload"]["failures"]


def test_absent_report_is_absence_not_error(store, tmp_path):
    result = R.reconcile(tmp_path)
    assert result["ingested"] == []
    assert result["probe_errors"] == {}


def test_malformed_report_marks_probe_stale(store, tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "hourly-latest.json").write_text("{not json")
    assert "hourly" in R.reconcile(tmp_path)["probe_errors"]


def test_schema_refused_report_marks_probe_stale(store, tmp_path):
    # complete + failures is the silent-success shape the schema refuses.
    _write(tmp_path, "hourly",
           _report(verdict="complete", failures=["boom"]))
    result = R.reconcile(tmp_path)
    assert result["ingested"] == []
    assert "refused by schema" in result["probe_errors"]["hourly"]


def test_retention_prunes_oldest_per_tier(store, tmp_path):
    for hour in range(6):
        _write(tmp_path, "hourly",
               _report(stamp=f"20260906-0{hour}0000"))
        R.reconcile(tmp_path)
    hourly = sorted(k for k in store if k.startswith("hourly:"))
    assert len(hourly) == 3  # retention monkeypatched to 3
    assert hourly[-1] == "hourly:20260906-050000"
    assert "hourly:20260906-000000" not in store


def test_blocking_read_times_out_not_hangs(store, tmp_path):
    fifo = tmp_path / "hourly-latest.json"
    os.mkfifo(fifo)  # a read blocks forever: the NFS-hang stand-in
    start = time.monotonic()
    result = R.reconcile(tmp_path, timeout=1.0)
    elapsed = time.monotonic() - start
    assert elapsed < 5.0
    assert "hourly" in result["probe_errors"]
    assert "exceeded" in result["probe_errors"]["hourly"]
