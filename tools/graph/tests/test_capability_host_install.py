"""Tests for the capability host-install runner.

Covers the runner loop of the Capability Host-Install Runner protocol
(graph://149705db-a39): fingerprint-driven skip, success/failure state
rows, the failed-run fingerprint-preservation rule, the missing-intent
``unknown`` case, and the success_marker-lied-about-outcome case. A final
integration block drives the REAL ``agents/capabilities/video/install.sh``
against a synthetic sha256-pinned tarball to pin the headline acceptance:
a fresh host installs a verified ``bin/ffmpeg``, and a tampered
``ffmpeg.pin`` is refused with nothing extracted.

State I/O runs against a temp ``GRAPH_DB`` through the host-direct ``ops``
client (the autouse ``_isolate_graph_env`` fixture pins host mode).
"""

from __future__ import annotations

import hashlib
import io
import json
import lzma
import os
import stat
import tarfile
from pathlib import Path

import pytest

from tools.graph import capability_host_install as runner
from tools.graph import ops


# ── fixtures ─────────────────────────────────────────────────


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Fresh temp GRAPH_DB; host-direct client (no dashboard, no network)."""
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "graph.db"))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield tmp_path


def _read_state(name: str) -> dict | None:
    return runner._read_state(ops, ops.CALLER_ORG, name)


def _make_capability(
    root: Path,
    *,
    name: str = "autonomy/demo",
    dir_name: str = "demo",
    command: list[str],
    fingerprint_content: str = "v1",
    success_marker: str = "installed.marker",
    extra_host_install: dict | None = None,
) -> Path:
    """Write a synthetic capability tree under ``root`` and return its dir."""
    cap_dir = root / "agents" / "capabilities" / dir_name
    (cap_dir / "install").mkdir(parents=True, exist_ok=True)
    fp_rel = f"agents/capabilities/{dir_name}/install/inputs.txt"
    (root / fp_rel).write_text(fingerprint_content)
    host_install = {
        "command": command,
        "fingerprint_files": [fp_rel],
        "success_marker": success_marker,
    }
    if extra_host_install:
        host_install.update(extra_host_install)
    manifest = {
        "name": name,
        "version": 1,
        "implements": [{"contract": "demo", "version": 1}],
        "delivery_mode": "mounted_tools",
        "package_root": f"agents/capabilities/{dir_name}",
        "probe": {"kind": "command", "entrypoint": "demo --help"},
        "host_install": host_install,
    }
    (cap_dir / "manifest.json").write_text(json.dumps(manifest))
    return cap_dir


def _run(root: Path, impl=None):
    return runner.run(impl, repo_root=root, client=ops, org=ops.CALLER_ORG)


# ── synthetic-command runner-loop tests ──────────────────────


def test_installs_and_writes_ready(graph_db_env, tmp_path):
    cap = _make_capability(
        tmp_path,
        command=["bash", "-c", "touch installed.marker; echo done"],
    )
    results = _run(tmp_path)
    assert [r["outcome"] for r in results] == ["ready"]
    assert (cap / "installed.marker").exists()

    state = _read_state("autonomy/demo")
    assert state["state"] == "ready"
    assert state["last_exit_code"] == 0
    assert state["last_fingerprint"]
    assert state["last_succeeded_at"]
    assert state["runner_version"] == runner.RUNNER_VERSION
    assert "done" in state.get("stdout_tail", "")


def test_rerun_skips_on_fingerprint_match(graph_db_env, tmp_path):
    # The command appends a line each time it runs; a skip leaves it alone.
    cap = _make_capability(
        tmp_path,
        command=["bash", "-c", "echo x >> runs.log; touch installed.marker"],
    )
    _run(tmp_path)
    first = (cap / "runs.log").read_text()

    results = _run(tmp_path)
    assert [r["outcome"] for r in results] == ["skipped"]
    assert (cap / "runs.log").read_text() == first  # command did NOT re-run

    state = _read_state("autonomy/demo")
    assert state["state"] == "ready"


def test_fingerprint_change_triggers_reinstall(graph_db_env, tmp_path):
    cap = _make_capability(
        tmp_path,
        command=["bash", "-c", "echo x >> runs.log; touch installed.marker"],
    )
    _run(tmp_path)
    # Change the intent file → stale → reruns.
    (tmp_path / "agents/capabilities/demo/install/inputs.txt").write_text("v2")
    results = _run(tmp_path)
    assert [r["outcome"] for r in results] == ["ready"]
    assert (cap / "runs.log").read_text().count("x") == 2


def test_failure_preserves_last_fingerprint(graph_db_env, tmp_path):
    """Protocol step 5: a failed run does NOT update last_fingerprint."""
    cap = _make_capability(
        tmp_path,
        command=["bash", "-c", "touch installed.marker"],
    )
    _run(tmp_path)
    good_fp = _read_state("autonomy/demo")["last_fingerprint"]

    # New intent (fingerprint changes) + a command that now fails.
    (tmp_path / "agents/capabilities/demo/install/inputs.txt").write_text("v2")
    (cap / "manifest.json").write_text(
        (cap / "manifest.json").read_text().replace(
            "touch installed.marker",
            "echo boom >&2; exit 3",
        )
    )
    results = _run(tmp_path)
    assert [r["outcome"] for r in results] == ["failed"]

    state = _read_state("autonomy/demo")
    assert state["state"] == "failed"
    assert state["last_exit_code"] == 3
    assert state["last_fingerprint"] == good_fp  # unchanged
    assert "boom" in state["stderr_tail"]


def test_missing_fingerprint_file_is_unknown(graph_db_env, tmp_path):
    cap = _make_capability(
        tmp_path,
        command=["bash", "-c", "touch installed.marker"],
    )
    (tmp_path / "agents/capabilities/demo/install/inputs.txt").unlink()
    results = _run(tmp_path)
    assert [r["outcome"] for r in results] == ["unknown"]
    assert not (cap / "installed.marker").exists()  # command never ran

    state = _read_state("autonomy/demo")
    assert state["state"] == "unknown"


def test_success_marker_missing_after_exit0_is_failure(graph_db_env, tmp_path):
    _make_capability(
        tmp_path,
        command=["bash", "-c", "echo did-nothing"],  # exit 0, no marker
    )
    results = _run(tmp_path)
    assert [r["outcome"] for r in results] == ["failed"]

    state = _read_state("autonomy/demo")
    assert state["state"] == "failed"
    assert state["last_exit_code"] == 0
    assert "success_marker missing" in state["stderr_tail"]
    assert "last_fingerprint" not in state  # never succeeded


def test_timeout_is_failure_with_exit_minus_one(graph_db_env, tmp_path):
    _make_capability(
        tmp_path,
        command=["bash", "-c", "sleep 5; touch installed.marker"],
        extra_host_install={"timeout_seconds": 1},
    )
    results = _run(tmp_path)
    assert [r["outcome"] for r in results] == ["failed"]

    state = _read_state("autonomy/demo")
    assert state["state"] == "failed"
    assert state["last_exit_code"] == -1
    assert "timed out" in state["stderr_tail"]


def test_env_and_cwd_are_honored(graph_db_env, tmp_path):
    _make_capability(
        tmp_path,
        command=["bash", "-c", 'echo "$DEMO_TOKEN" > token.out; touch installed.marker'],
        extra_host_install={"env": {"DEMO_TOKEN": "sentinel-42"}},
    )
    _run(tmp_path)
    # cwd defaults to package_root, so token.out lands inside the cap dir.
    assert (
        tmp_path / "agents/capabilities/demo/token.out"
    ).read_text().strip() == "sentinel-42"


def test_impl_filter_no_match_returns_empty(graph_db_env, tmp_path):
    _make_capability(tmp_path, command=["bash", "-c", "touch installed.marker"])
    assert _run(tmp_path, "does-not-exist") == []


def test_impl_filter_matches_short_name(graph_db_env, tmp_path):
    _make_capability(tmp_path, command=["bash", "-c", "touch installed.marker"])
    results = _run(tmp_path, "demo")
    assert [r["name"] for r in results] == ["autonomy/demo"]


def test_no_host_install_impls_returns_empty(graph_db_env, tmp_path):
    # A capability without host_install must be ignored entirely.
    cap = tmp_path / "agents/capabilities/plain"
    cap.mkdir(parents=True)
    (cap / "manifest.json").write_text(json.dumps({
        "name": "autonomy/plain", "version": 1,
        "implements": [{"contract": "x", "version": 1}],
        "delivery_mode": "mounted_tools",
        "package_root": "agents/capabilities/plain",
        "probe": {"kind": "command", "entrypoint": "x --help"},
    }))
    assert _run(tmp_path) == []


# ── integration: the real video install.sh + sha256 pin ──────


def _build_fake_ffmpeg_tarball(dest: Path, version: str) -> str:
    """Build a .tar.xz carrying fake ffmpeg/ffprobe scripts; return sha256."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for tool in ("ffmpeg", "ffprobe"):
            script = f'#!/bin/bash\necho "{tool} version {version} fake-static"\n'
            data = script.encode()
            info = tarfile.TarInfo(f"ffmpeg-{version}-amd64-static/{tool}")
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    xz = lzma.compress(buf.getvalue())
    dest.write_bytes(xz)
    return hashlib.sha256(xz).hexdigest()


