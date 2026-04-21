"""Unified session launcher for all Claude agent containers.

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
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IMAGE = "autonomy-agent:dashboard"
DEFAULT_OPUS_MODEL = "claude-opus-4-7[1m]"


# ── Credential Resolution ─────────────────────────────────────────────────────

def _resolve_credentials() -> dict | None:
    """Resolve Claude credentials from env, setup-token, or credentials file.

    Returns a dict with 'type' key:
      {"type": "token", "token": "..."}
      {"type": "creds_file", "path": "/path/to/.credentials.json"}
    Returns None if no credentials found.
    """
    creds_dir = Path(os.environ.get("CLAUDE_CREDENTIALS_DIR", str(Path.home() / ".claude")))
    setup_token_file = creds_dir / ".setup-token"
    creds_file = creds_dir / ".credentials.json"

    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not oauth_token and setup_token_file.exists():
        try:
            oauth_token = setup_token_file.read_text().strip()
        except OSError:
            pass

    if oauth_token:
        return {"type": "token", "token": oauth_token}

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
    extra_env: dict | None = None,
    output_dir: str | None = None,
    model: str = DEFAULT_OPUS_MODEL,
    global_claude_md: Path | str | None = None,
    resume_uuid: str | None = None,
    privileged: bool = False,
    startup_script: str | Path | None = None,
    network_host: bool = True,
) -> str | None:
    """Launch a Claude agent container session.

    Handles credential resolution, session directory creation, .session_meta.json
    writing, default volume mounts, and docker command building/execution.

    Args:
        session_type: "dispatch" | "librarian" | "chatwith" | "terminal"
        name: Container name (used in .session_meta.json and as label)
        prompt: Prompt text for --print batch mode. None for interactive sessions.
        mounts: Extra volume mounts {host_path: "container_path[:mode]"}.
                Entries whose container path matches a default override it.
        metadata: Extra fields merged into .session_meta.json
                  (e.g. bead_id, job_id, context_id).
        detach: True  → docker run -d, returns container_id string on success.
                False → builds docker run -it --rm command, returns it as a
                        shell-safe string for the caller to pass to tmux.
        image: Docker image to use.
        working_dir: Working directory inside the container.
        extra_env: Additional environment variables {key: value}.
        output_dir: Pre-created output directory. If None, a new directory under
                    data/agent-runs/ is created using name + UTC timestamp.
        model: Claude model to pass via --model flag. Defaults to DEFAULT_OPUS_MODEL.
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

    Returns:
        detach=True:  container_id string on success, None on failure.
        detach=False: docker command string on success, None on failure.
    """
    # ── Credentials ───────────────────────────────────────────
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
        }
        if metadata:
            meta_doc.update(metadata)
            if "graph_org" not in meta_doc:
                gp = meta_doc.get("graph_project")
                if gp:
                    meta_doc["graph_org"] = gp
        (sessions_dir / ".session_meta.json").write_text(json.dumps(meta_doc, indent=2))

    # ── Auth args (may copy creds file into run_dir) ───────────
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
    default_mounts: dict[str, str] = {
        str(REPO_ROOT): "/workspace/repo:ro",
        str(REPO_ROOT / "data" / "graph.db"): "/home/agent/graph.db:ro",
        str(REPO_ROOT / ".beads"): "/data/.beads",
        str(run_dir): "/workspace/output",
        str(sessions_dir): "/home/agent/.claude/projects",
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

    if global_claude_md is not None:
        cmd.extend(["-v", f"{global_claude_md}:/home/agent/.claude/CLAUDE.md:ro"])

    # Per-project startup script (used by the dind entrypoint wrapper).
    if startup_script is not None:
        cmd.extend(["-v", f"{startup_script}:/startup.sh:ro"])

    if extra_env:
        for k, v in extra_env.items():
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
        resume_flag = f" --resume {resume_uuid}" if resume_uuid else ""
        shell_cmd = f"cat /workspace/output/.prompt.md | claude --dangerously-skip-permissions --model {model}{resume_flag} -p"
        if privileged:
            # Keep the dind wrapper entrypoint so /startup.sh still runs.
            cmd += [image, "sh", "-c", shell_cmd]
        else:
            cmd += ["--entrypoint", "sh", image, "-c", shell_cmd]
    else:
        if privileged:
            cmd += [image, "claude", "--dangerously-skip-permissions", "--model", model]
        else:
            cmd += [image, "--dangerously-skip-permissions", "--model", model]
        if resume_uuid:
            cmd += ["--resume", resume_uuid]

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
        creds_copy = creds.get("creds_copy")
        if creds_copy:
            _schedule_creds_cleanup(container_id, creds_copy)

        return container_id

    else:
        # For tmux-based sessions: return a shell-safe command string
        return shlex.join(cmd)
