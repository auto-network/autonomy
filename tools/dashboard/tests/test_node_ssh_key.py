"""The node's GitHub SSH key comes only from the vault (auto-zhbje): the
dashboard releases node.github-ssh-key into its ramfs key cache, the
entrypoint's ssh config points there and copies nothing from disk, and a
clone with no released key says to unlock the vault."""

from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

import pytest

from tools.dashboard import node_ssh

REPO = Path(__file__).resolve().parents[3]
ENTRYPOINT = REPO / "deploy/entrypoint.sh"
KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----"


def _vault(monkeypatch, *, warm=True, value=KEY):
    from tools.graph import settings_ops

    monkeypatch.setattr(settings_ops, "personal_delegate_audited_is_warm", lambda: warm)
    monkeypatch.setattr(
        settings_ops, "read_set_key",
        lambda set_id, key, *, org, peers=None:
            ({"payload": {"value": value}} if value is not None and key == node_ssh.VAULT_KEY
             else None))


def _release(tmp_path):
    return node_ssh.release_node_ssh_key(directory=tmp_path / "node-ssh",
                                         memory_check=lambda d: None)


@pytest.mark.parametrize("sealed", [KEY, KEY + "\n", KEY.replace("\n", "\r\n") + "\r\n"])
def test_the_key_is_released_0600_and_newline_terminated(monkeypatch, tmp_path, sealed):
    """OpenSSH refuses a private key file without a final newline."""
    _vault(monkeypatch, value=sealed)
    assert _release(tmp_path) == "ok"
    f = tmp_path / "node-ssh" / "id_ed25519"
    assert f.read_text() == KEY + "\n"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_a_cold_vault_keeps_the_released_key_and_unsealed_clears_it(monkeypatch, tmp_path):
    _vault(monkeypatch)
    _release(tmp_path)
    _vault(monkeypatch, warm=False)
    assert _release(tmp_path) == "vault-cold"
    assert (tmp_path / "node-ssh" / "id_ed25519").exists()
    _vault(monkeypatch, value=None)
    assert _release(tmp_path) == "unsealed"
    assert not (tmp_path / "node-ssh" / "id_ed25519").exists()


def test_unlock_releases_the_node_key(monkeypatch):
    from tools.dashboard import mcp_relay_routes, unlock_routes
    from tools.dashboard.plugins.backup import credentials

    ran = []
    monkeypatch.setattr(credentials, "release_offsite_in_background", lambda config=None: None)
    monkeypatch.setattr(mcp_relay_routes, "release_relay_credentials", lambda: None)
    monkeypatch.setattr(node_ssh, "release_node_ssh_key", lambda: ran.append(True))
    unlock_routes._schedule_vault_releases()
    for _ in range(100):
        if ran:
            break
        import time
        time.sleep(0.01)
    assert ran == [True]


@pytest.mark.parametrize("released, hinted", [(False, True), (True, False)])
def test_a_denied_clone_names_the_unreleased_key(monkeypatch, tmp_path, released, hinted):
    from agents import workspace_manager as wm

    keycache = tmp_path / "keycache"
    if released:
        (keycache / "node-ssh").mkdir(parents=True)
        (keycache / "node-ssh" / "id_ed25519").write_text(KEY + "\n")
    monkeypatch.setenv("AUTONOMY_KEYCACHE_MOUNT", str(keycache))

    class Denied:
        returncode, stdout = 128, ""
        stderr = "git@github.com: Permission denied (publickey).\nfatal: Could not read"

    monkeypatch.setattr(wm.subprocess, "run", lambda *a, **k: Denied())
    with pytest.raises(wm.WorkspaceError) as err:
        wm._run_git(["fetch"])
    assert ("unlock the vault" in str(err.value)) is hinted


def test_the_entrypoint_copies_no_key_and_points_ssh_at_the_released_one(tmp_path):
    text = ENTRYPOINT.read_text()
    start = text.index('AUT_HOME="$(getent passwd autonomy | cut -d: -f6)"')
    end = text.index("\nfi\n", start) + 4
    block = text[start:end]
    assert "data/artifacts" not in block.replace("data/artifacts/*/*/id_* left", "")
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("stale plaintext copy")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("ssh-keyscan", "chown"):
        (bin_dir / tool).write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / tool).chmod(0o755)
    block = block.replace('$(getent passwd autonomy | cut -d: -f6)', str(home))
    subprocess.run(["sh", "-c", block], check=True, timeout=30,
                   env={"PATH": f"{bin_dir}:/usr/bin:/bin"})
    assert not (home / ".ssh" / "id_ed25519").exists()
    config = (home / ".ssh" / "config").read_text()
    assert re.search(r"^Host github-\* github\.com$", config, re.M)
    assert "IdentityFile /run/autonomy-keycache/node-ssh/id_ed25519" in config