def _stage_video_capability(root: Path, *, sha: str, version: str) -> Path:
    """Copy the real install.sh into a synthetic video capability tree."""
    real_install = (
        Path(runner._REPO_ROOT)
        / "agents/capabilities/video/install/install.sh"
    )
    cap = root / "agents/capabilities/video"
    (cap / "install").mkdir(parents=True, exist_ok=True)
    (cap / "install" / "install.sh").write_text(real_install.read_text())
    (cap / "install" / "ffmpeg.pin").write_text(
        f"url=https://example.invalid/ffmpeg.tar.xz\n"
        f"sha256={sha}\nversion={version}\n"
    )
    manifest = {
        "name": "autonomy/video", "version": 1,
        "implements": [{"contract": "video_tooling", "version": 1}],
        "delivery_mode": "mounted_tools",
        "package_root": "agents/capabilities/video",
        "probe": {"kind": "command", "entrypoint": "video-probe --help"},
        "host_install": {
            "command": ["bash", "install/install.sh"],
            "cwd": "agents/capabilities/video",
            "fingerprint_files": [
                "agents/capabilities/video/install/ffmpeg.pin"
            ],
            "timeout_seconds": 600,
            "success_marker": "bin/ffmpeg",
        },
    }
    (cap / "manifest.json").write_text(json.dumps(manifest))
    return cap


