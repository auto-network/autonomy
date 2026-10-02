"""Unified session launcher for all agent containers.

All four launch paths (dispatch, librarian, chatwith, terminal) go through
launch_session(), which handles:
- Credential resolution (one implementation)
- Default volume mounts: platform snapshot (ro), data/uploads (ro),
  .beads (rw, credential key masked), per-run sessions dir
- Session directory creation
- Writing .session_meta.json with type + metadata + timestamp
- Building and executing the docker run command
- Credential cleanup after container exits

Returns container_id (detach=True) or docker command string (detach=False),
or None on failure.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import secrets
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from agents.env_sources import (
    parse_capability_env_source,
    parse_workspace_env_source,
)
from tools.data_paths import DATA_ROOT, HOST_HOME_MOUNT

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IMAGE = "autonomy-session-platform"
#: The operator's in-node host terminal (graph://89d3c8df-544 §2-3).
HOST_TERMINAL_IMAGE = "autonomy-host-terminal"
HOST_DOCKER_SOCKET = "/var/run/docker.sock"

DEFAULT_OPUS_MODEL = "claude-opus-4-8[1m]"

#: Every harness a session can run. The launcher, the CLI, the workspace
#: schema and the dashboard all validate against the same tuple.
SUPPORTED_HARNESSES = ("claude", "codex", "grok")

# ── Grok Build (xAI) ─────────────────────────────────────────────────────────
# The third harness. Grok keeps its state root under $GROK_HOME (transcripts in
# sessions/, the folder-trust store, the stored sign-in); the launcher binds
# the session's run_dir/sessions there so the dashboard tails the ACP
# ``updates.jsonl`` exactly as it tails a Claude JSONL or a Codex rollout.
#
# Where Grok's requests go is a workspace decision expressed in its ``env``:
#   * ``XAI_API_KEY`` (usually ``credential:<key>``) — first-party xAI. When a
#     workspace sets nothing, the launcher offers the operator's vault row
#     ``grok.api-key`` (audited vault, a ROW not a schema: see
#     tools/graph/schemas/vault_credential.py).
#   * ``GROK_GATEWAY_BASE_URL`` + ``GROK_GATEWAY_API_KEY`` (+ optional
#     ``GROK_GATEWAY_MODELS`` comma list) — any OpenAI-compatible endpoint
#     (OpenRouter, a corporate gateway). Grok Build refuses to start without a
#     stored sign-in or a first-party key, so gateway sessions mint the stored
#     sign-in through Grok's external-auth-provider contract: the image ships
#     ``autonomy-grok-auth``, which prints the gateway key, and the launch runs
#     ``grok login`` once before the TUI. Verified live 2026-09-22 (v1.0.40).
GROK_HOME = "/home/agent/.grok"
GROK_AUTH_SHIM = "/usr/local/bin/autonomy-grok-auth"
GROK_API_KEY_ENV = "XAI_API_KEY"
GROK_VAULT_KEY = "grok.api-key"
GROK_GATEWAY_BASE_URL_ENV = "GROK_GATEWAY_BASE_URL"
GROK_GATEWAY_API_KEY_ENV = "GROK_GATEWAY_API_KEY"
GROK_GATEWAY_MODELS_ENV = "GROK_GATEWAY_MODELS"
GROK_GATEWAY_CONTEXT_WINDOW_ENV = "GROK_GATEWAY_CONTEXT_WINDOW"
GROK_GATEWAY_DEFAULT_CONTEXT_WINDOW = 500_000
GROK_CONFIG_FILENAME = "grok-config.toml"


# ── Capability materialization ────────────────────────────────────────────────


CAPABILITY_BIN_DIR = "/etc/autonomy/cap-bin"


def _capability_mounts(capabilities) -> dict[str, str]:
    """Return ``{host_path: container_spec}`` for every enabled capability.

    For each :class:`MaterializedCapability`:

    * the package root mounts read-only at ``mount_target`` (deterministic
      ``/opt/autonomy/capabilities/<impl-slug>``). For ``image_baked``
      delivery the binary is already in the image, but the package mount
      keeps ``SKILL.md`` / ``primer.md`` / bundled scripts inspectable
      from inside the container at the same canonical path used for
      ``mounted_tools`` — agents do not need to know which delivery mode
      was chosen.
    * declared repo-local ``tool_paths`` must live inside the package root and
      are already covered by its mount. An external path is refused before a
      docker command is built: projecting it below the read-only package mount
      requires runc to create a child mountpoint inside that read-only tree.
      External bundles use ``tool_target`` and its explicit non-nested target.
    * a ``tool_target`` (when set) mounts the declared repo-local
      ``source`` at the absolute container ``target`` — the Jira-style
      ``/opt/jira-tools`` runtime location described in
      graph://86e04207-a25 § Example 2. The shim directory exposing
      ``expose_commands`` is materialized separately (see
      :func:`_capability_command_surface`).
    * every ``secret_file_bindings`` entry mounts the host source at the
      declared container path read-only. Secret values stay file-based —
      they are never injected as env vars per the runtime model
      (graph://86e04207-a25 § Runtime materialization).
    """
    mounts: dict[str, str] = {}
    for cap in capabilities:
        package_host = REPO_ROOT / cap.package_root
        mounts[str(package_host)] = f"{cap.mount_target}:ro"
        for tool_path in cap.tool_paths:
            tool_host = REPO_ROOT / tool_path
            # Subpaths are already covered by the package-root mount.  Anything
            # else is an invalid legacy Setting that predates the schema guard;
            # refuse it here too so it cannot reach docker/runc.
            try:
                tool_host.relative_to(package_host)
                continue
            except ValueError:
                raise ValueError(
                    f"capability {cap.implementation}: tool_path {tool_path!r} "
                    f"is outside package_root {cap.package_root!r}; use "
                    "tool_target with an explicit non-nested container target"
                )
        if cap.tool_target is not None:
            tt = cap.tool_target
            tt_host = REPO_ROOT / tt.source
            mounts[str(tt_host)] = f"{tt.target}:ro"
        for container_path, host_source in cap.secret_file_bindings.items():
            mounts[host_source] = f"{container_path}:ro"
    return mounts


def _capability_command_surface(
    capabilities, run_dir: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    """Materialize PATH-visible command shims for every ``tool_target``.

    For each capability that declares ``tool_target.expose_commands`` the
    launcher writes a tiny POSIX shim script under ``<run_dir>/cap-bin/``
    that ``exec``s the matching binary at the declared absolute target
    (``/opt/jira-tools/jira-read`` etc.). The shim directory is then
    bind-mounted at :data:`CAPABILITY_BIN_DIR` and exposed via the
    ``AUTONOMY_CAPABILITY_BIN`` env var so the container image can prepend
    it to ``PATH``.

    The surface also carries the universal ``bd`` safety gate, so it is
    mounted even when no capability declares an ``expose_commands`` list.
    """
    # Command names share one shim directory, so collisions are possible in
    # principle. First capability wins (input is contract-sorted, so the
    # outcome is deterministic); the loser is logged, never silently
    # overwritten.
    shims_to_emit: dict[str, str] = {}
    for cap in capabilities:
        tt = getattr(cap, "tool_target", None)
        if tt is None:
            continue
        for cmd in tt.expose_commands:
            if cmd in shims_to_emit:
                logger.warning(
                    "capability %s: command %r already exposed by another "
                    "capability (-> %s); keeping the first shim",
                    cap.implementation, cmd, shims_to_emit[cmd])
                continue
            shims_to_emit[cmd] = f"{tt.target}/{cmd}"
    shim_dir = run_dir / "cap-bin"
    shim_dir.mkdir(parents=True, exist_ok=True)

    for cmd, exec_target in shims_to_emit.items():
        shim_path = shim_dir / cmd
        shim_path.write_text(
            f'#!/bin/sh\nexec {exec_target} "$@"\n'
        )
        # 0755 — executable for everyone so the agent user can invoke it.
        shim_path.chmod(0o755)
    # The golden-rule close gate (auto-w41na) rides every session's cap-bin,
    # not just capability opt-ins: the gate script's CONTENT is copied in
    # (never an exec-wrapper — a wrapper's $0 would differ from cap-bin/bd
    # and the shim's self-location would loop), so `bd` fleet-wide refuses
    # to close runtime-critical work without a functional-proof reference.
    gate_src = Path(__file__).resolve().parents[1] / "tools" / "beads" / "bd"
    if gate_src.is_file():
        gate_path = shim_dir / "bd"
        gate_path.write_text(gate_src.read_text(encoding="utf-8"))
        gate_path.chmod(0o755)
    return (
        {str(shim_dir): f"{CAPABILITY_BIN_DIR}:ro"},
        {"AUTONOMY_CAPABILITY_BIN": CAPABILITY_BIN_DIR},
    )


# Claude Code only loads a skill whose SKILL.md opens with a frontmatter block
# carrying at least these keys, and whose directory name is a simple slug.
_SKILL_REQUIRED_FRONTMATTER = ("name", "description")
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _skill_frontmatter(text: str) -> dict[str, str] | None:
    """Parse the leading ``---`` frontmatter block of a SKILL.md into a flat
    key/value map. Returns None when the block is absent or unterminated.
    Flat ``key: value`` lines only — enough to validate the harness contract
    without a YAML dependency."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    fm: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fm
        key, sep, value = line.partition(":")
        if sep:
            fm[key.strip()] = value.strip()
    return None


def _capability_skill_surface(capabilities, run_dir: Path, harness: str) -> dict[str, str]:
    """Install each enabled capability's SKILL.md where the harness actually
    discovers skills.

    Claude Code scans BOTH ``<cwd>/.claude/skills/`` (project) and
    ``~/.claude/skills/`` (personal) at startup; a personal skill is available
    regardless of the working directory. We copy each valid skill under
    ``<run_dir>/cap-skills/<name>/`` and bind-mount it into the personal dir
    ``/home/agent/.claude/skills/<name>`` because that is writable and
    cwd-independent on every workspace — including ones whose working dir is a
    read-only mount (Operator: cwd=/workspace/repo:ro) or simply isn't
    /workspace/repo (widgets-ng: cwd=/workspace/widgets_ng). The old
    /workspace/repo hardcode both failed on those (read-only mkdir aborts the
    whole container launch) and, even where it mounted, sat outside the
    harness's cwd so the skill was never discovered. /home/agent/.claude is
    container-local (only its ``projects`` and ``CLAUDE.md`` are host-bound),
    so nesting here never touches the operator's real home. The skill must open
    with frontmatter carrying ``name`` +
    ``description`` and the name must be a plain slug — a SKILL.md missing
    either is skipped with a warning, because projecting it would look
    installed while the harness silently never loads it.

    Codex projection is a deliberate gap for now: its skills directory is a
    bind of the host user's home, and nesting mounts there would create
    directories in the operator's real home.

    Grok Build scans ``~/.claude/skills`` too (its Claude-compatibility
    layer is on by default — verified with ``grok inspect`` 2026-09-22), so
    the same personal-dir projection serves it unchanged.
    """
    if harness not in ("claude", "grok"):
        return {}
    mounts: dict[str, str] = {}
    for cap in capabilities:
        if not cap.skill_path:
            continue
        src = REPO_ROOT / cap.skill_path
        if not src.is_file():
            continue
        text = src.read_text()
        fm = _skill_frontmatter(text) or {}
        name = fm.get("name", "")
        missing = [k for k in _SKILL_REQUIRED_FRONTMATTER if not fm.get(k)]
        if missing:
            logger.warning(
                "capability %s: SKILL.md at %s lacks frontmatter key(s) %s; "
                "not installed as a skill",
                cap.implementation, cap.skill_path, ", ".join(missing))
            continue
        if not _SKILL_NAME_RE.match(name):
            logger.warning(
                "capability %s: skill name %r is not a plain slug "
                "([a-z0-9-]); not installed as a skill",
                cap.implementation, name)
            continue
        dst_dir = run_dir / "cap-skills" / name
        dst_dir.mkdir(parents=True, exist_ok=True)
        org_primer = getattr(cap, "org_primer", "").strip()
        if org_primer:
            text = (
                f"{text.rstrip()}\n\n## Organization-specific guidance\n\n"
                f"{org_primer}\n"
            )
        (dst_dir / "SKILL.md").write_text(text)
        mounts[str(dst_dir)] = f"/home/agent/.claude/skills/{name}:ro"
    return mounts


def _resolve_env_source(env_name: str, source: str) -> str | None:
    """Resolve a single ``env_bindings`` source identifier to a literal value.

    Supported source schemes (graph://86e04207-a25 § Runtime
    materialization, "today the idea was you could forward it from the
    host env directly, or a file source would be <file> + <var_name>"):

    * ``host:VAR_NAME`` — read ``os.environ["VAR_NAME"]`` at launch
      time. Drops the binding if the host env var is unset.
    * ``file:/abs/path:VAR_NAME`` — parse the file as dotenv-style
      ``KEY=VALUE`` pairs (``#`` comments and blank lines skipped;
      values may be optionally double- or single-quoted) and return
      ``VAR_NAME``'s value. Drops the binding if the file is missing,
      unreadable, or the var is absent.
    * any other string — passed through verbatim. Keeps test fixtures
      and any direct-literal callers working without migration; a
      future vault-backed scheme will register here.

    Returns ``None`` when the binding can't be resolved, in which case
    the caller drops it. The capability's runtime probe (e.g.
    ``probe_v1`` for autonomy/github) then surfaces the missing env
    via ``state=degraded reason=env_missing`` — the operator gets the
    diagnostic without us having to fail the launch.
    """
    parsed = parse_capability_env_source(source)
    if parsed.kind == "host":
        var = parsed.variable
        if not parsed.valid:
            logger.warning(
                "capability env %s: malformed host source %r (empty var name)",
                env_name, source,
            )
            return None
        value = os.environ.get(var)
        if value is None:
            logger.info(
                "capability env %s: host env %r unset; binding dropped",
                env_name, var,
            )
            return None
        return value

    if parsed.kind == "file":
        path_str, var = parsed.locator, parsed.variable
        if not parsed.valid:
            if parsed.error == "missing_separator":
                logger.warning(
                    "capability env %s: malformed file source %r (expected "
                    "file:/path:VAR_NAME)", env_name, source,
                )
            else:
                logger.warning(
                    "capability env %s: malformed file source %r", env_name, source,
                )
            return None
        path = Path(path_str)
        if not path.is_file():
            logger.info(
                "capability env %s: file %s missing; binding dropped",
                env_name, path,
            )
            return None
        try:
            text = path.read_text()
        except OSError:
            logger.exception(
                "capability env %s: failed reading %s", env_name, path,
            )
            return None
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                continue
            key, val = stripped.split("=", 1)
            if key.strip() != var:
                continue
            val = val.strip()
            # Strip a single matching pair of surrounding quotes.
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            return val
        logger.info(
            "capability env %s: var %r not in %s; binding dropped",
            env_name, var, path,
        )
        return None

    if parsed.kind == "credential":
        key = parsed.locator
        if not parsed.valid:
            logger.warning(
                "capability env %s: malformed credential source %r (empty key)",
                env_name, source,
            )
            return None
        value = _resolve_credential(key)
        if value is None:
            # Vault locked, key absent, or release refused. Drop the binding —
            # the capability probe then surfaces `degraded reason=env_missing`,
            # and we NEVER fall through to a wrong or empty value. Logged by KEY
            # only; the value is never logged and never mentioned.
            logger.info(
                "capability env %s: credential %r unavailable; binding dropped",
                env_name, key,
            )
            return None
        return value

    # Plain literal value — backward compat with test fixtures and any
    # caller that hasn't migrated to the source-scheme syntax.
    return parsed.literal


def _resolve_credential(key: str) -> str | None:
    """Open a vault-sealed credential by *key* at launch and return its plaintext
    value, or None if the vault is locked, the key is absent, or the release is
    refused — the fail-closed cases the caller drops the binding on.

    NEVER logs, formats, or re-raises the value. The plaintext exists only as
    this return value, which the launcher places directly into the container's
    ``-e`` arg; nothing else holds it, so a traceback or logging middleware
    cannot serialize a secret.
    """
    from tools.graph import ops as _ops, settings_ops as _settings_ops
    from tools.graph.schemas.vault_credential import VAULT_AUDITED_SET_ID

    # Fail CLOSED on a cold vault. With no key holder registered no decryption
    # is possible, so read_set cannot return plaintext — and this must NOT be
    # mistaken for "credential not configured" (auto-0815). The launch-time
    # preflight refuses the launch by name on this state; here we only ensure no
    # wrong value flows, logging it as a vault state and never as a missing key.
    if getattr(_settings_ops, "_vault_key_holder", None) is None:
        logger.error(
            "credential %r: vault key holder not registered (vault cold); "
            "cannot decrypt", key,
        )
        return None

    # The audited set is @home("personal"); org=None routes to personal.db
    # regardless of the acting org (required, not a default — auto-0815).
    try:
        members = _ops.read_set(VAULT_AUDITED_SET_ID, org=None, peers=[])
    except Exception:
        logger.exception("credential %r: vault set read failed", key)
        return None

    for row in getattr(members, "members", []) or []:
        if getattr(row, "key", None) != key:
            continue
        if getattr(row, "vault_error", None) is not None:
            # Row exists but did not open (e.g. VAULT_NO_KEY_HOLDER) — a vault
            # state, fail closed; never the value, never confused with absence.
            logger.error(
                "credential %r: vault open failed (%s)", key,
                getattr(row.vault_error, "reason", "unknown"),
            )
            return None
        payload = getattr(row, "payload", None)
        if isinstance(payload, dict):
            val = payload.get("value")
            if isinstance(val, str) and val:
                return val
        return None  # row present but malformed -> fail closed
    return None  # key genuinely absent -> "not configured", caller drops binding


def _capability_env(capabilities) -> dict[str, str]:
    """Return ``{env_name: value}`` for non-secret capability env bindings.

    Pulls from each capability's ``env_bindings`` (the org install's
    declared non-secret env), resolving each value via
    :func:`_resolve_env_source`. Bindings that fail to resolve are
    dropped — the capability's probe surfaces the missing env so the
    Dashboard can show ``state=degraded reason=env_missing`` rather
    than the launcher hard-failing the session.

    Secret values are deliberately excluded from this path — they go
    through ``secret_file_bindings`` and surface as file mounts, not
    env vars (graph://86e04207-a25 § Runtime materialization).
    """
    out: dict[str, str] = {}
    for cap in capabilities:
        for env_name, source in cap.env_bindings.items():
            value = _resolve_env_source(env_name, source)
            if value is not None:
                out[env_name] = value
    return out


def _declared_credential_keys(capabilities) -> set:
    """The vault credential keys any capability's ``env_bindings`` declare via a
    ``credential:<key>`` source. The launch preflight uses this to refuse a
    launch by name on a COLD vault — a launch that needs a vault credential must
    fail loudly ("cannot read"), not silently drop the binding as if the
    credential were "not configured"."""
    keys: set = set()
    for cap in capabilities:
        for _env_name, source in getattr(cap, "env_bindings", {}).items():
            if isinstance(source, str):
                parsed = parse_capability_env_source(source)
                if parsed.kind == "credential" and parsed.valid:
                    keys.add(parsed.locator)
    return keys


def _credential_keys_in_env(env_mapping) -> set:
    """The vault credential keys a plain env mapping (a workspace's ``env`` /
    ``extra_env``) declares via a ``credential:<key>`` value. A client organization's
    workspaces deliver their GitHub tokens through workspace ``env`` today; the
    migration rewrites those values in place to ``credential:<key>``, so the
    launch path resolves them here (ONLY the credential: scheme — every other
    value stays the literal it has always been). Mirrors
    :func:`_declared_credential_keys` so the same vault-cold preflight refuses a
    credential-bearing launch by name rather than dropping the token silently."""
    keys: set = set()
    for _name, source in (env_mapping or {}).items():
        if isinstance(source, str):
            parsed = parse_workspace_env_source(source)
            if parsed.kind == "credential" and parsed.valid:
                keys.add(parsed.locator)
    return keys


# ── Credential Resolution ─────────────────────────────────────────────────────


def _credentials_org() -> str:
    """Return the substrate org for Claude credential/usage rows: ``personal``.

    These are host-local, per-instance secrets — never shared across orgs
    (each autonomy install has its own accounts). They live in ``personal.db``
    and are read with ``peers=[]`` (see the callers) so no other org DB is
    consulted. Pinned to ``personal`` regardless of ambient ``GRAPH_ORG`` so the
    launcher, refresh poller, usage poller, UI, and ``graph claude`` CLI all
    converge on the same local rows.
    """
    return "personal"


def _accounts(harness: str) -> list[Any]:
    """Every account of *harness* in the operator's vault (record v16 §10.9).

    A node whose dashboard reloaded this code without restarting has not run
    the startup migration yet; when the vault holds no account of the
    harness, the pre-vault rows are migrated here, once, so a launch never
    fails for want of a restart.
    """
    from tools.graph import harness_credentials as hv
    accounts = hv.list_accounts(harness)
    if not accounts:
        try:
            migrated = hv.migrate_plaintext_accounts()
        except Exception:
            logger.exception("session_launcher: pre-vault account migration failed")
            migrated = {}
        if migrated.get("deprecated"):
            logger.info("session_launcher: migrated pre-vault rows %s", migrated)
            accounts = hv.list_accounts(harness)
    return accounts


def _claude_accounts() -> list[Any]:
    """Every launchable Claude account: a fresh setup token, or an OAuth
    bundle the launcher can write into the container's credentials file."""
    return [a for a in _accounts("claude") if a.launchable]


def _claude_usage_rows() -> list[dict]:
    """Return live ``dashboard.harness.usage`` rows for Claude.

    Best-effort — failures (DB unavailable, settings substrate missing,
    etc.) collapse to ``[]`` so token selection falls back to random
    pick rather than crashing the launch path.
    """
    try:
        from tools.graph import settings_ops
        from tools.dashboard import harness_usage_settings as hus
    except Exception:
        return []
    try:
        members = settings_ops.read_set(
            hus.HARNESS_USAGE_SET_ID, org=_credentials_org(), peers=[],
        )
    except Exception:
        return []
    payloads: list[dict] = []
    for member in members.members:
        payload = member.payload
        if not isinstance(payload, dict):
            continue
        if (payload.get("harness") or "").lower() != "claude":
            continue
        payloads.append(payload)
    return payloads


def _token_headroom_score(payload: dict) -> float:
    """Return min(short headroom, long headroom) for max-min ranking.

    A token's score is the smaller of its two window headrooms — the
    bottleneck. Picking ``max(score)`` is the max-min rule from the
    design note: choose the credential with the most slack on its
    tightest window.
    """
    windows = payload.get("windows") if isinstance(payload, dict) else None
    if not isinstance(windows, dict):
        return 0.0
    short = windows.get("short") if isinstance(windows.get("short"), dict) else {}
    long_ = windows.get("long") if isinstance(windows.get("long"), dict) else {}

    def _headroom(window: dict) -> float:
        used = window.get("used_percent")
        if not isinstance(used, (int, float)):
            return 0.0
        return max(0.0, 100.0 - float(used))

    return min(_headroom(short), _headroom(long_))


def _is_usage_stale(payload: dict, *, now: datetime) -> bool:
    """``True`` when the harness-usage row is older than the schema's TTL.

    The schema's TTL is 15 minutes; rows older than that are considered
    stale enough to fall through to the random-pick branch even though
    cache_gc hasn't swept them yet. Missing / malformed ``updated_at``
    counts as stale (we don't trust unrecoverable telemetry).
    """
    from tools.dashboard.harness_usage_settings import HARNESS_USAGE_CACHE_TTL

    raw = payload.get("updated_at")
    if not isinstance(raw, str) or not raw.strip():
        return True
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    return (now - ts) > HARNESS_USAGE_CACHE_TTL


def _reading_window_open(payload: dict, *, now: Any) -> bool:
    """Whether a stored reading's own window has not reset yet."""
    from tools.dashboard.harness_usage_settings import reading_still_valid
    return reading_still_valid(payload, now_epoch=int(now.timestamp()))


def _usage_exhausted(payload: Any, *, now: Any) -> bool:
    """Whether a still-valid reading says this account has no headroom."""
    from tools.dashboard.harness_usage_settings import is_exhausted
    return is_exhausted(payload, now_epoch=int(now.timestamp()))


def _is_usage_usable(payload: dict) -> bool:
    """``True`` only when the row carries real, usable headroom telemetry.

    A *failed* usage poll still writes a fresh row (``status='unavailable'``,
    empty ``windows``, the error in ``note``) so the dashboard can show the
    account as degraded instead of dropping it. For token selection that state
    is **usage-unknown**, not **zero-headroom**: counting it as fresh telemetry
    lets its 0 score lose the max-min comparison every time and silently
    starves the account. Excluding it makes the "all tokens have fresh usage"
    gate fail, so an unknown-usage account falls through to the random branch
    and is still selected with real probability.
    """
    if not isinstance(payload, dict):
        return False
    if payload.get("status") not in (None, "ok"):
        return False
    windows = payload.get("windows")
    if not isinstance(windows, dict) or not windows:
        return False
    return any(
        isinstance(w, dict) and isinstance(w.get("used_percent"), (int, float))
        for w in windows.values()
    )


def _reading_snapshot(payload: Any) -> dict | None:
    """The non-secret part of a usage reading a selection was made on: when
    it was taken, its status, and each window's used_percent and resets_at."""
    if not isinstance(payload, dict):
        return None
    windows = payload.get("windows") if isinstance(payload.get("windows"), dict) else {}
    return {
        "updated_at": payload.get("updated_at"),
        "status": payload.get("status"),
        "windows": {name: {"used_percent": w.get("used_percent"),
                           "resets_at": w.get("resets_at")}
                    for name, w in windows.items() if isinstance(w, dict)},
    }


def _resolve_credentials_via_substrate(
    *, prefer_alias: str | None,
    rng: random.Random | None = None,
    empty_vault_expected: bool = False,
) -> dict | None:
    """The account picker over the operator's vault (record v16 §10.9).

    Returns ``{"type": "token", "token": <setup token>, "harness_token":
    <account id>, "alias": ...}`` for an account with a fresh setup token,
    or ``{"type": "vault", "harness_token": <account id>, "alias": ...}``
    for an account whose OAuth bundle the launcher writes into the
    container's credentials file (declared through the mount plan, written
    after validation). ``None`` when no launchable account exists.

    Selection: ``prefer_alias`` wins when it names an account; otherwise
    max-min headroom over the usage readings when every account has a
    fresh one, else a uniform random pick. Accounts whose reading says
    exhausted are skipped while any other remains. No launchable account
    is a refusal with the remedy logged; nothing interactive runs here.
    """
    rng = rng or random
    accounts = _claude_accounts()
    if not accounts:
        from tools.graph import harness_credentials as hv
        if any(not a.openable for a in hv.list_accounts("claude")):
            # Accounts exist but the vault is cold: the operator has not
            # unlocked since the dashboard started. The launch waits for the
            # unlock (cold until unlock is the design).
            logger.error(
                "session_launcher: Claude accounts are in the vault but it is "
                "not open; unlock the dashboard before launching",
            )
            return None
        # No account at all. The two ways in are the operator's own acts
        # (graph claude install, or a sign-in on the machine found by the
        # Getting Started scan); neither can run unattended from here. A
        # caller that imports one next (the host terminal's bootstrap) asks
        # for INFO: the empty vault is its expected first step, and the
        # ERROR belongs to its final result (auto-gksaw).
        logger.log(
            logging.INFO if empty_vault_expected else logging.ERROR,
            "session_launcher: no Claude account in the vault; remedy: "
            "`graph claude install --alias <name>`, or sign in to Claude on "
            "this machine and run `graph credentials import`",
        )
        return None

    # The readings the decision is made on, kept for the record (auto-dgr2c)
    # whichever branch decides: an alias pick ignores them, but what they
    # said at that moment is still the evidence a later question needs.
    now = datetime.now(timezone.utc)
    usage_by_org: dict[str, dict] = {}
    for payload in _claude_usage_rows():
        account_id = payload.get("account_id")
        if not isinstance(account_id, str) or not account_id:
            continue
        if _reading_window_open(payload, now=now):
            usage_by_org[account_id] = payload
            continue
        if _is_usage_stale(payload, now=now):
            continue
        if not _is_usage_usable(payload):
            continue
        usage_by_org[account_id] = payload
    candidates = [a.id for a in accounts]
    excluded: list[dict] = []
    chosen = None
    method = None
    if prefer_alias:
        for acct in accounts:
            if acct.get("alias") == prefer_alias:
                chosen, method = acct, "alias"
                break
    if chosen is None:
        exhausted = {
            a.id for a in accounts
            if _usage_exhausted(usage_by_org.get(a.id), now=now)
        }
        if exhausted and len(exhausted) < len(accounts):
            excluded = [{"account_id": a, "reason": "exhausted",
                         "reading": _reading_snapshot(usage_by_org.get(a))}
                        for a in sorted(exhausted)]
            accounts = [a for a in accounts if a.id not in exhausted]
        if len(accounts) == 1:
            chosen, method = accounts[0], "only"
        elif all(a.id in usage_by_org for a in accounts):
            chosen, method = max(
                accounts,
                key=lambda a: (_token_headroom_score(usage_by_org.get(a.id, {})), a.id),
            ), "headroom"
        else:
            chosen, method = rng.choice(accounts), "random"
        if exhausted and len(exhausted) == len(candidates):
            method += "-all-exhausted"

    out: dict = {"harness_token": chosen.id}
    out["selection"] = {
        "harness": "claude", "account_id": chosen.id, "alias": chosen.get("alias"),
        "method": method, "candidates": candidates, "excluded": excluded,
        "reading": _reading_snapshot(usage_by_org.get(chosen.id)),
        "at": now.isoformat(timespec="seconds"),
    }
    alias = chosen.get("alias")
    if alias:
        out["alias"] = alias
    if chosen.setup_token_fresh():
        out["type"] = "token"
        out["token"] = chosen.get("setup")
    else:
        out["type"] = "vault"
    return out


def _resolve_credentials(
    *, prefer_alias: str | None = None, empty_vault_expected: bool = False,
) -> dict | None:
    """Resolve Claude credentials for a session launch.

    Returns a dict shaped:
      {"type": "token", "token": <raw_key>, "alias": <alias>,
       "harness_token": <org_uuid>}

    Or ``None`` when no credentials are reachable.

    Selection order:
      1. ``CLAUDE_CODE_OAUTH_TOKEN`` env var (legacy compat — operator
         override, returned without an alias).
      2. Substrate-backed picker over ``dashboard.claude.setup_tokens``
         rows. ``prefer_alias`` wins when matched; otherwise max-min
         over harness usage if every token has fresh telemetry, else
         uniform random pick. Empty rows trigger ``graph claude
         install`` and a re-read.

    ``empty_vault_expected``: the caller imports an account next when the
    vault has none, so that case is logged at INFO rather than ERROR.
    """
    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if oauth_token:
        return {"type": "token", "token": oauth_token}

    return _resolve_credentials_via_substrate(
        prefer_alias=prefer_alias, empty_vault_expected=empty_vault_expected,
    )


def _setup_auth_docker_args(creds: dict, run_dir: Path) -> list[str] | None:
    """Convert resolved credentials to docker run args.

    For substrate-backed credentials (``type=="token"`` with a
    ``harness_token``), the picker has already pulled the long-lived
    ``raw_key`` from the substrate. We pass it as
    ``CLAUDE_CODE_OAUTH_TOKEN`` so the in-container ``claude`` binary
    picks it up directly — the env-var path Anthropic supports across
    every supported version.

    Returns docker arg list, or None if creds type is unrecognised.
    """
    if creds["type"] == "token":
        return ["-e", f"CLAUDE_CODE_OAUTH_TOKEN={creds['token']}"]
    if creds["type"] == "vault":
        # The credentials file rides the mount plan; no environment needed.
        return []

    return None


def _codex_git_root(worktree_host: Path) -> str | None:
    """Return the repository root Codex resolves trust to for a worktree.

    Codex, started with cwd inside a git worktree, applies trust to the *main*
    working tree it derives from — the parent of ``--git-common-dir``
    (``<root>/.git`` -> ``<root>``). The managed clone is bind-mounted at the
    same absolute host path inside the container, so the host-computed path is
    also the path Codex sees at runtime. Generic — no workspace is hardcoded.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(worktree_host), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return None
        common = Path(out.stdout.strip())
        if not common.is_absolute():
            common = (worktree_host / common).resolve()
        return str(common.parent if common.name == ".git" else common)
    except Exception:
        return None


def _checkout_source_head(clone: Path) -> None:
    """Detach-checkout ``REPO_ROOT``'s current HEAD commit in ``clone``."""
    from agents import workspace_manager

    workspace_manager._run_git(
        ["fetch", "--quiet", "--no-tags", str(REPO_ROOT), "HEAD"], cwd=clone, timeout=600,
    )
    workspace_manager._run_git(
        ["checkout", "--force", "--detach", "FETCH_HEAD"], cwd=clone, timeout=600,
    )


def _ensure_platform_snapshot() -> str | None:
    """The host path to mount at ``/workspace/repo`` when nothing else claims it.

    A read-only git snapshot of the platform checkout — the managed clone
    mechanism the workspace layer already uses for read-only repos: clone
    (or fetch) ``REPO_ROOT`` under ``data/repos/local/``, then detach-checkout
    its integration tip so the working tree is current. The snapshot carries
    ``tools/``, ``agents/`` and everything PYTHONPATH/graph/bd need, and none
    of the live root's ``data/`` (org DBs, private keys, session secrets) —
    gitignored state does not survive a clone (auto-j3oj3).

    Returns ``None`` on failure (logged loudly). Callers must launch without
    a platform mount in that case, never fall back to the live root.
    """
    try:
        from agents import workspace_manager

        clone = workspace_manager.ensure_managed_clone(str(REPO_ROOT))
        try:
            workspace_manager._update_readonly_clone(clone)
        except Exception:
            # A checkout on a detached HEAD (a release image built from a
            # commit, not a branch) clones with no default branch and no
            # origin/HEAD, so there is no integration tip to check out. The
            # snapshot is then the exact commit this node runs.
            _checkout_source_head(clone)
        return str(clone)
    except Exception as exc:
        print(
            "  ERROR: could not prepare the platform snapshot for "
            f"/workspace/repo — launching without it, so graph/bd/tools will be "
            f"absent from this session ({exc}). If this is 'fatal: repository "
            "... does not exist', the release image dropped its .git and "
            "ensure_managed_clone cannot clone REPO_ROOT — ship the release WITH "
            "its .git (deploy/Dockerfile; NODE-VOLUME-MODEL.md).",
            file=sys.stderr,
        )
        return None


def _generate_codex_config(base_config: Path, git_root: str, run_dir: Path) -> Path | None:
    """Write a per-session Codex ``config.toml`` that pre-trusts ``git_root``.

    The shared ``~/.codex/config.toml`` is mounted ``:ro``, so Codex cannot
    persist trust itself ("config/batchWrite failed") and hangs on the trust
    dialog. We instead generate a per-session copy with the trust entry already
    present so Codex never needs to write. Idempotent; returns None on any error
    (the caller then mounts the shared config unchanged).
    """
    try:
        text = base_config.read_text()
        marker = f'[projects."{git_root}"]'
        if marker not in text:
            text = text.rstrip("\n") + f'\n\n{marker}\ntrust_level = "trusted"\n'
        out = run_dir / "codex-config.toml"
        out.write_text(text)
        return out
    except Exception:
        return None


# ── Harness sign-ins: delivered into the session's private ramfs ─────────────
# A sign-in is opened from the operator's vault at launch, held in memory, and
# written only into the container's own private ramfs at /run/secrets
# (agents/secret_ramfs.deliver_secret_file) once the container is running. A
# small prefix on the harness argv waits for the files and symlinks them to
# where each harness reads them, so the harness starts signed in. Nothing is
# staged on the data volume and there is nothing to clean up: the kernel frees
# the mount with the container (auto-1cc4q; the old run_dir copies were served
# by the output API and outlived their sessions by weeks).
CLAUDE_BUNDLE_FILENAME = "claude-credentials.json"
CLAUDE_BUNDLE_CONTAINER_PATH = "/home/agent/.claude/.credentials.json"
CODEX_AUTH_FILENAME = "codex-auth.json"
CODEX_AUTH_CONTAINER_PATH = "/home/agent/.codex/auth.json"
GROK_AUTH_FILENAME = "grok-auth.json"
GROK_AUTH_CONTAINER_PATH = f"{GROK_HOME}/auth.json"
SIGNIN_CONTAINER_PATHS = {
    CODEX_AUTH_FILENAME: CODEX_AUTH_CONTAINER_PATH,
    CLAUDE_BUNDLE_FILENAME: CLAUDE_BUNDLE_CONTAINER_PATH,
    GROK_AUTH_FILENAME: GROK_AUTH_CONTAINER_PATH,
}
# How long the container waits for its sign-ins, and how long the launcher
# waits for the container to be running before delivering them.
SIGNIN_WAIT_S = 120
SIGNIN_CONTAINER_WAIT_S = 300

# ── vault links (auto-2eqpb) ────────────────────────────────────────────────
#
# A workspace's declared vault links ride the SAME step as the sign-ins: the
# value is opened from the audited vault in memory, delivered into the
# session's private ramfs as ``vault.<entry>``, and linked at the declared
# path. Two differences, both so the links exist before anything reads them:
# the link step runs BEFORE the image's own entrypoint (its ssh-agent block
# and /startup.sh read the linked files), and /etc/autonomy/artifacts --
# where the Anchore scripts read them, and which the unprivileged session
# user cannot create -- is a small tmpfs owned by that user. It holds only
# symlinks; the values stay in the ramfs.
VAULT_LINK_FILE_PREFIX = "vault."
VAULT_LINK_DIR = "/etc/autonomy/artifacts"
#: The account recorded for a vault link in a pending delivery record, so a
#: re-delivery after a dashboard reload reopens the same entry.
VAULT_LINK_ACCOUNT_PREFIX = "vault:"


def _vault_link_payloads(vault_links, carried=None):
    """``(payloads, dests, accounts, refusals)`` for a launch's vault links.

    payloads: ``{vault.<entry>: bytes}``; dests: ``{vault.<entry>: path}``;
    accounts: the pending-record account per file; refusals: the required
    links that could not be opened, by key. A carried launch (a member's,
    on a runner) reads only what was carried, never this machine's vault."""
    payloads: dict[str, bytes] = {}
    dests: dict[str, str] = {}
    accounts: dict[str, str] = {}
    refusals: list[str] = []
    for link in vault_links or ():
        if carried is not None:
            value = carried.credentials.get(link.key)
        else:
            value = _resolve_credential(link.key)
        if not value:
            if link.required:
                refusals.append(link.key)
            else:
                logger.warning("vault link %s (%s) could not be opened; launching "
                               "without it", link.key, link.path)
            continue
        filename = f"{VAULT_LINK_FILE_PREFIX}{link.vault}"
        payloads[filename] = value.encode("utf-8")
        dests[filename] = link.path
        accounts[filename] = f"{VAULT_LINK_ACCOUNT_PREFIX}{link.key}"
    return payloads, dests, accounts, refusals


def _image_entrypoint(image: str) -> list[str] | None:
    """The image's own ENTRYPOINT (exec form), or None when it has none or
    it cannot be read. Read at launch, never assumed: images differ, and a
    workspace's provision row can carry its own Dockerfile."""
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{json .Config.Entrypoint}}", image],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        value = json.loads(r.stdout.strip() or "null")
    except ValueError:
        return None
    if not isinstance(value, list) or not value or not all(
            isinstance(part, str) and part for part in value):
        return None
    return value


