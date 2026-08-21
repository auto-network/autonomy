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

from tools.data_paths import DATA_ROOT

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IMAGE = "autonomy-session-platform"

DEFAULT_OPUS_MODEL = "claude-opus-4-8[1m]"


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
    /workspace/repo (enterprise-ng: cwd=/workspace/enterprise_ng). The old
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
    """
    if harness != "claude":
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


_HOST_ENV_PREFIX = "host:"
_FILE_ENV_PREFIX = "file:"
_CREDENTIAL_ENV_PREFIX = "credential:"


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
    if source.startswith(_HOST_ENV_PREFIX):
        var = source[len(_HOST_ENV_PREFIX):].strip()
        if not var:
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

    if source.startswith(_FILE_ENV_PREFIX):
        rest = source[len(_FILE_ENV_PREFIX):]
        # ``rest`` is ``/abs/path:VAR_NAME``. Split from the right so a
        # path that itself contains ``:`` (rare but possible on POSIX)
        # still works as long as the var name doesn't contain ``:``.
        if ":" not in rest:
            logger.warning(
                "capability env %s: malformed file source %r (expected "
                "file:/path:VAR_NAME)", env_name, source,
            )
            return None
        path_str, var = rest.rsplit(":", 1)
        var = var.strip()
        if not path_str or not var:
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

    if source.startswith(_CREDENTIAL_ENV_PREFIX):
        key = source[len(_CREDENTIAL_ENV_PREFIX):].strip()
        if not key:
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
    return source


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
            if isinstance(source, str) and source.startswith(_CREDENTIAL_ENV_PREFIX):
                k = source[len(_CREDENTIAL_ENV_PREFIX):].strip()
                if k:
                    keys.add(k)
    return keys


def _credential_keys_in_env(env_mapping) -> set:
    """The vault credential keys a plain env mapping (a workspace's ``env`` /
    ``extra_env``) declares via a ``credential:<key>`` value. Anchore's six
    workspaces deliver their GitHub tokens through workspace ``env`` today; the
    migration rewrites those values in place to ``credential:<key>``, so the
    launch path resolves them here (ONLY the credential: scheme — every other
    value stays the literal it has always been). Mirrors
    :func:`_declared_credential_keys` so the same vault-cold preflight refuses a
    credential-bearing launch by name rather than dropping the token silently."""
    keys: set = set()
    for _name, source in (env_mapping or {}).items():
        if isinstance(source, str) and source.startswith(_CREDENTIAL_ENV_PREFIX):
            k = source[len(_CREDENTIAL_ENV_PREFIX):].strip()
            if k:
                keys.add(k)
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


def _setup_token_rows() -> list[Any]:
    """Return ``dashboard.claude.setup_tokens`` rows from substrate.

    Filters out rows whose substrate ``expires_at`` (``created_at + 1y``
    per the schema's @cache TTL) has elapsed. The cache_gc sweep handles
    expiry asynchronously; we filter inline so a stale row that hasn't
    been swept yet doesn't get picked.
    """
    from tools.graph import ops as _ops
    from tools.graph.schemas.claude_setup_tokens import (
        CLAUDE_SETUP_TOKEN_TTL,
        CLAUDE_SETUP_TOKENS_SET_ID,
    )

    try:
        members = _ops.read_set(
            CLAUDE_SETUP_TOKENS_SET_ID, org=_credentials_org(), peers=[],
        )
    except Exception:
        logger.exception("session_launcher: read_set(claude.setup_tokens) failed")
        return []
    rows = list(getattr(members, "members", []) or [])
    now = datetime.now(timezone.utc)
    fresh: list[Any] = []
    for row in rows:
        created_at = getattr(row, "created_at", None)
        if not created_at:
            # Missing created_at → assume fresh; we've nothing better to go on.
            fresh.append(row)
            continue
        try:
            dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            fresh.append(row)
            continue
        if dt + CLAUDE_SETUP_TOKEN_TTL > now:
            fresh.append(row)
    return fresh


def _credentials_rows() -> list[Any]:
    """Return ``dashboard.claude.credentials`` rows from substrate."""
    from tools.graph import ops as _ops
    from tools.graph.schemas.claude_credentials import (
        CLAUDE_CREDENTIALS_SET_ID,
    )

    try:
        members = _ops.read_set(
            CLAUDE_CREDENTIALS_SET_ID, org=_credentials_org(), peers=[],
        )
    except Exception:
        logger.exception("session_launcher: read_set(claude.credentials) failed")
        return []
    return list(getattr(members, "members", []) or [])


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


def _credentials_alias_for_org(creds_rows: list[Any], org_uuid: str) -> str | None:
    """Look up the operator-friendly alias for ``org_uuid`` from creds rows."""
    for row in creds_rows:
        if getattr(row, "key", None) != org_uuid:
            continue
        payload = getattr(row, "payload", None)
        if isinstance(payload, dict):
            alias = payload.get("alias")
            if isinstance(alias, str) and alias:
                return alias
    return None


def _resolve_credentials_via_substrate(
    *, prefer_alias: str | None,
    rng: random.Random | None = None,
) -> dict | None:
    """Substrate-backed credential picker.

    Returns ``{"type": "token", "token": <raw_key>, "alias": <alias>,
    "harness_token": <org_uuid>}`` for the picked credential, or
    ``None`` when no rows are available even after auto-install.

    Selection rule (graph note ``73c4e9ef-bbc``):

    * If ``prefer_alias`` is set and matches an installed credentials
      row, pick the matching setup-token row (operator override).
    * If every setup-token row has a fresh harness-usage row, pick the
      one with the highest min(short_headroom, long_headroom) — the
      max-min over the bottleneck window. Deterministic given fixed
      usage rows.
    * Otherwise (some tokens lack telemetry, or all do), random pick
      drawn uniformly from the available setup-token rows so brand-new
      accounts get exercised before we know their utilization.

    Empty unexpired-tokens set triggers ``graph claude install`` and
    re-reads the substrate; if install also yields nothing we return
    ``None``.
    """
    rng = rng or random
    tokens = _setup_token_rows()
    if not tokens:
        # Auto-install path. Best-effort: failures here surface to the
        # caller as None and the existing "No Claude credentials found"
        # error message takes over.
        try:
            subprocess.run(["graph", "claude", "install"], check=True, timeout=30)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
            logger.exception(
                "session_launcher: `graph claude install` failed; "
                "no Claude credentials available",
            )
            return None
        tokens = _setup_token_rows()
        if not tokens:
            return None

    creds_rows = _credentials_rows()

    chosen = None
    if prefer_alias:
        # Operator override — find the credentials row whose alias
        # matches, then pick the setup-token row keyed by the same org
        # UUID. A typo / unknown alias falls through to the usage-based
        # selection rather than failing dispatch.
        target_org_uuid: str | None = None
        for row in creds_rows:
            payload = getattr(row, "payload", None)
            if not isinstance(payload, dict):
                continue
            if payload.get("alias") == prefer_alias:
                target_org_uuid = getattr(row, "key", None)
                break
        if target_org_uuid:
            for token_row in tokens:
                if getattr(token_row, "key", None) == target_org_uuid:
                    chosen = token_row
                    break

    if chosen is None:
        usage_rows = _claude_usage_rows()
        now = datetime.now(timezone.utc)
        usage_by_org: dict[str, dict] = {}
        for payload in usage_rows:
            account_id = payload.get("account_id")
            if not isinstance(account_id, str) or not account_id:
                continue
            # A reading whose window has not reset is still true, however
            # old it is: usage only rises until the window rolls. Judging it
            # by a fixed time-to-live discards a fact that has not stopped
            # being a fact.
            if _reading_window_open(payload, now=now):
                usage_by_org[account_id] = payload
                continue
            if _is_usage_stale(payload, now=now):
                continue
            if not _is_usage_usable(payload):
                # Fresh-but-failed telemetry (429 / status=unavailable / empty
                # windows) is usage-UNKNOWN, not zero headroom. Leaving it out
                # drops the "all tokens fresh" gate → random branch → the
                # account is still selected with real probability instead of
                # being silently starved by a losing 0 score.
                continue
            usage_by_org[account_id] = payload

        token_keys = [getattr(t, "key", None) for t in tokens]

        # An account a live reading says is exhausted is not a candidate.
        # Falling back to a random choice here would hand out the one token
        # we KNOW cannot serve a session -- and it would do it precisely when
        # the account is maxed, because that is when /usage answers 429 and
        # the old code called the result unknown.
        exhausted = {
            k for k in token_keys
            if k and _usage_exhausted(usage_by_org.get(k), now=now)
        }
        if exhausted and len(exhausted) < len([k for k in token_keys if k]):
            tokens = [t for t in tokens
                      if getattr(t, "key", None) not in exhausted]
            token_keys = [getattr(t, "key", None) for t in tokens]

        if all(k in usage_by_org for k in token_keys if k):
            # Every token has fresh telemetry → max-min deterministic.
            def _score(token_row: Any) -> tuple[float, str]:
                org_uuid = getattr(token_row, "key", "") or ""
                payload = usage_by_org.get(org_uuid, {})
                # Tiebreak by org UUID so the same usage state always
                # produces the same pick.
                return (_token_headroom_score(payload), org_uuid)
            chosen = max(tokens, key=_score)
        else:
            # Some / all tokens lack fresh usage → uniform random pick.
            chosen = rng.choice(tokens)

    org_uuid = getattr(chosen, "key", None)
    payload = getattr(chosen, "payload", None)
    if not isinstance(payload, dict):
        return None
    raw_key = payload.get("raw_key")
    if not isinstance(raw_key, str) or not raw_key:
        return None
    alias = _credentials_alias_for_org(creds_rows, org_uuid) if org_uuid else None
    out: dict = {
        "type": "token",
        "token": raw_key,
        "harness_token": org_uuid,
    }
    if alias:
        out["alias"] = alias
    return out


def _resolve_credentials(
    *, prefer_alias: str | None = None,
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
    """
    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if oauth_token:
        return {"type": "token", "token": oauth_token}

    return _resolve_credentials_via_substrate(prefer_alias=prefer_alias)


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

    return None


def _schedule_creds_cleanup(container_id: str, creds_copy: str) -> None:
    """Spawn a daemon thread that deletes the credentials copy after the container exits."""

    def _wait_and_delete() -> None:
        try:
            subprocess.run(
                ["docker", "wait", container_id],
                capture_output=True,
                timeout=7200,
            )
        except Exception:
            pass
        try:
            Path(creds_copy).unlink(missing_ok=True)
        except OSError:
            pass

    t = threading.Thread(target=_wait_and_delete, daemon=True)
    t.start()


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
        workspace_manager._update_readonly_clone(clone)
        return str(clone)
    except Exception as exc:
        print(
            "  ERROR: could not prepare the platform snapshot for "
            f"/workspace/repo — launching without it ({exc})",
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


def _codex_credentials_org() -> str:
    """Return the substrate org for Codex credential rows: ``personal``.

    Host-local, per-instance secrets — never shared cross-org. Mirror of
    :func:`_credentials_org`, ``codex_credentials_refresh._credentials_org``,
    and ``credential_import.CREDENTIALS_ORG`` so the launcher, the refresh
    poller, and the importer all converge on the same ``personal.db`` rows.
    """
    return "personal"


def _codex_credential_rows() -> list[Any]:
    """Return ``dashboard.codex.credentials`` rows from substrate.

    Best-effort: any failure (DB unavailable, set never created) collapses
    to ``[]`` so a Codex launch degrades to "no auth mounted" rather than
    crashing the launch path.
    """
    from tools.graph import ops as _ops
    from tools.graph.schemas.codex_credentials import (
        CODEX_CREDENTIALS_SET_ID,
    )

    try:
        members = _ops.read_set(
            CODEX_CREDENTIALS_SET_ID, org=_codex_credentials_org(), peers=[],
        )
    except Exception:
        logger.exception("session_launcher: read_set(codex.credentials) failed")
        return []
    return list(getattr(members, "members", []) or [])


def _pick_codex_credential_row(rows: list[Any]) -> Any | None:
    """Pick the single active Codex credential row from ``rows`` or ``None``.

    Codex authenticates one account at a time (not load-balanced), but the
    surface is account-keyed and may in future hold several rows. Keep only
    rows carrying the full OAuth triple and pick the one with the greatest
    ``expires_at_ms`` (the freshest / most-alive), tie-broken by key so the
    choice is deterministic.
    """
    def _exp(row: Any) -> int:
        payload = getattr(row, "payload", None)
        v = payload.get("expires_at_ms") if isinstance(payload, dict) else None
        return v if isinstance(v, int) and not isinstance(v, bool) else -1

    usable: list[Any] = []
    for row in rows:
        payload = getattr(row, "payload", None)
        if not isinstance(payload, dict):
            continue
        if not all(
            isinstance(payload.get(k), str) and payload.get(k)
            for k in ("access_token", "refresh_token", "id_token")
        ):
            continue
        usable.append(row)
    if not usable:
        return None
    return max(usable, key=lambda r: (_exp(r), getattr(r, "key", "") or ""))


def _materialize_codex_auth_json(run_dir: Path) -> Path | None:
    """Reconstruct ``~/.codex/auth.json`` from the substrate row into ``run_dir``.

    The single credential path (bead auto-l1h3f): Codex sessions no longer
    read the operator's host ``~/.codex/auth.json``. Instead we rebuild the
    on-disk auth.json shape Codex expects from the account-keyed
    ``dashboard.codex.credentials`` row (the account_id is the row key, not
    a payload field) and mount that per-session copy read-only. The refresh
    poller keeps the substrate row ahead of expiry, so the materialized copy
    is fresh at launch.

    Returns the host path to the written file, or ``None`` when no usable
    row exists. A missing row means Codex is unavailable to the session; we
    WARN with the remedy (``graph credentials import``) before returning
    ``None`` so a missed migration surfaces as an operator-visible error
    rather than a silent sign-in prompt.
    """
    row = _pick_codex_credential_row(_codex_credential_rows())
    if row is None:
        # No usable Codex credential row: the launch mounts nothing and the
        # session prompts for sign-in with no operator-visible cause. A
        # missed migration must present as an error message, not an outage —
        # WARN with the remedy inline instead of returning None silently.
        logger.warning(
            "session_launcher: no usable Codex credential row — the session "
            "will launch WITHOUT mounted Codex auth and will prompt for "
            "sign-in. Remedy: run `graph credentials import`.",
        )
        return None
    payload = row.payload
    now_iso = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    auth_doc = {
        "auth_mode": payload.get("auth_mode") or "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": payload["id_token"],
            "access_token": payload["access_token"],
            "refresh_token": payload["refresh_token"],
            "account_id": row.key,
        },
        # Informational only for Codex; carry the poller's stamp when present.
        "last_refresh": payload.get("last_refresh_at") or now_iso,
    }
    out = Path(run_dir) / "codex-auth.json"
    try:
        out.write_text(json.dumps(auth_doc, indent=2))
        out.chmod(0o600)
    except OSError:
        logger.exception("session_launcher: could not write codex auth.json")
        # A partial write (or a write that landed before chmod failed) would
        # leave a credential file behind — possibly at default perms. Remove it
        # so a None return always means "nothing on disk", never a lingering,
        # possibly world-readable credential.
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return out


def _codex_auth_target(run_dir) -> "Path | None":
    """The path _materialize_codex_auth_json WOULD write, iff a usable Codex
    credential row exists — a DECLARE with no write, so the plan can reference the
    credential's future location before mount validation has succeeded (auto-vm8qh
    criterion 6: no credential is written until resolve/emit has passed)."""
    if run_dir is None:
        return None
    if _pick_codex_credential_row(_codex_credential_rows()) is None:
        return None
    return Path(run_dir) / "codex-auth.json"


def _resolve_optional_tool_mounts(
    worktree_host: Path | None = None,
    run_dir: Path | None = None,
    materialize_auth: bool = True,
) -> dict[str, str]:
    """Return optional host mounts that make Codex usable inside containers.

    Claude is already handled via dedicated credential resolution plus the
    mounted sessions directory. Codex's *credentials* are now resolved the
    same way — from the ``dashboard.codex.credentials`` substrate row, NOT
    the host ``~/.codex/auth.json`` (retired in bead auto-l1h3f). Its
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
    # materialized from substrate below — this table deliberately omits it
    # so no launcher code path reads the host ~/.codex/auth.json.
    codex_mounts = {
        config_source: "/home/agent/.codex/config.toml:ro",
        host_codex_home / "skills": "/home/agent/.codex/skills:ro",
        host_codex_home / "rules": "/home/agent/.codex/rules:ro",
    }
    for host_path, container_spec in codex_mounts.items():
        if host_path.exists():
            mounts[str(host_path)] = container_spec

    # Codex credentials: materialize a per-session auth.json from the
    # substrate row and mount it read-only. Missing row → no auth mount →
    # Codex unavailable (the truthful state). This is the ONLY credential
    # path; the host ~/.codex/auth.json mount is retired.
    if run_dir is not None:
        # DECLARE the target path (no write) when materialize_auth is False, so the
        # plan can reference the credential's future location before mount
        # validation succeeds; write it only when True (auto-vm8qh criterion 6).
        codex_auth = (
            _materialize_codex_auth_json(run_dir) if materialize_auth
            else _codex_auth_target(run_dir)
        )
        if codex_auth is not None:
            mounts[str(codex_auth)] = "/home/agent/.codex/auth.json:ro"

    agents_home = Path.home() / ".agents"
    if agents_home.exists():
        mounts[str(agents_home)] = "/home/agent/.agents:ro"

    return mounts


# ── Shared mount plan builder ─────────────────────────────────────────────────

def _delete_if_present(path) -> None:
    """Best-effort delete of a materialized per-session credential copy on an
    early-return (mount validation) failure path, where no container exists yet
    to schedule the normal post-exit cleanup against."""
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def build_mount_plan(
    *,
    run_dir,
    sessions_dir,
    harness: str,
    working_dir: str,
    caller_mounts=None,
    include_capabilities: bool = False,
    capabilities=(),
    global_claude_md=None,
    startup_script=None,
):
    """The one dest-keyed MountPlan both entry points build and emit (auto-vm8qh).

    Origins are DERIVED from source paths (mount_plan.mount_spec): platform-managed
    paths under REPO_ROOT/DATA_ROOT are NODE, external workspace-declared host
    paths are HOST, /dev/null is DEVICE — so a platform worktree/clone in the
    caller dict can never be mislabelled HOST and fabricate raw -v /app/... on a
    containerized node. Returns (plan, shim_env, codex_auth_copy).

    include_capabilities gates the capability/shim/skill block (launch_session
    only; the CLI does not mount capabilities). global_claude_md/startup_script
    are present only for launch_session.
    """
    from agents.mount_plan import MountPlan, mount_spec

    transcript_mount = (
        "/home/agent/.codex/sessions" if harness == "codex"
        else "/home/agent/.claude/projects"
    )
    plan = MountPlan()
    # Beads state comes from the STATE volume, not the code volume. It is
    # Dolt-backed accumulated state — the same category as worktrees and
    # agent-runs — and the code volume has no .beads on a fresh node, because
    # it is gitignored and never in the image. Sourcing it from REPO_ROOT
    # emitted volume-subpath=.beads against autonomy-code, docker refused the
    # missing subpath, and NO CONTAINER WAS CREATED: every downstream symptom
    # on a fresh node, including a tmux session that looked like it died, was
    # this one mount (auto-qk4ip, found on sjc-2).
    plan.set(mount_spec(DATA_ROOT / ".beads", "/data/.beads"), replace=False)
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

    # DECLARE the optional tool mounts, including the Codex auth.json at the path
    # it WILL occupy — materialize_auth=False writes no credential here. The caller
    # writes it only after mount validation succeeds (criterion 6).
    codex_auth_target = None
    for host_path, container_spec in _resolve_optional_tool_mounts(
        worktree_host=worktree_host, run_dir=run_dir, materialize_auth=False,
    ).items():
        plan.set(mount_spec(host_path, container_spec))
        if container_spec.split(":")[0] == "/home/agent/.codex/auth.json":
            codex_auth_target = host_path

    if global_claude_md is not None:
        plan.set(mount_spec(global_claude_md, "/home/agent/.claude/CLAUDE.md:ro"))
        # Mirror to AGENTS.md so Codex picks up the same workspace primer.
        plan.set(mount_spec(global_claude_md, "/home/agent/.codex/AGENTS.md:ro"))
    if startup_script is not None:
        plan.set(mount_spec(startup_script, "/startup.sh:ro"))

    return plan, shim_env, codex_auth_target


# ── Main Launch Function ──────────────────────────────────────────────────────

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
                    remains the default; ``codex`` is also supported for
                    interactive and non-interactive runs.
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
        needs_nested_docker: Preserve the image's nested-daemon entrypoint
                    (Dockerfile.dind and descendants). This controls image
                    behavior only; it does not select container isolation.
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

    Returns:
        detach=True:  container_id string on success, None on failure.
        detach=False: docker command string on success, None on failure.
    """
    if harness not in {"claude", "codex"}:
        print(
            f"  ERROR: unsupported harness {harness!r} for session '{name}'",
            file=sys.stderr,
        )
        return None
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
    # sessions use the optional ~/.codex mounts instead, so they must not
    # hard-fail on missing Claude credentials.
    auth_args: list[str] = []
    creds: dict | None = None
    if harness == "claude":
        creds = _resolve_credentials()
        _lap("resolve_credentials")
        if creds is None:
            print(
                f"  ERROR: No Claude credentials found for {session_type} session '{name}'",
                file=sys.stderr,
            )
            return None

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
    # org / graph_tags come in via `metadata` and are also exported as
    # GRAPH_ORG / GRAPH_TAGS env vars below so the in-container graph CLI
    # routes to the right org DB and applies tags.
    #
    # graph_org (the per-org DB routing slug ingest.py reads) is derived
    # from "org" (canonical, auto-nuupw) or the legacy "graph_project" key
    # if the caller didn't supply "graph_org" explicitly.
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
        if creds is not None and creds.get("harness_token"):
            # Operator-facing credential pointer for triage. The dashboard
            # reads this back when the session is registered so the drawer
            # can show "which Anthropic account is this session burning".
            # auto-08n3f: this is now the Anthropic org UUID (the substrate
            # key on dashboard.claude.credentials). The dashboard joins it
            # to the row's friendly ``alias`` for display.
            meta_doc["harness_token"] = creds["harness_token"]
        if metadata:
            meta_doc.update(metadata)
            if "graph_org" not in meta_doc:
                resolved = meta_doc.get("org") or meta_doc.get("graph_project")
                if resolved:
                    meta_doc["graph_org"] = resolved
        (sessions_dir / ".session_meta.json").write_text(json.dumps(meta_doc, indent=2))

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
        VolumeSubpathUnsupported, BindRefuseMissing,
    )
    # f51kg: give this session its OWN memory-only secret directory, bound at
    # /run/secrets. On a containerized node it is created 0700 + chowned to the
    # session uid (1000, the agent user) via a privileged host-ns helper BEFORE
    # the container starts, then bound refuse-missing so a race that removes it
    # fails the launch rather than yielding a look-alike on-disk directory. In
    # `delivered` mode the SESSION writes the plaintext here (no dashboard
    # variable holds it), on ramfs (never swappable), 0700-isolated from other
    # sessions. Best-effort: a node without the delivery ramfs launches without
    # it and secret delivery fails closed later — never a launch that needs no
    # secret. Cleanup is the sweeper's (auto-pw9bs.5); an early session-end
    # unlink is a memory-reclamation optimisation only. (An empty subdir left by
    # a launch refused downstream is reclaimed by the sweeper — not an authority
    # leak.)
    mounts = dict(mounts or {})
    try:
        from agents import secret_ramfs
        _secret_host_dir = secret_ramfs.provision_session_dir(name, 1000)
        mounts[_secret_host_dir] = BindRefuseMissing(
            f"{secret_ramfs.SESSION_SECRET_DST}:rw"
        )
    except Exception:
        logger.warning(
            "session %s: per-session secret directory unavailable — secret "
            "delivery will fail closed for this session", name, exc_info=True,
        )
    plan, shim_env, codex_auth_target = build_mount_plan(
        run_dir=run_dir,
        sessions_dir=sessions_dir,
        harness=harness,
        working_dir=working_dir,
        caller_mounts=mounts,
        include_capabilities=True,
        capabilities=capabilities,
        global_claude_md=global_claude_md,
        startup_script=startup_script,
    )

    # Resolve+validate the DECLARED plan into argv NOW — before any authority is
    # minted below (the session token, the materialized Codex credential). A
    # socket / unresolvable / subpath refusal returns here having written nothing
    # and minted nothing, so there is nothing to leak or clean up (auto-vm8qh
    # criterion 6). The validated argv is spliced into cmd at the emission point.
    _topo = discover_topology()
    try:
        _mount_argv = mount_args(plan, _topo)
    except SocketMountRefused:
        print(f"  ERROR: refusing host Docker socket mount for session '{name}'",
              file=sys.stderr)
        return None
    except (MountUnresolvable, VolumeSubpathUnsupported) as _exc:
        print(f"  ERROR: {_exc}", file=sys.stderr)
        return None

    # Validation passed — NOW it is safe to materialize the Codex credential the
    # plan declared (at codex_auth_target). Nothing above this line wrote a
    # credential or minted a token.
    codex_auth_copy = None
    if codex_auth_target is not None:
        # A credential was DECLARED (a usable row existed at plan time, so the
        # validated argv already binds codex_auth_target). If materialization
        # fails HERE (write error, or the row expired/raced away between declare
        # and now), the file the argv references does not exist: host-process -v
        # would fabricate a directory there, and the fallback bind would fail at
        # docker-run — but only AFTER the token below is minted. So a declared
        # credential that fails to materialize is a launch refusal, taken before
        # any authority is minted, leaving no partial file behind.
        if _materialize_codex_auth_json(run_dir) is None:
            _delete_if_present(codex_auth_target)
            print(
                f"  ERROR: refusing to launch session '{name}': a Codex "
                "credential was declared but failed to materialize",
                file=sys.stderr,
            )
            return None
        codex_auth_copy = codex_auth_target  # str path, for post-exit cleanup

    # Preflight EVERY input the docker run depends on that could be missing —
    # the image, the runtime, and every mount source (host binds AND
    # volume-subpaths) — and refuse with the whole list, by name, BEFORE minting
    # the token below. `docker run` with any of these missing creates NO
    # container and reports only a nameless failure (an hour lost to a missing
    # image; the wjzh4/qk4ip class for mounts). Discover what is missing first,
    # do not hand docker a doomed command. Runs HERE (after the codex credential
    # is materialized above) so every declared source already exists and nothing
    # needs excluding. Each check fails OPEN if it cannot run: docker stays the
    # backstop, we lose only the naming, never a good launch.
    from agents import launch_preflight
    _problems = launch_preflight.preflight(
        image=image, runtime_args=runtime_args, plan=plan, topo=_topo,
        credential_keys=(_declared_credential_keys(capabilities)
                         | _credential_keys_in_env(extra_env)),
    )
    if _problems:
        print(
            f"  ERROR: refusing to launch session '{name}': "
            f"{len(_problems)} launch input(s) missing — docker would fail to "
            f"create a container with no useful message:\n"
            + "\n".join(p.line() for p in _problems),
            file=sys.stderr,
        )
        if codex_auth_copy is not None:
            _delete_if_present(codex_auth_copy)
        return None

    _lap("mounts_assembled")

    # ── Session token ────────────────────────────────────────────
    from tools.dashboard.dao import auth_db
    # A container token is authoritative for the caller's organization, so it is
    # stamped ONLY from the canonical metadata["org"] key — never the
    # graph_org/graph_project fallback chain that feeds the advisory GRAPH_ORG
    # env below. A container that cannot be assigned an org must not receive a
    # token: fail the launch loudly rather than mint an org-less container token
    # (which the caller-org guard would refuse anyway). No backfill exists, so
    # this is the only thing keeping every live container token org-stamped.
    token_org = (metadata or {}).get("org")
    if not isinstance(token_org, str) or not token_org.strip():
        print(
            f"  ERROR: refusing to launch session '{name}' without a canonical "
            "metadata['org'] to stamp on its session token",
            file=sys.stderr,
        )
        return None
    token_org = token_org.strip()
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    auth_db.insert_token(token_hash, name, token_org)
    _lap("session_token")

    # ── Networking ─────────────────────────────────────────────
    # host-networked containers can just use localhost; bridge-networked
    # ones need host.docker.internal + an --add-host entry so DNS resolves
    # to the docker bridge gateway.
    if network_host:
        network_args = ["--network=host"]
        graph_api = "https://localhost:8080"
    else:
        network_args = ["--add-host=host.docker.internal:host-gateway"]
        graph_api = "https://host.docker.internal:8080"

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
        "-e", f"GRAPH_API={graph_api}",
        "-e", f"CROSSTALK_TOKEN={raw_token}",
        "-e", "CODEX_HOME=/home/agent/.codex",
        *auth_args,
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

    if extra_env:
        for k, v in extra_env.items():
            # Workspace env resolves ONLY the credential: scheme through the
            # vault — Anchore's GH tokens live here as credential:<key> after the
            # migration. Every other value stays the literal it has always been;
            # a value like "host:8080" or "file:///x" must NOT be reinterpreted
            # as a source scheme (that is why this is not a blanket
            # _resolve_env_source over workspace env). A credential: that cannot
            # resolve — cold vault (named at preflight above) or absent key —
            # drops the binding rather than injecting a wrong/empty value; the
            # value is never logged.
            if isinstance(v, str) and v.startswith(_CREDENTIAL_ENV_PREFIX):
                key = v[len(_CREDENTIAL_ENV_PREFIX):].strip()
                resolved = _resolve_credential(key) if key else None
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
    for k, v in _capability_env(capabilities).items():
        cmd.extend(["-e", f"{k}={v}"])

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

    # Entrypoint, image, and arguments.
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
        if needs_nested_docker:
            # Keep the dind wrapper entrypoint so /startup.sh still runs.
            cmd += [image, "sh", "-c", shell_cmd]
        else:
            cmd += ["--entrypoint", "sh", image, "-c", shell_cmd]
    else:
        if harness == "claude":
            if needs_nested_docker:
                cmd += [
                    image,
                    "claude",
                    "--dangerously-skip-permissions",
                    "--model",
                    resolved_model or DEFAULT_OPUS_MODEL,
                ]
            else:
                cmd += [
                    image,
                    "--dangerously-skip-permissions",
                    "--model",
                    resolved_model or DEFAULT_OPUS_MODEL,
                ]
            if resume_uuid:
                cmd += ["--resume", resume_uuid]
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
            if needs_nested_docker:
                cmd += [image, *codex_args]
            else:
                cmd += ["--entrypoint", "codex", image, *codex_args[1:]]

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

        # Schedule credential cleanup after container exits
        creds_copy = creds.get("creds_copy") if creds else None
        if creds_copy:
            _schedule_creds_cleanup(container_id, creds_copy)
        if codex_auth_copy:
            _schedule_creds_cleanup(container_id, codex_auth_copy)

        return container_id

    else:
        # For tmux-based sessions: return a shell-safe command string
        return shlex.join(cmd)
