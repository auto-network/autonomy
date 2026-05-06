"""Unified session launcher for all agent containers.

All four launch paths (dispatch, librarian, chatwith, terminal) go through
launch_session(), which handles:
- Credential resolution (one implementation)
- Default volume mounts: repo (ro), graph.db (ro), .beads (rw), per-run sessions dir
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
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IMAGE = "autonomy-agent:dashboard"
DEFAULT_OPUS_MODEL = "claude-opus-4-7[1m]"


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
    * any declared repo-local ``tool_paths`` mount under
      ``<mount_target>/<basename>`` so the package surface matches the
      repo layout. Subpaths inside the package root would be redundant
      with the package mount; only paths that escape the package root
      get an extra mount entry.
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
            # Skip subpaths already covered by the package-root mount.
            try:
                tool_host.relative_to(package_host)
                continue
            except ValueError:
                pass
            container_path = f"{cap.mount_target}/{Path(tool_path).name}"
            mounts[str(tool_host)] = f"{container_path}:ro"
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

    Returns ``(mounts, env)`` — both empty when no capability declares an
    ``expose_commands`` list, so the function is a no-op for callers that
    do not opt in.
    """
    shims_to_emit: list[tuple[str, str]] = []
    for cap in capabilities:
        tt = getattr(cap, "tool_target", None)
        if tt is None:
            continue
        for cmd in tt.expose_commands:
            shims_to_emit.append((cmd, f"{tt.target}/{cmd}"))
    if not shims_to_emit:
        return {}, {}
    shim_dir = run_dir / "cap-bin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    for cmd, exec_target in shims_to_emit:
        shim_path = shim_dir / cmd
        shim_path.write_text(
            f'#!/bin/sh\nexec {exec_target} "$@"\n'
        )
        # 0755 — executable for everyone so the agent user can invoke it.
        shim_path.chmod(0o755)
    return (
        {str(shim_dir): f"{CAPABILITY_BIN_DIR}:ro"},
        {"AUTONOMY_CAPABILITY_BIN": CAPABILITY_BIN_DIR},
    )


_HOST_ENV_PREFIX = "host:"
_FILE_ENV_PREFIX = "file:"


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

    # Plain literal value — backward compat with test fixtures and any
    # caller that hasn't migrated to the source-scheme syntax.
    return source


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


# ── Credential Resolution ─────────────────────────────────────────────────────


CLAUDE_TOKEN_FILE_PREFIX = ".setup-token"
CLAUDE_DEFAULT_ALIAS = "default"


def _claude_credentials_dir() -> Path:
    return Path(
        os.environ.get(
            "CLAUDE_CREDENTIALS_DIR", str(Path.home() / ".claude"),
        )
    )


def list_claude_token_files() -> list[Path]:
    """Return every ``~/.claude/.setup-token*`` file on the host, sorted.

    Each file is one Claude account: ``.setup-token`` is the default
    account (alias ``"default"``); ``.setup-token.<alias>`` carries the
    suffix as its alias. Operators add accounts by saving a fresh
    setup-token from the Anthropic console under a new suffix; ``rm``
    removes one. ``ls`` is the registry — no separate config file.

    Honors ``CLAUDE_CREDENTIALS_DIR`` for tests. Missing dirs return ``[]``.
    """
    creds_dir = _claude_credentials_dir()
    if not creds_dir.is_dir():
        return []
    return sorted(
        p for p in creds_dir.iterdir()
        if p.is_file() and p.name.startswith(CLAUDE_TOKEN_FILE_PREFIX)
    )


def alias_for_token_file(p: Path) -> str:
    """Map a ``.setup-token*`` filename to its operator-facing alias.

    ``.setup-token`` → ``"default"``; ``.setup-token.primary`` →
    ``"primary"``. Mirror of the convention enforced by
    :func:`list_claude_token_files`.
    """
    if p.name == CLAUDE_TOKEN_FILE_PREFIX:
        return CLAUDE_DEFAULT_ALIAS
    return p.name.removeprefix(CLAUDE_TOKEN_FILE_PREFIX + ".")


def _read_token_file(p: Path) -> str | None:
    try:
        token = p.read_text().strip()
    except OSError:
        return None
    return token or None


