"""Interpreter, project-profile, and workspace discovery for Agent Test."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any


def project_config(repo: Path) -> dict[str, Any]:
    path = repo / "pyproject.toml"
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    configured = value.get("tool", {}).get("agent-test", {})
    return configured if isinstance(configured, dict) else {}


def profiles(repo: Path) -> dict[str, dict[str, Any]]:
    configured = project_config(repo).get("profiles", {})
    if not isinstance(configured, dict):
        return {}
    return {
        str(name): value
        for name, value in configured.items()
        if isinstance(value, dict)
    }


def _resolve_candidate(repo: Path, raw: str) -> str:
    path = Path(raw).expanduser()
    if path.is_absolute() or os.path.sep in raw:
        # Do not resolve interpreter symlinks: a venv's ``python`` commonly
        # points at the system binary, but invoking it through the venv path is
        # what activates that environment's prefix and site-packages.
        return os.path.abspath(path if path.is_absolute() else repo / path)
    return raw


def python_candidates(repo: Path, requested: str = "auto") -> list[str]:
    if requested != "auto":
        return [_resolve_candidate(repo, requested)]
    values: list[str] = []
    configured = project_config(repo).get("python")
    if isinstance(configured, str) and configured.strip():
        values.append(_resolve_candidate(repo, configured.strip()))
    virtual_env = os.environ.get("VIRTUAL_ENV", "").strip()
    if virtual_env:
        values.append(os.path.abspath(Path(virtual_env) / "bin/python"))
    values.extend(
        os.path.abspath(repo / name / "bin/python")
        for name in (".venv", "venv", "env")
    )
    values.append(str(Path(sys.executable).resolve()))
    unique: list[str] = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return unique


def inspect_python(candidate: str) -> dict[str, Any]:
    code = (
        "import json,sys; import pytest; "
        "print(json.dumps({'executable':sys.executable,'pytest':pytest.__version__}))"
    )
    try:
        result = subprocess.run(
            [candidate, "-c", code],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"candidate": candidate, "usable": False, "reason": str(exc)[:500]}
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return {
            "candidate": candidate,
            "usable": False,
            "reason": (detail[-1] if detail else f"exit {result.returncode}")[:500],
        }
    try:
        details = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        details = {}
    return {
        "candidate": candidate,
        "usable": True,
        "executable": details.get("executable", candidate),
        "pytest": details.get("pytest", "unknown"),
    }


def choose_python(repo: Path, requested: str = "auto") -> tuple[str | None, list[dict[str, Any]]]:
    diagnoses = [inspect_python(value) for value in python_candidates(repo, requested)]
    usable = next((item for item in diagnoses if item["usable"]), None)
    return (str(usable["candidate"]) if usable else None), diagnoses


def workspace_fingerprint(repo: Path, selectors: list[str]) -> str:
    """Hash the code state relevant to deciding whether a rerun is unchanged."""
    digest = hashlib.sha256()
    digest.update(json.dumps(selectors, sort_keys=True).encode())
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, check=True, timeout=5
        ).stdout
        diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD"],
            cwd=repo,
            capture_output=True,
            check=True,
            timeout=20,
        ).stdout
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z"],
            cwd=repo,
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout
        digest.update(head)
        digest.update(diff)
        digest.update(status)
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=repo,
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout.split(b"\0")
        for raw_path in sorted(value for value in untracked if value):
            digest.update(raw_path)
            try:
                digest.update((repo / raw_path.decode()).read_bytes())
            except (OSError, UnicodeDecodeError):
                continue
    except (OSError, subprocess.SubprocessError):
        for path in sorted(repo.rglob("*.py")):
            if not path.is_file():
                continue
            digest.update(str(path.relative_to(repo)).encode())
            try:
                digest.update(path.read_bytes())
            except OSError:
                continue
    return digest.hexdigest()


def pytest_parallelism(repo: Path, profile: dict[str, Any]) -> int:
    """Infer actual pytest worker parallelism independently of slot weight."""
    path = repo / "pyproject.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        addopts = data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts", "")
    except (OSError, tomllib.TOMLDecodeError):
        addopts = ""
    tokens = shlex.split(addopts) if isinstance(addopts, str) else list(addopts or [])
    tokens.extend(str(value) for value in profile.get("pytest_args") or [])
    workers = 1
    for index, token in enumerate(tokens):
        if token in {"-n", "--numprocesses"} and index + 1 < len(tokens):
            try:
                workers = max(1, int(tokens[index + 1]))
            except ValueError:
                workers = (os.cpu_count() or 1) if tokens[index + 1] == "auto" else 1
        elif token.startswith("-n") and token != "-n":
            try:
                workers = max(1, int(token[2:].removeprefix("=")))
            except ValueError:
                pass
        elif token.startswith("--numprocesses="):
            try:
                workers = max(1, int(token.split("=", 1)[1]))
            except ValueError:
                pass
    return workers


def requested_resources(repo: Path, profile: dict[str, Any], mode: str) -> dict[str, int]:
    """Return honest machine-wide slot weights for one pytest process."""
    configured = profile.get("resources") or {}
    if not isinstance(configured, dict):
        raise ValueError("profile resources must be an object")
    resources: dict[str, int] = {}
    for name, amount in configured.items():
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 1:
            raise ValueError(f"profile resource {name!r} must be a positive integer")
        resources[str(name)] = amount
    if "tests" not in resources:
        resources["tests"] = pytest_parallelism(repo, profile)
    if mode == "collect":
        resources.pop("browsers", None)
    return resources
