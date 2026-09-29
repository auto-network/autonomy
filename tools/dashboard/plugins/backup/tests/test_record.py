"""Recording a finished run: its report becomes its backup.run row.

The store side is faked with an in-memory dict whose write path runs the
REAL validate_payload, so a report the schema would refuse fails here
too. The report is a real file in tmp_path.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tools.dashboard.plugins.backup import record as R
from tools.dashboard.plugins.backup.entrypoints.schemas import (
    RUN_SET_ID,
    SCHEMA_REVISION,
)
from tools.graph.schemas.registry import SchemaValidationError, validate_payload


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
    monkeypatch.setattr(settings_ops, "read_set", read_set)
    monkeypatch.setattr(settings_ops, "remove_setting", remove_setting)
    monkeypatch.setattr(R, "_run_retention", lambda: 3)
    return rows


def _write(root, tier, report):
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{tier}-latest.json").write_text(json.dumps(report))


def test_records_and_strips_key_segments(store, tmp_path):
    _write(tmp_path, "hourly", _report())
    assert R.record("hourly", tmp_path) == "hourly:20260906-001110"
    payload = store["hourly:20260906-001110"]["payload"]
    assert "tier" not in payload and "stamp" not in payload
    assert payload["verdict"] == "complete"


def test_offsite_verdict_rewrites_the_same_run(store, tmp_path):
    _write(tmp_path, "hourly", _report(offsite="unknown"))
    R.record("hourly", tmp_path)
    _write(tmp_path, "hourly", _report(offsite="complete"))
    R.record("hourly", tmp_path)
    assert list(store) == ["hourly:20260906-001110"]
    assert store["hourly:20260906-001110"]["payload"]["offsite"] == "complete"


def test_failed_run_is_recorded_too(store, tmp_path):
    _write(tmp_path, "hourly", _report(verdict="failed"))
    R.record("hourly", tmp_path)
    assert store["hourly:20260906-001110"]["payload"]["failures"]


def test_missing_or_malformed_report_raises(store, tmp_path):
    with pytest.raises(OSError):
        R.record("hourly", tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "hourly-latest.json").write_text("{not json")
    with pytest.raises(ValueError):
        R.record("hourly", tmp_path)
    _write(tmp_path, "hourly", _report(tier="daily"))
    with pytest.raises(ValueError):
        R.record("hourly", tmp_path)
    with pytest.raises(ValueError):
        R.record("weekly", tmp_path)
    assert store == {}


def test_schema_refused_report_is_not_recorded(store, tmp_path):
    # complete + failures is the silent-success shape the schema refuses.
    _write(tmp_path, "hourly",
           _report(verdict="complete", failures=["boom"]))
    with pytest.raises(SchemaValidationError):
        R.record("hourly", tmp_path)
    assert store == {}


def test_retention_prunes_oldest_of_the_tier(store, tmp_path):
    _write(tmp_path, "daily", _report(tier="daily", stamp="20260901-030000"))
    R.record("daily", tmp_path)
    for hour in range(6):
        _write(tmp_path, "hourly",
               _report(stamp=f"20260906-0{hour}0000"))
        R.record("hourly", tmp_path)
    hourly = sorted(k for k in store if k.startswith("hourly:"))
    assert len(hourly) == 3  # retention monkeypatched to 3
    assert hourly[-1] == "hourly:20260906-050000"
    assert "daily:20260901-030000" in store
