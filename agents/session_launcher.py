"""Unified session launcher for all Claude agent containers.

All four launch paths (dispatch, librarian, chatwith, terminal) go through
launch_session(), which handles:
- Credential resolution (one implementation)
- Default volume mounts: repo (ro), graph.db (rw), .beads (rw), per-run sessions dir
- Session directory creation
- Writing .session_meta.json with type + metadata + timestamp
- Building and executing the docker run command
- Credential cleanup after container exits

Returns container_id (detach=True) or docker command string (detach=False),
or None on failure.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IMAGE = "autonomy-agent"


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
    model: str = "claude-opus-4-6",
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
        model: Claude model to pass via --model flag. Defaults to claude-opus-4-6.

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
    if output_dir is not None:
        run_dir = Path(output_dir)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        run_dir = REPO_ROOT / "data" / "agent-runs" / f"{name}-{ts}"

    run_dir.mkdir(parents=True, exist_ok=True)
    sessions_dir = run_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    # ── Write .session_meta.json ───────────────────────────────
    meta_doc: dict = {
        "type": session_type,
        "container_name": name,
        "launched_at": datetime.now(timezone.utc).isoformat(),
    }
    if metadata:
        meta_doc.update(metadata)
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
        str(REPO_ROOT / "data" / "graph.db"): "/home/agent/graph.db",
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

    # ── Assemble docker command ────────────────────────────────
    cmd: list[str] = [
        "docker", "run",
        "--name", name,
        "--network=host",
        "-e", f"BD_ACTOR={session_type}:{name}",
        "-e", "BD_READONLY=0",
        "-e", "GRAPH_DB=/home/agent/graph.db",
        *auth_args,
    ]

    for host_path, container_spec in default_mounts.items():
        cmd.extend(["-v", f"{host_path}:{container_spec}"])

    if extra_env:
        for k, v in extra_env.items():
            cmd.extend(["-e", f"{k}={v}"])

    cmd.extend(["-w", working_dir])

    # Mode flags: -d for detached, -it --rm for interactive
    if detach:
        cmd.insert(2, "-d")
    else:
        cmd.insert(2, "--rm")
        cmd.insert(2, "-it")

    # Entrypoint, image, and arguments
    # Write prompt to file instead of passing on command line — avoids the
    # prompt text appearing in /proc/cmdline where pkill -f can match it.
    if prompt is not None:
        prompt_file = run_dir / ".prompt.md"
        prompt_file.write_text(prompt)
        cmd += ["--entrypoint", "sh", image,
                "-c", f"cat /workspace/output/.prompt.md | claude --dangerously-skip-permissions --model {model} -p"]
    else:
        cmd += [image, "--dangerously-skip-permissions", "--model", model]

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
