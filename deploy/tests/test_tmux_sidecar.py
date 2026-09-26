"""The `tmux` sidecar owns every session pane (auto-y5mam, graph://89d3c8df-544 §1)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"
ENTRYPOINT = REPO_ROOT / "deploy/entrypoint.sh"
TMUX_DIR = "/run/autonomy-tmux"


def _compose_config() -> dict:
    if not shutil.which("docker"):
        pytest.skip("docker compose CLI not available")
    env = dict(os.environ)
    env.setdefault("AUTONOMY_HOST_HOME", str(Path.home()))
    env.setdefault("AUTONOMY_SUBNET", "172.16.0.0/24")
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config", "--format", "json"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _mounts(service: dict) -> dict[str, str]:
    """target -> source for every volume entry of a resolved service."""
    return {v["target"]: v.get("source", "") for v in service.get("volumes", [])}


def test_tmux_service_mounts_the_node_volumes_socket_and_socket_dir():
    config = _compose_config()
    tmux = config["services"]["tmux"]

    assert _mounts(tmux) == {
        "/app": "autonomy-code",
        "/app/data": "autonomy-data",
        "/app/orgs": "autonomy-orgs",
        "/var/run/docker.sock": "/var/run/docker.sock",
        TMUX_DIR: "autonomy-tmux",
    }
    assert tmux["environment"]["TMUX_TMPDIR"] == TMUX_DIR
    assert tmux["environment"]["TERM"] == "xterm-256color"
    assert tmux["environment"]["AUTONOMY_PROVISION_RAMFS"] == "0"
    assert tmux["command"] == ["tmux", "-D"]
    assert tmux["init"] is True
    assert tmux["restart"] == "unless-stopped"
    assert tmux["healthcheck"]["test"] == [
        "CMD-SHELL", f"test -S {TMUX_DIR}/tmux-1000/default",
    ]
    assert config["volumes"]["autonomy-tmux"]["name"] == "autonomy-tmux"


def test_tmux_service_carries_every_autonomy_variable_the_dashboard_gets():
    services = _compose_config()["services"]
    dashboard_env = {
        k: v for k, v in services["dashboard"]["environment"].items()
        if k.startswith("AUTONOMY_")
    }
    tmux_env = services["tmux"]["environment"]
    for key, value in dashboard_env.items():
        assert tmux_env.get(key) == value, key


def test_tmux_service_shares_the_dashboard_image():
    services = _compose_config()["services"]
    assert services["tmux"]["image"] == services["dashboard"]["image"]


def test_dashboard_is_a_client_of_the_sidecar_socket():
    dashboard = _compose_config()["services"]["dashboard"]

    assert _mounts(dashboard)[TMUX_DIR] == "autonomy-tmux"
    assert dashboard["depends_on"]["tmux"]["condition"] == "service_healthy"


def test_no_host_tmp_mount_anywhere():
    config = _compose_config()
    assert "/host-tmp" not in json.dumps(config)
    assert "host-tmp" not in COMPOSE.read_text()
    assert "host-tmp" not in ENTRYPOINT.read_text()


def _tmux_block() -> str:
    text = ENTRYPOINT.read_text()
    match = re.search(
        r"^if \[ -d /run/autonomy-tmux \]; then\n.*?^fi\n", text, re.M | re.S,
    )
    assert match, "entrypoint has no /run/autonomy-tmux block"
    return match.group(0)


def _run_block(socket_dir: Path) -> str:
    script = _tmux_block().replace(TMUX_DIR, str(socket_dir))
    result = subprocess.run(
        ["sh", "-c", 'unset TMUX_TMPDIR\n' + script + 'printf %s "${TMUX_TMPDIR-unset}"'],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_entrypoint_exports_tmux_tmpdir_when_the_socket_dir_exists(tmp_path):
    socket_dir = tmp_path / "autonomy-tmux"
    socket_dir.mkdir()
    assert _run_block(socket_dir) == str(socket_dir)


def test_entrypoint_leaves_tmux_tmpdir_unset_without_the_socket_dir(tmp_path):
    assert _run_block(tmp_path / "absent") == "unset"
