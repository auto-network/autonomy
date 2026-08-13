"""Layer-0 harness bootstrap: deterministic discover / verify / record.

The pre-agent part of clean-room install (bead auto-n130b). Runs on first
dashboard launch, before any session can be created, because the onboarding
agent itself needs a harness to run (chicken-and-egg). Everything here is
plain code — no agent — and idempotent: the machine state IS the progress, so
every entry re-probes fresh.

Three responsibilities:

1. DISCOVER — probe for an installed ``claude`` / ``codex`` CLI on PATH, parse
   ``--version``, and (when present) run one authenticated no-op to tell
   *installed-needs-sign-in* apart from *ready*.
2. RECORD — on a successful probe, upsert one ``autonomy.harness.bootstrap#1``
   row per harness carrying discovery results ONLY (slug, path, version, an
   ``auth`` flag, timestamp). Never tokens or credential material.
3. GATE — expose :func:`has_verified_harness` so the server can render the
   ``/bootstrap`` walkthrough instead of the session UI until one harness is
   verified (``auth == "ok"``).

Auth always stays inside the harness's own tooling. We never collect or proxy
credentials — sign-in is the harness's own command; we only observe whether a
no-op invocation succeeds.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any

from tools.graph import settings_ops
from tools.graph.schemas.harness_bootstrap import (
    HARNESS_BOOTSTRAP_ORG,
    SCHEMA_REVISION,
    SET_ID,
    VALID_HARNESSES,
)


# Per-harness command contract. ``version`` / ``noop`` are what we run to
# classify state; ``install`` / ``signin`` are the operator-facing guidance
# strings the walkthrough shows (we never run sign-in — it opens the harness's
# own OAuth flow and stays there).
HARNESS_SPECS: dict[str, dict[str, Any]] = {
    "claude": {
        "label": "Claude Code",
        "version_cmd": ["claude", "--version"],
        # One authenticated no-op: exit 0 proves the CLI runs AND is signed in.
        "noop_cmd": ["claude", "-p", "ok", "--max-turns", "1"],
        "install_cmd": "npm install -g @anthropic-ai/claude-code",
        "signin_cmd": "claude",
        "signin_how": "Run this in a terminal, type /login, and finish in your browser.",
    },
    "codex": {
        "label": "Codex",
        "version_cmd": ["codex", "--version"],
        "noop_cmd": ["codex", "exec", "--skip-git-repo-check", "ok"],
        "install_cmd": "npm install -g @openai/codex",
        "signin_cmd": "codex login",
        "signin_how": "Run this in a terminal and finish in your browser.",
    },
}

# States a harness can be in, most-bare first.
STATE_NOT_INSTALLED = "not-installed"
STATE_NEEDS_SIGN_IN = "installed-needs-sign-in"
STATE_READY = "ready"

_VERSION_TIMEOUT = 15
_NOOP_TIMEOUT = 90


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run(cmd: list[str], timeout: int) -> tuple[int, str]:
    """Run *cmd*, returning ``(exit_code, combined_output)``.

    Never raises for the caller: a missing binary or a timeout maps to a
    non-zero exit so classification stays a pure branch on the result.
    Isolated as a module function so tests can monkeypatch it without
    touching real CLIs.
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except Exception as exc:  # pragma: no cover - defensive
        return 1, str(exc)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out.strip()


def _which(slug: str) -> str | None:
    return shutil.which(slug)


def _parse_version(output: str) -> str | None:
    """Pull the first token that looks like a version out of ``--version``.

    ``claude --version`` prints e.g. ``1.2.3 (Claude Code)``; ``codex
    --version`` prints e.g. ``codex 0.4.0``. We keep the whole trimmed first
    line — it is the version string operators recognize — and only reject
    empty output.
    """
    if not output:
        return None
    first = output.splitlines()[0].strip()
    return first or None


