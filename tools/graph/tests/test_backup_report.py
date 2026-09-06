"""backup-all.sh emits run-report.json matching its own accounting.

Runs the real script against a synthetic manifest-complete data root
(auto-yj2wa). The report is what the dashboard reconciler ingests, so
its verdicts, per-store rows, and offsite stamping must be true on both
the success and failure paths — a failed run must report too.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "tools" / "graph" / "backup-all.sh"


@pytest.fixture
def synthetic(tmp_path):
    from tools.data_paths import STORE_MANIFEST
    root = tmp_path / "data"
    for store in STORE_MANIFEST:
        target = root / store.relative
        if store.kind == "db":
            target.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(target)
            conn.execute("CREATE TABLE t(x)")
            conn.commit()
            conn.close()
        elif store.kind == "file":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("key material\n")
            target.chmod(0o600)
        else:
            target.mkdir(parents=True, exist_ok=True)
            (target / "payload.txt").write_text("x")
    conn = sqlite3.connect(root / "orgs" / "autonomy.db")
    conn.execute("CREATE TABLE sources(id)")
    conn.commit()
    conn.close()
    beads = root / ".beads"
    beads.mkdir()
    (beads / "metadata.json").write_text(json.dumps({"dolt_database": "auto"}))
    backups = tmp_path / "backups"
    backups.mkdir()
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    dump = fakebin / "mysqldump"
    dump.write_text("#!/usr/bin/env bash\necho '-- dump'\n")
    dump.chmod(dump.stat().st_mode | stat.S_IEXEC)
    return root, backups, fakebin


def _run(root, backups, fakebin):
    env = {**os.environ,
           "AUTONOMY_DATA_ROOT": str(root),
           "AUTONOMY_BACKUP_ROOT": str(backups),
           "PATH": f"{fakebin}:{os.environ['PATH']}"}
    env.pop("BEADS_DIR", None)
    return subprocess.run(["bash", str(SCRIPT), "hourly"],
                          capture_output=True, text=True, env=env,
                          timeout=300)


def test_success_report_matches_accounting(synthetic):
    root, backups, fakebin = synthetic
    proc = _run(root, backups, fakebin)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads((backups / "hourly" / "latest-report.json").read_text())
    assert report["verdict"] == "complete"
    assert report["failures"] == []
    # Offsite is unconfigured in the fixture: stamped skipped, never
    # silently "complete".
    assert report["offsite"] == "skipped"
    assert report["exit_code"] == 0
    ok_rows = [row for row in report["stores"] if row["status"] == "ok"]
    dump_rows = [row for row in report["stores"] if row["action"] == "dump"]
    assert report["store_count"] == len(ok_rows) - len(dump_rows)
    assert report["beads_databases"] == len(dump_rows) == 1
    assert report["total_bytes"] == sum(r["bytes"] for r in report["stores"])
    # The report also travels inside the capture dir, beside the marker.
    capture_dirs = [d for d in (backups / "hourly").iterdir() if d.is_dir()]
    assert len(capture_dirs) == 1
    assert json.loads(
        (capture_dirs[0] / "run-report.json").read_text()) == report
    assert (capture_dirs[0] / ".backup-complete").exists()


def test_failed_run_reports_with_reasons(synthetic):
    root, backups, fakebin = synthetic
    (root / "auth.db").unlink()
    proc = _run(root, backups, fakebin)
    assert proc.returncode == 1
    report = json.loads((backups / "hourly" / "latest-report.json").read_text())
    assert report["verdict"] == "failed"
    assert report["exit_code"] == 1
    assert any("auth" in reason for reason in report["failures"])
    auth = [row for row in report["stores"] if row["name"] == "auth"][0]
    assert auth["status"] == "missing"
    # The dir is renamed *-FAILED and carries its report.
    failed_dirs = list((backups / "hourly").glob("*-FAILED"))
    assert len(failed_dirs) == 1
    assert (failed_dirs[0] / "run-report.json").exists()
    assert not (failed_dirs[0] / ".backup-complete").exists()