# ── Carried credentials: an organization member's launch on a runner ─────────
# A member who launches on another member's runner brings every secret the
# session needs in the launch request, over the authenticated pair
# (graph://7eb29bc8-31a v6 §9.8, D9). The runner reads none of its own: no
# vault, no account picker, no host environment, no host file. Carried values
# reach the container the way sign-ins do -- written into its private ramfs
# once it runs -- and an environment variable is exported from its file by
# the same argv prefix, so no value is ever a ``docker run -e`` argument,
# which Docker would keep on the runner's disk in the container's config.
#: ramfs filename prefix for a carried environment variable.
ENV_FILE_PREFIX = "env."
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class CarriedCredentials:
    """What a member's dashboard carried: ``credentials`` maps a
    ``credential:`` key to its value, ``env`` maps an environment variable
    name to its value (``env_from_host`` names, a Claude setup token),
    ``signins`` maps a sign-in filename to its content. Held in memory only;
    its repr names nothing."""

    __slots__ = ("credentials", "env", "signins")

    def __init__(self, credentials=None, env=None, signins=None) -> None:
        self.credentials: dict[str, str] = dict(credentials or {})
        self.env: dict[str, str] = dict(env or {})
        self.signins: dict[str, bytes] = dict(signins or {})

    def __repr__(self) -> str:
        return (f"CarriedCredentials(credentials={sorted(self.credentials)}, "
                f"env={sorted(self.env)}, signins={sorted(self.signins)})")


