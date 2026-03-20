"""Autonomy Dispatcher — non-blocking claim/poll/collect loop.

Owns all bead state mutations. Agents run --readonly.
No LLM in this loop — just a state machine.

Launches agent containers in detached mode (docker run -d) and polls for
completion each cycle. This enables: concurrent dispatch, resilience to
dispatcher restarts, and responsive polling between agent runs.

Dispatches all readiness:approved beads by priority, routing each to the
correct container image via LABEL_IMAGE_MAP. The --queue flag optionally
narrows to a specific label.

Usage:
    python -m agents.dispatcher                  # Dispatch all approved beads
    python -m agents.dispatcher --queue dashboard  # Only dashboard-labeled beads
    python -m agents.dispatcher --loop           # Run continuously
    python -m agents.dispatcher --loop --interval 30
    python -m agents.dispatcher --dry-run        # Show what would be dispatched
    python -m agents.dispatcher --max-concurrent 3  # Run up to 3 agents at once
    python -m agents.dispatcher --max-concurrent-librarians 2  # Run up to 2 librarians
"""

from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import importlib

from agents.dispatch_db import init_db, insert_run, insert_launch_run, update_live_stats, get_currently_running
from agents.librarian_db import enqueue as enqueue_job, dequeue, complete_job, fail_job
from agents.session_launcher import launch_session

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCH_SCRIPT = Path(__file__).parent / "launch.sh"
DISPATCH_STATE_PATH = REPO_ROOT / "data" / "dispatch.state"

# Map labels to container images. Beads with these labels get dispatched
# to specialized images with the right dependencies baked in.
LABEL_IMAGE_MAP = {
    "dashboard": "autonomy-agent:dashboard",
    # Add more as project images are created:
    # "scraper": "autonomy-agent:scraper",
}
DEFAULT_IMAGE = "autonomy-agent"

# Kill containers exceeding this runtime (seconds)
MAX_AGENT_RUNTIME = 600

# Librarian agent type registry — maps job_type to prompt + primer
LIBRARIAN_DIR = Path(__file__).parent / "librarians"
LIBRARIAN_TYPES: dict[str, dict] = {
    "review_report": {
        "prompt_path": LIBRARIAN_DIR / "experience_reviewer" / "prompt.md",
        "primer_module": "agents.librarians.experience_reviewer.primer",
    },
}


@dataclass
class DispatchResult:
    bead_id: str
    exit_code: int
    decision: dict | None = None
    output_dir: str = ""
    error: str = ""
    commit_hash: str = ""
    worktree_path: str = ""
    branch: str = ""
    branch_base: str = ""
    labels: list[str] = field(default_factory=list)


@dataclass
class RunningAgent:
    """Tracks a launched agent container that hasn't been collected yet."""
    bead_id: str
    container_name: str
    container_id: str
    output_dir: str
    worktree_path: str
    branch: str
    branch_base: str
    image: str
    started_at: float
    labels: list[str] = field(default_factory=list)
    # Live stats tracking — accumulated each poll cycle, persisted to dispatch_runs
    jsonl_offset: int = 0          # byte offset into the session JSONL file
    prev_cpu_usec: int = 0         # previous cpu.stat usage_usec reading
    prev_cpu_poll_time: float = 0.0  # wall time of previous CPU reading


@dataclass
class RunningLibrarian:
    """Tracks a launched librarian container that hasn't been collected yet."""
    job_id: str
    job_type: str
    container_name: str
    container_id: str
    output_dir: str
    started_at: float
    jsonl_offset: int = 0
    prev_cpu_usec: int = 0
    prev_cpu_poll_time: float = 0.0


@dataclass
class DispatcherConfig:
    max_concurrent: int = 1
    max_concurrent_librarians: int = 1
    label_filter: str | None = None  # Optional queue label to narrow dispatch
    dry_run: bool = False
    interval: int = 60  # Seconds between dispatch cycles
    loop: bool = False


# ── Helpers ──────────────────────────────────────────────────────


class BdCommandError(Exception):
    """Raised when a bd command fails and check=True."""

    def __init__(self, args: list[str], returncode: int, stderr: str):
        self.args_list = args
        self.returncode = returncode
        self.stderr = stderr
        cmd_str = " ".join(["bd"] + args)
        super().__init__(f"{cmd_str} failed (exit {returncode}): {stderr}")


