"""Offsite backup credentials come only from what the dashboard released
from the vault into the host's ramfs key cache (auto-5gdao): backup-env.sh
reads those files and nothing else -- no agents/backup.env, no
agents/.restic.pw, no generated password -- and a cold vault is a skip with
its reason in the run report."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
ENV_SH = REPO / "tools/graph/backup-env.sh"
OFFSITE_SH = REPO / "tools/graph/backup-offsite.sh"
STORES = REPO / "tools/graph/backup_stores.py"


def _released(tmp_path, bucket="autonomy-offsite", provider="b2"):
    d = tmp_path / "keycache" / "backup"
    d.mkdir(parents=True)
    (d / "offsite.env").write_text(f"BACKUP_PROVIDER={provider}\nBACKUP_BUCKET={bucket}\n")
    (d / "restic-password").write_text("pw")
    (d / "b2-key-id").write_text("kid")
    (d / "b2-application-key").write_text("akey")
    return tmp_path / "keycache"


def _source(keycache, script="", cwd=None):
    return subprocess.run(
        ["bash", "-c", f'source "{ENV_SH}" && {script or "env"}'],
        capture_output=True, text=True, timeout=30, cwd=cwd,
        env={"PATH": "/usr/bin:/bin", "AUTONOMY_KEYCACHE_MOUNT": str(keycache)})


def test_released_files_are_the_only_source(tmp_path):
    keycache = _released(tmp_path)
    r = _source(keycache)
    assert r.returncode == 0, r.stderr
    env = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    assert env["RESTIC_PASSWORD_FILE"] == str(keycache / "backup" / "restic-password")
    assert "RESTIC_PASSWORD" not in env
    assert env["RESTIC_REPOSITORY"] == "rclone:b2:autonomy-offsite/restic"
    assert env["RCLONE_CONFIG_B2_ACCOUNT"] == "kid" and env["RCLONE_CONFIG_B2_KEY"] == "akey"
    assert "deprecated" not in r.stderr


def test_the_plaintext_files_are_never_read_even_when_present():
    src = ENV_SH.read_text()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "backup.env" not in code and ".restic.pw" not in code
    assert "openssl rand" not in code


def test_no_release_is_vault_cold_and_never_generates_a_password(tmp_path):
    keycache = tmp_path / "keycache"
    r = _source(keycache, cwd=tmp_path)
    assert r.returncode != 0
    assert "vault-cold" in r.stderr
    assert not any(p.name.endswith(".pw") for p in tmp_path.rglob("*"))


def test_an_operator_set_value_is_read_never_executed(tmp_path):
    marker = tmp_path / "pwned"
    keycache = _released(tmp_path, bucket=f"$(touch {marker})")
    _source(keycache, script="true")
    assert not marker.exists()


@pytest.mark.parametrize("provider", ["r2", "s3"])
def test_providers_without_vault_rows_are_refused_by_name(tmp_path, provider):
    r = _source(_released(tmp_path, provider=provider), script="true")
    assert r.returncode != 0 and "no vault-released credentials" in r.stderr


def test_offsite_skips_with_its_reason_when_nothing_was_released(tmp_path):
    r = subprocess.run(["bash", str(OFFSITE_SH), "hourly"], capture_output=True,
                       text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin",
                            "AUTONOMY_KEYCACHE_MOUNT": str(tmp_path / "none"),
                            "AUTONOMY_DATA_ROOT": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    assert "skipping (reason=vault-cold)" in r.stdout


def test_the_run_report_records_the_skip_reason(tmp_path):
    report = tmp_path / "run-report.json"
    report.write_text(json.dumps({"offsite": "pending", "exit_code": 0}))
    subprocess.run([sys.executable, str(STORES), "report-offsite", "skipped", "0",
                    "--skip-reason=vault-cold", str(report)], check=True, timeout=30)
    assert json.loads(report.read_text())["skip_reason"] == "vault-cold"
    subprocess.run([sys.executable, str(STORES), "report-offsite", "complete", "0",
                    "--repo-bytes=5", str(report)], check=True, timeout=30)
    data = json.loads(report.read_text())
    assert "skip_reason" not in data and data["offsite_repo_bytes"] == 5