def carried_requirements(env, env_from_host, capabilities) -> tuple[set, list, list]:
    """What a launch on a runner must carry for a workspace, and what it
    cannot run with: ``(credential keys, env_from_host names, refusals)``.

    A capability binding that reads this machine's environment or a file on
    it, and a capability secret file, are the runner owner's data; each is a
    refusal by name, never silently dropped."""
    keys = _credential_keys_in_env(env) | _declared_credential_keys(capabilities)
    refusals: list[str] = []
    for cap in capabilities or ():
        slug = getattr(cap, "implementation", None) or getattr(cap, "slug", None) or "?"
        for env_name, source in getattr(cap, "env_bindings", {}).items():
            if isinstance(source, str):
                kind = parse_capability_env_source(source).kind
                if kind in ("host", "file"):
                    refusals.append(
                        f"capability {slug}: {env_name} is read from the runner's "
                        f"own {'environment' if kind == 'host' else 'files'}")
        if getattr(cap, "secret_file_bindings", None):
            refusals.append(f"capability {slug}: mounts secret files from the runner")
    return keys, list(env_from_host or ()), refusals


def _carried_env_files(env_values: dict[str, str]) -> dict[str, bytes]:
    """``{ramfs filename: content}`` for carried environment variables."""
    return {f"{ENV_FILE_PREFIX}{name}": value.encode()
            for name, value in env_values.items()}


def _pick_account(harness: str, rng: random.Random | None = None) -> Any | None:
    """The account a Codex or Grok session launches with: the only
    launchable one, or a uniform random pick among several (record v16
    §10.9; no headroom reading exists for these harnesses yet)."""
    accounts = [a for a in _accounts(harness) if a.launchable]
    if not accounts:
        return None
    return (rng or random).choice(accounts) if len(accounts) > 1 else accounts[0]


