"""Session secret delivery is per-container-private; only the key cache is host-side."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agents import secret_ramfs


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_no_shared_delivery_bind_in_compose():
    """The 2026-08-30 four-incident class: a shared delivery root bound into
    a node dashboard let its sweeper destroy every session's secrets. The
    compose file must not bind one, ever again."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    volumes = compose["services"]["dashboard"]["volumes"]
    for volume in volumes:
        source = volume.get("source") if isinstance(volume, dict) else str(volume)
        assert "autonomy-secrets" not in str(source), (
            "the shared secret-delivery root is retired; session delivery is "
            "a private in-container ramfs and must not be bound into any "
            "dashboard")


def test_keycache_bind_remains_with_one_way_propagation():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    volumes = compose["services"]["dashboard"]["volumes"]
    keycache = next(
        volume for volume in volumes
        if isinstance(volume, dict)
        and volume.get("source") == secret_ramfs.KEYCACHE_MOUNT
    )
    assert keycache["target"] == secret_ramfs.KEYCACHE_MOUNT
    assert keycache["bind"]["propagation"] == "rslave"


def test_provisioner_provisions_only_the_keycache(monkeypatch):
    calls = []
    monkeypatch.setattr(
        secret_ramfs,
        "provision",
        lambda path, *, container_bound: calls.append((path, container_bound)),
    )
    assert secret_ramfs.main([]) == 0
    assert calls == [(secret_ramfs.KEYCACHE_MOUNT, True)]


def test_deliver_script_heals_an_orphaned_bind_and_refuses_tmpfs():
    """The script must mount a FRESH ramfs over both a non-ramfs path and an
    unwritable (orphaned ``//deleted``) ramfs, refuse tmpfs by magic, and
    write the file atomically at 0600."""
    script = secret_ramfs._deliver_script("fleet-ssh-key", 1000)
    assert 'touch "/run/secrets/.w"' in script          # orphan probe
    assert "mount -t ramfs ramfs" in script
    assert f"{secret_ramfs.TMPFS_MAGIC:x}" in script    # refused by name
    assert "umask 077" in script
    assert 'mv -f "/run/secrets/fleet-ssh-key.tmp" "/run/secrets/fleet-ssh-key"' in script


def test_deliver_pipes_plaintext_over_stdin_only(monkeypatch):
    """The value must transit stdin — never argv (docker persists argv in the
    helper's on-disk config, even transiently)."""
    seen = {}

    def fake_run(cmd, *, input=None, capture_output=None, timeout=None):
        seen["cmd"] = cmd
        seen["input"] = input

        class R:
            returncode = 0
            stdout = b""
            stderr = b""
        return R()

    monkeypatch.setattr(secret_ramfs.subprocess, "run", fake_run)
    monkeypatch.setattr(secret_ramfs, "_container_pid", lambda c: 4242)
    monkeypatch.setattr(secret_ramfs, "_container_image", lambda c: "img")

    path = secret_ramfs.deliver_secret_file("auto-x", "key", b"SECRET-BYTES")

    assert path == "/run/secrets/key"
    assert seen["input"] == b"SECRET-BYTES"
    assert not any(b"SECRET" in str(tok).encode() for tok in seen["cmd"])
    # The helper enters the TARGET container's namespace, not PID 1's.
    assert "-t" in seen["cmd"] and "4242" in seen["cmd"]
    assert "nsenter" in seen["cmd"]
    assert "-i" in seen["cmd"]


def test_deliver_refuses_unsafe_names():
    with pytest.raises(secret_ramfs.ProvisionError):
        secret_ramfs.deliver_secret_file("auto-x", "../etc/passwd", b"x")
    with pytest.raises(secret_ramfs.ProvisionError):
        secret_ramfs.deliver_secret_file("bad name", "key", b"x")


def test_deliver_fails_closed_on_helper_error(monkeypatch):
    def fake_run(cmd, *, input=None, capture_output=None, timeout=None):
        class R:
            returncode = 3
            stdout = b""
            stderr = b"REFUSE: /run/secrets is not ramfs"
        return R()

    monkeypatch.setattr(secret_ramfs.subprocess, "run", fake_run)
    monkeypatch.setattr(secret_ramfs, "_container_pid", lambda c: 1)
    monkeypatch.setattr(secret_ramfs, "_container_image", lambda c: "img")
    with pytest.raises(secret_ramfs.ProvisionError, match="not ramfs"):
        secret_ramfs.deliver_secret_file("auto-x", "key", b"x")


def test_destroy_removes_the_exact_file_via_nsenter(monkeypatch):
    seen = {}

    def fake_run(cmd, *, capture_output=None, timeout=None):
        seen["cmd"] = cmd

        class R:
            returncode = 0
            stdout = b""
            stderr = b""
        return R()

    monkeypatch.setattr(secret_ramfs.subprocess, "run", fake_run)
    monkeypatch.setattr(secret_ramfs, "_container_pid", lambda c: 77)
    monkeypatch.setattr(secret_ramfs, "_container_image", lambda c: "img")
    secret_ramfs.destroy_secret_file("auto-x", "fleet-key")
    joined = " ".join(str(t) for t in seen["cmd"])
    assert "rm -f" in joined and "/run/secrets/fleet-key" in joined
    assert "77" in seen["cmd"]  # the target container's pid, not PID 1


def test_destroy_is_a_noop_when_container_is_gone(monkeypatch):
    def gone(c):
        raise secret_ramfs.ProvisionError("not running")

    monkeypatch.setattr(secret_ramfs, "_container_pid", gone)
    ran = []
    monkeypatch.setattr(secret_ramfs.subprocess, "run",
                        lambda *a, **k: ran.append(a))
    secret_ramfs.destroy_secret_file("auto-x", "key")   # must not raise
    assert ran == []   # container gone -> file already freed, no helper run


def test_helper_runs_as_root_or_nsenter_has_no_capabilities(monkeypatch):
    """The session/dashboard images end with USER agent (uid 1000); a
    non-root process in a --privileged container holds no effective
    capabilities, so nsenter setns fails EPERM (proven 2026-08-30). Every
    privileged helper MUST pass --user 0."""
    seen = {}

    def fake_run(cmd, *, input=None, capture_output=None, timeout=None):
        seen["cmd"] = cmd

        class R:
            returncode = 0
            stdout = b""
            stderr = b""
        return R()

    monkeypatch.setattr(secret_ramfs.subprocess, "run", fake_run)
    monkeypatch.setattr(secret_ramfs, "_container_pid", lambda c: 5)
    monkeypatch.setattr(secret_ramfs, "_container_image", lambda c: "autonomy-session")

    secret_ramfs.deliver_secret_file("auto-x", "key", b"v")
    cmd = seen["cmd"]
    assert "--user" in cmd and cmd[cmd.index("--user") + 1] == "0"
    assert cmd.index("--user") < cmd.index("--privileged")

    secret_ramfs.destroy_secret_file("auto-x", "key")
    cmd = seen["cmd"]
    assert "--user" in cmd and cmd[cmd.index("--user") + 1] == "0"
