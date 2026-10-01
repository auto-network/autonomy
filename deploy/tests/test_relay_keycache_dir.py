"""The MCP relay's release directory exists from boot (auto-5gdao review of
70697920): compose binds /run/autonomy-keycache/mcp-relay read-only with
create_host_path: false, so before any vault release -- a first deploy, or
after a reboot until unlock -- a missing directory made docker refuse to
start the relay at all, before relay.sh's wait loop could run."""

from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "deploy/entrypoint.sh"
KEYCACHE = "/run/autonomy-keycache"


def _provisioning_block() -> str:
    text = ENTRYPOINT.read_text()
    match = re.search(r'^if \[ "\$\{AUTONOMY_PROVISION_RAMFS:-1\}" = "1" \]; then\n(.*?)^else\n',
                      text, re.S | re.M)
    assert match, "the entrypoint's key-cache provisioning block moved"
    return match.group(1)


def test_the_relay_release_dir_exists_after_keycache_setup_with_no_release(tmp_path):
    keycache = tmp_path / "keycache"
    keycache.mkdir()
    block = (_provisioning_block()
             .replace(KEYCACHE, str(keycache))
             .replace('"$(dirname "$0")/provision-secret-ramfs.sh"', "true"))
    subprocess.run(["sh", "-c", block], check=True, timeout=30)
    for child in ("mcp-relay", "voice"):      # voice: auto-es7ja
        made = keycache / child
        assert made.is_dir() and list(made.iterdir()) == []
        assert stat.S_IMODE(made.stat().st_mode) == 0o700


def test_the_relay_binds_exactly_that_directory_read_only():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    binds = [v for v in compose["services"]["mcp-relay"]["volumes"]
             if isinstance(v, dict) and v.get("type") == "bind"]
    assert binds == [{"type": "bind", "source": f"{KEYCACHE}/mcp-relay",
                      "target": "/run/mcp-relay-secrets", "read_only": True,
                      "bind": {"create_host_path": False}}]