def _claude_usage_rows() -> list[dict]:
    """Return live ``dashboard.harness.usage`` rows for Claude.

    Best-effort — failures (DB unavailable, settings substrate missing,
    etc.) collapse to ``[]`` so token selection falls back to alphabetical
    order rather than crashing the launch path.
    """
    try:
        from tools.graph import settings_ops
        from tools.dashboard import harness_usage_settings as hus
    except Exception:
        return []
    try:
        members = settings_ops.read_set(
            hus.HARNESS_USAGE_SET_ID, org=settings_ops.CALLER_ORG,
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


def _token_headroom_score(payload: dict) -> tuple[float, float]:
    """Return ``(short_headroom, long_headroom)`` for sort ranking.

    Higher headroom = more capacity remaining = pick first. Missing or
    malformed values collapse to 0 so a row with no telemetry sorts
    BELOW a row with valid <100% utilization but ABOVE one at 100%.
    """
    windows = payload.get("windows") if isinstance(payload, dict) else None
    if not isinstance(windows, dict):
        return (0.0, 0.0)
    short = windows.get("short") if isinstance(windows.get("short"), dict) else {}
    long_ = windows.get("long") if isinstance(windows.get("long"), dict) else {}

    def _headroom(window: dict) -> float:
        used = window.get("used_percent")
        if not isinstance(used, (int, float)):
            return 0.0
        return max(0.0, 100.0 - float(used))

    return (_headroom(short), _headroom(long_))


def _select_token_alias(
    files: list[Path], *, prefer_alias: str | None,
) -> Path | None:
    """Choose which ``.setup-token*`` file to use for a launch.

    Selection rule (graph note: see bead description):

    * Caller-supplied ``prefer_alias`` wins when the file exists.
    * Otherwise rank by ``dashboard.harness.usage`` headroom — highest
      ``(100 - short.used_percent)`` first, ties broken by long-window
      headroom. Tokens with no usage row yet (first launch) rank at top
      so brand-new accounts get used.
    * Within ranks, alphabetical alias order is the final tiebreak so
      the same-utilization case is deterministic across launches.
    """
    if not files:
        return None
    if prefer_alias:
        for p in files:
            if alias_for_token_file(p) == prefer_alias:
                return p
        # Unknown alias falls through to rate-limit selection rather
        # than failing — the operator's intent is a hint, not a hard
        # constraint, and a typo shouldn't block dispatch.

    rows_by_alias: dict[str, dict] = {}
    for payload in _claude_usage_rows():
        alias = payload.get("alias")
        if isinstance(alias, str) and alias:
            rows_by_alias[alias] = payload

    def _rank(p: Path) -> tuple[float, float, str, str]:
        alias = alias_for_token_file(p)
        payload = rows_by_alias.get(alias)
        if payload is None:
            # First-launch / no telemetry yet: rank at top so brand-new
            # tokens get exercised before ones we've already seen.
            return (1e9, 1e9, "0", alias)
        short, long_ = _token_headroom_score(payload)
        return (short, long_, "1", alias)

    ranked = sorted(files, key=_rank, reverse=True)
    return ranked[0]


def _resolve_credentials(
    *, prefer_alias: str | None = None,
) -> dict | None:
    """Resolve Claude credentials from env, setup-token files, or credentials file.

    Returns a dict with 'type' key:
      {"type": "token", "token": "...", "alias": "default" | "primary" | ...}
      {"type": "creds_file", "path": "/path/to/.credentials.json"}
    Returns None if no credentials found.

    Selection order:
      1. ``CLAUDE_CODE_OAUTH_TOKEN`` env var → returned without an alias
         (legacy compat path; carrier of caller intent).
      2. Multi-file enumeration of ``~/.claude/.setup-token*``. When
         multiple files exist, ``prefer_alias`` wins; otherwise pick by
         rate-limit headroom from the live ``dashboard.harness.usage``
         rows (see :func:`_select_token_alias`).
      3. ``~/.claude/.credentials.json`` (legacy creds-file path) when no
         setup-token files exist.
    """
    creds_dir = _claude_credentials_dir()

    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if oauth_token:
        return {"type": "token", "token": oauth_token}

    files = list_claude_token_files()
    chosen = _select_token_alias(files, prefer_alias=prefer_alias)
    if chosen is not None:
        token = _read_token_file(chosen)
        if token:
            return {
                "type": "token",
                "token": token,
                "alias": alias_for_token_file(chosen),
            }

    creds_file = creds_dir / ".credentials.json"
    if creds_file.exists():
        return {"type": "creds_file", "path": str(creds_file)}

    return None


def _setup_auth_docker_args(creds: dict, run_dir: Path) -> list[str] | None:
    """Convert resolved credentials to docker run args.

    For creds_file type, copies the credentials file into run_dir and adds
    a volume mount for it. Sets creds["creds_copy"] to the copy path so the
    caller can schedule cleanup.

    Returns docker arg list, or None if creds type is unrecognised.
    """
    if creds["type"] == "token":
        return ["-e", f"CLAUDE_CODE_OAUTH_TOKEN={creds['token']}"]

    if creds["type"] == "creds_file":
        creds_copy = run_dir / ".credentials.json"
        shutil.copy2(creds["path"], str(creds_copy))
        creds["creds_copy"] = str(creds_copy)
        return ["-v", f"{creds_copy}:/home/agent/.claude/.credentials.json:ro"]

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


def _resolve_optional_tool_mounts() -> dict[str, str]:
    """Return optional host mounts that make Codex usable inside containers.

    Claude is already handled via dedicated credential resolution plus the
    mounted sessions directory. Codex keeps its login/config under ~/.codex,
    so mount only the durable control files and skill/rule directories rather
    than the whole mutable state tree.
    """

    mounts: dict[str, str] = {}

    host_codex_home = Path.home() / ".codex"
    codex_mounts = {
        host_codex_home / "auth.json": "/home/agent/.codex/auth.json:ro",
        host_codex_home / "config.toml": "/home/agent/.codex/config.toml:ro",
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
    privileged: bool = False,
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
        privileged: If True, pass ``--privileged`` to docker run. Required for
                    images that run a nested docker daemon (Dockerfile.dind
                    and its descendants). Combine with ``startup_script``.
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
    resolved_model = model or (DEFAULT_OPUS_MODEL if harness == "claude" else None)

    # ── Credentials ───────────────────────────────────────────
    # Claude sessions need host auth injected into the container. Codex
    # sessions use the optional ~/.codex mounts instead, so they must not
    # hard-fail on missing Claude credentials.
    auth_args: list[str] = []
    creds: dict | None = None
    if harness == "claude":
        creds = _resolve_credentials()
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
        run_dir = REPO_ROOT / "data" / "agent-runs" / f"{name}-{ts}"

    run_dir.mkdir(parents=True, exist_ok=True)
    sessions_dir = run_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # ── Write .session_meta.json (skip for resumed sessions) ──
    # graph_project / graph_tags come in via `metadata` and are also exported
    # as GRAPH_SCOPE / GRAPH_TAGS env vars below so the in-container graph CLI
    # respects the project's hard boundary and soft tags.
    #
    # graph_org (the per-org DB routing slug) is derived from graph_project
    # if the caller didn't supply it explicitly — after the workspaces→orgs
    # consolidation (auto-0wj9) the yaml's ``graph_project`` field IS the
    # owning org slug, so a single source of truth carries both concerns.
    if not resume_uuid:
        meta_doc: dict = {
            "type": session_type,
            "container_name": name,
            "launched_at": datetime.now(timezone.utc).isoformat(),
            "harness": harness,
        }
        if creds is not None and creds.get("alias"):
            # Operator-facing credential pointer for triage. The dashboard
            # reads this back when the session is registered so the drawer
            # can show "which Anthropic account is this session burning".
            # auto-ghhdg renamed the field from claude_token_alias to
            # harness_token (harness-agnostic; will hold an org UUID once
            # bead 4 of the substrate-credentials cutover lands).
            meta_doc["harness_token"] = creds["alias"]
        if metadata:
            meta_doc.update(metadata)
            if "graph_org" not in meta_doc:
                gp = meta_doc.get("graph_project")
                if gp:
                    meta_doc["graph_org"] = gp
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

    # ── Build default volume mount table ──────────────────────
    # Key: host path.  Value: container_path[:mode]
    # The table is ordered; callers can override any entry by matching container path.
    transcript_mount = (
        "/home/agent/.codex/sessions"
        if harness == "codex"
        else "/home/agent/.claude/projects"
    )
    default_mounts: dict[str, str] = {
        str(REPO_ROOT): "/workspace/repo:ro",
        str(REPO_ROOT / "data" / "graph.db"): "/home/agent/graph.db:ro",
        str(REPO_ROOT / ".beads"): "/data/.beads",
        str(run_dir): "/workspace/output",
        str(sessions_dir): transcript_mount,
    }

    # Apply caller overrides: if a caller mount targets the same container path
    # as a default, replace the default.
    if mounts:
        for host_path, container_spec in mounts.items():
            container_path = container_spec.split(":")[0]
            # Drop any default that maps to the same container path
            for dk in list(default_mounts):
                if default_mounts[dk].split(":")[0] == container_path:
                    del default_mounts[dk]
            default_mounts[str(host_path)] = container_spec

    # Capability mounts: package roots, tool subtrees, and secret files for
    # every enabled MaterializedCapability. Caller-supplied mounts for the
    # same container path still win (matching the override semantics above).
    for host_path, container_spec in _capability_mounts(capabilities).items():
        container_path = container_spec.split(":")[0]
        if any(
            spec.split(":")[0] == container_path
            for spec in default_mounts.values()
        ):
            continue
        default_mounts[host_path] = container_spec

    # Per-session shim directory for ``tool_target.expose_commands``.
    # Built lazily — when no capability declares expose_commands, no
    # disk artefact, mount, or env var is created.
    shim_mounts, shim_env = _capability_command_surface(capabilities, run_dir)
    for host_path, container_spec in shim_mounts.items():
        container_path = container_spec.split(":")[0]
        if any(
            spec.split(":")[0] == container_path
            for spec in default_mounts.values()
        ):
            continue
        default_mounts[host_path] = container_spec

    # ── CrossTalk token ──────────────────────────────────────────
    from tools.dashboard.dao import auth_db
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    auth_db.insert_token(token_hash, name)

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

    # Project / org scoping:
    #   GRAPH_SCOPE — hard project boundary (search/list filter).
    #   GRAPH_TAGS  — soft tags auto-applied to notes.
    #   GRAPH_ORG   — per-org write routing slug (auto-txg5.3). Every
    #                 ops.* write in this container lands in that org's DB.
    #                 Defaults to the same value as graph_project (the yaml
    #                 field IS the org slug after auto-0wj9).
    if metadata:
        graph_project = metadata.get("graph_project")
        if graph_project:
            cmd.extend(["-e", f"GRAPH_SCOPE={graph_project}"])
        graph_tags = metadata.get("graph_tags")
        if graph_tags:
            if isinstance(graph_tags, (list, tuple)):
                graph_tags = ",".join(str(t) for t in graph_tags)
            cmd.extend(["-e", f"GRAPH_TAGS={graph_tags}"])
        graph_org = metadata.get("graph_org") or graph_project
        if graph_org:
            cmd.extend(["-e", f"GRAPH_ORG={graph_org}"])

    for host_path, container_spec in default_mounts.items():
        cmd.extend(["-v", f"{host_path}:{container_spec}"])

    for host_path, container_spec in _resolve_optional_tool_mounts().items():
        cmd.extend(["-v", f"{host_path}:{container_spec}"])

    if global_claude_md is not None:
        cmd.extend(["-v", f"{global_claude_md}:/home/agent/.claude/CLAUDE.md:ro"])
        # Mirror to AGENTS.md so Codex picks up the same workspace primer.
        # Claude reads CLAUDE.md, Codex reads AGENTS.md — same content,
        # different file, no harness branching needed.
        cmd.extend(["-v", f"{global_claude_md}:/home/agent/.codex/AGENTS.md:ro"])

    # Per-project startup script (used by the dind entrypoint wrapper).
    if startup_script is not None:
        cmd.extend(["-v", f"{startup_script}:/startup.sh:ro"])

    if extra_env:
        for k, v in extra_env.items():
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

    if privileged:
        cmd.insert(2, "--privileged")

    # Mode flags: -d for detached, -it --rm for interactive
    if detach:
        cmd.insert(2, "-d")
    else:
        cmd.insert(2, "--rm")
        cmd.insert(2, "-it")

    # Entrypoint, image, and arguments.
    # Base images have ENTRYPOINT=["claude", "--dangerously-skip-permissions"];
    # dind-based images have a shell wrapper that does `exec "$@"` so the
    # caller must pass the full command starting with `claude`. `privileged`
    # is the proxy for dind here.
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
        if privileged:
            # Keep the dind wrapper entrypoint so /startup.sh still runs.
            cmd += [image, "sh", "-c", shell_cmd]
        else:
            cmd += ["--entrypoint", "sh", image, "-c", shell_cmd]
    else:
        if harness == "claude":
            if privileged:
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
            if privileged:
                cmd += [image, *codex_args]
            else:
                cmd += ["--entrypoint", "codex", image, *codex_args[1:]]

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

        return container_id

    else:
        # For tmux-based sessions: return a shell-safe command string
        return shlex.join(cmd)
