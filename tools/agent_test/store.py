"""Durable filesystem state for Agent Test runs."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


LIVE_STATUSES = frozenset({"starting", "running", "stopping"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def repository_root(start: Path | None = None) -> Path:
    cwd = (start or Path.cwd()).resolve()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return cwd
    return Path(result.stdout.strip()).resolve()


def state_root(repo: Path) -> Path:
    override = os.environ.get("AGENT_TEST_STATE_DIR", "").strip()
    if override:
        return Path(override).resolve()
    digest = hashlib.sha256(str(repo).encode()).hexdigest()[:10]
    if Path("/workspace/output").is_dir():
        return Path("/workspace/output/agent-test") / f"{repo.name}-{digest}"
    xdg = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return xdg / "agent-test" / f"{repo.name}-{digest}"


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%m%d-%H%M%S")
    return f"at-{stamp}-{secrets.token_hex(2)}"


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


@contextlib.contextmanager
def state_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_dir(root: Path, run_id: str) -> Path:
    return root / "runs" / run_id


def manifest_path(directory: Path) -> Path:
    return directory / "run.json"


def update_manifest(directory: Path, changes: dict[str, Any]) -> dict[str, Any]:
    lock_path = directory / ".manifest.lock"
    directory.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        current = read_json(manifest_path(directory), {})
        current.update(changes)
        atomic_write_json(manifest_path(directory), current)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return current


def list_manifests(root: Path) -> list[dict[str, Any]]:
    manifests: list[dict[str, Any]] = []
    runs = root / "runs"
    if not runs.is_dir():
        return manifests
    for path in runs.glob("*/run.json"):
        value = read_json(path)
        if isinstance(value, dict):
            value["_directory"] = str(path.parent)
            manifests.append(value)
    manifests.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return manifests


def pid_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reconcile_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("status") not in LIVE_STATUSES:
        return manifest
    if pid_alive(manifest.get("worker_pid")):
        return manifest
    directory = Path(manifest["_directory"])
    updated = update_manifest(
        directory,
        {
            "status": "stopped" if manifest.get("status") == "stopping" else "error",
            "finished_at": manifest.get("finished_at") or utc_now(),
            "message": manifest.get("message") or "run supervisor exited before recording completion",
        },
    )
    updated["_directory"] = str(directory)
    return updated


def live_manifest(root: Path) -> dict[str, Any] | None:
    for manifest in list_manifests(root):
        reconciled = reconcile_manifest(manifest)
        if reconciled.get("status") in LIVE_STATUSES:
            return reconciled
    return None


def resolve_manifest(root: Path, run_id: str | None) -> dict[str, Any] | None:
    if run_id:
        directory = run_dir(root, run_id)
        value = read_json(manifest_path(directory))
        if not isinstance(value, dict):
            return None
        value["_directory"] = str(directory)
        return reconcile_manifest(value)
    manifests = list_manifests(root)
    return reconcile_manifest(manifests[0]) if manifests else None


def guidance_first(root: Path, concept: str) -> bool:
    """Return true once for a versioned guidance concept."""
    with state_lock(root):
        path = root / "guidance.json"
        value = read_json(path, {})
        seen = value.setdefault("seen", {})
        if concept in seen:
            return False
        seen[concept] = utc_now()
        value["schema"] = 1
        atomic_write_json(path, value)
        return True
