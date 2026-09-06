"""The restore-drill runner (auto-mu7qf).

The real checks live in backup-restore.sh (exercised on real snapshots
by the host drill); here a controllable stand-in script proves the
RUNNER's contract: event parsing, row recording through the real
schema validation, single-flight, group-killing timeouts, and the
restore_drill_failed raise/clear through the real registry composition.
"""
from __future__ import annotations

import textwrap
from types import SimpleNamespace

import pytest

from tools.dashboard.attention_index_service import (
    AttentionIndexService,
    InMemoryAttentionIndexStore,
)
from tools.dashboard.attention_registry import (
    build_production_attention_registry,
)
from tools.dashboard.plugins.backup import drill as D
from tools.dashboard.plugins.backup.attention import publication_runtimes
from tools.graph.schemas.registry import validate_payload


PASS_SCRIPT = """\
#!/usr/bin/env bash
echo "@snapshot 56f74548"
echo "drill: restoring..."
echo "@check restore ok latest kind=db snapshot restored"
echo "@check marker ok stores=32 beads_databases=3"
echo "@check integrity ok 22 databases passed integrity_check"
echo "@check sources-sanity ok orgs/autonomy.db sources=10835"
echo "@check beads-count ok 3/3 dumps"
echo "@verdict pass"
"""

FAIL_SCRIPT = """\
#!/usr/bin/env bash
echo "@snapshot 56f74548"
echo "@check restore ok latest kind=db snapshot restored"
echo "@check integrity fail personal.db FAIL: file is not a database"
echo "@verdict fail"
exit 1
"""

HANG_SCRIPT = """\
#!/usr/bin/env bash
echo "@snapshot 56f74548"
echo "@check restore ok latest kind=db snapshot restored"
sleep 3600
"""


@pytest.fixture
def store(monkeypatch):
    """In-memory drill rows with the REAL schema validation, plus a
    captured attention index built from the real production registry."""
    rows: dict[str, dict] = {}

    def write_by_key(set_id, rev, key, payload, *, org, **kw):
        validate_payload(set_id, rev, payload)
        assert org == "machine"
        rows[key] = payload

    def read_set(set_id, *, org, peers=None, **kw):
        return [SimpleNamespace(key=key, id=key, payload=payload)
                for key, payload in rows.items()]

    import tools.graph.settings_ops as settings_ops
    monkeypatch.setattr(settings_ops, "write_by_key", write_by_key)
    monkeypatch.setattr(settings_ops, "read_set", read_set)
    monkeypatch.setattr(settings_ops, "remove_setting",
                        lambda sid, *, org: rows.pop(sid, None))
    monkeypatch.setattr(D, "_drill_retention", lambda: 25)

    index = AttentionIndexService(
        registry=build_production_attention_registry(
            runtimes=publication_runtimes()),
        store=InMemoryAttentionIndexStore())
    from tools.dashboard import attention_routes
    monkeypatch.setattr(attention_routes, "_runtime",
                        SimpleNamespace(index=index))
    import tools.dashboard.plugins.backup.entrypoints.api as api
    monkeypatch.setattr(api, "_read_config",
                        lambda: {"drill_timeout_minutes": 30,
                                 "drill_retention": 25})
    return SimpleNamespace(rows=rows, index=index)


def _script(tmp_path, body: str):
    path = tmp_path / "fake-drill.sh"
    path.write_text(textwrap.dedent(body))
    path.chmod(0o755)
    return path


def _open_ids(index):
    return {item.attention_id for item in index.store.list_items()
            if item.payload.get("attention_state") == "needs_attention"}


def test_event_parsing():
    checks, snap, verdict = D.parse_events(
        "@snapshot abc123\nnoise\n@check integrity ok 22 databases\n"
        "@check beads-count fail 2 dumps, marker says 3\n@verdict fail\n")
    assert snap == "abc123"
    assert verdict == "fail"
    assert checks == [
        {"name": "integrity", "status": "ok", "detail": "22 databases"},
        {"name": "beads-count", "status": "fail",
         "detail": "2 dumps, marker says 3"},
    ]


def test_passing_drill_records_and_resolves(store, tmp_path):
    # Seed an open failure so the pass has something to clear.
    D._publish_outcome("fail", "20260906-000000",
                       [{"name": "integrity", "status": "fail"}])
    assert "backup:drill" in _open_ids(store.index)

    result = D.run_drill("manual", script=_script(tmp_path, PASS_SCRIPT))
    assert result["verdict"] == "pass"
    assert result["snapshot_id"] == "56f74548"
    assert len(result["checks"]) == 5
    row = store.rows[result["stamp"]]
    assert row["verdict"] == "pass"
    assert row["trigger"] == "manual"
    assert "backup:drill" not in _open_ids(store.index)


def test_failing_drill_records_reason_and_raises(store, tmp_path):
    result = D.run_drill("scheduled", script=_script(tmp_path, FAIL_SCRIPT))
    assert result["verdict"] == "fail"
    integrity = [c for c in result["checks"] if c["name"] == "integrity"][0]
    assert integrity["status"] == "fail"
    assert "file is not a database" in integrity["detail"]
    assert "backup:drill" in _open_ids(store.index)
    item = store.index.store.get_item("backup:drill")
    assert "integrity" in item.payload["safe_summary"]


def test_timeout_kills_the_group_and_records(store, tmp_path):
    result = D.run_drill("manual", script=_script(tmp_path, HANG_SCRIPT),
                         timeout_s=1.0)
    assert result["verdict"] == "timeout"
    runtime = [c for c in result["checks"] if c["name"] == "runtime"][0]
    assert "killed after" in runtime["detail"]
    assert "backup:drill" in _open_ids(store.index)


def test_single_flight(store, tmp_path):
    import threading
    import time

    script = _script(tmp_path, "#!/usr/bin/env bash\nsleep 0.5\n"
                               "echo '@check restore ok x'\n"
                               "echo '@verdict pass'\n")
    results = {}

    def first():
        results["first"] = D.run_drill("manual", script=script)

    thread = threading.Thread(target=first)
    thread.start()
    time.sleep(0.15)
    with pytest.raises(D.DrillAlreadyRunning):
        D.run_drill("manual", script=script)
    thread.join()
    assert results["first"]["verdict"] == "pass"
    assert D.running_stamp() is None


def test_abandoned_running_row_finalizes(store, monkeypatch):
    """A hot-reload mid-drill must not leave an eternal 'running' row
    pinning the run button (found live 2026-09-06)."""
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    store.rows["20260906-055554"] = {
        "verdict": "running", "trigger": "scheduled", "started_at": old}
    finalized = D.finalize_abandoned()
    assert finalized == ["20260906-055554"]
    row = store.rows["20260906-055554"]
    assert row["verdict"] == "fail"
    assert "abandoned" in row["checks"][0]["detail"]
    # A FRESH running row (this process could still own it) is left alone.
    recent = datetime.now(timezone.utc).isoformat()
    store.rows["20260906-090000"] = {
        "verdict": "running", "trigger": "manual", "started_at": recent}
    assert D.finalize_abandoned() == []


def test_exit_zero_without_pass_verdict_is_a_fail(store, tmp_path):
    # A script that dies before @verdict must not read as success.
    script = _script(tmp_path,
                     "#!/usr/bin/env bash\necho '@check restore ok x'\n")
    result = D.run_drill("manual", script=script)
    assert result["verdict"] == "fail"
