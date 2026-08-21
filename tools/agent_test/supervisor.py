"""Small resident launch broker for Agent Test workers."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .store import atomic_write_json


IDLE_SECONDS = 30 * 60


def _paths(root: Path) -> tuple[Path, Path]:
    return root / "supervisor.sock", root / "supervisor.json"


def _request(socket_path: Path, payload: dict[str, Any], timeout: float = 2) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path))
        client.sendall(json.dumps(payload).encode() + b"\n")
        raw = b""
        while not raw.endswith(b"\n"):
            part = client.recv(65536)
            if not part:
                break
            raw += part
    value = json.loads(raw.decode())
    return value if isinstance(value, dict) else {"ok": False, "error": "invalid response"}


def ensure_supervisor(root: Path, repo: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    socket_path, _metadata_path = _paths(root)
    try:
        response = _request(socket_path, {"op": "ping"}, timeout=0.25)
        if response.get("ok"):
            return response
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    log = (root / "supervisor.log").open("ab")
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                f"{__package__}.supervisor",
                "--state-root",
                str(root),
                "--repo",
                str(repo),
            ],
            cwd=repo,
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log.close()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            response = _request(socket_path, {"op": "ping"}, timeout=0.25)
            if response.get("ok"):
                return response
        except (OSError, ValueError, json.JSONDecodeError):
            time.sleep(0.03)
    raise RuntimeError("Agent Test supervisor did not become ready")


def start_worker(root: Path, repo: Path, directory: Path) -> dict[str, Any]:
    ensure_supervisor(root, repo)
    socket_path, _metadata_path = _paths(root)
    return _request(socket_path, {"op": "start", "run_dir": str(directory)}, timeout=3)


def supervisor_status(root: Path) -> dict[str, Any] | None:
    socket_path, _metadata_path = _paths(root)
    try:
        response = _request(socket_path, {"op": "ping"}, timeout=0.25)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return response if response.get("ok") else None


def serve(root: Path, repo: Path) -> int:
    socket_path, metadata_path = _paths(root)
    root.mkdir(parents=True, exist_ok=True)
    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    server.listen(8)
    server.settimeout(1)
    atomic_write_json(metadata_path, {"pid": os.getpid(), "repo": str(repo)})
    last_request = time.monotonic()
    try:
        while time.monotonic() - last_request < IDLE_SECONDS:
            try:
                connection, _address = server.accept()
            except TimeoutError:
                continue
            last_request = time.monotonic()
            with connection:
                raw = b""
                while not raw.endswith(b"\n") and len(raw) < 1_000_000:
                    part = connection.recv(65536)
                    if not part:
                        break
                    raw += part
                try:
                    request = json.loads(raw.decode())
                except (UnicodeDecodeError, json.JSONDecodeError):
                    request = {}
                if request.get("op") == "ping":
                    response = {"ok": True, "pid": os.getpid()}
                elif request.get("op") == "start":
                    directory = Path(str(request.get("run_dir") or "")).resolve()
                    allowed = (root / "runs").resolve()
                    if directory.parent != allowed or not (directory / "run.json").is_file():
                        response = {"ok": False, "error": "invalid run directory"}
                    else:
                        log = (directory / "worker.log").open("ab")
                        try:
                            child = subprocess.Popen(
                                [sys.executable, "-m", f"{__package__}.worker", "--run-dir", str(directory)],
                                cwd=repo,
                                env=os.environ.copy(),
                                stdin=subprocess.DEVNULL,
                                stdout=log,
                                stderr=subprocess.STDOUT,
                                start_new_session=True,
                                close_fds=True,
                            )
                        finally:
                            log.close()
                        response = {"ok": True, "worker_pid": child.pid, "supervisor_pid": os.getpid()}
                else:
                    response = {"ok": False, "error": "unsupported operation"}
                connection.sendall(json.dumps(response).encode() + b"\n")
    finally:
        server.close()
        try:
            socket_path.unlink()
        except FileNotFoundError:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    return serve(Path(args.state_root).resolve(), Path(args.repo).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
