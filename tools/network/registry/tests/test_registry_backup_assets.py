from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[4]
DEPLOY = REPO_ROOT / "tools/network/registry/deploy"


def _database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE links (token TEXT PRIMARY KEY)")
    connection.execute("INSERT INTO links VALUES ('bearer-must-not-be-logged')")
    connection.commit()
    connection.close()


def _credentials(path: Path) -> Path:
    path.mkdir()
    for name, value in {
        "repository": "s3:https://example.invalid/bucket",
        "restic-password": "password",
        "access-key-id": "key-id",
        "secret-access-key": "secret-key",
    }.items():
        (path / name).write_text(value)
    return path


def _run_backup(
    tmp_path: Path,
    database: Path,
    credentials: Path,
    restic: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(DEPLOY / "backup-registry.sh")],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "APP_DIR": str(REPO_ROOT),
            "REGISTRY_DB_PATH": str(database),
            "REGISTRY_BACKUP_STATUS_DIR": str(tmp_path / "status"),
            "CREDENTIALS_DIRECTORY": str(credentials),
            "PYTHON": sys.executable,
            "RESTIC": str(restic),
        },
    )


def test_backup_job_creates_verified_snapshot_and_success_marker(
    tmp_path: Path,
) -> None:
    database = tmp_path / "registry.db"
    _database(database)
    credentials = _credentials(tmp_path / "credentials")

    restic = tmp_path / "restic"
    restic.write_text(
        "#!/bin/sh\n"
        "test \"$1\" = backup || exit 9\n"
        "printf '%s\\n' '{\"message_type\":\"summary\",\"snapshot_id\":\"abc123\"}'\n"
    )
    restic.chmod(0o755)

    result = _run_backup(tmp_path, database, credentials, restic)

    assert result.returncode == 0
    assert "snapshot=abc123" in result.stdout
    assert "bearer-must-not-be-logged" not in result.stdout + result.stderr
    assert (tmp_path / "status/last-success").read_text().endswith(" abc123\n")


def test_partial_upload_cannot_replace_last_success(tmp_path: Path) -> None:
    database = tmp_path / "registry.db"
    _database(database)
    credentials = _credentials(tmp_path / "credentials")
    status = tmp_path / "status"
    status.mkdir()
    marker = status / "last-success"
    marker.write_text("2026-08-13T00:00:00Z known-good\n")

    restic = tmp_path / "restic"
    restic.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' '{\"message_type\":\"summary\",\"snapshot_id\":\"partial\"}'\n"
        "exit 7\n"
    )
    restic.chmod(0o755)

    result = _run_backup(tmp_path, database, credentials, restic)

    assert result.returncode == 7
    assert marker.read_text() == "2026-08-13T00:00:00Z known-good\n"
    assert "secret-key" not in result.stdout + result.stderr


def test_empty_restic_success_cannot_replace_last_success(tmp_path: Path) -> None:
    database = tmp_path / "registry.db"
    _database(database)
    credentials = _credentials(tmp_path / "credentials")
    status = tmp_path / "status"
    status.mkdir()
    marker = status / "last-success"
    marker.write_text("2026-08-13T00:00:00Z known-good\n")

    restic = tmp_path / "restic"
    restic.write_text("#!/bin/sh\nexit 0\n")
    restic.chmod(0o755)

    result = _run_backup(tmp_path, database, credentials, restic)

    assert result.returncode != 0
    assert "without a snapshot id" in result.stderr
    assert marker.read_text() == "2026-08-13T00:00:00Z known-good\n"


def test_backup_unit_has_hourly_credentials_and_no_destructive_retention() -> None:
    service = (DEPLOY / "autonomy-registry-backup.service").read_text()
    timer = (DEPLOY / "autonomy-registry-backup.timer").read_text()
    script = (DEPLOY / "backup-registry.sh").read_text()

    assert service.count("LoadCredential=") == 4
    assert "ReadOnlyPaths=/var/lib/private/autonomy-registry" in service
    assert "OnCalendar=hourly" in timer
    assert "Persistent=true" in timer
    assert " forget " not in script
    assert " prune " not in script
    restore = (DEPLOY / "restore-registry.sh").read_text()
    assert "scratch target must be under /tmp or /var/tmp" in restore
