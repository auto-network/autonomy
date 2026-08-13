from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[4]
DEPLOY = REPO_ROOT / "tools/network/registry/deploy"


def test_backup_job_creates_verified_snapshot_and_success_marker(
    tmp_path: Path,
) -> None:
    database = tmp_path / "registry.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE links (token TEXT PRIMARY KEY)")
    connection.execute("INSERT INTO links VALUES ('bearer-must-not-be-logged')")
    connection.commit()
    connection.close()

    credentials = tmp_path / "credentials"
    credentials.mkdir()
    for name, value in {
        "repository": "s3:https://example.invalid/bucket",
        "restic-password": "password",
        "access-key-id": "key-id",
        "secret-access-key": "secret-key",
    }.items():
        (credentials / name).write_text(value)

    restic = tmp_path / "restic"
    restic.write_text(
        "#!/bin/sh\n"
        "test \"$1\" = backup || exit 9\n"
        "printf '%s\\n' '{\"message_type\":\"summary\",\"snapshot_id\":\"abc123\"}'\n"
    )
    restic.chmod(0o755)

    result = subprocess.run(
        [str(DEPLOY / "backup-registry.sh")],
        check=True,
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

    assert "snapshot=abc123" in result.stdout
    assert "bearer-must-not-be-logged" not in result.stdout + result.stderr
    assert (tmp_path / "status/last-success").read_text().endswith(" abc123\n")


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
