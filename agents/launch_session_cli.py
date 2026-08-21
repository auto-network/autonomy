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
        --image autonomy-session \
        --org <org-slug> \
        [--detach]
        [--harness claude|codex]

Prints to stdout (parseable by bash):
    CONTAINER_ID=<id>       (detach mode)
    OUTPUT_DIR=<path>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agents.session_launcher import launch_session, DEFAULT_IMAGE, DEFAULT_OPUS_MODEL, REPO_ROOT
from tools.data_paths import DATA_ROOT
from agents.workspace_settings import load_workspaces


def _workspace_org(workspace_id: str) -> str | None:
    """Return the owning org slug for the workspace ``workspace_id``, if any.

    Fallback source for ``--org`` when the caller didn't pass it explicitly
    (A3: org is required, but may be resolved from workspace config).
    """
    if not workspace_id:
        return None
    try:
        ws = load_workspaces().get(workspace_id)
        return ws.graph_project if ws else None
    except Exception:
        return None


def _available_orgs() -> list[str]:
    """Best-effort list of known org slugs, for the missing-org error message."""
    try:
        orgs_dir = DATA_ROOT / "orgs"
        return sorted(p.stem for p in orgs_dir.glob("*.db"))
    except Exception:
        return []


def _workspace_model(workspace_id: str) -> str | None:
    """Return the model declared on the workspace ``workspace_id``.

    Keyed on the workspace id (the Setting key), NOT ``graph_project`` —
    several workspaces can share one graph_project (e.g. ``autonomy``,
    ``autonomy-codex``, ``autonomy-developer`` all map to ``autonomy``), so a
    graph_project lookup collapses them to the first match and the model is
    effectively per-org rather than per-workspace. Returns None when
    ``workspace_id`` is empty or unknown — callers chain through their own
    hardcoded fallback.
    """
    if not workspace_id:
        return None
    try:
        ws = load_workspaces().get(workspace_id)
        return ws.model if ws else None
    except Exception:
        return None


def _workspace_runtime(workspace_id: str) -> tuple[bool, str]:
    """Return independent nested-Docker and isolation-runtime settings."""
    if not workspace_id:
        return False, "standard"
    try:
        ws = load_workspaces().get(workspace_id)
        if ws is not None:
            return ws.needs_nested_docker, ws.session_runtime
    except Exception:
        pass
    return False, "standard"


