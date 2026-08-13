"""Capability host-install runner.

Implements the runner loop of the Capability Host-Install Runner protocol
(graph://149705db-a39). A capability declares a ``host_install`` block on
its ``agents/capabilities/<impl>/manifest.json``; this module walks those
manifests, fingerprints each install's *intent* files, and executes the
install command once per host per fingerprint change, tracking the result
in the ``dashboard.capability.host_install_state#1`` Setting.

The runner is host-side substrate maintenance: it spawns arbitrary
commands on the host and populates the capability's dependency tree inside
``package_root``. It is deliberately *not* a build system — it just spawns
the ecosystem tooling the capability declares and records what happened.

CLI surface: ``graph capability host-install [<impl>]`` (see
:func:`tools.graph.capability_cmd.cmd_host_install`).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

# Repo root: tools/graph/capability_host_install.py → parents[2] is the repo.
_REPO_ROOT = Path(__file__).resolve().parents[2]

STATE_SET_ID = "dashboard.capability.host_install_state"
STATE_REVISION = 1

# Protocol defaults for optional host_install fields.
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_SUCCESS_MARKER = "node_modules"

# Last 4 KB of stdout/stderr, per the protocol's State Setting shape.
TAIL_BYTES = 4096

# The runner's own version string, recorded on every state row for
# forensics. Bump when the runner's observable behaviour changes.
RUNNER_VERSION = "1.0"

_CAPABILITIES_DIRNAME = "agents/capabilities"


# ── fingerprinting ───────────────────────────────────────────


def compute_fingerprint(
    repo_root: Path, fingerprint_files: list[str]
) -> tuple[str | None, list[str]]:
    """Hash the concatenation of every ``fingerprint_files`` entry's bytes.

    Returns ``(hex_digest, [])`` when every listed file is present, or
    ``(None, missing)`` when one or more are absent — the runner treats a
    missing intent file as ``state=unknown`` and skips (protocol § Failure
    modes: "``fingerprint_files`` missing on disk").

    The hash mixes each file's repo-relative path in as a domain separator
    so two files swapping contents cannot collide to the same digest.
    """
    h = hashlib.sha256()
    missing: list[str] = []
    for rel in fingerprint_files:
        p = repo_root / rel
        if not p.is_file():
            missing.append(rel)
            continue
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    if missing:
        return None, missing
    return h.hexdigest(), []


def _tail(text: str) -> str:
    """Return the last :data:`TAIL_BYTES` bytes of ``text`` (UTF-8 safe)."""
    if not text:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= TAIL_BYTES:
        return text
    return raw[-TAIL_BYTES:].decode("utf-8", errors="replace")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── manifest discovery ───────────────────────────────────────


def discover_impls(
    repo_root: Path, impl: str | None = None
) -> list[dict]:
    """Return capability impls declaring ``host_install``, in stable order.

    Each entry is ``{"name", "dir", "package_root", "host_install",
    "manifest_path"}``. When ``impl`` is given, restrict to the single
    matching impl — matched by full manifest name (``autonomy/video``),
    directory basename (``video``), or the name's trailing segment.

    Impls without a ``host_install`` block are skipped: a capability with
    no dependencies omits it (the jira / bare-script pattern).
    """
    caps_dir = repo_root / _CAPABILITIES_DIRNAME
    out: list[dict] = []
    if not caps_dir.is_dir():
        return out
    for manifest_path in sorted(caps_dir.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict):
            continue
        host_install = manifest.get("host_install")
        if not isinstance(host_install, dict):
            continue
        name = manifest.get("name") or manifest_path.parent.name
        dir_name = manifest_path.parent.name
        package_root = manifest.get("package_root") or (
            f"{_CAPABILITIES_DIRNAME}/{dir_name}"
        )
        entry = {
            "name": name,
            "dir": dir_name,
            "package_root": package_root,
            "host_install": host_install,
            "manifest_path": manifest_path,
        }
        if impl is not None and impl not in (
            name, dir_name, str(name).rsplit("/", 1)[-1]
        ):
            continue
        out.append(entry)
    out.sort(key=lambda e: e["name"])
    return out


# ── per-impl filesystem lock ─────────────────────────────────


@contextmanager
def _impl_lock(lock_path: Path) -> Iterator[bool]:
    """Take an exclusive, non-blocking flock at ``lock_path``.

    Yields ``True`` when the lock is held (and releases on exit), ``False``
    when another runner already holds it — the caller logs and skips
    rather than waiting (protocol § Failure modes: "Concurrent runs").
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


