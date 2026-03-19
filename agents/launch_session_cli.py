"""CLI wrapper around launch_session() for use by agents/launch.sh.

Called by launch.sh for the docker run portion of agent dispatch.
Writes .session_meta.json, resolves credentials, and executes docker run -d.

Usage:
    python -m agents.launch_session_cli \
        --session-type dispatch \
        --name agent-auto-xyz-12345 \
        --prompt-file /path/to/prompt.md \
        --bead-id auto-xyz \
        --worktree /path/to/worktree \
        --git-dir /path/to/.git \
        --output-dir /path/to/output \
        --image autonomy-agent \
        [--detach]

Prints to stdout (parseable by bash):
    CONTAINER_ID=<id>       (detach mode)
    OUTPUT_DIR=<path>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agents.session_launcher import launch_session, DEFAULT_IMAGE, REPO_ROOT


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch a Claude agent session container")
    parser.add_argument("--session-type", default="dispatch",
                        choices=["dispatch", "librarian", "chatwith", "terminal"])
    parser.add_argument("--name", required=True, help="Container name")
    parser.add_argument("--prompt-file", help="Path to prompt file (reads content)")
    parser.add_argument("--bead-id", default="", help="Bead ID for metadata")
    parser.add_argument("--worktree", default="", help="Worktree path (overrides /workspace/repo)")
    parser.add_argument("--git-dir", default="", help="Git dir path (absolute, same on host+container)")
    parser.add_argument("--output-dir", default="", help="Pre-created output directory")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help="Docker image")
    parser.add_argument("--detach", action="store_true", help="Run container in background")
    args = parser.parse_args()

    # Read prompt from file if provided
    prompt: str | None = None
    if args.prompt_file:
        prompt_path = Path(args.prompt_file)
        if not prompt_path.exists():
            print(f"ERROR: prompt file not found: {args.prompt_file}", file=sys.stderr)
            return 1
        prompt = prompt_path.read_text()

    # Build extra mounts for dispatch (worktree overrides repo, git dir added)
    extra_mounts: dict[str, str] = {}
    if args.worktree:
        # Override the default read-only repo mount with the writable worktree
        extra_mounts[args.worktree] = "/workspace/repo"
    if args.git_dir:
        # Mount .git at the same absolute path so worktree's .git file reference resolves
        extra_mounts[args.git_dir] = args.git_dir

    metadata: dict = {}
    if args.bead_id:
        metadata["bead_id"] = args.bead_id

    output_dir = args.output_dir if args.output_dir else None

    if args.detach:
        container_id = launch_session(
            session_type=args.session_type,
            name=args.name,
            prompt=prompt,
            mounts=extra_mounts if extra_mounts else None,
            metadata=metadata if metadata else None,
            detach=True,
            image=args.image,
            output_dir=output_dir,
        )
        if not container_id:
            return 1

        resolved_output = output_dir or str(REPO_ROOT / "data" / "agent-runs" / args.name)
        print(f"CONTAINER_ID={container_id}")
        print(f"OUTPUT_DIR={resolved_output}")
        return 0

    else:
        # Foreground mode: run docker command blocking (no -it, no -d)
        # Build command without detach and without -it for batch --print mode
        import shlex
        import subprocess
        from agents.session_launcher import (
            _resolve_credentials, _setup_auth_docker_args,
            _schedule_creds_cleanup,
        )
        from datetime import datetime, timezone

        creds = _resolve_credentials()
        if creds is None:
            print("ERROR: No Claude credentials found", file=sys.stderr)
            return 1

        if output_dir:
            run_dir = Path(output_dir)
        else:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            run_dir = REPO_ROOT / "data" / "agent-runs" / f"{args.name}-{ts}"

        run_dir.mkdir(parents=True, exist_ok=True)
        sessions_dir = run_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        # Write session meta
        import json
        meta_doc = {
            "type": args.session_type,
            "container_name": args.name,
            "launched_at": datetime.now(timezone.utc).isoformat(),
        }
        if metadata:
            meta_doc.update(metadata)
        (sessions_dir / ".session_meta.json").write_text(json.dumps(meta_doc, indent=2))

        auth_args = _setup_auth_docker_args(creds, run_dir)
        if auth_args is None:
            return 1

        mounts = {
            str(REPO_ROOT): "/workspace/repo:ro",
            str(REPO_ROOT / "data" / "graph.db"): "/home/agent/graph.db",
            str(REPO_ROOT / ".beads"): "/data/.beads",
            str(run_dir): "/workspace/output",
            str(sessions_dir): "/home/agent/.claude/projects",
        }
        if args.worktree:
            # Remove default repo mount, add writable worktree
            for k in list(mounts):
                if mounts[k].split(":")[0] == "/workspace/repo":
                    del mounts[k]
            mounts[args.worktree] = "/workspace/repo"
        if args.git_dir:
            mounts[args.git_dir] = args.git_dir

        cmd: list[str] = [
            "docker", "run",
            "--rm",
            "--name", args.name,
            "--network=host",
            "-e", f"BD_ACTOR={args.session_type}:{args.name}",
            "-e", "BD_READONLY=0",
            "-e", "GRAPH_DB=/home/agent/graph.db",
            *auth_args,
        ]
        for host_path, container_spec in mounts.items():
            cmd.extend(["-v", f"{host_path}:{container_spec}"])
        cmd.extend(["-w", "/workspace/repo"])

        if prompt is not None:
            cmd += ["--entrypoint", "claude", args.image,
                    "--dangerously-skip-permissions", "--print", prompt]
        else:
            cmd += [args.image, "--dangerously-skip-permissions"]

        result = subprocess.run(cmd)

        # Cleanup credentials copy if used
        creds_copy = creds.get("creds_copy")
        if creds_copy:
            from pathlib import Path as _Path
            _Path(creds_copy).unlink(missing_ok=True)

        print(f"OUTPUT_DIR={run_dir}")
        return result.returncode


if __name__ == "__main__":
    sys.exit(main())
