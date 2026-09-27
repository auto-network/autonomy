"""Published-image installer: embedded trust root, refusal before any pull."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "deploy" / "install-published.sh"
PUBLIC_KEY = ROOT / "deploy" / "cosign.pub"
GOOD = "a" * 64
BAD = "0" * 64


def _exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _lock(tmp_path: Path, node_digest: str = GOOD, node_ref: str | None = None,
          host_terminal: bool = True, service_gateway: bool = True) -> Path:
    node = node_ref or f"ghcr.io/example/autonomy-node@sha256:{node_digest}"
    lock = tmp_path / "image-lock.env"
    lock.write_text(
        "AUTONOMY_IMAGE_LOCK_VERSION=1\n"
        "AUTONOMY_RELEASE_TAG=test\n"
        f"AUTONOMY_NODE_IMAGE={node}\n"
        f"AUTONOMY_SESSION_IMAGE=ghcr.io/example/autonomy-session@sha256:{GOOD}\n"
        f"AUTONOMY_SESSION_PLATFORM_IMAGE=ghcr.io/example/autonomy-session-platform@sha256:{GOOD}\n"
        f"AUTONOMY_SESSION_DIND_IMAGE=ghcr.io/example/autonomy-session-dind@sha256:{GOOD}\n"
        + (f"AUTONOMY_HOST_TERMINAL_IMAGE=ghcr.io/example/autonomy-host-terminal@sha256:{GOOD}\n"
           if host_terminal else "")
        + (f"AUTONOMY_SERVICE_GATEWAY_IMAGE=ghcr.io/example/autonomy-service-gateway@sha256:{GOOD}\n"
           if service_gateway else ""),
        encoding="utf-8",
    )
    return lock


def _run(tmp_path: Path, lock: Path, *extra: str,
         env_extra: dict | None = None) -> tuple[subprocess.CompletedProcess, str]:
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    # The stand-in daemon answers the engine API floor check (auto-8pohz) and
    # records every other call.
    _exe(fake / "docker", '#!/usr/bin/env bash\necho "docker $*" >>"$T_LOG"\n'
         '[[ "$1" == version ]] && echo 1.47\nexit 0\n')
    _exe(
        fake / "cosign",
        f'#!/usr/bin/env bash\necho "cosign $*" >>"$T_LOG"\n'
        f'[[ "$*" == *@sha256:{BAD}* ]] && exit 1\nexit 0\n',
    )
    env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", T_LOG=str(log),
               AUTONOMY_COSIGN_BIN=str(fake / "cosign"), HOME=str(tmp_path),
               AUTONOMY_READY_TIMEOUT="1")
    env.update(env_extra or {})
    result = subprocess.run(
        ["bash", str(INSTALL), "--lock", str(lock), "--dir", str(tmp_path / "node"), "--yes",
         *extra],
        capture_output=True, text=True, env=env, timeout=60,
    )
    return result, log.read_text(encoding="utf-8") if log.exists() else ""


def test_embedded_key_is_the_committed_project_key():
    body = INSTALL.read_text(encoding="utf-8")
    embedded = re.search(r"PROJECT_PUBLIC_KEY='(.*?)'", body, re.S).group(1)
    assert embedded.strip() == PUBLIC_KEY.read_text(encoding="utf-8").strip()


def test_unsigned_image_is_refused_before_any_pull(tmp_path):
    result, calls = _run(tmp_path, _lock(tmp_path, node_digest=BAD))
    assert result.returncode == 4, result.stderr
    assert "SIGNATURE CHECK FAILED" in result.stderr
    assert "docker pull" not in calls
    assert "compose up" not in calls


def test_floating_tag_in_lock_is_refused(tmp_path):
    result, calls = _run(tmp_path, _lock(tmp_path, node_ref="ghcr.io/example/autonomy-node:latest"))
    assert result.returncode == 2
    assert "refusing non-digest lock entry" in result.stderr
    assert "cosign verify" not in calls


def test_every_image_is_verified_with_the_embedded_key(tmp_path):
    result, calls = _run(tmp_path, _lock(tmp_path))
    assert result.returncode == 5  # fakes never answer /api/ping
    verifies = [line for line in calls.splitlines() if line.startswith("cosign verify")]
    assert len(verifies) == 6
    assert all("--key" in line for line in verifies)
    lines = calls.splitlines()
    last_verify = max(i for i, line in enumerate(lines) if line.startswith("cosign verify"))
    first_pull = min(i for i, line in enumerate(lines) if line.startswith("docker pull"))
    assert last_verify < first_pull
    assert any(line.startswith("docker compose up -d --no-build") for line in lines)
    # The launcher starts the host terminal from this local name.
    assert any(line.startswith("docker tag ") and line.endswith(" autonomy-host-terminal")
               for line in lines)


def test_a_release_lock_without_the_host_terminal_image_still_installs(tmp_path):
    """deploy/releases/2026.09.26-0d46057.env pins four images; its node
    predates the in-node host terminal and needs no host-terminal image."""
    result, calls = _run(tmp_path, _lock(tmp_path, host_terminal=False, service_gateway=False))
    assert result.returncode == 5, result.stderr  # fakes never answer /api/ping
    verifies = [line for line in calls.splitlines() if line.startswith("cosign verify")]
    assert len(verifies) == 4
    assert not any(line.endswith(" autonomy-host-terminal") for line in calls.splitlines())
    assert "AUTONOMY_SERVICE_GATEWAY_IMAGE" not in _env_file(tmp_path)


def test_the_service_gateway_image_is_verified_pulled_and_recorded_for_the_dashboard(tmp_path):
    """The dashboard starts the gateway from its own Compose run, where .env
    is not read: the pinned reference is recorded in .env, which
    docker-compose.yml passes into the dashboard's environment (Windows run 5:
    "No such image: autonomy-service-gateway:local")."""
    result, calls = _run(tmp_path, _lock(tmp_path))
    assert result.returncode == 5, result.stderr
    ref = f"ghcr.io/example/autonomy-service-gateway@sha256:{GOOD}"
    lines = calls.splitlines()
    assert f"cosign verify --insecure-ignore-tlog --key {tmp_path / 'node' / 'tools' / 'cosign.pub'} {ref}" in lines \
        or any(line.startswith("cosign verify") and line.endswith(ref) for line in lines)
    assert any(line.startswith("docker pull") and line.endswith(ref) for line in lines)
    assert _env_file(tmp_path)["AUTONOMY_SERVICE_GATEWAY_IMAGE"] == ref


def _env_file(tmp_path: Path) -> dict[str, str]:
    lines = (tmp_path / "node" / ".env").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def test_host_home_defaults_to_the_invoking_user(tmp_path):
    result, _ = _run(tmp_path, _lock(tmp_path))
    assert result.returncode == 5, result.stderr
    assert _env_file(tmp_path)["AUTONOMY_HOST_HOME"] == str(tmp_path)


def test_host_home_under_sudo_is_the_sudo_users_home_not_root(tmp_path):
    """Windows walkthrough, 2026-09-26: run as root, the node was pointed at
    /root, which holds none of the operator's sign-ins and which the
    dashboard (uid 1000) cannot read. Under sudo the invoking user's home is
    used. ``id`` and ``getent`` are faked so the test needs no root."""
    alice = tmp_path / "home-alice"
    alice.mkdir()
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    _exe(fake / "id", '#!/usr/bin/env bash\n[[ "$1" == -u ]] && { echo 0; exit 0; }\n'
                      'exec /usr/bin/id "$@"\n')
    _exe(fake / "getent", f'#!/usr/bin/env bash\necho "alice:x:1000:1000::{alice}:/bin/bash"\n')
    result, _ = _run(tmp_path, _lock(tmp_path), env_extra={"SUDO_USER": "alice"})
    assert result.returncode == 5, result.stderr
    assert _env_file(tmp_path)["AUTONOMY_HOST_HOME"] == str(alice)


def test_host_home_option_wins(tmp_path):
    chosen = tmp_path / "elsewhere"
    chosen.mkdir()
    result, _ = _run(tmp_path, _lock(tmp_path), "--host-home", str(chosen))
    assert result.returncode == 5, result.stderr
    assert _env_file(tmp_path)["AUTONOMY_HOST_HOME"] == str(chosen)


def test_missing_host_home_is_refused_before_any_pull(tmp_path):
    result, calls = _run(tmp_path, _lock(tmp_path), "--host-home", str(tmp_path / "nope"))
    assert result.returncode == 2
    assert "does not exist" in result.stderr
    assert "docker pull" not in calls