def _pick_selection(harness: str, acct: Any, candidates: int) -> dict:
    """The record of a Codex or Grok pick (auto-dgr2c)."""
    return {"harness": harness, "account_id": acct.id, "alias": acct.get("alias"),
            "method": "only" if candidates == 1 else "random",
            "candidates_count": candidates, "excluded": [],
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _claude_bundle_doc(account_id: str) -> bytes | None:
    """``~/.claude/.credentials.json`` for the chosen account from its vault
    rows, opened at launch while the operator is unlocked."""
    from tools.graph import harness_credentials as hv
    acct = hv.read_account("claude", account_id)
    if acct is None or not acct.has(*hv.CLAUDE_BUNDLE):
        logger.warning(
            "session_launcher: the Claude account %s has no openable OAuth "
            "bundle in the vault; remedy: unlock, or run `graph credentials import`",
            account_id,
        )
        return None
    bundle = {
        "accessToken": acct.get("access"),
        "refreshToken": acct.get("refresh"),
        "expiresAt": acct.expires_ms(),
        "scopes": hv.scopes_list(acct.get("scopes")),
    }
    return json.dumps({"claudeAiOauth": bundle}, indent=2).encode()


def _codex_auth_doc(acct) -> bytes:
    """``~/.codex/auth.json`` for a Codex vault account. The host
    ``~/.codex/auth.json`` is never read (bead auto-l1h3f)."""
    now_iso = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    auth_doc = {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": acct.get("id"),
            "access_token": acct.get("access"),
            "refresh_token": acct.get("refresh"),
            "account_id": acct.id,
        },
        "last_refresh": acct.get("refreshed_at") or now_iso,
    }
    return json.dumps(auth_doc, indent=2).encode()


def _signin_payloads(claude_account: str | None,
                     accounts_out: dict | None = None,
                     *, harness: str | None = None,
                     picked: dict | None = None) -> dict[str, bytes] | None:
    """Every sign-in this launch delivers, ``{filename: content}``.

    Only the sign-in of the session's own *harness* (auto-9hu6y, default
    decided by host-0927-113441): a Claude session no longer carries Codex
    and Grok tokens it does not use. ``harness=None`` keeps every one, for
    callers that do not know it.

    *picked*, when given, holds the Codex or Grok account the launcher
    already chose (``{harness: account}``) and recorded, so the sign-in
    delivered is that account's, not a second pick (auto-dgr2c).

    *accounts_out*, when given, receives ``{filename: account id}`` for each
    included sign-in: the key selection only, never the secret, so a
    re-delivery can reopen the same account (:func:`_open_signin`).

    A wanted Codex or Grok sign-in whose account the vault lacks is WARNED
    with the remedy and simply absent — the truthful "not signed in". The
    Claude bundle is included when the session's Claude credential is a
    vault account (only a Claude session resolves one). Returns None when a vault account that WAS chosen cannot
    be opened: the launch is refused rather than started half signed in."""
    payloads: dict[str, bytes] = {}
    def wants(name: str) -> bool:
        return harness is None or harness == name

    if wants("codex"):
        codex = picked["codex"] if picked and "codex" in picked else _pick_account("codex")
        if codex is None:
            logger.warning(
                "session_launcher: no Codex account in the vault — the session "
                "will launch WITHOUT Codex sign-in and will prompt for it. "
                "Remedy: run `graph credentials import`.",
            )
        else:
            payloads[CODEX_AUTH_FILENAME] = _codex_auth_doc(codex)
            if accounts_out is not None:
                accounts_out[CODEX_AUTH_FILENAME] = codex.id
    if claude_account:
        bundle = _claude_bundle_doc(claude_account)
        if bundle is None:
            return None
        payloads[CLAUDE_BUNDLE_FILENAME] = bundle
        if accounts_out is not None:
            accounts_out[CLAUDE_BUNDLE_FILENAME] = claude_account
    grok = ((picked["grok"] if picked and "grok" in picked else _pick_account("grok"))
            if wants("grok") else None)
    if grok is not None:
        auth = grok.get("auth")
        if auth:
            payloads[GROK_AUTH_FILENAME] = auth.encode()
            if accounts_out is not None:
                accounts_out[GROK_AUTH_FILENAME] = grok.id
        else:
            logger.warning(
                "session_launcher: the Grok account has no stored sign-in; "
                "remedy: unlock, or run `graph credentials import`",
            )
    return payloads


def _open_signin(filename: str, account_id: str) -> bytes | None:
    """Reopen one sign-in from the vault by the account a launch chose."""
    if filename.startswith(VAULT_LINK_FILE_PREFIX):
        if not account_id.startswith(VAULT_LINK_ACCOUNT_PREFIX):
            return None
        value = _resolve_credential(account_id[len(VAULT_LINK_ACCOUNT_PREFIX):])
        return value.encode("utf-8") if value else None
    if filename == CLAUDE_BUNDLE_FILENAME:
        return _claude_bundle_doc(account_id)
    harness = {CODEX_AUTH_FILENAME: "codex", GROK_AUTH_FILENAME: "grok"}.get(filename)
    if harness is None:
        return None
    acct = next((a for a in _accounts(harness) if a.id == account_id), None)
    if acct is None:
        return None
    if filename == CODEX_AUTH_FILENAME:
        return _codex_auth_doc(acct)
    auth = acct.get("auth")
    return auth.encode() if auth else None


def signin_argv_prefix(filenames, dests: dict[str, str] | None = None) -> list[str]:
    """argv placed before the harness command: wait (bounded) for each
    delivered sign-in, symlink it where its harness reads it, then exec the
    harness. Empty when nothing is delivered. It runs inside the shared
    entrypoint's exec, so no image carries it and no image rebuild is
    needed. A sign-in that never arrives is reported and the harness starts
    anyway — it prompts, the truthful state.

    Known limit: a harness that saves a refreshed sign-in by replacing the
    file (rename) swaps the symlink for a regular file in the container's
    writable layer, where it stays for the session's life. That is not the
    data volume, and the container is removed when the session ends."""
    filenames = sorted(filenames)
    dests = dests or {}
    pairs = [f"{f}:{dests.get(f) or SIGNIN_CONTAINER_PATHS[f]}" for f in filenames
             if not f.startswith(ENV_FILE_PREFIX)]
    env_names = [f[len(ENV_FILE_PREFIX):] for f in filenames
                 if f.startswith(ENV_FILE_PREFIX)]
    if any(not _ENV_NAME_RE.fullmatch(n) for n in env_names):
        raise ValueError("a carried environment variable name is not a shell name")
    if not pairs and not env_names:
        return []
    from agents.secret_ramfs import SESSION_SECRET_DST
    wait = (f'while [ ! -s "$f" ] && [ "$n" -lt {int(SIGNIN_WAIT_S * 10)} ]; do '
            f"sleep 0.1; n=$((n+1)); done; ")
    script = "n=0; "
    if pairs:
        script += (
            f"for p in {' '.join(shlex.quote(p) for p in pairs)}; do "
            f'f="{SESSION_SECRET_DST}/${{p%%:*}}"; d="${{p#*:}}"; ' + wait +
            f'if [ -s "$f" ]; then mkdir -p "${{d%/*}}" && ln -sfn "$f" "$d"; '
            f'else case "$f" in */{VAULT_LINK_FILE_PREFIX}*) '
            f'echo "autonomy: vault link $f was not delivered; $d is absent" >&2;; '
            f'*) echo "autonomy: sign-in $f was not delivered; the harness '
            f'will ask you to sign in" >&2;; esac; fi; done; ')
    for env_name in env_names:
        # The value never appears in argv: the shell reads it from the file.
        script += (
            f'f="{SESSION_SECRET_DST}/{ENV_FILE_PREFIX}{env_name}"; ' + wait +
            f'if [ -s "$f" ]; then {env_name}="$(cat "$f")"; export {env_name}; '
            f'else echo "autonomy: {env_name} was not delivered" >&2; fi; ')
    script += 'exec "$@"'
    return ["sh", "-c", script, "autonomy-signin"]


def deliver_signins(container: str, payloads: dict[str, bytes], *,
                    wait_s: float = SIGNIN_CONTAINER_WAIT_S) -> list[str]:
    """Wait for *container* to be running, then write each sign-in into its
    private ramfs. Returns the filenames that could not be delivered (empty
    on success). The buffers are wiped as each helper returns."""
    from agents.secret_ramfs import ProvisionError, deliver_secret_file
    deadline = time.monotonic() + wait_s
    while True:
        try:
            r = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", container],
                capture_output=True, text=True, timeout=15)
            if r.returncode == 0 and r.stdout.strip() == "true":
                break
        except (OSError, subprocess.TimeoutExpired):
            pass
        if time.monotonic() >= deadline:
            logger.error("session_launcher: %s never started; its sign-ins "
                         "were not delivered", container)
            return sorted(payloads)
        time.sleep(0.5)
    failed = []
    for filename, content in payloads.items():
        buf = bytearray(content)
        try:
            deliver_secret_file(container, filename, bytes(buf))
        except (ProvisionError, OSError, subprocess.TimeoutExpired) as exc:
            logger.error("session_launcher: sign-in %s not delivered into "
                         "%s: %s", filename, container, exc)
            failed.append(filename)
        finally:
            buf[:] = b"\x00" * len(buf)
    return failed


#: One JSON record per background delivery in flight, so a dashboard reload
#: that kills the delivering thread can finish the job (auto-fgheq). It holds
#: which accounts were chosen, never a secret.
SIGNIN_PENDING_DIR = DATA_ROOT / "signin-pending"


def _pending_path(container: str) -> Path:
    return SIGNIN_PENDING_DIR / f"{container}.json"


