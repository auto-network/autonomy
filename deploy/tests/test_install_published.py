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


def _lock(tmp_path: Path, node_digest: str = GOOD, node_ref: str | None = None) -> Path:
    node = node_ref or f"ghcr.io/example/autonomy-node@sha256:{node_digest}"
    lock = tmp_path / "image-lock.env"
    lock.write_text(
        "AUTONOMY_IMAGE_LOCK_VERSION=1\n"
        "AUTONOMY_RELEASE_TAG=test\n"
        f"AUTONOMY_NODE_IMAGE={node}\n"
        f"AUTONOMY_SESSION_IMAGE=ghcr.io/example/autonomy-session@sha256:{GOOD}\n"
        f"AUTONOMY_SESSION_PLATFORM_IMAGE=ghcr.io/example/autonomy-session-platform@sha256:{GOOD}\n"
        f"AUTONOMY_SESSION_DIND_IMAGE=ghcr.io/example/autonomy-session-dind@sha256:{GOOD}\n"
        f"AUTONOMY_HOST_TERMINAL_IMAGE=ghcr.io/example/autonomy-host-terminal@sha256:{GOOD}\n",
        encoding="utf-8",
    )
    return lock


def _run(tmp_path: Path, lock: Path) -> tuple[subprocess.CompletedProcess, str]:
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    _exe(fake / "docker", '#!/usr/bin/env bash\necho "docker $*" >>"$T_LOG"\n')
    _exe(
        fake / "cosign",
        f'#!/usr/bin/env bash\necho "cosign $*" >>"$T_LOG"\n'
        f'[[ "$*" == *@sha256:{BAD}* ]] && exit 1\nexit 0\n',
    )
    env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", T_LOG=str(log),
               AUTONOMY_COSIGN_BIN=str(fake / "cosign"), HOME=str(tmp_path),
               AUTONOMY_READY_TIMEOUT="1")
    result = subprocess.run(
        ["bash", str(INSTALL), "--lock", str(lock), "--dir", str(tmp_path / "node"), "--yes"],
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
    assert len(verifies) == 5
    assert all("--key" in line for line in verifies)
    lines = calls.splitlines()
    last_verify = max(i for i, line in enumerate(lines) if line.startswith("cosign verify"))
    first_pull = min(i for i, line in enumerate(lines) if line.startswith("docker pull"))
    assert last_verify < first_pull
    assert any(line.startswith("docker compose up -d --no-build") for line in lines)
    # The launcher starts the host terminal from this local name.
    assert any(line.startswith("docker tag ") and line.endswith(" autonomy-host-terminal")
               for line in lines)