# ── state Setting I/O ────────────────────────────────────────


def _read_state(client: Any, org: Any, name: str) -> dict | None:
    """Return the current ``host_install_state`` payload for ``name``."""
    members = client.read_set(STATE_SET_ID, org=org)
    member_list = getattr(members, "members", members)
    for m in member_list:
        mkey = m.get("key") if isinstance(m, dict) else getattr(m, "key", None)
        if mkey == name:
            payload = (
                m.get("payload") if isinstance(m, dict)
                else getattr(m, "payload", None)
            )
            return payload or {}
    return None


def _write_state(client: Any, org: Any, name: str, payload: dict) -> None:
    """Upsert the single evolving state row for ``name``.

    Prefers ``upsert_by_key`` when the client exposes it (the host-direct
    ``ops`` path) so a re-run updates the same base row in place; falls
    back to ``add_setting`` (the HttpClient path, where the dashboard
    server upserts by key server-side and fires ``setting.changed``).
    """
    upsert = getattr(client, "upsert_by_key", None)
    if upsert is not None:
        upsert(STATE_SET_ID, STATE_REVISION, name, payload, org=org, state="raw")
    else:
        client.add_setting(
            STATE_SET_ID, STATE_REVISION, name, payload, org=org, state="raw"
        )


# ── the runner loop ──────────────────────────────────────────


