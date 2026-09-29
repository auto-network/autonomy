"""The restore-drill runner (auto-mu7qf).

The real checks live in backup-restore.sh (exercised on real snapshots
by the host drill); here a controllable stand-in script proves the
RUNNER's contract: event parsing, row recording through the real
schema validation, single-flight, group-killing timeouts, and that a
drill in flight is known only to the process (nothing stored until it
ends).
"""
from __future__ import annotations

import textwrap
from types import SimpleNamespace

import pytest

from tools.dashboard.plugins.backup import drill as D
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
    """In-memory drill rows with the REAL schema validation."""
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

    import tools.dashboard.plugins.backup.entrypoints.api as api
    monkeypatch.setattr(api, "_read_config",
                        lambda: {"drill_timeout_minutes": 30,
                                 "drill_retention": 25})
    return SimpleNamespace(rows=rows)


def _script(tmp_path, body: str):
    path = tmp_path / "fake-drill.sh"
    path.write_text(textwrap.dedent(body))
    path.chmod(0o755)
    return path


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


def test_passing_drill_records(store, tmp_path):
    result = D.run_drill("manual", script=_script(tmp_path, PASS_SCRIPT))
    assert result["verdict"] == "pass"
    assert result["snapshot_id"] == "56f74548"
    assert len(result["checks"]) == 5
    row = store.rows[result["stamp"]]
    assert row["verdict"] == "pass"
    assert row["trigger"] == "manual"


def test_failing_drill_records_reason(store, tmp_path):
    result = D.run_drill("manual", script=_script(tmp_path, FAIL_SCRIPT))
    assert result["verdict"] == "fail"
    integrity = [c for c in result["checks"] if c["name"] == "integrity"][0]
    assert integrity["status"] == "fail"
    assert "file is not a database" in integrity["detail"]
    assert store.rows[result["stamp"]]["verdict"] == "fail"


def test_timeout_kills_the_group_and_records(store, tmp_path):
    result = D.run_drill("manual", script=_script(tmp_path, HANG_SCRIPT),
                         timeout_s=1.0)
    assert result["verdict"] == "timeout"
    runtime = [c for c in result["checks"] if c["name"] == "runtime"][0]
    assert "killed after" in runtime["detail"]


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
    # In flight: known to the process, nothing stored yet.
    assert D.running()["trigger"] == "manual"
    assert store.rows == {}
    thread.join()
    assert results["first"]["verdict"] == "pass"
    assert D.running() is None
    assert list(store.rows) == [results["first"]["stamp"]]


def test_exit_zero_without_pass_verdict_is_a_fail(store, tmp_path):
    # A script that dies before @verdict must not read as success.
    script = _script(tmp_path,
                     "#!/usr/bin/env bash\necho '@check restore ok x'\n")
    result = D.run_drill("manual", script=script)
    assert result["verdict"] == "fail"


def test_drill_script_is_the_checked_in_restore_script():
    """A drill runs DRILL_SCRIPT by default. A wrong parent count sent it
    to tools/tools/graph/backup-restore.sh, so every drill failed with
    "No such file or directory" before its first check."""
    assert D.DRILL_SCRIPT.is_file(), D.DRILL_SCRIPT
    assert D.DRILL_SCRIPT.parts[-3:] == ("tools", "graph", "backup-restore.sh")
    assert "tools/tools" not in str(D.DRILL_SCRIPT)