def _process_start_ticks(pid: int) -> int | None:
    """``/proc/<pid>/stat`` field 22 (start time in clock ticks), which with
    the pid identifies one process even after its pid is reused."""
    try:
        with open(f"/proc/{int(pid)}/stat") as fh:
            return int(fh.read().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _clear_pending(container: str) -> None:
    try:
        _pending_path(container).unlink()
    except OSError:
        pass


def deliver_signins_in_background(container: str,
                                  payloads: dict[str, bytes],
                                  accounts: dict[str, str] | None = None) -> None:
    """Deliver from a daemon thread, for launches whose container is started
    by someone else after this returns (tmux, the foreground CLI). The
    thread lives only until delivery (bounded by SIGNIN_CONTAINER_WAIT_S).

    With *accounts* (``{filename: account id}``) a pending record is kept
    until the thread finishes, so :func:`redeliver_pending_signins` can
    complete a delivery the thread did not live to make."""
    if not payloads:
        return
    if accounts:
        try:
            SIGNIN_PENDING_DIR.mkdir(parents=True, exist_ok=True)
            _pending_path(container).write_text(json.dumps({
                "deadline": time.time() + SIGNIN_CONTAINER_WAIT_S + SIGNIN_WAIT_S,
                "accounts": {f: accounts[f] for f in payloads if f in accounts},
                # The process whose thread is delivering: the dashboard
                # worker, or a foreground CLI that outlives any reload.
                "owner_pid": os.getpid(),
                "owner_start": _process_start_ticks(os.getpid()),
            }))
        except OSError:
            logger.exception("session_launcher: could not record the pending "
                             "sign-in delivery for %s", container)

    def run() -> None:
        try:
            deliver_signins(container, payloads)
        finally:
            _clear_pending(container)

    threading.Thread(target=run, name=f"signin-{container}", daemon=True).start()


def _missing_signins(container: str, filenames) -> list[str]:
    """The sign-ins not present in the container's private ramfs."""
    from agents.secret_ramfs import SESSION_SECRET_DST, SESSION_SECRET_UID
    missing = []
    for filename in filenames:
        try:
            r = subprocess.run(
                ["docker", "exec", "-u", str(SESSION_SECRET_UID), container,
                 "test", "-s", f"{SESSION_SECRET_DST}/{filename}"],
                capture_output=True, timeout=15)
            if r.returncode != 0:
                missing.append(filename)
        except (OSError, subprocess.TimeoutExpired):
            missing.append(filename)
    return missing


def redeliver_pending_signins(*, since: float, now: float | None = None) -> dict[str, list[str]]:
    """Finish sign-in deliveries a dashboard reload interrupted (auto-fgheq).

    Run once when a dashboard worker activates. A record whose owner process
    (pid and start time) is still alive is left alone: its delivery thread is
    still working, and a second writer would race it on the same temp file in
    the ramfs (a foreground CLI outlives any reload; an old worker overlaps
    its replacement). A record from a dead owner has its missing sign-ins
    reopened from the vault by the account the launch chose and delivered.
    A record without owner fields falls back to its age: older than *since*
    (this worker's start) means a predecessor's. The owner check assumes the
    writer shares this process's PID namespace, as launches spawned by the
    dashboard do; a launcher in another namespace writing to the same
    DATA_ROOT would read as dead here, and would need its namespace id
    recorded and treated as alive until the deadline. Expired records are dropped,
    with a warning when their container is running without its sign-ins.
    Returns ``{container: [filenames delivered]}``. Never raises."""
    now = time.time() if now is None else now
    done: dict[str, list[str]] = {}
    try:
        records = sorted(SIGNIN_PENDING_DIR.glob("*.json"))
    except OSError:
        return done
    for path in records:
        container = path.stem
        try:
            record = json.loads(path.read_text())
            deadline = float(record.get("deadline") or 0)
            accounts = dict(record.get("accounts") or {})
            owner_pid = record.get("owner_pid")
            owner_start = record.get("owner_start")
            if owner_pid is not None and owner_start is not None:
                if _process_start_ticks(int(owner_pid)) == int(owner_start):
                    continue  # its delivery thread is still alive
            elif path.stat().st_mtime >= since:
                continue
        except (OSError, ValueError, TypeError, AttributeError):
            _clear_pending(container)
            continue
        if deadline <= now or not accounts:
            if accounts:
                try:
                    missing = _missing_signins(container, accounts)
                    running = subprocess.run(
                        ["docker", "inspect", "-f", "{{.State.Running}}", container],
                        capture_output=True, text=True, timeout=15,
                    ).stdout.strip() == "true"
                except (OSError, subprocess.TimeoutExpired):
                    missing, running = [], False
                if running and missing:
                    logger.warning(
                        "session_launcher: %s started its harness without sign-ins "
                        "%s: the reload that interrupted their delivery outlasted "
                        "the container's %d s wait", container, missing, SIGNIN_WAIT_S)
            _clear_pending(container)
            continue
        try:
            payloads = {}
            for filename in _missing_signins(container, accounts):
                content = _open_signin(filename, str(accounts[filename]))
                if content is None:
                    logger.error("session_launcher: sign-in %s for %s could not "
                                 "be reopened from the vault", filename, container)
                    continue
                payloads[filename] = content
            failed = (deliver_signins(container, payloads,
                                      wait_s=max(1.0, deadline - now))
                      if payloads else [])
            done[container] = sorted(set(payloads) - set(failed))
            if done[container]:
                logger.warning("session_launcher: re-delivered sign-ins %s to %s "
                               "after a dashboard reload", done[container], container)
        except Exception:
            logger.exception("session_launcher: sign-in re-delivery for %s failed",
                             container)
        finally:
            _clear_pending(container)
    return done


class GrokLaunchProfile:
    """How one Grok session reaches a model, derived from the workspace env.

    ``mode`` is ``"xai"`` (first-party key in ``XAI_API_KEY``) or
    ``"gateway"`` (OpenAI-compatible ``base_url`` + key). ``default_model`` is
    what the launcher passes as ``-m``: the raw model id for xAI, the TOML
    ``[model.<key>]`` name for a gateway (Grok resolves ``-m`` against the
    catalog key, and TOML splits dotted keys, so ``x-ai/grok-4.6`` becomes
    ``x-ai-grok-4-6``).
    """

    __slots__ = ("mode", "base_url", "models", "default_model", "context_window")

    def __init__(self, *, mode: str, base_url: str | None = None,
                 models: tuple[str, ...] = (), default_model: str | None = None,
                 context_window: int = GROK_GATEWAY_DEFAULT_CONTEXT_WINDOW):
        self.mode = mode
        self.base_url = base_url
        self.models = tuple(models)
        self.default_model = default_model
        self.context_window = context_window

    @property
    def needs_login(self) -> bool:
        return self.mode == "gateway"


def grok_model_key(model_id: str) -> str:
    """The TOML-safe ``[model.<key>]`` name for a gateway model id.

    Dots split TOML keys (``[model.grok-4.6]`` is ``model.grok-4.6`` → three
    nested tables, and Grok then reports "preferred model not in available
    models") and slashes are not bare-key characters, so both collapse to
    ``-``. Deterministic, so the launcher's ``-m`` and the config agree.
    """
    key = re.sub(r"[^A-Za-z0-9_-]+", "-", str(model_id or "")).strip("-")
    return key or "gateway-model"


def _grok_launch_profile(extra_env, model: str | None) -> GrokLaunchProfile:
    """Read the workspace env (raw, unresolved) and decide the Grok mode.

    Only *presence* and literal values matter here — ``credential:`` refs are
    resolved later, at docker-cmd assembly, like every other workspace env.
    """
    env = {str(k): v for k, v in (extra_env or {}).items()}
    base_url = str(env.get(GROK_GATEWAY_BASE_URL_ENV) or "").strip()
    if not base_url:
        return GrokLaunchProfile(mode="xai", default_model=model or None)
    models = [
        m.strip() for m in str(env.get(GROK_GATEWAY_MODELS_ENV) or "").split(",")
        if m.strip()
    ]
    if model and model not in models:
        models.insert(0, model)
    try:
        ctx = int(str(env.get(GROK_GATEWAY_CONTEXT_WINDOW_ENV) or "").strip()
                  or GROK_GATEWAY_DEFAULT_CONTEXT_WINDOW)
    except ValueError:
        ctx = GROK_GATEWAY_DEFAULT_CONTEXT_WINDOW
    default = grok_model_key(model or (models[0] if models else "")) if (model or models) else None
    return GrokLaunchProfile(
        mode="gateway", base_url=base_url.rstrip("/"), models=tuple(models),
        default_model=default, context_window=ctx,
    )


def _toml_str(value: str) -> str:
    return json.dumps(str(value))


def render_grok_config(profile: GrokLaunchProfile) -> str:
    """The per-session ``config.toml`` Grok Build reads from ``$GROK_HOME``.

    Contains no secret: gateway models name the ENV VAR holding the key
    (``env_key``), never the key. Always-approve is the launch flag's config
    twin so a ``/new`` inside the session keeps the same permission mode.
    """
    lines = [
        "# Generated by agents/session_launcher.py for one Autonomy session.",
        "[cli]",
        "auto_update = false",
        "",
        "[features]",
        "telemetry = false",
        "",
        "[ui]",
        'permission_mode = "always-approve"',
        "",
    ]
    if profile.mode == "gateway":
        lines += [
            "[auth]",
            f"auth_provider_command = {_toml_str(GROK_AUTH_SHIM)}",
            'auth_provider_label = "Autonomy gateway"',
            "auth_token_ttl = 2592000",
            "",
        ]
        if profile.default_model:
            lines += ["[models]", f"default = {_toml_str(profile.default_model)}", ""]
        for model_id in profile.models:
            key = grok_model_key(model_id)
            lines += [
                f"[model.{key}]",
                f"model = {_toml_str(model_id)}",
                f"base_url = {_toml_str(profile.base_url or '')}",
                f"name = {_toml_str(model_id + ' (gateway)')}",
                f'env_key = "{GROK_GATEWAY_API_KEY_ENV}"',
                'api_backend = "chat_completions"',
                f"context_window = {int(profile.context_window)}",
                "",
            ]
    elif profile.default_model:
        lines += ["[models]", f"default = {_toml_str(profile.default_model)}", ""]
    return "\n".join(lines)


def _generate_grok_config(run_dir: Path, profile: GrokLaunchProfile) -> Path:
    """Write the session's Grok config into run_dir (mounted at
    /workspace/output); the launch script copies it into ``$GROK_HOME`` so
    Grok can still append its own bookkeeping keys to a writable file."""
    out = Path(run_dir) / GROK_CONFIG_FILENAME
    out.write_text(render_grok_config(profile))
    return out


def _grok_vault_key_available() -> bool:
    """True when the operator's audited vault holds ``grok.api-key``.

    Presence only — never the value. Lets the launcher offer the default key
    binding only when it exists, so a gateway-less workspace with no key logs
    one clear "no Grok credential" line instead of a phantom resolve failure.
    """
    try:
        from tools.graph import ops as _ops
        from tools.graph.schemas.vault_credential import VAULT_AUDITED_SET_ID
        members = _ops.read_set(VAULT_AUDITED_SET_ID, org=None, peers=[])
    except Exception:
        return False
    return any(getattr(row, "key", None) == GROK_VAULT_KEY
               for row in getattr(members, "members", []) or [])


def _grok_env_with_default_key(extra_env, profile: GrokLaunchProfile) -> dict:
    """Workspace env plus the vault default for first-party Grok sessions.

    A workspace that names ``XAI_API_KEY`` itself (usually a ``credential:``
    reference) is left alone; a gateway workspace never gets a first-party
    key. Everything else receives ``credential:grok.api-key`` when that vault
    row exists, resolved by the same path as every workspace credential.
    """
    env = dict(extra_env or {})
    if profile.mode != "xai" or env.get(GROK_API_KEY_ENV):
        return env
    if _grok_vault_key_available():
        env[GROK_API_KEY_ENV] = f"credential:{GROK_VAULT_KEY}"
    else:
        logger.warning(
            "grok: no %s in the workspace env and no vault row %r — the session "
            "will start but Grok will refuse to run until a key is provided",
            GROK_API_KEY_ENV, GROK_VAULT_KEY,
        )
    return env


_UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.IGNORECASE,
)


def grok_resume_id(resume_uuid: str) -> str:
    """The Grok session UUID from a dashboard ``session_uuid``.

    The dashboard stores the transcript directory's name (the session UUID)
    for Grok rows; accept a bare UUID or anything ending in one.
    """
    m = _UUID_RE.search(str(resume_uuid or ""))
    return m.group(1) if m else str(resume_uuid)


def _grok_common_args(profile: GrokLaunchProfile, *, resume_uuid: str | None,
                      session_id: str | None) -> list[str]:
    args = ["--trust", "--always-approve"]
    if profile.default_model:
        args += ["-m", profile.default_model]
    if resume_uuid:
        args += ["--resume", grok_resume_id(resume_uuid)]
    elif session_id:
        args += ["--session-id", session_id]
    return args


def grok_launch_script(
    profile: GrokLaunchProfile,
    *,
    prompt_file: str | None,
    resume_uuid: str | None,
    session_id: str | None,
) -> str:
    """The ``sh -c`` body every Grok session runs.

    1. Install the generated config into ``$GROK_HOME`` (a writable copy —
       Grok appends bookkeeping keys to it at startup).
    2. Gateway mode: ``grok login`` mints the stored sign-in through the
       auth shim, non-interactively (0.2s, verified 2026-09-22).
    3. exec the harness: headless with ``--prompt-file`` (session still
       persisted under sessions/, so the viewer renders dispatch runs too),
       or the TUI with ``--no-alt-screen`` so tmux capture-pane sees it.
    """
    config_src = f"/workspace/output/{GROK_CONFIG_FILENAME}"
    home = f'"${{GROK_HOME:-{GROK_HOME}}}"'
    # A config that cannot be installed must stop the launch here, loudly: a
    # gateway session without its [auth] block would otherwise sit in
    # `grok login` waiting for a browser that never comes (seen 2026-09-22
    # when the home dir was root-owned in a scratch container).
    install = (
        f"mkdir -p {home} && cp {shlex.quote(config_src)} {home}/config.toml"
        f" || {{ echo 'grok: cannot install config into {home}' >&2; exit 97; }}"
    )
    steps = [install]
    if profile.needs_login:
        # The auth shim answers in milliseconds; the timeout only bounds the
        # failure mode where Grok escalates to an interactive sign-in.
        steps.append("timeout 120 grok login >/dev/null 2>&1 || true")
    argv = ["grok", *_grok_common_args(profile, resume_uuid=resume_uuid, session_id=session_id)]
    if prompt_file is not None:
        argv += ["--output-format", "streaming-json", "--prompt-file", prompt_file]
    else:
        argv += ["--no-alt-screen"]
    steps.append("exec " + shlex.join(argv))
    return "; ".join(steps)


def _resolve_optional_tool_mounts(
    worktree_host: Path | None = None,
    run_dir: Path | None = None,
) -> dict[str, str]:
    """Return optional host mounts that make Codex usable inside containers.

    No sign-in is mounted: every harness credential is delivered into the
    session's private ramfs (:func:`_signin_payloads`). Codex's
    config/skills/rules are ordinary host content (not credentials) and are
    still mounted from ``~/.codex`` read-only.

    When ``worktree_host`` and ``run_dir`` are supplied, mount a generated
    per-session ``config.toml`` that pre-trusts the worktree's git-root instead
    of the shared (read-only) host config — otherwise Codex tries to write trust
    into the ``:ro`` mount, fails ``config/batchWrite``, and hangs on the trust
    dialog. Without those args (e.g. the CLI path) the shared config is mounted
    unchanged.
    """

    mounts: dict[str, str] = {}

    host_codex_home = Path.home() / ".codex"
    base_config = host_codex_home / "config.toml"

    config_source = base_config
    if worktree_host is not None and run_dir is not None and base_config.exists():
        git_root = _codex_git_root(worktree_host)
        if git_root:
            generated = _generate_codex_config(base_config, git_root, run_dir)
            if generated is not None:
                config_source = generated

    # Non-credential host content only. The credential (auth.json) is
    # delivered into the private ramfs — this table deliberately omits it
    # so no launcher code path reads the host ~/.codex/auth.json.
    codex_mounts = {
        config_source: "/home/agent/.codex/config.toml:ro",
        host_codex_home / "skills": "/home/agent/.codex/skills:ro",
        host_codex_home / "rules": "/home/agent/.codex/rules:ro",
    }
    for host_path, container_spec in codex_mounts.items():
        if host_path.exists():
            mounts[str(host_path)] = container_spec

    agents_home = Path.home() / ".agents"
    if agents_home.exists():
        mounts[str(agents_home)] = "/home/agent/.agents:ro"

    return mounts


# ── Shared mount plan builder ─────────────────────────────────────────────────

# A privileged session's nested dockerd keeps its store on a per-session named
# volume instead of the container's writable layer (auto-ipq3l). overlay2
# cannot stack on the overlay writable layer, so there the startup scripts had
# to run vfs, which copies every image layer for every container: 17 GB of
# release images held 105 GB. On a volume (ext4 underneath) they pick overlay2.
# The name is derived from the session, so resume and restart reuse the store;
# only the session monitor's tombstoned GC removes it.
DIND_VOLUME_PREFIX = "autonomy-dind-"
DIND_VOLUME_LABEL = "autonomy.session"


def dind_volume_name(session: str) -> str:
    return f"{DIND_VOLUME_PREFIX}{session}"