def _run_one(
    impl: dict,
    *,
    repo_root: Path,
    client: Any,
    org: Any,
    runner_version: str,
    now_fn: Callable[[], str],
    log_fn: Callable[[str], None],
) -> dict:
    name = impl["name"]
    hi = impl["host_install"]
    package_root = repo_root / impl["package_root"]
    fingerprint_files = hi["fingerprint_files"]

    fingerprint, missing = compute_fingerprint(repo_root, fingerprint_files)
    prior = _read_state(client, org, name) or {}
    prior_fp = prior.get("last_fingerprint")
    prior_succeeded = prior.get("last_succeeded_at")

    def _carry(payload: dict) -> dict:
        # Preserve the last-successful fingerprint / timestamp across a
        # non-success write so retries stay armed (protocol steps 5 + the
        # unknown case): a failed / unknown row must NOT advertise the new
        # intent as installed.
        if prior_fp:
            payload.setdefault("last_fingerprint", prior_fp)
        if prior_succeeded:
            payload.setdefault("last_succeeded_at", prior_succeeded)
        return payload

    # Failure mode: intent files absent → unknown, skip until they appear.
    if fingerprint is None:
        log_fn(f"⚠ {name}: fingerprint file(s) missing {missing}; skipping")
        _write_state(client, org, name, _carry({
            "state": "unknown",
            "last_attempted_at": now_fn(),
            "runner_version": runner_version,
        }))
        return {"name": name, "outcome": "unknown", "missing": missing}

    success_marker = hi.get("success_marker", DEFAULT_SUCCESS_MARKER)
    marker_path = package_root / success_marker

    # Skip when current: fingerprint matches AND success_marker present.
    if prior_fp == fingerprint and marker_path.exists():
        log_fn(f"✓ {name}: current (fingerprint match); skipping")
        return {
            "name": name, "outcome": "skipped", "fingerprint": fingerprint,
        }

    lock_path = package_root / ".host_install.lock"
    with _impl_lock(lock_path) as acquired:
        if not acquired:
            log_fn(f"⧗ {name}: another runner holds the lock; skipping")
            return {"name": name, "outcome": "locked"}

        cwd = repo_root / hi.get("cwd", impl["package_root"])
        env = dict(os.environ)
        env.update(hi.get("env", {}))
        timeout = hi.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
        command = list(hi["command"])
        attempted_at = now_fn()

        log_fn(f"▸ {name}: installing ({' '.join(command)})")
        timed_out = False
        try:
            proc = subprocess.run(
                command, cwd=str(cwd), env=env, timeout=timeout,
                capture_output=True, text=True,
            )
            exit_code = proc.returncode
            stdout, stderr = proc.stdout or "", proc.stderr or ""
        except subprocess.TimeoutExpired as exc:
            exit_code = -1
            timed_out = True
            stdout = exc.stdout or "" if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "" if isinstance(exc.stderr, str) else "")
            stderr += f"\n[runner] timed out after {timeout}s"
        except FileNotFoundError as exc:
            exit_code = 127
            stdout, stderr = "", f"[runner] command not found: {exc}"

        marker_present = marker_path.exists()

        if exit_code == 0 and marker_present and not timed_out:
            log_fn(f"✓ {name}: installed (fingerprint {fingerprint[:12]})")
            _write_state(client, org, name, {
                "state": "ready",
                "last_fingerprint": fingerprint,
                "last_attempted_at": attempted_at,
                "last_succeeded_at": now_fn(),
                "last_exit_code": 0,
                "stdout_tail": _tail(stdout),
                "runner_version": runner_version,
            })
            return {
                "name": name, "outcome": "ready",
                "fingerprint": fingerprint, "exit_code": 0,
            }

        # Failure: non-zero exit, timeout, or missing success_marker after
        # exit 0 (the command "lied about its outcome"). Do NOT update
        # last_fingerprint — the next run retries the same install.
        if exit_code == 0 and not marker_present:
            stderr += (
                f"\n[runner] success_marker missing after exit 0: "
                f"{success_marker}"
            )
        log_fn(f"✗ {name}: install failed (exit {exit_code})")
        payload = _carry({
            "state": "failed",
            "last_attempted_at": attempted_at,
            "last_exit_code": exit_code,
            "stderr_tail": _tail(stderr),
            "runner_version": runner_version,
        })
        if stdout:
            payload["stdout_tail"] = _tail(stdout)
        _write_state(client, org, name, payload)
        return {"name": name, "outcome": "failed", "exit_code": exit_code}


def run(
    impl: str | None = None,
    *,
    repo_root: Path | None = None,
    client: Any = None,
    org: Any = None,
    runner_version: str = RUNNER_VERSION,
    now_fn: Callable[[], str] | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> list[dict]:
    """Run the host-install loop over impls declaring ``host_install``.

    ``impl`` restricts the run to one implementation (matched loosely — see
    :func:`discover_impls`). Returns a per-impl result list; failures in one
    impl never abort the others (protocol § "Failures in one impl don't
    block the others").
    """
    repo_root = Path(repo_root) if repo_root is not None else _REPO_ROOT
    if client is None:
        from .client import get_client
        client = get_client()
    if org is None:
        from . import ops
        org = ops.CALLER_ORG
    now_fn = now_fn or _now_iso
    log_fn = log_fn or (lambda _msg: None)

    impls = discover_impls(repo_root, impl)
    results: list[dict] = []
    for entry in impls:
        try:
            results.append(_run_one(
                entry,
                repo_root=repo_root,
                client=client,
                org=org,
                runner_version=runner_version,
                now_fn=now_fn,
                log_fn=log_fn,
            ))
        except Exception as exc:  # noqa: BLE001 — one impl's crash is isolated
            log_fn(f"✗ {entry['name']}: runner error: {exc}")
            results.append({
                "name": entry["name"], "outcome": "error", "error": str(exc),
            })
    return results
