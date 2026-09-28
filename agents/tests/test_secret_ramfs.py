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


# ── daemon-frame propagation probe (auto-b0326) ──

# The shape of a stock WSL2 node's root (private) with a shared NFS mount, a
# private drvfs drive, a slave mount, and a path with an escaped space.
_MOUNTINFO = "\n".join([
    "22 1 8:32 / / rw,relatime - ext4 /dev/sdc rw",
    "30 22 0:40 / /mnt/nas rw,relatime shared:5 - nfs4 nas:/export rw",
    "31 22 0:41 / /mnt/c rw,noatime - 9p drvfs rw",
    "32 22 0:42 / /srv/my\\040dir rw master:7 - ext4 /dev/sdd rw",
    "33 22 0:43 / /srv/stack rw shared:9 - ext4 /dev/sde rw",
    "34 33 0:44 / /srv/stack rw - tmpfs tmpfs rw",
])


def test_propagating_from_mountinfo_uses_the_containing_mount():
    resolved = {
        "tmp": "/tmp/x",                  # under the private /
        "nas": "/mnt/nas/ws",             # shared
        "nasty": "/mnt/nasty",            # a sibling name, not under /mnt/nas
        "drive": "/mnt/c/autonomy-test",  # private drvfs
        "slave": "/srv/my dir/z",         # master:N is a slave
        "stacked": "/srv/stack/a",        # the top of two stacked mounts is private
        "unresolved": "",
    }
    assert secret_ramfs.propagating_from_mountinfo(_MOUNTINFO, resolved) == {"nas", "slave"}


def test_daemon_propagating_reads_the_daemon_frame_through_the_socket(monkeypatch, tmp_path):
    import base64
    import subprocess

    sock = tmp_path / "docker.sock"
    sock.touch()
    monkeypatch.setattr(secret_ramfs, "_DOCKER_SOCKET", str(sock))
    monkeypatch.setattr(secret_ramfs, "_own_container_id", lambda: "cid")
    monkeypatch.setattr(secret_ramfs, "_own_image", lambda cid: "img")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        lines = [_MOUNTINFO, "--- resolved"]
        for p, real in (("/home/tester/ws", "/home/tester/ws"), ("/data/link", "/mnt/nas/ws")):
            lines.append(f"{base64.b64encode(p.encode()).decode()} {real}")
        return subprocess.CompletedProcess(argv, 0, "\n".join(lines) + "\n", "")

    monkeypatch.setattr(secret_ramfs.subprocess, "run", fake_run)
    got = secret_ramfs.daemon_propagating(["/home/tester/ws", "/data/link"])
    assert got == {"/data/link"}   # a symlink is judged by its target's mount
    assert seen["argv"][:9] == ["docker", "run", "--rm", "--user", "0", "--privileged",
                                "--pid=host", "--entrypoint", "nsenter"]
    assert seen["argv"][10:14] == ["-t", "1", "-m", "--"]


def test_daemon_propagating_is_unknown_when_the_probe_cannot_run(monkeypatch, tmp_path):
    monkeypatch.setattr(secret_ramfs, "_own_container_id", lambda: "cid")
    monkeypatch.setattr(secret_ramfs, "_DOCKER_SOCKET", str(tmp_path / "absent.sock"))
    assert secret_ramfs.daemon_propagating(["/x"]) is None
    assert secret_ramfs.daemon_propagating([]) == set()


def test_launcher_probes_only_when_the_plan_has_an_rslave_bind(monkeypatch, caplog):
    from agents import mount_plan as mp
    from agents import session_launcher

    calls = []
    monkeypatch.setattr(secret_ramfs, "daemon_propagating",
                        lambda paths: calls.append(list(paths)) or None)
    topo = mp.NodeTopology(is_host_process=False)

    plain = mp.MountPlan()
    plain.set(mp.mount_spec("/root", mp.PrivateBind("/host-home:ro")))
    assert session_launcher._daemon_propagating(plain, topo) == set()
    assert calls == []

    ws = mp.MountPlan()
    ws.set(mp.mount_spec("/srv/ws", mp.BindRefuseMissing("/workspace/ws")))
    # An unrunnable probe (None) means no source propagates: bind private,
    # and say so.
    with caplog.at_level("WARNING", logger="agents.session_launcher"):
        assert session_launcher._daemon_propagating(ws, topo) == set()
    assert calls == [["/srv/ws"]]
    assert "propagation probe unavailable" in caplog.text and "/srv/ws" in caplog.text