def _ensure_dind_volume(session: str, org: str | None) -> str | None:
    """Create the session's nested-Docker volume with its labels, or reuse it.

    Returns the volume name, or None when docker refused. ``docker run --mount``
    would create a missing volume by itself, but without the labels the GC's
    orphan sweep keys on, so the launch creates it first."""
    volume = dind_volume_name(session)
    try:
        found = subprocess.run(["docker", "volume", "inspect", volume],
                               capture_output=True, text=True, timeout=30)
        if found.returncode == 0:
            return volume
        labels = ["--label", f"{DIND_VOLUME_LABEL}={session}"]
        if org:
            labels += ["--label", f"autonomy.org={org}"]
        made = subprocess.run(["docker", "volume", "create", *labels, volume],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  ERROR: docker volume create {volume}: {exc}", file=sys.stderr)
        return None
    if made.returncode != 0:
        print(f"  ERROR: docker volume create {volume}: {made.stderr.strip()}",
              file=sys.stderr)
        return None
    return volume


def _beads_credential_env_args(org: str | None) -> list[str]:
    """``-e`` args carrying the session's tracker SQL credentials.

    Reads credentials.env from the same beads dir build_mount_plan
    mounts for this org (per-org dir when provisioned, else shared).
    No credentials file → no args: bd's defaults apply.
    """
    from tools.data_paths import beads_client_env, org_beads_dir
    env = beads_client_env(org_beads_dir(org))
    out: list[str] = []
    for key in ("BEADS_DOLT_SERVER_USER", "BEADS_DOLT_PASSWORD"):
        if env.get(key):
            out += ["-e", f"{key}={env[key]}"]
    return out


def build_mount_plan(
    *,
    run_dir,
    sessions_dir,
    harness: str,
    working_dir: str,
    caller_mounts=None,
    org: str | None = None,
    include_capabilities: bool = False,
    capabilities=(),
    global_claude_md=None,
    startup_script=None,
    allow_docker_socket: bool = False,
):
    """The one dest-keyed MountPlan both entry points build and emit (auto-vm8qh).

    Origins are DERIVED from source paths (mount_plan.mount_spec): platform-managed
    paths under REPO_ROOT/DATA_ROOT are NODE, external workspace-declared host
    paths are HOST, /dev/null is DEVICE — so a platform worktree/clone in the
    caller dict can never be mislabelled HOST and fabricate raw -v /app/... on a
    containerized node. Returns (plan, shim_env). No sign-in is mounted; they
    are delivered into the private ramfs (:func:`deliver_signins`).

    include_capabilities gates the capability/shim/skill block (launch_session
    only; the CLI does not mount capabilities). global_claude_md/startup_script
    are present only for launch_session. allow_docker_socket is the host-terminal
    carve-out; only launch_session(host_terminal=True) passes it.
    """
    from agents.mount_plan import MountPlan, mount_spec

    transcript_mount = {
        "codex": "/home/agent/.codex/sessions",
        "grok": f"{GROK_HOME}/sessions",
    }.get(harness, "/home/agent/.claude/projects")
    plan = MountPlan(allow_docker_socket=allow_docker_socket)
    # Beads state comes from the STATE volume, not the code volume. It is
    # Dolt-backed accumulated state — the same category as worktrees and
    # agent-runs — and the code volume has no .beads on a fresh node, because
    # it is gitignored and never in the image. Sourcing it from REPO_ROOT
    # emitted volume-subpath=.beads against autonomy-code, docker refused the
    # missing subpath, and NO CONTAINER WAS CREATED: every downstream symptom
    # on a fresh node, including a tmux session that looked like it died, was
    # this one mount (auto-qk4ip, found on sjc-2).
    # Per-org beads sandbox (operator ruling 2026-08-24): an org whose
    # tracker config exists at DATA_ROOT/.beads/orgs/<slug>/ gets THAT
    # mounted as its /data/.beads — same shared Dolt server (port pinned
    # in its config.yaml), its own database (named in metadata.json), its
    # own issue prefix. Every other org — autonomy included — keeps the
    # shared tracker (database "auto") via the fallback, so nothing
    # changes for existing sessions until an org dir is provisioned.
    # Launching a session for an org is a write: the session will file beads.
    # Resolve its tracker, provisioning on first sight. A provisioning failure
    # is not fatal here — the launch proceeds on the shared tracker with a loud
    # log, because a session that cannot start is worse than one whose beads
    # need re-homing, and the dashboard's own write path already refuses.
    beads_src = DATA_ROOT / ".beads"
    if org:
        org_beads = DATA_ROOT / ".beads" / "orgs" / str(org)
        if (org_beads / "metadata.json").is_file():
            beads_src = org_beads
        else:
            # First sight of this org. Provisioning is not fatal to a launch: a
            # session that cannot start is worse than one whose beads need
            # re-homing, and the dashboard's own write path already refuses.
            try:
                from tools.beads_provision import ensure_org_beads_dir
                beads_src = ensure_org_beads_dir(
                    str(org), orgs_root=DATA_ROOT / ".beads" / "orgs")
            except Exception as exc:
                logger.warning(
                    "beads: no tracker for org %r and could not provision one "
                    "(%s) — session launches on the shared tracker", org, exc,
                )
    # The mount source must EXIST or `--mount type=bind` refuses it and NO
    # container is created (auto-qk4ip). On a fresh node DATA_ROOT/.beads does
    # not exist yet — the shared tracker's local dir is state that accretes, not
    # something the image ships or the optional `beads` profile creates — so
    # every first session launch refused with "1 launch input missing" until
    # someone hand-made it (sjc-2, 2026-09-08). Create it idempotently instead
    # of hard-failing: a launcher must not require an optional subsystem's dir.
    try:
        Path(beads_src).mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        logger.warning(
            "beads: could not ensure mount source %s exists (%s); the launch "
            "may refuse the .beads mount", beads_src, exc,
        )
    plan.set(mount_spec(beads_src, "/data/.beads"), replace=False)
    # ``.beads`` is rw for bd, but its dolt-remote credential is host-only
    # material no session reads; mask it with an empty ro device bind (auto-j3oj3).
    plan.set(mount_spec("/dev/null", "/data/.beads/.beads-credential-key:ro"), replace=False)
    plan.set(mount_spec(run_dir, "/workspace/output"), replace=False)
    plan.set(mount_spec(sessions_dir, transcript_mount), replace=False)

    # Caller mounts: dispatcher worktrees/clones/git-metadata/artifacts (NODE, under
    # DATA_ROOT) plus workspace-declared host paths (HOST), or the CLI's
    # --worktree/--git-dir. Override; origin derived per source.
    for host_path, container_spec in (caller_mounts or {}).items():
        plan.set(mount_spec(host_path, container_spec))

    # Platform snapshot when nothing claims /workspace/repo.
    if not plan.has_dest("/workspace/repo"):
        snapshot = _ensure_platform_snapshot()
        if snapshot is not None:
            plan.set(mount_spec(snapshot, "/workspace/repo:ro"), replace=False)

    # Nested data/uploads view — only where its mount point exists (a read-only
    # /workspace/repo without data/uploads makes runc's mkdir an OCI failure).
    repo_mount_host = next(
        (s.source for s in plan.specs() if s.dest == "/workspace/repo"), None,
    )
    uploads_target = "/workspace/repo/data/uploads"
    if (repo_mount_host is not None
            and (Path(repo_mount_host) / "data" / "uploads").is_dir()
            and not plan.has_dest("/workspace/repo/data")
            and not plan.has_dest(uploads_target)):
        plan.set(mount_spec(DATA_ROOT / "uploads", f"{uploads_target}:ro"), replace=False)

    # Capability mounts / shim / skills (launch_session only; fill-if-absent, so a
    # caller mount at the same dest still wins).
    shim_env: dict = {}
    if include_capabilities:
        for host_path, container_spec in _capability_mounts(capabilities).items():
            plan.set(mount_spec(host_path, container_spec), replace=False)
        shim_mounts, shim_env = _capability_command_surface(capabilities, run_dir)
        for host_path, container_spec in shim_mounts.items():
            plan.set(mount_spec(host_path, container_spec), replace=False)
        for host_path, container_spec in _capability_skill_surface(
                capabilities, run_dir, harness).items():
            plan.set(mount_spec(host_path, container_spec), replace=False)

    # Codex trust pre-seed: worktree_host from the plan built so far (before the
    # optional tool mounts) — the pre-refactor derivation point.
    working_mounts: list = []
    for s in plan.specs():
        cp = s.dest.rstrip("/") or "/"
        if working_dir == cp or working_dir.startswith(f"{cp}/"):
            working_mounts.append((len(cp), Path(s.source)))
    worktree_host = max(working_mounts, default=(0, None), key=lambda i: i[0])[1]

    for host_path, container_spec in _resolve_optional_tool_mounts(
        worktree_host=worktree_host, run_dir=run_dir,
    ).items():
        plan.set(mount_spec(host_path, container_spec))

    if global_claude_md is not None:
        plan.set(mount_spec(global_claude_md, "/home/agent/.claude/CLAUDE.md:ro"))
        # Mirror to AGENTS.md so Codex picks up the same workspace primer.
        plan.set(mount_spec(global_claude_md, "/home/agent/.codex/AGENTS.md:ro"))
    if startup_script is not None:
        plan.set(mount_spec(startup_script, "/startup.sh:ro"))

    return plan, shim_env


def _host_terminal_profile() -> tuple[dict, list[str]]:
    """Mounts and docker args of the host-terminal profile (graph://89d3c8df-544 §3).

    The node's own code, data and org trees writable at /workspace/repo (NODE
    origin, so a containerized node resolves them to the autonomy-code,
    autonomy-data and autonomy-orgs volumes), the host Docker socket, and the
    operator home read-only at /host-home as a strict host bind. Raises
    RuntimeError, before anything is written or minted, when the socket or
    AUTONOMY_HOST_HOME is missing.
    """
    from agents.mount_plan import PrivateBind

    try:
        socket_gid = os.stat(HOST_DOCKER_SOCKET).st_gid
    except OSError as exc:
        raise RuntimeError(
            f"host terminal needs the Docker socket at {HOST_DOCKER_SOCKET}: {exc}"
        ) from exc
    host_home = os.environ.get("AUTONOMY_HOST_HOME", "").strip()
    if not host_home:
        raise RuntimeError(
            "host terminal needs AUTONOMY_HOST_HOME (the operator home, mounted "
            "read-only at /host-home); set it in .env"
        )
    mounts = {
        str(REPO_ROOT): "/workspace/repo",
        str(DATA_ROOT): "/workspace/repo/data",
        str(REPO_ROOT / "orgs"): "/workspace/repo/orgs",
        HOST_DOCKER_SOCKET: HOST_DOCKER_SOCKET,
        host_home: PrivateBind(f"{HOST_HOME_MOUNT}:ro"),
        # Full /mnt writable so the host terminal sees whatever the node's
        # daemon-host mounts there — on a WSL2 node the Windows drives (/mnt/c,
        # /mnt/d as 9p/drvfs), the WSLg sockets (/mnt/wslg), NAS automounts.
        # A plain (private-propagation) bind: docker binds recursively, so the
        # submounts present at launch come through; rslave would also pass
        # later mounts in, but docker refuses it where the host's / is a private
        # mount (WSL2 Ubuntu), and then no host terminal starts at all. A drive
        # mounted after launch appears on the terminal's next start (operator
        # decision 2026-09-26: "plain mount is fine, keep it simple").
        "/mnt": PrivateBind("/mnt"),
    }
    args = [
        "--group-add", str(socket_gid),
        "-e", "AUTONOMY_DATA_ROOT=/workspace/repo/data",
    ]
    return mounts, args


# ── Main Launch Function ──────────────────────────────────────────────────────

def _daemon_propagating(plan, topo) -> set:
    """The plan's rslave bind sources whose daemon-frame mount is shared or
    slave, the only ones docker accepts ``bind-propagation=rslave`` for
    (auto-b0326). No probe runs when the plan has no such bind; an unrunnable
    probe yields the empty set, so every bind is emitted private, which docker
    always accepts."""
    from agents.mount_plan import propagation_sources
    from agents import secret_ramfs

    sources = propagation_sources(plan, topo)
    if not sources:
        return set()
    found = secret_ramfs.daemon_propagating(sources)
    if found is None:
        # Visible, so a lost NFS live-remount pickup has a findable cause.
        logger.warning(
            "propagation probe unavailable; binding %d workspace source(s) "
            "without rslave: %s", len(sources), ", ".join(sources),
        )
        return set()
    return found


def launch_session(
    session_type: str,
    name: str,
    prompt: str | None = None,
    mounts: dict | None = None,
    metadata: dict | None = None,
    detach: bool = True,
    image: str = DEFAULT_IMAGE,
    working_dir: str = "/workspace/repo",
    harness: str = "claude",
    extra_env: dict | None = None,
    output_dir: str | None = None,
    model: str | None = None,
    global_claude_md: Path | str | None = None,
    resume_uuid: str | None = None,
    needs_nested_docker: bool = False,
    runtime: str | None = None,
    startup_script: str | Path | None = None,
    network_host: bool = True,
    capabilities: tuple = (),
    host_terminal: bool = False,
    claude_alias: str | None = None,
    carried: CarriedCredentials | None = None,
    vault_links: tuple = (),
) -> str | None:
    """Launch an agent container session.

    Handles credential resolution, session directory creation, .session_meta.json
    writing, default volume mounts, and docker command building/execution.

    Args:
        session_type: "dispatch" | "librarian" | "chatwith" | "terminal"
        name: Container name (used in .session_meta.json and as label)
        prompt: Prompt text for a non-interactive run. None for interactive sessions.
        mounts: Extra volume mounts {host_path: "container_path[:mode]"}.
                Entries whose container path matches a default override it.
        metadata: Extra fields merged into .session_meta.json
                  (e.g. bead_id, job_id, context_id).
        detach: True  → docker run -d, returns container_id string on success.
                False → builds docker run -it --rm command, returns it as a
                        shell-safe string for the caller to pass to tmux.
        image: Docker image to use.
        working_dir: Working directory inside the container.
        harness: Agent CLI to launch inside the container. ``claude``
                    remains the default; ``codex`` and ``grok`` (xAI Grok
                    Build) are also supported for interactive and
                    non-interactive runs.
        extra_env: Additional environment variables {key: value}.
        output_dir: Pre-created output directory. If None, a new directory under
                    data/agent-runs/ is created using name + UTC timestamp.
        model: Optional model id to pass to the selected harness. When omitted,
                    Claude uses DEFAULT_OPUS_MODEL and Codex uses its own
                    configured default.
        global_claude_md: Host path to mount as the Claude global user-level
                    CLAUDE.md (~/.claude/CLAUDE.md) inside the container.
                    None (default) skips the mount.
        resume_uuid: Claude session UUID to resume. When set, output_dir must
                    be provided (reuses existing session directory), session
                    meta creation is skipped, and --resume is appended to the
                    entrypoint command.
        needs_nested_docker: The workspace runs a nested docker daemon
                    (started by its own startup script). Selects the
                    default ``privileged`` runtime; it no longer affects
                    the command shape — every image shares one entrypoint.
        runtime: Isolation selector. ``privileged`` adds ``--privileged``;
                    ``sysbox`` adds ``--runtime=sysbox-runc``; ``standard``
                    adds neither. When omitted, nested-Docker sessions default
                    to ``privileged`` and all other sessions to ``standard``.
        startup_script: Host path to a shell script to mount read-only at
                    ``/startup.sh`` inside the container. The dind entrypoint
                    wrapper picks it up and runs it in the background before
                    exec'ing the main command. Exit status lands in
                    ``/workspace/output/.setup-exit``; log in ``.setup.log``.
        network_host: True (default) runs the container with ``--network=host``
                    so localhost:8080 reaches the host dashboard directly.
                    Set False to use the default bridge network; the launcher
                    then adds ``--add-host=host.docker.internal:host-gateway``
                    and rewrites ``GRAPH_API`` to
                    ``https://host.docker.internal:8080`` so the container
                    can still reach the dashboard.
        capabilities: Tuple of resolved
                    :class:`~agents.workspace_settings.MaterializedCapability`
                    rows for the workspace. Each one contributes a
                    deterministic package-root mount at
                    ``/opt/autonomy/capabilities/<impl-slug>``, declared
                    secret-file mounts, and non-secret env bindings.
                    Disabled / not-installed capabilities never reach
                    the launcher because the resolver filters them out.
        host_terminal: The operator's in-node host terminal, a code-only
                    profile no Setting can select (graph://89d3c8df-544 §3):
                    image autonomy-host-terminal, working dir /workspace/repo,
                    the node's code/data/orgs writable, the Docker socket,
                    the operator home read-only at /host-home, and a
                    local-operator session token (org None). Raises
                    RuntimeError when the socket or AUTONOMY_HOST_HOME is
                    missing.
        claude_alias: Prefer this Claude account (by alias) when resolving
                    credentials; the usual pick otherwise.
        carried: An organization member's launch on this runner: every secret
                    comes from here, and nothing from this machine's vault,
                    account picker, host environment or host files. A
                    ``credential:`` the workspace names that was not carried,
                    or a capability that reads this machine's environment or
                    files, refuses the launch.

    Returns:
        detach=True:  container_id string on success, None on failure.
        detach=False: docker command string on success, None on failure.
    """
    if harness not in SUPPORTED_HARNESSES:
        print(
            f"  ERROR: unsupported harness {harness!r} for session '{name}'",
            file=sys.stderr,
        )
        return None
    host_terminal_args: list[str] = []
    if host_terminal:
        profile_mounts, host_terminal_args = _host_terminal_profile()
        mounts = {**(mounts or {}), **profile_mounts}
        image = HOST_TERMINAL_IMAGE
        working_dir = "/workspace/repo"
    resolved_runtime = runtime or (
        "privileged" if needs_nested_docker else "standard"
    )
    if not isinstance(resolved_runtime, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+", resolved_runtime
    ):
        print(
            f"  ERROR: invalid session runtime {resolved_runtime!r} for '{name}'",
            file=sys.stderr,
        )
        return None
    runtime_args: list[str]
    if resolved_runtime == "standard":
        runtime_args = []
    elif resolved_runtime == "privileged":
        runtime_args = ["--privileged"]
    else:
        docker_runtime = (
            "sysbox-runc" if resolved_runtime == "sysbox" else resolved_runtime
        )
        runtime_args = [f"--runtime={docker_runtime}"]
    resolved_model = model or (DEFAULT_OPUS_MODEL if harness == "claude" else None)
    # Grok: decide the request path from the workspace env before anything is
    # written, and pin the session UUID so the transcript directory is known
    # from byte zero (a resume keeps the original session instead).
    grok_profile: GrokLaunchProfile | None = None
    grok_session_id: str | None = None
    if carried is not None:
        _keys, _host, _refusals = carried_requirements(extra_env, (), capabilities)
        _missing = sorted(k for k in _keys if k not in carried.credentials)
        _refusals += [f"sign-in {f!r} is not one this launcher delivers"
                      for f in sorted(carried.signins) if f not in SIGNIN_CONTAINER_PATHS]
        _refusals += [f"{n!r} is not an environment variable name"
                      for n in sorted(set(carried.env) | set(extra_env or {}))
                      if not _ENV_NAME_RE.fullmatch(n)]
        if _missing or _refusals:
            print(
                f"  ERROR: credential-refused for session '{name}': "
                + "; ".join([f"credential {k} was not carried" for k in _missing]
                            + _refusals),
                file=sys.stderr,
            )
            return None
    if harness == "grok":
        grok_profile = _grok_launch_profile(extra_env, model)
        if carried is None:
            extra_env = _grok_env_with_default_key(extra_env, grok_profile)
        elif (grok_profile.mode == "xai" and not (extra_env or {}).get(GROK_API_KEY_ENV)
              and GROK_VAULT_KEY in carried.credentials):
            extra_env = {**(extra_env or {}),
                         GROK_API_KEY_ENV: f"credential:{GROK_VAULT_KEY}"}
        if not resume_uuid:
            import uuid as _uuid
            grok_session_id = str(_uuid.uuid4())

    # Per-step launch timing. launch_session was opaquely eating ~12-15s of
    # session-create wall-clock (the worktrees finish in ~2s); these markers
    # attribute that time to a specific step so the slow one is obvious in the
    # log. Each line carries step_ms (since the previous marker) and total_ms
    # (since entry). Grep ``launch-timing:``.
    _lt0 = time.monotonic()
    _lt_prev = [_lt0]

    def _lap(step: str) -> None:
        now = time.monotonic()
        logger.info(
            "launch-timing: %-24s name=%s  step_ms=%d  total_ms=%d",
            step, name, int((now - _lt_prev[0]) * 1000),
            int((now - _lt0) * 1000),
        )
        _lt_prev[0] = now

    # ── Credentials ───────────────────────────────────────────
    # Claude sessions need host auth injected into the container. Codex
    # sessions use the optional ~/.codex mounts instead, and Grok sessions
    # carry their key in the env (see _grok_env_with_default_key), so neither
    # must hard-fail on missing Claude credentials.
    auth_args: list[str] = []
    creds: dict | None = None
    if harness == "claude" and carried is None:
        creds = (_resolve_credentials(prefer_alias=claude_alias) if claude_alias
                 else _resolve_credentials())
        _lap("resolve_credentials")
        if creds is None:
            print(
                f"  ERROR: No Claude credentials found for {session_type} session '{name}'",
                file=sys.stderr,
            )
            return None

    # The account decision, recorded once per launch (auto-dgr2c). A Codex or
    # Grok account is picked ONCE here and the same pick is delivered as the
    # sign-in, so the record names the account the session actually holds.
    picked: dict[str, Any] = {}
    selection: dict | None = None
    if carried is not None:
        selection = {"method": "carried"}
    elif harness == "claude" and creds is not None:
        selection = creds.get("selection") or {"method": "environment"}
    elif harness in ("codex", "grok"):
        _acct = _pick_account(harness)
        if _acct is not None:
            picked[harness] = _acct
            selection = _pick_selection(
                harness, _acct, sum(1 for a in _accounts(harness) if a.launchable))
    if selection is not None:
        logger.info(
            "account selection: session=%s harness=%s account=%s method=%s "
            "reading=%s excluded=%s", name, harness, selection.get("account_id"),
            selection.get("method"), json.dumps(selection.get("reading")),
            [e.get("account_id") for e in selection.get("excluded") or ()])

    # ── Session directory setup ────────────────────────────────
    if resume_uuid and not output_dir:
        print(
            f"  ERROR: output_dir is required when resume_uuid is set for '{name}'",
            file=sys.stderr,
        )
        return None

    if output_dir is not None:
        run_dir = Path(output_dir)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        # DASHBOARD_AGENT_RUNS_DIR mirrors server.py's AGENT_RUNS_DIR override
        # so tests (per-worker tmp redirect in the dashboard conftest) don't
        # litter the real repo's data/agent-runs with launch fixtures.
        base = Path(os.environ.get(
            "DASHBOARD_AGENT_RUNS_DIR",
            str(DATA_ROOT / "agent-runs"),
        ))
        run_dir = base / f"{name}-{ts}"

    run_dir.mkdir(parents=True, exist_ok=True)
    sessions_dir = run_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # ── Write .session_meta.json (skip for resumed sessions) ──
    # org (canonical, auto-nuupw; the per-org DB routing slug ingest.py
    # reads) and graph_tags come in via `metadata`.
    if not resume_uuid:
        meta_doc: dict = {
            "type": session_type,
            "container_name": name,
            "launched_at": datetime.now(timezone.utc).isoformat(),
            "harness": harness,
            # Seed the dashboard's monitored-session row before the first
            # provider response declares its model in the JSONL.  Dispatch
            # cards can therefore paint the provider/model badge immediately,
            # and the monitor selects the correct parser from byte zero.
            "model": resolved_model,
            "needs_nested_docker": needs_nested_docker,
            "session_runtime": resolved_runtime,
        }
        if grok_profile is not None:
            # The transcript lands at sessions/<encoded cwd>/<uuid>/updates.jsonl;
            # recording the uuid lets a reader find it without a directory walk.
            meta_doc["grok_session_id"] = grok_session_id
            meta_doc["grok_mode"] = grok_profile.mode
        if creds is not None and creds.get("harness_token"):
            # Operator-facing credential pointer for triage: the account id
            # of the vault record this session launched with (record v16
            # §10.9). The dashboard joins it to the account's alias.
            meta_doc["harness_token"] = creds["harness_token"]
        elif harness in picked:
            meta_doc["harness_token"] = picked[harness].id
        if selection is not None:
            meta_doc["account_selection"] = selection
        if metadata:
            meta_doc.update(metadata)
        (sessions_dir / ".session_meta.json").write_text(json.dumps(meta_doc, indent=2))

    if grok_profile is not None:
        # Config carries no secret (env var NAMES only), so writing it before
        # mount validation leaks nothing on a refused launch.
        _generate_grok_config(run_dir, grok_profile)

    # ── Auth args (may copy creds file into run_dir) ───────────
    if creds is not None:
        auth_args = _setup_auth_docker_args(creds, run_dir)
        if auth_args is None:
            print(
                f"  ERROR: Unrecognised credential type for {session_type} session '{name}'",
                file=sys.stderr,
            )
            return None
    _lap("session_dir+meta+auth")

    # ── Build default volume mount table ──────────────────────
    # Key: host path.  Value: container_path[:mode]
    # The table is ordered; callers can override any entry by matching container path.
    #
    # The mount table IS the permission list: container ``agent`` and the host
    # operator share uid 1000, so file modes inside a mounted tree are
    # decoration. Anything mounted is fully readable. That is why the platform
    # mount is a git snapshot (code only — a checkout reproduces no gitignored
    # key, org DB, or secret) and never the live host root (auto-j3oj3), and
    # why host ``data/`` is exposed only as the single deliberate
    # ``data/uploads`` read-only mount below (built in build_mount_plan).
    try:
        # Pre-create so docker binds the operator-owned directory instead of
        # manufacturing a root-owned one inside the live repo.
        (DATA_ROOT / "uploads").mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    # One dest-keyed plan, built once through the shared builder and emitted once
    # (declare->resolve->emit, bead auto-vm8qh). Origins are DERIVED from source
    # paths, so platform-managed mounts under DATA_ROOT/REPO_ROOT resolve as NODE
    # and only external workspace-declared host paths are HOST. The docker socket
    # is refused once, over the FULL plan, inside mount_args() below.
    from agents.mount_plan import (
        mount_args, discover_topology, SocketMountRefused, MountUnresolvable,
        VolumeSubpathUnsupported,
    )
    # Secret delivery needs NOTHING at launch: a vault release provisions a
    # PRIVATE ramfs inside this container's own mount namespace on first
    # delivery (agents.secret_ramfs.deliver_secret_file) — no shared host
    # directory, no pre-created bind, nothing another process can find or
    # destroy, and the kernel frees it when the container exits. The old
    # per-session host subdir under /run/autonomy-secrets is retired
    # (2026-08-30, fourth shared-root incident).
    mounts = dict(mounts or {})
    plan, shim_env = build_mount_plan(
        run_dir=run_dir,
        sessions_dir=sessions_dir,
        harness=harness,
        working_dir=working_dir,
        caller_mounts=mounts,
        org=(metadata or {}).get("org"),
        include_capabilities=True,
        capabilities=capabilities,
        global_claude_md=global_claude_md,
        startup_script=startup_script,
        allow_docker_socket=host_terminal,
    )

    # Resolve+validate the DECLARED plan into argv NOW — before any authority is
    # minted below (the session token, the materialized Codex credential). A
    # socket / unresolvable / subpath refusal returns here having written nothing
    # and minted nothing, so there is nothing to leak or clean up (auto-vm8qh
    # criterion 6). The validated argv is spliced into cmd at the emission point.
    _topo = discover_topology()
    try:
        _mount_argv = mount_args(plan, _topo, _daemon_propagating(plan, _topo))
    except SocketMountRefused:
        print(f"  ERROR: refusing host Docker socket mount for session '{name}'",
              file=sys.stderr)
        return None
    except (MountUnresolvable, VolumeSubpathUnsupported) as _exc:
        print(f"  ERROR: {_exc}", file=sys.stderr)
        return None

    # Validation passed — NOW open the sign-ins from the vault, in memory only.
    # They are written nowhere on the host: delivery into the container's
    # private ramfs happens once it is running (auto-1cc4q). A vault account
    # that was chosen but cannot be opened refuses the launch before any
    # authority is minted below.
    signin_accounts: dict[str, str] = {}
    if carried is not None:
        # No account ids: a re-delivery would reopen THIS machine's vault.
        signins = dict(carried.signins)
        carried_env = dict(carried.env)
    else:
        signins = _signin_payloads(
            creds.get("harness_token")
            if creds is not None and creds.get("type") == "vault" else None,
            accounts_out=signin_accounts, harness=harness, picked=picked)
    if signins is None:
        print(
            f"  ERROR: refusing to launch session '{name}': a sign-in "
            "account in the vault was chosen but could not be opened",
            file=sys.stderr,
        )
        return None
    # The workspace's vault links (auto-2eqpb), on the same carrier. A
    # required one that cannot be opened -- absent, or the vault cold --
    # refuses the launch by name; it is never dropped silently.
    vault_payloads, vault_dests, vault_accounts, vault_refusals = (
        _vault_link_payloads(vault_links, carried))
    if vault_refusals:
        print(
            f"  ERROR: refusing to launch session '{name}': required vault "
            f"link(s) could not be opened: {', '.join(vault_refusals)} "
            "(absent from the audited vault, or the vault is cold)",
            file=sys.stderr,
        )
        return None
    if vault_payloads:
        vault_entrypoint = _image_entrypoint(image)
        if vault_entrypoint is None:
            print(
                f"  ERROR: refusing to launch session '{name}': image {image!r} "
                "declares no ENTRYPOINT that can be read, so its vault links "
                "cannot be placed before it runs",
                file=sys.stderr,
            )
            return None
        signins = {**signins, **vault_payloads}
        if carried is None:
            # A carried launch records no accounts: a re-delivery after a
            # reload would open THIS machine's vault for a member's session.
            signin_accounts.update(vault_accounts)

    # Preflight EVERY input the docker run depends on that could be missing —
    # the image, the runtime, and every mount source (host binds AND
    # volume-subpaths) — and refuse with the whole list, by name, BEFORE minting
    # the token below. `docker run` with any of these missing creates NO
    # container and reports only a nameless failure (an hour lost to a missing
    # image; the wjzh4/qk4ip class for mounts). Discover what is missing first,
    # do not hand docker a doomed command. Each check fails OPEN if it cannot
    # run: docker stays the backstop, we lose only the naming, never a good
    # launch.
    from agents import launch_preflight
    _problems = launch_preflight.preflight(
        image=image, runtime_args=runtime_args, plan=plan, topo=_topo,
        credential_keys=(set() if carried is not None else
                         (_declared_credential_keys(capabilities)
                          | _credential_keys_in_env(extra_env))),
    )
    if _problems:
        print(
            f"  ERROR: refusing to launch session '{name}': "
            f"{len(_problems)} launch input(s) missing — docker would fail to "
            f"create a container with no useful message:\n"
            + "\n".join(p.line() for p in _problems),
            file=sys.stderr,
        )
        return None

    dind_args: list[str] = []
    if resolved_runtime == "privileged":
        dind_volume = _ensure_dind_volume(name, (metadata or {}).get("org"))
        if dind_volume is None:
            return None
        dind_args = ["--mount",
                     f"type=volume,src={dind_volume},dst=/var/lib/docker"]

    _lap("mounts_assembled")

    # ── Session token ────────────────────────────────────────────
    from tools.dashboard.dao import auth_db
    # A container token is authoritative for the caller's organization, so it is
    # stamped ONLY from the canonical metadata["org"] key. A container that cannot be assigned an org must not receive a
    # token: fail the launch loudly rather than mint an org-less container token
    # (which the caller-org guard would refuse anyway). No backfill exists, so
    # this is the only thing keeping every live container token org-stamped.
    token_org = (metadata or {}).get("org")
    if host_terminal:
        # The host terminal is a local operator: its token carries org None,
        # the value authenticate_session_request requires for a host session.
        token_org = None
    elif not isinstance(token_org, str) or not token_org.strip():
        print(
            f"  ERROR: refusing to launch session '{name}' without a canonical "
            "metadata['org'] to stamp on its session token",
            file=sys.stderr,
        )
        return None
    else:
        token_org = token_org.strip()
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    auth_db.insert_token(token_hash, name, token_org)
    _lap("session_token")

    # ── Networking ─────────────────────────────────────────────
    # Three topologies, in precedence order:
    #  1. host-networked container -> localhost reaches the host dashboard;
    #  2. a CONTAINERIZED node (dashboard is a sibling container, not a host
    #     process) -> put the session on the node's own DNS-having network and
    #     reach the dashboard by its `dashboard` alias, because a bridge session's
    #     host.docker.internal resolves to the host gateway, where a container-
    #     published, tailscale-bound 8080 does not answer;
    #  3. a dev-box node (host process) on the default bridge -> host.docker.internal
    #     + an --add-host entry so DNS resolves to the docker bridge gateway.
    #  ``beads_dolt_host`` overrides where the container-local ``bd`` CLI reaches
    #  the shared Dolt SQL server (honored var is BEADS_DOLT_SERVER_HOST; the Go
    #  bd ignores DOLT_SQL_HOST/BEADS_DOLT_HOST/DOLT_HOST). The shared
    #  ``.beads/config.yaml`` pins ``dolt.host: 172.17.0.1`` (docker0), reachable
    #  from a host-netns or default-bridge container but NOT from the compose
    #  netns — so a compose-networked session must be pointed at the Dolt
    #  server's compose-network DNS name instead (auto f25941a6-a15). Never edit
    #  the shared config: host-net sessions still need 172.17.0.1.
    if not _topo.is_host_process and _topo.network:
        # DERIVED FROM TOPOLOGY, not the caller's network_host default: a
        # CONTAINERIZED/Compose dashboard puts the session on the SAME compose
        # network and reaches the dashboard by its service DNS name. On such a
        # node --network=host + https://localhost:8080 is simply wrong — the
        # dashboard publishes on a specific interface (its tailnet IP), NOT the
        # host's localhost, so host-net sessions could never reach the graph
        # API and every `graph` call failed (sjc-2, 2026-09-08). The old
        # `network_host=True` default was a native/dev-box assumption that the
        # Compose bring-up inherited unchanged; deriving the mode from topology
        # makes a fresh Compose node correct from install with no config.
        network_args = ["--network", _topo.network]
        graph_api = "https://dashboard:8080"
        beads_dolt_host = "dolt"
    elif network_host:
        network_args = ["--network=host"]
        graph_api = "https://localhost:8080"
        beads_dolt_host = None
    else:
        network_args = ["--add-host=host.docker.internal:host-gateway"]
        graph_api = "https://host.docker.internal:8080"
        beads_dolt_host = None

    # ── Assemble docker command ────────────────────────────────
    cmd: list[str] = [
        "docker", "run",
        # --init: tini as PID 1 so orphaned grandchildren get reaped. The
        # harness process is a poor PID 1 — browser/tool subprocess trees
        # that outlive their parent (e.g. agent-browser's Chromium after a
        # session close) otherwise accumulate as zombies until the pid
        # ceiling kills thread creation ('RuntimeError: can't start new
        # thread' after ~20k defunct entries — 2026-07-08/09 incidents).
        "--init",
        "--name", name,
        *network_args,
        "-e", f"BD_ACTOR={session_type}:{name}",
        "-e", f"AUTONOMY_SESSION={name}",
        "-e", "BD_READONLY=0",
        # Per-org tracker SQL credentials (operator ruling: no shared
        # root user). The credentials.env sits inside whichever beads
        # dir this session's /data/.beads mount resolves to — shared or
        # per-org — so the pair always matches the mounted tracker.
        *_beads_credential_env_args((metadata or {}).get("org")),
        *(["-e", f"BEADS_DOLT_SERVER_HOST={beads_dolt_host}"]
          if beads_dolt_host else []),
        "-e", f"GRAPH_API={graph_api}",
        "-e", f"CROSSTALK_TOKEN={raw_token}",
        "-e", "CODEX_HOME=/home/agent/.codex",
        "-e", f"GROK_HOME={GROK_HOME}",
        *auth_args,
        *host_terminal_args,
    ]

    # GRAPH_TAGS — soft tags auto-applied to notes. The container carries
    # no scope variable: its org is stamped on the session token and
    # enforced server-side; the CLI reads no ambient scope.
    if metadata:
        graph_tags = metadata.get("graph_tags")
        if graph_tags:
            if isinstance(graph_tags, (list, tuple)):
                graph_tags = ",".join(str(t) for t in graph_tags)
            cmd.extend(["-e", f"GRAPH_TAGS={graph_tags}"])

    # Splice in the mount argv resolved+validated above (before the token mint),
    # so a mount refusal never reached this point after minting authority.
    cmd.extend(_mount_argv)
    cmd.extend(dind_args)

    if extra_env:
        for k, v in extra_env.items():
            # Workspace env resolves ONLY the credential: scheme through the
            # vault — client GH tokens live here as credential:<key> after the
            # migration. Every other value stays the literal it has always been;
            # a value like "host:8080" or "file:///x" must NOT be reinterpreted
            # as a source scheme (that is why this is not a blanket
            # _resolve_env_source over workspace env). A credential: that cannot
            # resolve — cold vault (named at preflight above) or absent key —
            # drops the binding rather than injecting a wrong/empty value; the
            # value is never logged.
            parsed = parse_workspace_env_source(v) if isinstance(v, str) else None
            if parsed is not None and parsed.kind == "credential" and carried is not None:
                # Checked above: every credential the workspace names was carried.
                carried_env[k] = carried.credentials[parsed.locator]
            elif parsed is not None and parsed.kind == "credential":
                key = parsed.locator
                resolved = _resolve_credential(key) if parsed.valid else None
                if resolved is None:
                    logger.info(
                        "workspace env %s: credential %r unavailable; binding dropped",
                        k, key,
                    )
                    continue
                cmd.extend(["-e", f"{k}={resolved}"])
            else:
                cmd.extend(["-e", f"{k}={v}"])

    # Capability env bindings: non-secret env vars declared by org installs.
    # Secret values stay file-mounted (see _capability_mounts) and never
    # land here per graph://86e04207-a25 § Runtime materialization.
    if carried is None:
        for k, v in _capability_env(capabilities).items():
            cmd.extend(["-e", f"{k}={v}"])
    else:
        # Literal bindings only; host and file sources were refused above.
        for cap in capabilities:
            for k, source in cap.env_bindings.items():
                parsed = parse_capability_env_source(source)
                if parsed.kind == "credential":
                    carried_env[k] = carried.credentials[parsed.locator]
                elif parsed.kind == "literal":
                    cmd.extend(["-e", f"{k}={parsed.literal}"])
        signins.update(_carried_env_files(carried_env))

    # Command-surface env (e.g. AUTONOMY_CAPABILITY_BIN) — only present
    # when at least one capability declared ``tool_target.expose_commands``.
    for k, v in shim_env.items():
        cmd.extend(["-e", f"{k}={v}"])

    cmd.extend(["-w", working_dir])

    cmd[2:2] = runtime_args

    # Mode flags: -d for detached, -it --rm for interactive
    if detach:
        cmd.insert(2, "-d")
    else:
        cmd.insert(2, "--rm")
        cmd.insert(2, "-it")

    # Entrypoint, image, and arguments. With sign-ins to deliver, a prefix
    # after the image waits for them in /run/secrets and links them into
    # place before exec'ing the harness argv (signin_argv_prefix).
    if vault_payloads:
        # Vault links must exist before the image's entrypoint runs its
        # ssh-agent block and /startup.sh, so the one link step runs AS the
        # entrypoint and hands off to the image's own (read above, exec form)
        # with the same argv as always. The tmpfs gives the session user the
        # directory the Anchore scripts read from; it holds only symlinks.
        _prefix = signin_argv_prefix(signins, vault_dests)
        image_head = ["--entrypoint", _prefix[0]]
        if any(d == VAULT_LINK_DIR or d.startswith(VAULT_LINK_DIR + "/")
               for d in vault_dests.values()):
            from agents.secret_ramfs import SESSION_SECRET_UID
            image_head += ["--tmpfs", f"{VAULT_LINK_DIR}:uid={SESSION_SECRET_UID},"
                                      f"gid={SESSION_SECRET_UID},mode=0700"]
        image_head += [image, *_prefix[1:], *vault_entrypoint]
    else:
        image_head = [image, *signin_argv_prefix(signins)]
    # Base images have ENTRYPOINT=["claude", "--dangerously-skip-permissions"];
    # dind-based images have a shell wrapper that does `exec "$@"` so the
    # caller must pass the full command starting with `claude`.
    # Write prompt to file instead of passing on command line — avoids the
    # prompt text appearing in /proc/cmdline where pkill -f can match it.
    if prompt is not None:
        prompt_file = run_dir / ".prompt.md"
        prompt_file.write_text(prompt)
        prompt_pipe = "cat /workspace/output/.prompt.md | "
        if harness == "claude":
            resume_flag = (
                f" --resume {shlex.quote(resume_uuid)}"
                if resume_uuid else ""
            )
            shell_cmd = (
                f"{prompt_pipe}claude --dangerously-skip-permissions "
                f"--model {shlex.quote(resolved_model or DEFAULT_OPUS_MODEL)}"
                f"{resume_flag} -p"
            )
        elif harness == "grok":
            assert grok_profile is not None
            shell_cmd = grok_launch_script(
                grok_profile,
                prompt_file="/workspace/output/.prompt.md",
                resume_uuid=resume_uuid,
                session_id=grok_session_id,
            )
        else:
            if resume_uuid:
                m = re.search(
                    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
                    resume_uuid,
                )
                codex_cmd = [
                    "codex",
                    "exec",
                    "resume",
                    "--dangerously-bypass-approvals-and-sandbox",
                    m.group(1) if m else resume_uuid,
                    "-",
                ]
            else:
                codex_cmd = [
                    "codex",
                    "exec",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "-",
                ]
            if resolved_model:
                codex_cmd[3:3] = ["--model", resolved_model]
            shell_cmd = prompt_pipe + shlex.join(codex_cmd)
        # Every session image shares one entrypoint (session-entrypoint.sh):
        # it runs a mounted /startup.sh, writes the setup markers the
        # dashboard waits on, and execs this full argv. Whether setup runs
        # must never depend on image family, so there is exactly one
        # command shape per harness.
        cmd += [*image_head, "sh", "-c", shell_cmd]
    else:
        if harness == "claude":
            cmd += [
                *image_head,
                "claude",
                "--dangerously-skip-permissions",
                "--model",
                resolved_model or DEFAULT_OPUS_MODEL,
            ]
            if resume_uuid:
                cmd += ["--resume", resume_uuid]
        elif harness == "grok":
            assert grok_profile is not None
            # One shared entrypoint execs this argv; the script installs the
            # config, signs in (gateway mode) and execs the TUI.
            cmd += [*image_head, "sh", "-c", grok_launch_script(
                grok_profile,
                prompt_file=None,
                resume_uuid=resume_uuid,
                session_id=grok_session_id,
            )]
        else:
            codex_args = [
                "codex",
                "--no-alt-screen",
                "--dangerously-bypass-approvals-and-sandbox",
            ]
            if resolved_model:
                codex_args += ["--model", resolved_model]
            if resume_uuid:
                # Dashboard stores the rollout filename stem
                # (rollout-YYYY-MM-DDTHH-MM-SS-<uuid>) as session_uuid for
                # codex sessions; codex resume only accepts the canonical UUID.
                m = re.search(
                    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
                    resume_uuid,
                )
                codex_args += ["resume", m.group(1) if m else resume_uuid]
            cmd += [*image_head, *codex_args]

    _lap("docker_cmd_assembled")

    # ── Execute or return ──────────────────────────────────────
    if detach:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            print(
                f"  ERROR: docker run -d timed out for {session_type} '{name}'",
                file=sys.stderr,
            )
            return None

        if result.returncode != 0:
            print(
                f"  ERROR: docker run -d failed for {session_type} '{name}': {result.stderr.strip()}",
                file=sys.stderr,
            )
            return None

        container_id = result.stdout.strip()
        if not container_id:
            print(
                f"  ERROR: docker run returned empty container ID for {session_type} '{name}'",
                file=sys.stderr,
            )
            return None

        # The container is running: deliver its sign-ins into its private
        # ramfs now. One that cannot be delivered leaves a container that
        # would start its harness signed out, so the launch fails instead.
        failed = deliver_signins(name, signins) if signins else []
        if failed:
            print(
                f"  ERROR: sign-in delivery into {session_type} '{name}' "
                f"failed ({', '.join(failed)}); removing the container",
                file=sys.stderr,
            )
            subprocess.run(["docker", "rm", "-f", name],
                           capture_output=True, timeout=60)
            return None

        return container_id

    else:
        # For tmux-based sessions: return a shell-safe command string. The
        # caller starts the container; the sign-ins follow it from a thread
        # that waits for it to be running.
        deliver_signins_in_background(name, signins, signin_accounts)
        return shlex.join(cmd)