def _bead_title(bead_id: str) -> str | None:
    """Best-effort Dolt lookup of a bead's title, done once at dispatch.

    Stamped into ``.session_meta.json`` as ``bead_title`` so ingest never
    needs to hit Dolt on the hot path (see tools/graph/ingest.py W5).
    """
    try:
        from tools.dashboard.dao.beads import get_bead_title_priority
        info = get_bead_title_priority([bead_id]).get(bead_id)
        if info and info.get("title"):
            return info["title"].strip()
    except Exception:
        pass
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch an agent session container")
    parser.add_argument("--session-type", default="dispatch",
                        choices=["dispatch", "librarian", "chatwith", "terminal"])
    parser.add_argument("--name", required=True, help="Container name")
    parser.add_argument("--prompt-file", help="Path to prompt file (reads content)")
    parser.add_argument("--bead-id", default="", help="Bead ID for metadata")
    parser.add_argument("--worktree", default="", help="Worktree path (overrides /workspace/repo)")
    parser.add_argument("--git-dir", default="", help="Git dir path (absolute, same on host+container)")
    parser.add_argument("--output-dir", default="", help="Pre-created output directory")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help="Docker image")
    parser.add_argument("--model", default="", help="Optional model override")
    parser.add_argument("--harness", default="claude", choices=["claude", "codex"],
                        help="Harness to launch inside the container")
    parser.add_argument("--detach", action="store_true", help="Run container in background")
    parser.add_argument("--org", default="",
                        help="Organization slug — selects data/orgs/<org>.db for this "
                             "session's graph writes (GRAPH_ORG env + .session_meta.json "
                             "'org' field). Required.")
    parser.add_argument("--graph-project", default="",
                        help="DEPRECATED — use --org. Kept as a warned back-compat alias.")
    parser.add_argument("--workspace-id", default="",
                        help="Workspace id (Setting key) for per-workspace model resolution "
                             "and as a fallback org source when --org is omitted")
    parser.add_argument("--graph-tags", default="",
                        help="Comma-separated graph tags (GRAPH_TAGS env + .session_meta.json field)")
    args = parser.parse_args()

    org = args.org
    if not org and args.graph_project:
        print("WARNING: --graph-project is deprecated, use --org instead", file=sys.stderr)
        org = args.graph_project
    if not org:
        org = _workspace_org(args.workspace_id)
    if not org:
        available = _available_orgs()
        print(
            "ERROR: No organization specified for this session. Pass --org <slug>. "
            "The org selects which database (data/orgs/<slug>.db) your notes and "
            f"settings are written to. Available: {', '.join(available) if available else '(none found)'}",
            file=sys.stderr,
        )
        return 1
    args.org = org

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
        bead_title = _bead_title(args.bead_id)
        if bead_title:
            metadata["bead_title"] = bead_title
    metadata["org"] = org
    if args.graph_tags:
        # Stored as a list so .session_meta.json round-trips cleanly; the
        # session_launcher converts back to comma-separated for GRAPH_TAGS.
        metadata["graph_tags"] = [t for t in args.graph_tags.split(",") if t]

    output_dir = args.output_dir if args.output_dir else None

    workspace_model = _workspace_model(args.workspace_id)
    needs_nested_docker, session_runtime = _workspace_runtime(args.workspace_id)

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
            harness=args.harness,
            model=args.model or workspace_model or None,
            needs_nested_docker=needs_nested_docker,
            runtime=session_runtime,
        )
        if not container_id:
            return 1

        resolved_output = output_dir or str(DATA_ROOT / "agent-runs" / args.name)
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
            _resolve_optional_tool_mounts,
            _schedule_creds_cleanup,
        )
        from datetime import datetime, timezone

        creds = _resolve_credentials() if args.harness == "claude" else None
        if args.harness == "claude" and creds is None:
            print("ERROR: No Claude credentials found", file=sys.stderr)
            return 1

        if output_dir:
            run_dir = Path(output_dir)
        else:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            run_dir = DATA_ROOT / "agent-runs" / f"{args.name}-{ts}"

        run_dir.mkdir(parents=True, exist_ok=True)
        sessions_dir = run_dir / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)

        # Write session meta
        import json
        meta_doc = {
            "type": args.session_type,
            "container_name": args.name,
            "launched_at": datetime.now(timezone.utc).isoformat(),
            "harness": args.harness,
        }
        if creds is not None and creds.get("harness_token"):
            # auto-08n3f: stamp the Anthropic org UUID so the dashboard
            # joins it to the friendly alias on registration.
            meta_doc["harness_token"] = creds["harness_token"]
        if metadata:
            meta_doc.update(metadata)
            if "graph_org" not in meta_doc:
                resolved = meta_doc.get("org") or meta_doc.get("graph_project")
                if resolved:
                    meta_doc["graph_org"] = resolved
        (sessions_dir / ".session_meta.json").write_text(json.dumps(meta_doc, indent=2))

        auth_args: list[str] = []
        if creds is not None:
            auth_args = _setup_auth_docker_args(creds, run_dir)
            if auth_args is None:
                return 1

        # Same table shape as launch_session's defaults: the platform is a
        # read-only git snapshot, never the live host root (its data/ carries
        # org DBs and private keys, and container uid == host uid so a mount
        # is fully readable — auto-j3oj3). The .beads dolt credential is
        # masked; data/uploads is the one deliberate host-data view.
        # One dest-keyed plan, built through the SAME shared builder as
        # launch_session, so both entry points share one mount path (auto-vm8qh).
        # The CLI's --worktree/--git-dir are its caller mounts; origins are
        # derived from source paths inside build_mount_plan.
        from agents.session_launcher import build_mount_plan
        from agents.mount_plan import (
            mount_args, discover_topology, SocketMountRefused, MountUnresolvable,
            VolumeSubpathUnsupported,
        )
        caller_mounts: dict = {}
        if args.worktree:
            caller_mounts[str(args.worktree)] = "/workspace/repo"
        if args.git_dir:
            caller_mounts[str(args.git_dir)] = str(args.git_dir)
        plan, _shim_env, _codex_auth = build_mount_plan(
            run_dir=run_dir,
            sessions_dir=sessions_dir,
            harness=args.harness,
            working_dir="/workspace/repo",
            caller_mounts=caller_mounts,
            include_capabilities=False,
        )
        # Docker socket refusal is enforced in mount_args() over the full plan.

        cmd: list[str] = [
            "docker", "run",
            "--rm",
            "--name", args.name,
            "--network=host",
            "-e", f"BD_ACTOR={args.session_type}:{args.name}",
            "-e", "BD_READONLY=0",
            "-e", "CODEX_HOME=/home/agent/.codex",
            *auth_args,
        ]
        if session_runtime == "privileged":
            cmd.insert(2, "--privileged")
        elif session_runtime != "standard":
            docker_runtime = (
                "sysbox-runc"
                if session_runtime == "sysbox"
                else session_runtime
            )
            cmd.insert(2, f"--runtime={docker_runtime}")
        # The container carries no scope variable: its org is stamped on
        # the session token and enforced server-side (auto-nuupw's misfiling
        # class is closed by the token, not by an env export).
        if args.graph_tags:
            cmd.extend(["-e", f"GRAPH_TAGS={args.graph_tags}"])
        # Resolve+validate the DECLARED plan before materializing any credential
        # (the Codex auth.json declared by build_mount_plan) — a refusal here
        # writes and mints nothing (auto-vm8qh criterion 6).
        try:
            _mount_argv = mount_args(plan, discover_topology())
        except SocketMountRefused:
            print("ERROR: refusing host Docker socket mount", file=sys.stderr)
            return 1
        except (MountUnresolvable, VolumeSubpathUnsupported) as _exc:
            print(f"ERROR: {_exc}", file=sys.stderr)
            return 1
        if _codex_auth is not None:
            # The plan DECLARED the Codex auth mount, so _mount_argv already binds
            # _codex_auth. If materialization fails here (write error, or the row
            # expired/raced away since declare), the bound path was never written:
            # host-process -v would fabricate a dir there, the fallback bind would
            # fail only at docker-run. Refuse instead — delete any partial file and
            # return before Docker runs (auto-vm8qh criterion 6, foreground path).
            from agents.session_launcher import (
                _materialize_codex_auth_json, _delete_if_present,
            )
            if _materialize_codex_auth_json(run_dir) is None:
                _delete_if_present(_codex_auth)
                print("ERROR: a Codex credential was declared but failed to "
                      "materialize", file=sys.stderr)
                return 1
        cmd.extend(_mount_argv)
        cmd.extend(["-w", "/workspace/repo"])

        resolved_model = args.model or workspace_model or (
            DEFAULT_OPUS_MODEL if args.harness == "claude" else ""
        )

        if prompt is not None:
            prompt_in_output = run_dir / ".prompt.md"
            prompt_in_output.write_text(prompt)
            if args.harness == "claude":
                shell_cmd = (
                    "cat /workspace/output/.prompt.md | "
                    f"claude --dangerously-skip-permissions --model "
                    f"{shlex.quote(resolved_model)} -p"
                )
                if needs_nested_docker:
                    cmd += [args.image, "sh", "-c", shell_cmd]
                else:
                    cmd += ["--entrypoint", "sh", args.image, "-c", shell_cmd]
            else:
                codex_cmd = [
                    "codex",
                    "exec",
                    "--dangerously-bypass-approvals-and-sandbox",
                ]
                if resolved_model:
                    codex_cmd += ["--model", resolved_model]
                codex_cmd += ["-"]
                shell_cmd = "cat /workspace/output/.prompt.md | " + shlex.join(codex_cmd)
                if needs_nested_docker:
                    cmd += [args.image, "sh", "-c", shell_cmd]
                else:
                    cmd += ["--entrypoint", "sh", args.image, "-c", shell_cmd]
        else:
            if args.harness == "claude":
                if needs_nested_docker:
                    cmd += [
                        args.image,
                        "claude",
                        "--dangerously-skip-permissions",
                        "--model",
                        resolved_model,
                    ]
                else:
                    cmd += [
                        args.image,
                        "--dangerously-skip-permissions",
                        "--model",
                        resolved_model,
                    ]
            else:
                if needs_nested_docker:
                    cmd += [
                        args.image,
                        "codex",
                        "--no-alt-screen",
                        "--dangerously-bypass-approvals-and-sandbox",
                    ]
                else:
                    cmd += [
                        "--entrypoint",
                        "codex",
                        args.image,
                        "--no-alt-screen",
                        "--dangerously-bypass-approvals-and-sandbox",
                    ]
                if resolved_model:
                    cmd += ["--model", resolved_model]

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