def probe_harness(slug: str) -> dict[str, Any]:
    """Classify one harness on this host. Pure discovery — writes nothing.

    Returns a dict describing the current machine state for *slug*::

        {
          "harness": "claude",
          "label": "Claude Code",
          "present": bool,          # CLI resolves on PATH with a version
          "path": str | None,
          "version": str | None,
          "auth": "ok" | "missing" | None,   # None when not present
          "state": "not-installed" | "installed-needs-sign-in" | "ready",
          "install_cmd": str,
          "signin_cmd": str,
          "signin_how": str,   # one user-facing sentence for the sign-in step
        }
    """
    if slug not in HARNESS_SPECS:
        raise ValueError(f"unknown harness: {slug!r}")
    spec = HARNESS_SPECS[slug]
    result: dict[str, Any] = {
        "harness": slug,
        "label": spec["label"],
        "present": False,
        "path": None,
        "version": None,
        "auth": None,
        "state": STATE_NOT_INSTALLED,
        "install_cmd": spec["install_cmd"],
        "signin_cmd": spec["signin_cmd"],
        "signin_how": spec["signin_how"],
    }

    path = _which(slug)
    if not path:
        return result

    code, out = _run(spec["version_cmd"], _VERSION_TIMEOUT)
    version = _parse_version(out) if code == 0 else None
    if code != 0 or not version:
        # On PATH but --version failed: treat as not usably installed.
        return result

    result["present"] = True
    result["path"] = path
    result["version"] = version

    noop_code, _ = _run(spec["noop_cmd"], _NOOP_TIMEOUT)
    if noop_code == 0:
        result["auth"] = "ok"
        result["state"] = STATE_READY
    else:
        result["auth"] = "missing"
        result["state"] = STATE_NEEDS_SIGN_IN
    return result


def probe_all() -> dict[str, dict[str, Any]]:
    """Probe every known harness. ``{slug: probe_harness(slug)}``."""
    return {slug: probe_harness(slug) for slug in VALID_HARNESSES}


def record_discovery(
    slug: str,
    *,
    path: str,
    version: str,
    auth: str,
    verified_at: str | None = None,
) -> str:
    """Upsert the ``autonomy.harness.bootstrap#1`` row for *slug*.

    Discovery results ONLY — the payload never carries a token or any
    credential material. Returns the (stable) setting id.
    """
    payload = {
        "harness": slug,
        "path": path,
        "version": version,
        "auth": auth,
        "verified_at": verified_at or _now_iso(),
    }
    return settings_ops.upsert_by_key(
        SET_ID,
        SCHEMA_REVISION,
        slug,
        payload,
        org=HARNESS_BOOTSTRAP_ORG,
        state="raw",
    )


def verify_and_record(slug: str) -> dict[str, Any]:
    """Probe *slug* and, when the CLI is present, record its discovery row.

    A present harness is recorded whether or not it is signed in (``auth`` is
    ``ok`` or ``missing``) so the record reflects reality; a ``not-installed``
    result records nothing. The returned dict is the probe result plus a
    ``recorded`` flag.
    """
    result = probe_harness(slug)
    if result["present"] and result["auth"] in ("ok", "missing"):
        record_discovery(
            slug,
            path=result["path"],
            version=result["version"],
            auth=result["auth"],
            verified_at=result.setdefault("verified_at", _now_iso()),
        )
        result["recorded"] = True
    else:
        result["recorded"] = False
    return result


def recorded_rows() -> list[dict[str, Any]]:
    """Return the resolved ``autonomy.harness.bootstrap#1`` payloads."""
    members = settings_ops.read_set(SET_ID, org=HARNESS_BOOTSTRAP_ORG)
    rows: list[dict[str, Any]] = []
    for member in members:
        payload = member.payload
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def has_verified_harness() -> bool:
    """True when at least one recorded harness has ``auth == "ok"``.

    This is the server-side gate: while it is False, ``/bootstrap`` renders
    instead of the session UI. It reads the machine's recorded state and does
    NOT re-probe — the flow records a row only after a live verification, so a
    stale ``ok`` row is the operator's own prior successful bootstrap.
    """
    return any(row.get("auth") == "ok" for row in recorded_rows())
