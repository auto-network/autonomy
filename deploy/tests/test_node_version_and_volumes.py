"""Contract tests for auto-m7vh7: the three daemon volume names are pinned, and
the version stamp is real (never empty/unknown) on every supported build path."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "docker-compose.yml"
BUILD_SH = ROOT / "deploy" / "build.sh"

FORBIDDEN_ALIASES = ("autonomy-state", "autonomy-mounts", "autonomy-workspace-data")
EXPECTED_MOUNTS = {
    "autonomy-code": "/app",
    "autonomy-data": "/app/data",
    "autonomy-orgs": "/app/orgs",
}


def _compose():
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def test_dashboard_mounts_the_three_volumes_at_the_locked_destinations():
    svc = _compose()["services"]["dashboard"]
    mounts = {}
    for entry in svc["volumes"]:
        src, dst = entry.split(":")[0], entry.split(":")[1]
        mounts[src] = dst
    for vol, dest in EXPECTED_MOUNTS.items():
        assert mounts.get(vol) == dest, f"{vol} must mount at {dest}, got {mounts.get(vol)}"


def test_daemon_volume_names_are_pinned_not_project_scoped():
    """Explicit `name:` so `docker volume inspect autonomy-orgs` (the resolver's
    translation primitive) resolves, instead of `<project>_autonomy-orgs`."""
    vols = _compose()["volumes"]
    for vol in EXPECTED_MOUNTS:
        assert vol in vols, f"{vol} missing from top-level volumes"
        assert (vols[vol] or {}).get("name") == vol, (
            f"{vol} must set an explicit `name: {vol}` to pin the daemon name"
        )


def test_no_forbidden_volume_aliases_anywhere_in_compose():
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    for alias in FORBIDDEN_ALIASES:
        assert alias not in text, f"stale volume alias {alias!r} present in compose"


def test_compose_never_defaults_the_version_to_empty_or_unknown():
    """The bare `docker compose up` path must stamp an honest marker, not
    unknown/empty which would carry no provenance into the code volume."""
    args = _compose()["services"]["dashboard"]["build"]["args"]
    ver = str(args["AUTONOMY_VERSION"])
    assert "unknown" not in ver
    assert ":-source}" in ver, "bare-compose default must be the 'source' marker"


def test_build_wrapper_stamps_a_real_commit_and_time(tmp_path):
    """deploy/build.sh derives a real SHA + UTC time from the checkout and hands
    them to docker compose — the supported real-commit source build."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    stub = fake_bin / "docker"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'printf "VERSION=%s\\nTIME=%s\\nARGV=%s\\n" '
        '"$AUTONOMY_VERSION" "$AUTONOMY_BUILD_TIME" "$*" >> "$DOCKER_LOG"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["DOCKER_LOG"] = str(log)
    env.pop("AUTONOMY_VERSION", None)
    env.pop("AUTONOMY_BUILD_TIME", None)

    subprocess.run(["bash", str(BUILD_SH), "build"], env=env, check=True)

    out = log.read_text(encoding="utf-8")
    ver = re.search(r"VERSION=([0-9a-f]{40})\b", out)
    tim = re.search(r"TIME=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", out)
    assert ver, f"build.sh must pass a real 40-hex commit SHA; got:\n{out}"
    assert tim, f"build.sh must pass a UTC build time; got:\n{out}"
    assert "compose" in out and "build" in out, "build.sh must invoke docker compose"


@pytest.mark.parametrize(
    "bad_env",
    [
        {"AUTONOMY_VERSION": "not-a-sha"},
        {"AUTONOMY_VERSION": "source"},      # build.sh is the real-SHA path; source is invalid here
        {"AUTONOMY_VERSION": "DEADBEEF" * 5},  # 40 chars but uppercase — not a git SHA
        {"AUTONOMY_BUILD_TIME": "not-a-time"},
    ],
)
def test_build_wrapper_rejects_invalid_overrides(tmp_path, bad_env):
    """A bad version/time override must fail closed BEFORE docker is invoked, so
    garbage can never reach the image."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    called = tmp_path / "docker-was-called"
    stub = fake_bin / "docker"
    stub.write_text(f'#!/usr/bin/env bash\ntouch {called}\n', encoding="utf-8")
    stub.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env.pop("AUTONOMY_VERSION", None)
    env.pop("AUTONOMY_BUILD_TIME", None)
    env.update(bad_env)

    result = subprocess.run(
        ["bash", str(BUILD_SH), "build"], env=env, capture_output=True, text=True
    )
    assert result.returncode != 0, f"build.sh must reject {bad_env}"
    assert not called.exists(), "docker must not run when the version/time is invalid"