def test_video_fresh_install_populates_verified_ffmpeg(graph_db_env, tmp_path, monkeypatch):
    version = "7.0.2"
    tarball = tmp_path / "ffmpeg.tar.xz"
    sha = _build_fake_ffmpeg_tarball(tarball, version)
    cap = _stage_video_capability(tmp_path, sha=sha, version=version)
    monkeypatch.setenv("FFMPEG_TARBALL_CACHE", str(tarball))

    results = _run(tmp_path, "autonomy/video")
    assert [r["outcome"] for r in results] == ["ready"], results

    ffmpeg = cap / "bin" / "ffmpeg"
    assert ffmpeg.exists()
    assert os.access(ffmpeg, os.X_OK)
    assert (cap / "bin" / "ffprobe").exists()

    state = _read_state("autonomy/video")
    assert state["state"] == "ready"
    assert state["last_fingerprint"]


def test_video_tampered_pin_is_refused_nothing_extracted(graph_db_env, tmp_path, monkeypatch):
    """Negative case (protocol step 5): a mismatched sha256 fails closed
    BEFORE extraction — bin/ffmpeg is never created and the state row is
    failed with no fingerprint recorded."""
    version = "7.0.2"
    tarball = tmp_path / "ffmpeg.tar.xz"
    real_sha = _build_fake_ffmpeg_tarball(tarball, version)
    tampered = "0" * 64  # a pin that does not match the tarball
    assert tampered != real_sha
    cap = _stage_video_capability(tmp_path, sha=tampered, version=version)
    monkeypatch.setenv("FFMPEG_TARBALL_CACHE", str(tarball))

    results = _run(tmp_path, "autonomy/video")
    assert [r["outcome"] for r in results] == ["failed"], results

    assert not (cap / "bin" / "ffmpeg").exists()  # nothing extracted
    state = _read_state("autonomy/video")
    assert state["state"] == "failed"
    assert "last_fingerprint" not in state


# ── CLI-level exit-status wiring ─────────────────────────────


class _Args:
    def __init__(self, impl=None, org=None):
        self.impl = impl
        self.org = org


def test_cmd_exit_zero_on_ready(monkeypatch, capsys):
    from tools.graph import capability_cmd
    monkeypatch.setattr(
        capability_cmd, "get_client", lambda: object()
    )
    monkeypatch.setattr(
        runner, "run",
        lambda *a, **k: [{"name": "autonomy/demo", "outcome": "ready"}],
    )
    capability_cmd.cmd_host_install(_Args())  # no SystemExit → exit 0


def test_cmd_exit_nonzero_on_failure(monkeypatch):
    from tools.graph import capability_cmd
    monkeypatch.setattr(capability_cmd, "get_client", lambda: object())
    monkeypatch.setattr(
        runner, "run",
        lambda *a, **k: [{"name": "autonomy/demo", "outcome": "failed",
                          "exit_code": 1}],
    )
    with pytest.raises(SystemExit) as exc:
        capability_cmd.cmd_host_install(_Args())
    assert exc.value.code == 1


def test_cmd_named_impl_no_match_errors(monkeypatch):
    from tools.graph import capability_cmd
    monkeypatch.setattr(capability_cmd, "get_client", lambda: object())
    monkeypatch.setattr(runner, "run", lambda *a, **k: [])
    with pytest.raises(SystemExit) as exc:
        capability_cmd.cmd_host_install(_Args(impl="ghost"))
    assert exc.value.code == 1