def run_cmd(cmd: list[str], timeout: int = 15) -> str:
    """Run any command and return stdout. Logs stderr on failure."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout,
            cwd=str(REPO_ROOT),
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            print(f"  cmd {cmd[0]} failed (exit {result.returncode}): {stderr}",
                  file=sys.stderr)
            return ""
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"  cmd error: {e}", file=sys.stderr)
        return ""


def run_bd(args: list[str], timeout: int = 15, check: bool = False) -> str:
    """Run a bd command and return stdout.

    Logs stderr on non-zero exit code. If check=True, raises
    BdCommandError on failure instead of returning empty string.
    """
    try:
        result = subprocess.run(
            ["bd"] + args,
            capture_output=True, text=True, timeout=timeout,
            cwd=str(REPO_ROOT),
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            print(f"  bd {args[0]} failed (exit {result.returncode}): {stderr}",
                  file=sys.stderr)
            if check:
                raise BdCommandError(args, result.returncode, stderr)
            return ""
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"  bd error: {e}", file=sys.stderr)
        if check:
            raise BdCommandError(args, -1, str(e)) from e
        return ""


def _retry_bd(args: list[str], max_retries: int = 2, timeout: int = 15) -> str:
    """Run a bd command with retries for critical state mutations.

    Retries with exponential backoff (1s, 2s). Raises BdCommandError
    if all attempts fail.
    """
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            return run_bd(args, timeout=timeout, check=True)
        except BdCommandError as e:
            last_err = e
            if attempt < max_retries:
                wait = 2 ** attempt
                print(f"  Retrying bd {args[0]} in {wait}s "
                      f"(attempt {attempt + 2}/{max_retries + 1})...",
                      file=sys.stderr)
                time.sleep(wait)
    print(f"  CRITICAL: bd {' '.join(args)} failed after "
          f"{max_retries + 1} attempts: {last_err}",
          file=sys.stderr)
    raise last_err


# ── Bead queries ─────────────────────────────────────────────────


def get_ready_beads(label_filter: str | None = None) -> list[dict]:
    """Get beads approved for dispatch, optionally filtered by queue label.

    Queries for readiness:approved — the single human gate.
    The readiness dimension (idea -> draft -> specified -> approved) is set
    via bd set-state; the dispatcher only picks up approved beads.
    """
    query = 'status=open AND label="readiness:approved"'
    if label_filter:
        query += f" AND label={label_filter}"
    out = run_bd(["query", query, "--json"])

    if not out:
        return []
    try:
        beads = json.loads(out)
    except json.JSONDecodeError:
        return []

    if not isinstance(beads, list):
        return []

    return beads


def _read_dispatch_state() -> dict:
    """Read data/dispatch.state. Returns {} if missing or invalid."""
    try:
        return json.loads(DISPATCH_STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def is_label_paused(label: str | None) -> bool:
    """Return True if the given queue label is paused in dispatch.state."""
    if not label:
        return False
    state = _read_dispatch_state()
    return bool(state.get(label, False))


def get_paused_labels() -> set[str]:
    """Return the set of all labels currently paused."""
    state = _read_dispatch_state()
    return {label for label, paused in state.items() if paused}


_claimed_cache: set[str] = set()


def get_claimed_beads() -> set[str]:
    """Get IDs of beads currently claimed by the dispatcher.

    Queries dispatch_runs WHERE status=RUNNING in SQLite.
    On error, logs and returns the previous known set (never empty on error).
    """
    global _claimed_cache
    try:
        runs = get_currently_running()
        _claimed_cache = {r["bead_id"] for r in runs if r.get("bead_id")}
        return _claimed_cache
    except Exception as e:
        print(f"  WARNING: get_claimed_beads failed: {e}", file=sys.stderr)
        return _claimed_cache


def get_open_dependencies(bead_id: str) -> list[dict]:
    """Check if a bead has unclosed blocking dependencies.

    Returns a list of dependency dicts (with id, title, status) for any
    'blocks'-type dependency that is NOT closed. Parent-child relationships
    are excluded — a parent epic being open should not block its subtasks.

    Returns an empty list if the bead has no blocking open dependencies
    (i.e., it is safe to dispatch).
    """
    out = run_bd(["dep", "list", bead_id, "--json"])
    if not out:
        return []
    try:
        deps = json.loads(out)
    except json.JSONDecodeError:
        return []
    if not isinstance(deps, list):
        return []

    open_blockers = []
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        # Only 'blocks' type dependencies gate dispatch.
        # Parent-child deps are structural, not blocking.
        dep_type = dep.get("dependency_type", "")
        if dep_type == "parent-child":
            continue
        # Any non-closed dependency blocks dispatch
        if dep.get("status") != "closed":
            open_blockers.append(dep)

    return open_blockers


# ── Bead state mutations ────────────────────────────────────────




def release_bead(bead_id: str, status: str, reason: str) -> bool:
    """Release a bead after agent completion. Returns True if all ops succeed.

    For non-DONE outcomes, resets status to open so the bead can be
    re-queued. The dispatch_runs status is updated separately by _record_run().
    Uses retry for critical state mutations. Logs manual-cleanup
    warnings on persistent failure so stale beads are visible.
    """
    try:
        if status == "DONE":
            _retry_bd(["close", bead_id, "--reason", reason])
        elif status == "BLOCKED":
            _retry_bd(["update", bead_id, "-s", "open"])
            run_bd(["update", bead_id, "--append-notes", f"Blocked: {reason}"])
        elif status == "FAILED":
            _retry_bd(["update", bead_id, "-s", "open"])
            run_bd(["update", bead_id, "--append-notes", f"Failed: {reason}"])
        else:
            # Unknown status — log and release
            _retry_bd(["update", bead_id, "-s", "open"])
            run_bd(["update", bead_id, "--append-notes", f"Released (unknown status {status}): {reason}"])
    except BdCommandError as e:
        print(f"  Close: FAILED — bd close returned: {e}", file=sys.stderr)
        print(f"  STALE BEAD WARNING: {bead_id} may need manual cleanup "
              f"(intended status: {status})", file=sys.stderr)
        return False

    print(f"  Close: OK → {bead_id} closed")
    return True


# ── Image routing ────────────────────────────────────────────────


def image_for_bead(bead: dict) -> str:
    """Select the container image based on bead labels."""
    labels = bead.get("labels") or []
    for label in labels:
        if label in LABEL_IMAGE_MAP:
            return LABEL_IMAGE_MAP[label]
    return DEFAULT_IMAGE


# ── Non-blocking agent lifecycle ─────────────────────────────────


def start_agent(bead_id: str, image: str = DEFAULT_IMAGE) -> RunningAgent | None:
    """Launch an agent container in detached mode. Returns immediately.

    Calls launch.sh --detach which:
    1. Creates worktree and generates prompt
    2. Starts container with docker run -d
    3. Returns container metadata as key=value pairs

    Returns RunningAgent on success, None on failure.
    """
    print(f"  Starting agent for {bead_id} (image: {image})...")

    try:
        result = subprocess.run(
            [str(LAUNCH_SCRIPT), bead_id, f"--image={image}", "--detach"],
            capture_output=True, text=True,
            timeout=120,  # Prep phase only — worktree + prompt gen
            cwd=str(REPO_ROOT),
            env={**os.environ, "BD_READONLY": "1"},
        )
    except subprocess.TimeoutExpired:
        print(f"  ERROR: launch.sh --detach timed out for {bead_id}", file=sys.stderr)
        return None

    if result.returncode != 0:
        print(f"  ERROR: launch.sh --detach failed for {bead_id}: {result.stderr}",
              file=sys.stderr)
        return None

    # Parse key=value output from launch.sh --detach
    metadata = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            metadata[key.strip()] = value.strip()

    container_id = metadata.get("CONTAINER_ID", "")
    container_name = metadata.get("CONTAINER_NAME", "")
    output_dir = metadata.get("OUTPUT_DIR", "")
    worktree_path = metadata.get("WORKTREE_DIR", "")
    branch = metadata.get("BRANCH", "")
    branch_base = metadata.get("BRANCH_BASE", "")

    if not container_id or not output_dir:
        print(f"  ERROR: Missing container metadata from launch.sh for {bead_id}",
              file=sys.stderr)
        return None

    print(f"  Container started: {container_name} ({container_id[:12]})")

    return RunningAgent(
        bead_id=bead_id,
        container_name=container_name,
        container_id=container_id,
        output_dir=output_dir,
        worktree_path=worktree_path,
        branch=branch,
        branch_base=branch_base,
        image=image,
        started_at=time.time(),
    )


def poll_container(container_id: str) -> tuple[bool, int]:
    """Check if a docker container has exited.

    Returns (finished, exit_code). If still running, returns (False, -1).
    """
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format",
             "{{.State.Status}} {{.State.ExitCode}}", container_id],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            print(f"  poll_container({container_id[:12]}): docker inspect failed "
                  f"rc={result.returncode} stderr={result.stderr.strip()!r}",
                  file=sys.stderr)
            return False, -1

        parts = result.stdout.strip().split()
        status = parts[0] if parts else "unknown"
        exit_code = int(parts[1]) if len(parts) > 1 else -1

        print(f"  poll_container({container_id[:12]}): status={status} exit_code={exit_code}")

        if status == "exited":
            return True, exit_code
        elif status == "running":
            return False, -1
        else:
            # created, paused, restarting, removing, dead
            finished = status in ("dead", "removing")
            print(f"  poll_container({container_id[:12]}): unusual status={status}, "
                  f"finished={finished}")
            return finished, exit_code

    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError) as e:
        print(f"  poll_container({container_id[:12]}): exception {e}", file=sys.stderr)
        return False, -1


def collect_results(agent: RunningAgent, exit_code: int) -> DispatchResult:
    """Collect results from a completed agent container.

    Reads decision.json and commit hash from the output directory,
    then removes the docker container and cleans up temp files.
    """
    output_dir = agent.output_dir

    # Read decision file (written by agent inside container)
    decision = None
    decision_path = Path(output_dir) / "decision.json"
    if decision_path.exists():
        try:
            decision = json.loads(decision_path.read_text())
        except json.JSONDecodeError:
            pass

    # Check for new commits in the worktree
    commit_hash = ""
    if agent.worktree_path and Path(agent.worktree_path).exists():
        try:
            head = subprocess.run(
                ["git", "-C", agent.worktree_path, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if head and agent.branch_base and head != agent.branch_base:
                commit_hash = head
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    # Write collection artifacts for consistency with foreground mode
    if commit_hash:
        (Path(output_dir) / ".commit_hash").write_text(commit_hash)
    (Path(output_dir) / ".worktree_path").write_text(agent.worktree_path)
    (Path(output_dir) / ".branch").write_text(agent.branch)

    # Remove the stopped container (no --rm in detach mode)
    remove_container(agent.container_name)

    # Clean up detach-mode temp files stored in output dir
    for tmpfile in (".credentials.json", ".prompt.md"):
        p = Path(output_dir) / tmpfile
        if p.exists():
            p.unlink()

    return DispatchResult(
        bead_id=agent.bead_id,
        exit_code=exit_code,
        decision=decision,
        output_dir=output_dir,
        error="" if exit_code == 0 else f"Agent exited with code {exit_code}",
        commit_hash=commit_hash,
        worktree_path=agent.worktree_path,
        branch=agent.branch,
        branch_base=agent.branch_base,
        labels=list(agent.labels),
    )


def kill_container(container_name: str) -> None:
    """Kill a running docker container."""
    subprocess.run(
        ["docker", "kill", container_name],
        capture_output=True, text=True, timeout=10,
    )


def remove_container(container_name: str) -> None:
    """Remove a docker container (force, ignores errors if already gone)."""
    subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True, text=True, timeout=10,
    )


# ── Working tree / merge / cleanup ──────────────────────────────


def check_working_tree_clean() -> tuple[bool, str]:
    """Check if the working tree is clean (no uncommitted changes).

    Returns (is_clean, dirty_files_summary). If dirty, the summary lists
    modified/untracked files so the error message is actionable.
    """
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True, timeout=10,
        cwd=str(REPO_ROOT),
    )
    output = result.stdout.strip()
    if not output:
        return True, ""

    lines = output.splitlines()
    summary = "; ".join(lines[:10])
    if len(lines) > 10:
        summary += f" ... and {len(lines) - 10} more"
    return False, summary


def merge_branch(branch: str, bead_id: str, reason: str) -> tuple[bool, str]:
    """Merge a branch into the current HEAD, handling dirty working trees.

    If the working tree is dirty, attempts git stash before merging and
    restores afterward. Returns (success, error_message).
    """
    is_clean, dirty_files = check_working_tree_clean()
    stashed = False

    if not is_clean:
        print(f"  Working tree is dirty: {dirty_files}")
        print(f"  Attempting git stash before merge...")
        stash_result = subprocess.run(
            ["git", "stash", "push", "-m", f"dispatcher-auto-stash-{bead_id}"],
            capture_output=True, text=True, timeout=15,
            cwd=str(REPO_ROOT),
        )
        if stash_result.returncode != 0:
            msg = (
                f"Dirty working tree blocks merge and stash failed. "
                f"Dirty files: {dirty_files}. "
                f"Stash error: {stash_result.stderr.strip()}"
            )
            return False, msg

        # Verify stash actually saved something (git stash returns 0 even with nothing to stash)
        if "No local changes to save" in stash_result.stdout:
            # Shouldn't happen since we checked porcelain, but be safe
            pass
        else:
            stashed = True
            print(f"  Stashed local changes successfully")

    # Attempt the merge
    merge_result = subprocess.run(
        ["git", "merge", branch,
         "--no-edit", "-m",
         f"merge: {bead_id} — {reason}"],
        capture_output=True, text=True, timeout=30,
        cwd=str(REPO_ROOT),
    )

    merge_ok = merge_result.returncode == 0
    merge_err = merge_result.stderr.strip()

    if not merge_ok:
        # Abort the failed merge attempt
        subprocess.run(
            ["git", "merge", "--abort"],
            capture_output=True, text=True, timeout=5,
            cwd=str(REPO_ROOT),
        )

    # Restore stashed changes regardless of merge outcome
    if stashed:
        print(f"  Restoring stashed changes...")
        pop_result = subprocess.run(
            ["git", "stash", "pop"],
            capture_output=True, text=True, timeout=15,
            cwd=str(REPO_ROOT),
        )
        if pop_result.returncode != 0:
            print(f"  WARNING: stash pop failed: {pop_result.stderr.strip()}")
            print(f"  Stashed changes preserved in git stash list")

    if merge_ok:
        return True, ""
    else:
        if "overwritten by merge" in merge_err or "local changes" in merge_err.lower():
            msg = (
                f"Dirty working tree blocked merge even after stash attempt. "
                f"Error: {merge_err[:200]}"
            )
        else:
            msg = f"Merge conflict: {merge_err[:200]}"
        return False, msg


def cleanup_worktree(worktree_path: str) -> None:
    """Remove a git worktree after dispatch."""
    if worktree_path and Path(worktree_path).exists():
        subprocess.run(
            ["git", "worktree", "remove", worktree_path, "--force"],
            capture_output=True, text=True, timeout=15,
            cwd=str(REPO_ROOT),
        )


def find_worktree_for_bead(bead_id: str) -> str:
    """Find the most recent worktree path for a bead, if one exists."""
    worktrees_dir = REPO_ROOT / ".worktrees"
    if not worktrees_dir.exists():
        return ""
    candidates = sorted(
        worktrees_dir.glob(f"{bead_id}-*"),
        key=lambda p: p.name, reverse=True,
    )
    return str(candidates[0]) if candidates else ""


# ── Smoke test helpers ──────────────────────────────────────────

_DASHBOARD_WATCH_PATHS = (
    "tools/dashboard/server.py",
    "tools/dashboard/static/",
    "tools/dashboard/templates/",
    "tools/dashboard/dao/",
)

_DISPATCH_STATE_PATH = REPO_ROOT / "data" / "dispatch.state"
_START_DASHBOARD_SCRIPT = REPO_ROOT / "tools" / "dashboard" / "start-dashboard.sh"


def _dashboard_files_changed(branch_base: str) -> bool:
    """Return True if the merged commit touched any dashboard-owned paths."""
    if not branch_base:
        return False
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{branch_base}..HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=str(REPO_ROOT),
        )
        changed = result.stdout.strip().splitlines()
        for path in changed:
            for watch in _DASHBOARD_WATCH_PATHS:
                if path == watch or path.startswith(watch):
                    return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return False


def _maybe_restart_dashboard(branch_base: str) -> None:
    """Restart the dashboard server if merged commit touched dashboard files."""
    if not _dashboard_files_changed(branch_base):
        return

    print("  Dashboard files changed — restarting server...", file=sys.stderr)

    if not _START_DASHBOARD_SCRIPT.exists():
        print("  WARN: start-dashboard.sh not found, skipping restart", file=sys.stderr)
        return

    try:
        subprocess.run(
            [str(_START_DASHBOARD_SCRIPT), "--restart"],
            capture_output=True, text=True, timeout=30,
            cwd=str(REPO_ROOT),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"  WARN: dashboard restart failed: {e}", file=sys.stderr)
        return

    # Poll /api/stats up to 10s for readiness
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    try:
        import requests as _requests
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                r = _requests.get("https://localhost:8080/api/stats", verify=False, timeout=2)
                if r.status_code == 200:
                    print("  Dashboard ready.", file=sys.stderr)
                    return
            except Exception:
                pass
            time.sleep(0.5)
        print("  WARN: dashboard did not become ready within 10s", file=sys.stderr)
    except ImportError:
        pass


def _pause_dashboard_dispatch() -> None:
    """Write dashboard=true pause flag to data/dispatch.state."""
    try:
        state: dict = {}
        if _DISPATCH_STATE_PATH.exists():
            try:
                state = json.loads(_DISPATCH_STATE_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        state["dashboard"] = True
        _DISPATCH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _DISPATCH_STATE_PATH.write_text(json.dumps(state))
        print(f"  Dashboard dispatch paused — {_DISPATCH_STATE_PATH}", file=sys.stderr)
    except OSError as e:
        print(f"  WARN: could not write dispatch.state: {e}", file=sys.stderr)


def _is_dashboard_dispatch_paused() -> bool:
    """Return True if dashboard dispatch is paused via data/dispatch.state."""
    try:
        if _DISPATCH_STATE_PATH.exists():
            state = json.loads(_DISPATCH_STATE_PATH.read_text())
            return bool(state.get("dashboard"))
    except (json.JSONDecodeError, OSError):
        pass
    return False


# ── Decision processing ─────────────────────────────────────────


def process_decision(dispatch_result: DispatchResult) -> None:
    """Process agent decision and update bead state."""
    bead_id = dispatch_result.bead_id
    decision = dispatch_result.decision

    # Record commit hash on bead if agent committed
    if dispatch_result.commit_hash:
        commit = dispatch_result.commit_hash[:10]
        branch = dispatch_result.branch
        print(f"  Commit: {commit} on {branch}")
        run_bd(["update", bead_id, "--append-notes",
                f"commit: {dispatch_result.commit_hash} branch: {branch}"])

    if decision is None:
        print(f"  No decision file from {bead_id} (exit code {dispatch_result.exit_code})")
        release_bead(bead_id, "FAILED", f"No decision file. Exit code: {dispatch_result.exit_code}")
        cleanup_worktree(dispatch_result.worktree_path)
        return

    status = decision.get("status", "FAILED")
    reason = decision.get("reason", "No reason provided")
    notes = decision.get("notes", "")

    print(f"  Decision: {status} — {reason}")

    # Append agent notes to bead
    if notes:
        run_bd(["update", bead_id, "--append-notes", notes])

    # Record optional structured feedback fields
    scores = decision.get("scores")
    time_breakdown = decision.get("time_breakdown")
    failure_category = decision.get("failure_category")

    feedback_parts = []
    if scores and isinstance(scores, dict):
        parts = [f"{k}={v}" for k, v in scores.items()
                 if isinstance(v, (int, float))]
        if parts:
            feedback_parts.append(f"scores: {', '.join(parts)}")
    if time_breakdown and isinstance(time_breakdown, dict):
        parts = [f"{k}={v}%" for k, v in time_breakdown.items()
                 if isinstance(v, (int, float))]
        if parts:
            feedback_parts.append(f"time: {', '.join(parts)}")
    if failure_category and status in ("BLOCKED", "FAILED"):
        feedback_parts.append(f"failure_category: {failure_category}")

    if feedback_parts:
        run_bd(["update", bead_id, "--append-notes",
                "agent-feedback: " + " | ".join(feedback_parts)])

    # Create discovered beads — always include readiness:idea as pipeline entry point
    for new_bead in decision.get("discovered_beads", []):
        title = new_bead.get("title", "Untitled")
        desc = new_bead.get("description", "")
        labels = new_bead.get("labels", ["refinement"])
        priority = new_bead.get("priority", 2)

        # Ensure readiness:idea is present for the readiness pipeline
        if not any(l.startswith("readiness:") for l in labels):
            labels = labels + ["readiness:idea"]

        label_args = ["-l", ",".join(labels)] if labels else []
        out = run_bd([
            "create", title,
            "-d", desc,
            "-p", str(priority),
            *label_args,
        ])
        if out:
            print(f"  Created discovered bead: {out}")

    # Auto-merge to master on DONE if agent committed
    if status == "DONE" and dispatch_result.commit_hash and dispatch_result.branch:
        merge_ok, merge_err = merge_branch(
            dispatch_result.branch, bead_id, reason
        )
        if merge_ok:
            merge_hash = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
                cwd=str(REPO_ROOT),
            ).stdout.strip()
            print(f"  Merge: OK → master ({merge_hash[:10]})")
            run_bd(["update", bead_id, "--append-notes",
                    f"merged: {merge_hash}"])

            # ── Post-merge smoke test (dashboard beads only) ────────────────
            # Bootstrap guard: smoke.py won't exist until auto-mzbx merges.
            # The first merge (introducing smoke.py) skips self-gating.
            smoke_script = REPO_ROOT / "tools/dashboard/smoke.py"
            if "dashboard" in dispatch_result.labels and smoke_script.exists():
                try:
                    _maybe_restart_dashboard(dispatch_result.branch_base)
                    smoke_raw = subprocess.run(
                        [sys.executable, str(smoke_script)],
                        capture_output=True, text=True,
                        cwd=str(REPO_ROOT), timeout=60,
                    )
                    smoke = (
                        json.loads(smoke_raw.stdout)
                        if smoke_raw.stdout
                        else {"pass": False, "error": "no output"}
                    )
                    if dispatch_result.output_dir:
                        (Path(dispatch_result.output_dir) / "smoke_result.json").write_text(
                            json.dumps(smoke)
                        )
                    if not smoke.get("pass"):
                        run_bd(["update", bead_id, "--append-notes",
                                f"smoke test FAILED: {smoke}"])
                        status = "BLOCKED"
                        reason = "Post-merge smoke test failed — dashboard dispatch paused"
                        _pause_dashboard_dispatch()
                    else:
                        print(f"  Smoke test PASSED ({smoke.get('duration_ms', '?')}ms)")
                except Exception as smoke_err:
                    # Crash must not prevent bead from closing as DONE
                    print(f"  WARN: smoke test error for {bead_id}: {smoke_err}",
                          file=sys.stderr)
                    run_bd(["update", bead_id, "--append-notes",
                            f"smoke test errored (non-blocking): {smoke_err}"])
        else:
            print(f"  Merge: FAILED — {merge_err}")
            run_bd(["update", bead_id, "--append-notes",
                    f"merge failed on {dispatch_result.branch}: {merge_err}"])
            # Do NOT change status to BLOCKED — close the bead regardless.
            # The code is on the branch and can be manually merged later.
            # Leaving status as DONE prevents the infinite re-dispatch loop.

    # Release the bead with appropriate state
    release_bead(bead_id, status, reason)

    # Clean up worktree (branch persists for review)
    cleanup_worktree(dispatch_result.worktree_path)

    # Delete branch if it was merged successfully
    if status == "DONE" and dispatch_result.branch:
        subprocess.run(
            ["git", "branch", "-d", dispatch_result.branch],
            capture_output=True, text=True, timeout=5,
            cwd=str(REPO_ROOT),
        )


# ── Live stats collection ────────────────────────────────────────


def _find_jsonl_file(output_dir: str) -> Path | None:
    """Find the session JSONL file for a running agent.

    The file is at {output_dir}/sessions/**/*.jsonl — typically one per agent.
    Returns the first match, or None if not found yet.
    """
    session_dir = Path(output_dir) / "sessions"
    if not session_dir.exists():
        return None
    files = list(session_dir.glob("**/*.jsonl"))
    return files[0] if files else None


def _read_jsonl_incremental(
    jsonl_path: Path,
    offset: int,
) -> tuple[str | None, int, int, int, int, str | None]:
    """Read new JSONL lines starting at byte offset.

    Returns (snippet, context_tokens, new_offset, tool_delta, turn_delta, last_activity_iso).

    snippet        — last assistant/user text seen (first 300 chars), or None
    context_tokens — total input tokens from the last assistant turn (context window usage)
    new_offset     — byte offset after last complete line consumed
    tool_delta     — count of tool_use entries in new lines
    turn_delta     — count of assistant + thinking entries in new lines
    last_activity_iso — ISO timestamp of last entry seen, or None
    """
    snippet: str | None = None
    context_tokens = 0
    tool_delta = 0
    turn_delta = 0
    last_activity: str | None = None

    try:
        file_size = jsonl_path.stat().st_size
        if file_size <= offset:
            return snippet, context_tokens, offset, tool_delta, turn_delta, last_activity

        with open(jsonl_path, "rb") as fh:
            fh.seek(offset)
            data = fh.read()

        # Only process up to the last complete line (ends with \n)
        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            return snippet, context_tokens, offset, tool_delta, turn_delta, last_activity

        complete = data[: last_nl + 1]
        new_offset = offset + last_nl + 1

        for raw_line in complete.splitlines():
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = entry.get("type")
            ts = entry.get("timestamp", "")
            if ts:
                last_activity = ts

            # Count tool_use entries
            if etype == "tool_use":
                tool_delta += 1
                continue

            # Count assistant + thinking entries as turns
            if etype in ("assistant", "thinking"):
                turn_delta += 1

            if etype not in ("user", "assistant"):
                continue

            msg = entry.get("message", {})

            # Track context size: total input tokens from the latest assistant turn
            usage = msg.get("usage", {})
            if etype == "assistant" and usage:
                ctx = (usage.get("input_tokens", 0)
                       + usage.get("cache_creation_input_tokens", 0)
                       + usage.get("cache_read_input_tokens", 0))
                if ctx > 0:
                    context_tokens = ctx

            # Extract text for snippet
            content = msg.get("content", "")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        break

            text = text.strip()
            if text:
                snippet = text[:300]

        return snippet, context_tokens, new_offset, tool_delta, turn_delta, last_activity

    except OSError:
        return snippet, context_tokens, offset, tool_delta, turn_delta, last_activity


def _read_cgroup_mem_mb(container_id: str) -> int | None:
    """Read current memory usage in MB from cgroup memory.current.

    Returns integer MB, or None if the cgroup file is not accessible.
    This is essentially free — a single file read taking ~1ms.
    """
    cgroup_path = Path(
        f"/sys/fs/cgroup/system.slice/docker-{container_id}.scope/memory.current"
    )
    try:
        mem_bytes = int(cgroup_path.read_text().strip())
        return mem_bytes // (1024 * 1024)
    except (OSError, ValueError):
        return None


def _read_cgroup_cpu_usec(container_id: str) -> int | None:
    """Read cumulative CPU usage in microseconds from cgroup cpu.stat.

    Parses the usage_usec line from cpu.stat. Returns None if not accessible.
    This is essentially free — a single file read taking ~1ms.
    """
    cgroup_path = Path(
        f"/sys/fs/cgroup/system.slice/docker-{container_id}.scope/cpu.stat"
    )
    try:
        for line in cgroup_path.read_text().splitlines():
            if line.startswith("usage_usec"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _collect_live_stats(agent: RunningAgent) -> None:
    """Collect live stats for a running agent and persist to dispatch_runs.

    Called each poll cycle. Reads new JSONL lines incrementally (seeking
    to jsonl_offset), reads cgroup memory and CPU, then calls update_live_stats().
    All errors are swallowed — stats are best-effort and must not disrupt dispatch.
    """
    try:
        run_id = Path(agent.output_dir).name if agent.output_dir else agent.bead_id

        # --- JSONL: incremental read ---
        snippet: str | None = None
        context_tokens = 0
        tool_delta = 0
        turn_delta = 0
        last_activity: str | None = None

        jsonl_file = _find_jsonl_file(agent.output_dir)
        if jsonl_file:
            snippet, context_tokens, new_offset, tool_delta, turn_delta, last_activity = _read_jsonl_incremental(
                jsonl_file, agent.jsonl_offset
            )
            agent.jsonl_offset = new_offset

        # --- Cgroup: memory ---
        mem_mb = _read_cgroup_mem_mb(agent.container_id)

        # --- Cgroup: CPU (diff from previous reading) ---
        cpu_usec = _read_cgroup_cpu_usec(agent.container_id)
        cpu_pct: float | None = None
        now = time.time()

        if cpu_usec is not None and agent.prev_cpu_usec > 0:
            elapsed_usec = (now - agent.prev_cpu_poll_time) * 1_000_000
            if elapsed_usec > 0:
                cpu_pct = (cpu_usec - agent.prev_cpu_usec) / elapsed_usec * 100.0
                cpu_pct = max(0.0, cpu_pct)

        if cpu_usec is not None:
            agent.prev_cpu_usec = cpu_usec
            agent.prev_cpu_poll_time = now

        # --- Persist to DB ---
        update_live_stats(
            run_id=run_id,
            last_snippet=snippet,
            context_tokens=context_tokens,
            tool_delta=tool_delta,
            turn_delta=turn_delta,
            cpu_pct=cpu_pct,
            cpu_usec=cpu_usec,
            mem_mb=mem_mb,
            last_activity=last_activity,
            jsonl_offset=agent.jsonl_offset,
        )

    except Exception as e:
        print(f"  WARNING: live stats collection failed for {agent.bead_id}: {e}",
              file=sys.stderr)


# ── Session ingestion ───────────────────────────────────────────


def _ingest_session(result: DispatchResult) -> None:
    """Ingest agent session into the knowledge graph and link to bead."""
    ingest_proc = subprocess.run(
        ["graph", "sessions", "--all"],
        capture_output=True, text=True, timeout=30,
        cwd=str(REPO_ROOT),
    )
    if ingest_proc.returncode == 0:
        print(f"  Ingest: OK")
    else:
        print(f"  Ingest: FAILED — {ingest_proc.stderr.strip()}", file=sys.stderr)

    if result.output_dir:
        session_dir = Path(result.output_dir) / "sessions"
        jsonl_files = list(session_dir.glob("**/*.jsonl")) if session_dir.exists() else []
        if jsonl_files:
            session_name = jsonl_files[0].stem
            search_out = run_cmd(
                ["graph", "search", session_name, "--json", "--limit", "1"]
            )
            if search_out:
                try:
                    hits = json.loads(search_out)
                    if isinstance(hits, list) and hits:
                        src_id = hits[0].get("source_id", hits[0].get("id", ""))
                        if src_id:
                            subprocess.run(
                                ["graph", "link", result.bead_id, src_id,
                                 "-r", "implemented_by"],
                                capture_output=True, text=True, timeout=15,
                                cwd=str(REPO_ROOT),
                            )
                            print(f"  Linked {result.bead_id} -> {src_id} (implemented_by)")
                except json.JSONDecodeError:
                    pass


def _record_launch(agent: RunningAgent) -> None:
    """Record a RUNNING row at launch time. Best-effort — never raises."""
    try:
        run_id = Path(agent.output_dir).name if agent.output_dir else agent.bead_id
        insert_launch_run(
            run_id=run_id,
            bead_id=agent.bead_id,
            started_at=agent.started_at,
            branch=agent.branch,
            branch_base=agent.branch_base,
            image=agent.image,
            container_name=agent.container_name,
            output_dir=agent.output_dir,
        )
    except Exception as e:
        print(f"  WARNING: Failed to record launch to SQLite: {e}", file=sys.stderr)


def _record_run(agent: RunningAgent, result: DispatchResult) -> None:
    """Record dispatch run metadata to SQLite. Best-effort — never raises."""
    try:
        # Derive run_id from output dir name (e.g. auto-ahd-20260316-234902)
        run_id = Path(agent.output_dir).name if agent.output_dir else agent.bead_id

        decision = result.decision or {}
        status = decision.get("status", "FAILED")
        reason = decision.get("reason", "No decision file")

        insert_run(
            run_id=run_id,
            bead_id=agent.bead_id,
            started_at=agent.started_at,
            completed_at=time.time(),
            status=status,
            reason=reason,
            decision=result.decision,
            commit_hash=result.commit_hash,
            branch=result.branch or agent.branch,
            branch_base=agent.branch_base,
            image=agent.image,
            container_name=agent.container_name,
            exit_code=result.exit_code,
            output_dir=agent.output_dir,
        )
        print(f"  Record: OK → {run_id}")
    except Exception as e:
        print(f"  Record: FAILED — {e}", file=sys.stderr)


# ── Librarian launch / collect ───────────────────────────────────


def _build_librarian_prompt(job_type: str, payload: dict) -> str:
    """Assemble full prompt for a librarian agent: dynamic primer + static role definition."""
    config = LIBRARIAN_TYPES.get(job_type)
    if not config:
        raise ValueError(f"Unknown librarian job type: {job_type!r}")

    module = importlib.import_module(config["primer_module"])
    primer = module.build_primer(payload)

    static = config["prompt_path"].read_text()

    return primer + "\n\n---\n\n" + static


def start_librarian(job: dict) -> RunningLibrarian | None:
    """Launch a librarian container in detached mode. Returns immediately.

    Launches directly via docker run -d (no worktree, no git branch).
    Delegates to launch_session() which handles credential resolution,
    per-run session directory creation, and .session_meta.json writing.

    Returns RunningLibrarian on success, None on failure.
    """
    job_id = job["id"]
    job_type = job["job_type"]

    payload: dict = {}
    if job.get("payload"):
        try:
            payload = json.loads(job["payload"])
        except json.JSONDecodeError:
            pass

    print(f"  Starting librarian: {job_type} (job {job_id[:8]})")

    try:
        prompt = _build_librarian_prompt(job_type, payload)
    except Exception as e:
        print(f"  ERROR: failed to build librarian prompt for {job_type}: {e}",
              file=sys.stderr)
        return None

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = f"librarian-{job_type}-{job_id[:8]}-{ts}"
    output_dir = str(REPO_ROOT / "data" / "agent-runs" / run_id)

    container_name = f"librarian-{job_type}-{os.getpid()}-{job_id[:8]}"

    container_id = launch_session(
        session_type="librarian",
        name=container_name,
        prompt=prompt,
        metadata={"job_id": job_id, "job_type": job_type},
        detach=True,
        image=DEFAULT_IMAGE,
        output_dir=output_dir,
    )
    if not container_id:
        return None

    print(f"  Librarian container started: {container_name} ({container_id[:12]})")

    return RunningLibrarian(
        job_id=job_id,
        job_type=job_type,
        container_name=container_name,
        container_id=container_id,
        output_dir=output_dir,
        started_at=time.time(),
    )


def _record_librarian_launch(lib: RunningLibrarian) -> None:
    """Record a RUNNING row for a librarian at launch time. Best-effort."""
    try:
        run_id = Path(lib.output_dir).name if lib.output_dir else lib.job_id
        insert_launch_run(
            run_id=run_id,
            bead_id="",  # No bead — librarian is job-driven
            started_at=lib.started_at,
            branch="",
            branch_base="",
            image=DEFAULT_IMAGE,
            container_name=lib.container_name,
            output_dir=lib.output_dir,
            librarian_type=lib.job_type,
        )
    except Exception as e:
        print(f"  WARNING: Failed to record librarian launch to SQLite: {e}",
              file=sys.stderr)


def _record_librarian_run(lib: RunningLibrarian, exit_code: int, status: str) -> None:
    """Record librarian completion to dispatch_runs. Best-effort."""
    try:
        run_id = Path(lib.output_dir).name if lib.output_dir else lib.job_id
        insert_run(
            run_id=run_id,
            bead_id="",
            started_at=lib.started_at,
            completed_at=time.time(),
            status=status,
            reason=f"Librarian {lib.job_type} job {lib.job_id[:8]} completed",
            decision=None,
            commit_hash="",
            branch="",
            branch_base="",
            image=DEFAULT_IMAGE,
            container_name=lib.container_name,
            exit_code=exit_code,
            output_dir=lib.output_dir,
            librarian_type=lib.job_type,
        )
    except Exception as e:
        print(f"  WARNING: Failed to record librarian run to SQLite: {e}",
              file=sys.stderr)


# ── Poll and collect ─────────────────────────────────────────────


def poll_and_collect(running: list[RunningAgent]) -> None:
    """Poll running agents and collect results for any that completed or timed out.

    Modifies the running list in place — removes completed/timed-out agents.
    """
    completed: list[tuple[RunningAgent, int]] = []
    timed_out: list[RunningAgent] = []

    for agent in running:
        elapsed = time.time() - agent.started_at
        finished, exit_code = poll_container(agent.container_id)

        if finished:
            print(f"  Completed: {agent.bead_id} (exit={exit_code}, {elapsed:.0f}s)")
            completed.append((agent, exit_code))
        elif elapsed > MAX_AGENT_RUNTIME:
            # Check JSONL staleness — only timeout if agent hasn't written recently
            jsonl_file = _find_jsonl_file(agent.output_dir)
            stale = True
            if jsonl_file:
                try:
                    mtime = jsonl_file.stat().st_mtime
                    stale_secs = time.time() - mtime
                    stale = stale_secs > 300  # 5 minutes without JSONL writes
                except OSError:
                    pass
            if stale:
                print(f"  Timeout: {agent.bead_id} ({elapsed:.0f}s, JSONL stale)")
                timed_out.append(agent)
            else:
                print(f"  {agent.bead_id}: {elapsed:.0f}s elapsed but JSONL active, continuing")
        else:
            # Still running — collect live stats (JSONL tail + cgroup reads)
            _collect_live_stats(agent)

    # Process normally-completed agents
    for agent, exit_code in completed:
        running.remove(agent)
        print(f"  Collecting: {agent.bead_id} (container: {agent.container_name})")
        try:
            result = collect_results(agent, exit_code)
            decision_status = (result.decision or {}).get("status", "")
            process_decision(result)
            _record_run(agent, result)
            _ingest_session(result)
            # Enqueue review_report job after a successful DONE dispatch (best-effort)
            if decision_status == "DONE":
                try:
                    run_id = Path(agent.output_dir).name if agent.output_dir else agent.bead_id
                    report_path = str(Path(agent.output_dir) / "experience_report.md")
                    decision_path = str(Path(agent.output_dir) / "decision.json")
                    payload = json.dumps({
                        "bead_id": agent.bead_id,
                        "report_path": report_path,
                        "decision_path": decision_path,
                        "run_id": run_id,
                    })
                    job_id = enqueue_job("review_report", payload=payload)
                    print(f"  Enqueued review_report job {job_id[:8]} for {agent.bead_id}")
                except Exception as eq_err:
                    print(f"  WARNING: enqueue review_report failed for {agent.bead_id}: {eq_err}",
                          file=sys.stderr)
        except Exception as e:
            error_msg = f"Collection error: {type(e).__name__}: {e}"
            print(f"  ERROR collecting {agent.bead_id}: {error_msg}")
            release_bead(agent.bead_id, "FAILED", error_msg[:200])
            _record_run(agent, DispatchResult(
                bead_id=agent.bead_id, exit_code=exit_code, error=error_msg))
            cleanup_worktree(agent.worktree_path)

    # Handle timed-out agents — kill, then try to recover results
    for agent in timed_out:
        running.remove(agent)
        print(f"  Killing timed-out: {agent.bead_id} (container: {agent.container_name})")
        kill_container(agent.container_name)

        try:
            result = collect_results(agent, -1)
            if result.decision or result.commit_hash:
                print(f"  Recovered results from timed-out {agent.bead_id}")
                process_decision(result)
                _record_run(agent, result)
            else:
                print(f"  No results from timed-out {agent.bead_id}, marking FAILED")
                release_bead(agent.bead_id, "FAILED",
                             f"Agent timeout ({MAX_AGENT_RUNTIME}s)")
                _record_run(agent, result)
                cleanup_worktree(agent.worktree_path)
        except Exception as e:
            error_msg = f"Timeout collection error: {type(e).__name__}: {e}"
            print(f"  ERROR collecting timed-out {agent.bead_id}: {error_msg}")
            release_bead(agent.bead_id, "FAILED", error_msg[:200])
            _record_run(agent, DispatchResult(
                bead_id=agent.bead_id, exit_code=-1, error=error_msg))
            cleanup_worktree(agent.worktree_path)


def poll_and_collect_librarians(running_librarians: list[RunningLibrarian]) -> None:
    """Poll running librarian containers and collect results for completed ones.

    Modifies the running_librarians list in place. No merge or worktree cleanup.
    Updates job status to done/failed and ingests session into graph.
    """
    completed: list[tuple[RunningLibrarian, int]] = []
    timed_out: list[RunningLibrarian] = []

    for lib in running_librarians:
        elapsed = time.time() - lib.started_at
        finished, exit_code = poll_container(lib.container_id)

        if finished:
            print(f"  Librarian completed: {lib.job_type}/{lib.job_id[:8]} "
                  f"(exit={exit_code}, {elapsed:.0f}s)")
            completed.append((lib, exit_code))
        elif elapsed > MAX_AGENT_RUNTIME:
            jsonl_file = _find_jsonl_file(lib.output_dir)
            stale = True
            if jsonl_file:
                try:
                    stale_secs = time.time() - jsonl_file.stat().st_mtime
                    stale = stale_secs > 300
                except OSError:
                    pass
            if stale:
                print(f"  Librarian timeout: {lib.job_type}/{lib.job_id[:8]} "
                      f"({elapsed:.0f}s, JSONL stale)")
                timed_out.append(lib)
            else:
                print(f"  Librarian {lib.job_id[:8]}: {elapsed:.0f}s elapsed, JSONL active")
                _collect_live_stats_for_librarian(lib)
        else:
            _collect_live_stats_for_librarian(lib)

    for lib, exit_code in completed:
        running_librarians.remove(lib)
        status = "DONE" if exit_code == 0 else "FAILED"
        try:
            remove_container(lib.container_name)
            _record_librarian_run(lib, exit_code, status)
            _ingest_session(DispatchResult(
                bead_id=lib.job_id,
                exit_code=exit_code,
                output_dir=lib.output_dir,
            ))
            complete_job(lib.job_id, status="done" if exit_code == 0 else "failed")
            print(f"  Librarian collected: {lib.job_type}/{lib.job_id[:8]} → {status}")
        except Exception as e:
            print(f"  ERROR collecting librarian {lib.job_id[:8]}: {e}")
            try:
                fail_job(lib.job_id)
                _record_librarian_run(lib, exit_code, "FAILED")
            except Exception:
                pass

    for lib in timed_out:
        running_librarians.remove(lib)
        print(f"  Killing timed-out librarian: {lib.container_name}")
        kill_container(lib.container_name)
        try:
            remove_container(lib.container_name)
            fail_job(lib.job_id)
            _record_librarian_run(lib, -1, "FAILED")
        except Exception as e:
            print(f"  ERROR handling timed-out librarian {lib.job_id[:8]}: {e}")


def _collect_live_stats_for_librarian(lib: RunningLibrarian) -> None:
    """Collect live stats for a running librarian container (same as bead agents)."""
    try:
        run_id = Path(lib.output_dir).name if lib.output_dir else lib.job_id

        snippet: str | None = None
        context_tokens = 0
        tool_delta = 0
        turn_delta = 0
        last_activity: str | None = None

        jsonl_file = _find_jsonl_file(lib.output_dir)
        if jsonl_file:
            snippet, context_tokens, new_offset, tool_delta, turn_delta, last_activity = _read_jsonl_incremental(
                jsonl_file, lib.jsonl_offset
            )
            lib.jsonl_offset = new_offset

        mem_mb = _read_cgroup_mem_mb(lib.container_id)

        cpu_usec = _read_cgroup_cpu_usec(lib.container_id)
        cpu_pct: float | None = None
        now = time.time()
        if cpu_usec is not None and lib.prev_cpu_usec > 0:
            elapsed_usec = (now - lib.prev_cpu_poll_time) * 1_000_000
            if elapsed_usec > 0:
                cpu_pct = (cpu_usec - lib.prev_cpu_usec) / elapsed_usec * 100.0
                cpu_pct = max(0.0, cpu_pct)
        if cpu_usec is not None:
            lib.prev_cpu_usec = cpu_usec
            lib.prev_cpu_poll_time = now

        update_live_stats(
            run_id=run_id,
            last_snippet=snippet,
            context_tokens=context_tokens,
            tool_delta=tool_delta,
            turn_delta=turn_delta,
            cpu_pct=cpu_pct,
            cpu_usec=cpu_usec,
            mem_mb=mem_mb,
            last_activity=last_activity,
            jsonl_offset=lib.jsonl_offset,
        )
    except Exception as e:
        print(f"  WARNING: live stats failed for librarian {lib.job_id[:8]}: {e}",
              file=sys.stderr)


# ── Dispatch cycle ──────────────────────────────────────────────


def dispatch_cycle(
    config: DispatcherConfig,
    running: list[RunningAgent],
    running_librarians: list[RunningLibrarian],
) -> int:
    """Run one dispatch cycle. Non-blocking.

    Phase 1: Poll running bead agents for completion, collect results.
    Phase 2: Poll running librarian agents, collect results.
    Phase 3: Launch new bead agents if under max_concurrent.
    Phase 4: Launch new librarian agents from queue if slots available.

    Returns number of beads newly dispatched this cycle.
    """
    timestamp = datetime.now().strftime("%H:%M:%S")
    queue_info = f"queue: {config.label_filter}" if config.label_filter else "all approved"
    running_ids = ", ".join(a.bead_id for a in running) if running else "none"
    lib_ids = ", ".join(f"{l.job_type}/{l.job_id[:8]}" for l in running_librarians) if running_librarians else "none"
    print(f"\n[{timestamp}] pid={os.getpid()} cycle ({queue_info}, "
          f"{len(running)} beads: [{running_ids}], "
          f"{len(running_librarians)} librarians: [{lib_ids}])")

    # ── Phase 1: Poll running bead agents ─────────────────────
    poll_and_collect(running)

    # ── Phase 2: Poll running librarian agents ─────────────────
    poll_and_collect_librarians(running_librarians)

    # ── Phase 3: Launch new bead agents ─────────────────────────
    dispatched = 0
    slots = config.max_concurrent - len(running)
    if slots <= 0:
        print(f"  At capacity ({len(running)}/{config.max_concurrent})")
        slots = 0

    available = []
    if slots > 0:
        # Check if the entire queue is paused
        if is_label_paused(config.label_filter):
            print(f"  Queue '{config.label_filter}' is paused — skipping dispatch")
            slots = 0
    if slots > 0:
        ready = get_ready_beads(config.label_filter)
        if not ready:
            print("  No approved beads found")
        else:
            # Filter out already-claimed and currently-running beads
            claimed = get_claimed_beads()
            running_bead_ids = {a.bead_id for a in running}
            candidates = [b for b in ready
                          if b.get("id") not in claimed
                          and b.get("id") not in running_bead_ids]

            # When no queue filter, also skip beads whose labels are paused
            if config.label_filter is None:
                paused_labels = get_paused_labels()
                if paused_labels:
                    before = len(candidates)
                    candidates = [
                        b for b in candidates
                        if not paused_labels.intersection(set(b.get("labels") or []))
                    ]
                    skipped = before - len(candidates)
                    if skipped:
                        print(f"  Skipped {skipped} bead(s) whose labels are paused: {paused_labels}")

            if not candidates:
                print(f"  {len(ready)} ready but all claimed or running")
            else:
                # Filter out beads whose blocking dependencies are not yet closed
                for bead in candidates:
                    bead_id = bead.get("id", "")
                    if bead.get("dependency_count", 0) == 0 and "dependencies" not in bead:
                        available.append(bead)
                        continue
                    open_deps = get_open_dependencies(bead_id)
                    if open_deps:
                        dep_ids = ", ".join(d.get("id", "?") for d in open_deps)
                        print(f"  Skipping {bead_id}: blocked by open dependencies [{dep_ids}]")
                    else:
                        available.append(bead)

                if not available:
                    print(f"  {len(candidates)} candidate(s) but all blocked by dependencies")

    if available:
        print(f"  {len(available)} available beads, {slots} slot(s) open")
        available.sort(key=lambda b: b.get("priority", 99))

    dashboard_paused = _is_dashboard_dispatch_paused()
    if dashboard_paused:
        print("  WARN: dashboard dispatch paused (data/dispatch.state) — "
              "dashboard-labeled beads will be skipped")

    for bead in available[:slots]:
        bead_id = bead["id"]
        title = bead.get("title", "?")
        bead_labels = bead.get("labels") or []
        image = image_for_bead(bead)
        print(f"  Selected: {bead_id} — {title} (P{bead.get('priority', '?')}) [{image}]")

        if dashboard_paused and "dashboard" in bead_labels:
            print(f"  Skipping {bead_id}: dashboard dispatch paused (smoke test failure)")
            continue

        if config.dry_run:
            print("  [DRY RUN] Would dispatch this bead")
            continue

        # Launch agent container (blocks until container starts)
        agent = start_agent(bead_id, image=image)
        if agent:
            agent.labels = bead.get("labels") or []
            _record_launch(agent)
            running.append(agent)
            dispatched += 1
            print(f"  Dispatched: {bead_id} → {agent.container_name}")
        else:
            print(f"  Launch failed: {bead_id}")
            release_bead(bead_id, "FAILED", "Container launch failed")
            wt = find_worktree_for_bead(bead_id)
            if wt:
                cleanup_worktree(wt)

    # ── Phase 4: Launch librarian agents from queue ────────────
    lib_slots = config.max_concurrent_librarians - len(running_librarians)
    if lib_slots > 0 and not config.dry_run:
        try:
            job = dequeue(config.max_concurrent_librarians)
            if job:
                lib = start_librarian(job)
                if lib:
                    _record_librarian_launch(lib)
                    running_librarians.append(lib)
                    print(f"  Librarian dispatched: {lib.job_type}/{lib.job_id[:8]} → {lib.container_name}")
                else:
                    print(f"  Librarian launch failed for job {job['id'][:8]}")
                    fail_job(job["id"])
        except Exception as e:
            print(f"  WARNING: librarian queue check failed: {e}", file=sys.stderr)
    elif lib_slots <= 0:
        print(f"  Librarian pool at capacity ({len(running_librarians)}/{config.max_concurrent_librarians})")

    return dispatched


# ── Recovery ────────────────────────────────────────────────────


def reconcile_state(running: list[RunningAgent]) -> None:
    """Reconcile all state locations at startup after recover_running_agents().

    Compares the in-memory running list (agents with live containers) against:
    1. SQLite dispatch_runs RUNNING rows — mark orphaned ones FAILED
    2. Dolt in_progress beads — reset orphaned ones to open
    3. Worktrees — delete orphaned ones with no new commits

    "Orphaned" means the bead has no live container in the running list.
    If commit state cannot be determined (broken worktree), logs a warning
    and leaves the worktree in place.
    """
    active_bead_ids = {a.bead_id for a in running}
    print(f"  reconcile_state: {len(active_bead_ids)} live containers")

    # 1. Mark orphaned RUNNING rows in SQLite as FAILED
    try:
        running_rows = get_currently_running()
        orphaned_rows = [r for r in running_rows if r.get("bead_id") not in active_bead_ids]
        if orphaned_rows:
            from agents.dispatch_db import _get_conn
            conn = _get_conn()
            try:
                for row in orphaned_rows:
                    print(f"  reconcile: marking SQLite row {row['id']} FAILED "
                          f"(bead {row.get('bead_id')} has no container)")
                    conn.execute(
                        "UPDATE dispatch_runs SET status='FAILED', "
                        "reason='orphaned: no container at startup' "
                        "WHERE id=? AND status='RUNNING'",
                        (row["id"],),
                    )
                conn.commit()
            finally:
                conn.close()
        else:
            print("  reconcile: no orphaned SQLite RUNNING rows")
    except Exception as e:
        print(f"  WARNING: reconcile SQLite failed: {e}", file=sys.stderr)

    # 2. Reset orphaned Dolt in_progress beads to open
    try:
        out = run_bd(["query", "status=in_progress", "--json"])
        if out:
            try:
                in_progress_beads = json.loads(out)
            except json.JSONDecodeError:
                in_progress_beads = []
            if isinstance(in_progress_beads, list):
                for bead in in_progress_beads:
                    bead_id = bead.get("id", "")
                    if bead_id and bead_id not in active_bead_ids:
                        print(f"  reconcile: resetting {bead_id} to open (no container)")
                        run_bd(["update", bead_id, "-s", "open"])
    except Exception as e:
        print(f"  WARNING: reconcile Dolt in_progress failed: {e}", file=sys.stderr)

    # 3. Clean orphaned worktrees with no new commits
    worktrees_dir = REPO_ROOT / ".worktrees"
    if not worktrees_dir.exists():
        return
    try:
        for worktree in sorted(worktrees_dir.iterdir()):
            if not worktree.is_dir():
                continue

            # Extract bead_id: worktrees are named {bead_id}-{YYYYMMDD}-{HHMMSS}
            name = worktree.name
            parts = name.rsplit("-", 2)
            if len(parts) < 3:
                continue
            bead_id = "-".join(parts[:-2])

            if bead_id in active_bead_ids:
                continue  # Belongs to a live agent — leave it

            # Determine if the worktree has new commits
            try:
                head_result = subprocess.run(
                    ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                    capture_output=True, text=True, timeout=5,
                )
                if head_result.returncode != 0 or not head_result.stdout.strip():
                    print(f"  WARNING: reconcile: cannot determine commit state "
                          f"for worktree {name}, leaving it")
                    continue
                head = head_result.stdout.strip()
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                print(f"  WARNING: reconcile: error checking worktree {name}: {e}, "
                      f"leaving it")
                continue

            # Get branch_base from the most recent output dir for this bead
            branch_base = ""
            runs_dir = REPO_ROOT / "data" / "agent-runs"
            if runs_dir.exists():
                run_candidates = sorted(
                    runs_dir.glob(f"{bead_id}-*"),
                    key=lambda p: p.name, reverse=True,
                )
                if run_candidates:
                    base_file = run_candidates[0] / ".branch_base"
                    if base_file.exists():
                        branch_base = base_file.read_text().strip()

            if not branch_base:
                print(f"  WARNING: reconcile: no branch_base for worktree {name}, "
                      f"leaving it")
                continue

            if head != branch_base:
                print(f"  reconcile: leaving worktree {name} (has commits)")
                continue

            # No new commits — safe to remove
            print(f"  reconcile: removing orphaned worktree {name} (no commits)")
            subprocess.run(
                ["git", "worktree", "remove", str(worktree), "--force"],
                capture_output=True, text=True, timeout=15,
                cwd=str(REPO_ROOT),
            )
    except Exception as e:
        print(f"  WARNING: reconcile worktrees failed: {e}", file=sys.stderr)


def recover_running_agents() -> list[RunningAgent]:
    """Scan for running agent containers from a prior dispatcher session.

    Looks for docker containers matching the agent-* naming convention
    and reconstructs RunningAgent objects from their output dirs.
    Enables dispatcher restart without losing track of running agents.
    """
    recovered = []
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=agent-", "--no-trunc",
             "--format", "{{.ID}} {{.Names}} {{.Status}}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []

        for line in result.stdout.strip().splitlines():
            parts = line.split(None, 2)
            if len(parts) < 2:
                continue
            container_id, container_name = parts[0], parts[1]

            # Extract bead_id from container name: agent-{bead_id}-{pid}
            name_parts = container_name.split("-", 2)
            if len(name_parts) < 3 or name_parts[0] != "agent":
                continue
            # bead_id may contain hyphens, pid is the last segment
            bead_id_and_pid = container_name[len("agent-"):]
            bead_id = bead_id_and_pid.rsplit("-", 1)[0]

            # Find output dir
            runs_dir = REPO_ROOT / "data" / "agent-runs"
            if not runs_dir.exists():
                continue
            candidates = sorted(
                runs_dir.glob(f"{bead_id}-*"),
                key=lambda p: p.name, reverse=True,
            )
            if not candidates:
                continue
            output_dir = str(candidates[0])

            # Read metadata from output dir
            branch_base = ""
            base_file = Path(output_dir) / ".branch_base"
            if base_file.exists():
                branch_base = base_file.read_text().strip()

            worktree_path = ""
            wt_file = Path(output_dir) / ".worktree_path"
            if wt_file.exists():
                worktree_path = wt_file.read_text().strip()

            branch = ""
            branch_file = Path(output_dir) / ".branch"
            if branch_file.exists():
                branch = branch_file.read_text().strip()
            else:
                branch = f"agent/{bead_id}"

            # Estimate started_at from output dir timestamp (YYYYMMDD-HHMMSS)
            dir_name = Path(output_dir).name
            ts_part = dir_name.replace(f"{bead_id}-", "", 1)
            try:
                started_at = datetime.strptime(ts_part, "%Y%m%d-%H%M%S").timestamp()
            except ValueError:
                started_at = time.time()

            agent = RunningAgent(
                bead_id=bead_id,
                container_name=container_name,
                container_id=container_id,
                output_dir=output_dir,
                worktree_path=worktree_path,
                branch=branch,
                branch_base=branch_base,
                image="",  # Unknown — doesn't matter for poll/collect
                started_at=started_at,
            )
            recovered.append(agent)
            print(f"  Recovered running agent: {bead_id} (container: {container_name})")

    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return recovered


# ── Main ────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Autonomy Dispatcher")
    parser.add_argument("--loop", action="store_true", help="Run continuously")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between cycles (default: 60)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be dispatched")
    parser.add_argument("--queue", default=None, help="Optional label to narrow dispatch (default: all approved)")
    parser.add_argument("--max-concurrent", type=int, default=1, help="Max concurrent agents (default: 1)")
    parser.add_argument("--max-concurrent-librarians", type=int, default=1,
                        help="Max concurrent librarian agents (default: 1)")

    args = parser.parse_args()
    config = DispatcherConfig(
        max_concurrent=args.max_concurrent,
        max_concurrent_librarians=args.max_concurrent_librarians,
        label_filter=args.queue,
        dry_run=args.dry_run,
        interval=args.interval,
        loop=args.loop,
    )

    pid = os.getpid()
    print(f"Autonomy Dispatcher (pid={pid})")
    print(f"  Queue: {config.label_filter or 'all approved'}")
    print(f"  Max concurrent: {config.max_concurrent}")
    print(f"  Max concurrent librarians: {config.max_concurrent_librarians}")
    print(f"  Loop: {config.loop} (interval: {config.interval}s)")

    # Ensure only one dispatcher runs at a time
    pid_file = REPO_ROOT / "data" / "dispatcher.pid"
    if pid_file.exists():
        old_pid = pid_file.read_text().strip()
        try:
            old_pid_int = int(old_pid)
            if old_pid_int != pid:
                os.kill(old_pid_int, 0)  # Check if alive
                print(f"  ERROR: Another dispatcher is running (pid={old_pid}). Exiting.",
                      file=sys.stderr)
                sys.exit(1)
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # Stale PID file or dead process — safe to proceed
    pid_file.write_text(str(pid))

    # Initialize dispatch runs database
    init_db()

    # Recover agents from a prior dispatcher session
    running: list[RunningAgent] = recover_running_agents()
    if running:
        print(f"  Recovered {len(running)} running agent(s) from prior session")
        for agent in running:
            _record_launch(agent)  # Ensure RUNNING row exists in DB

    # Librarians are not recovered across restarts (stateless job queue handles re-run)
    running_librarians: list[RunningLibrarian] = []

    # Reconcile all state locations — clean up orphaned rows, beads, and worktrees
    reconcile_state(running)

    if config.loop:
        while True:
            try:
                dispatch_cycle(config, running, running_librarians)
                time.sleep(config.interval)
            except KeyboardInterrupt:
                print(f"\nDispatcher stopped. {len(running)} agent(s) still running.")
                if running:
                    print("Running containers (will continue in background):")
                    for a in running:
                        print(f"  {a.bead_id}: {a.container_name}")
                break
    else:
        # Single-shot: launch, then poll until all agents complete
        dispatch_cycle(config, running, running_librarians)
        if running or running_librarians:
            print(f"\nWaiting for {len(running)} agent(s) and "
                  f"{len(running_librarians)} librarian(s) to complete...")
            while running or running_librarians:
                time.sleep(5)
                poll_and_collect(running)
                poll_and_collect_librarians(running_librarians)


if __name__ == "__main__":
    main()
