"""Autonomy Dispatcher — non-blocking claim/poll/collect loop.

Owns all bead state mutations. Agents run --readonly.
No LLM in this loop — just a state machine.

Launches agent containers in detached mode (docker run -d) and polls for
completion each cycle. This enables: concurrent dispatch, resilience to
dispatcher restarts, and responsive polling between agent runs.

Dispatches all readiness:approved beads by priority, routing each to the
container image configured in .beads/config.yaml (the rig default). Per-bead
label routing comes from the ``autonomy.workspace#1`` settings — each
workspace's ``dispatch_labels`` list maps a bead label to its image plus its
graph scoping (graph_project + default_tags). The --queue flag optionally
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
import logging
import os
import sqlite3
import subprocess
import sys
import time

logger = logging.getLogger(__name__)
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import importlib

import re

from agents.dispatch_db import (
    init_db, insert_run, insert_launch_run, update_live_stats,
    get_currently_running, get_consecutive_failures,
    set_dispatcher_paused, is_paused as db_is_paused, get_pause_reason,
)
from agents.git_status import working_tree_clean_and_summary
from agents.librarian_db import enqueue as enqueue_job, dequeue, complete_job, fail_job
from agents.workspace_manager import WORKTREES_DIR, cleanup_session_worktrees
from agents.workspace_settings import WorkspaceV1, load_workspaces
from agents.session_launcher import launch_session, DEFAULT_OPUS_MODEL
from tools.data_paths import DATA_ROOT

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCH_SCRIPT = Path(__file__).parent / "launch.sh"
DISPATCH_STATE_PATH = DATA_ROOT / "dispatch.state"

DEFAULT_IMAGE = "autonomy-agent"
# DEFAULT_OPUS_MODEL is imported from session_launcher — the single source of
# truth that launch_session_cli also resolves against. Do NOT redefine it here;
# a second copy silently drifts from the one the launcher actually applies.
DEFAULT_SONNET_MODEL = "claude-sonnet-4-6"

# Friendly ``model:<name>`` bead-label values → the model id passed through to
# ``launch_session_cli --model``. A bead label is the highest-precedence lever:
# launch_session_cli resolves ``args.model or workspace_model or default``, so a
# value here wins over the workspace model, which wins over the built-in default.
# An unknown alias must FAIL the dispatch (see _resolve_bead_model) — a typo that
# quietly runs the expensive default is exactly the failure this map prevents.
#
# Two kinds of entry, and the difference matters:
#
#   FAMILY aliases (opus/sonnet/haiku) are PINNED to a specific version and are
#   deliberately NOT repointed when a new model ships. ``opus`` maps to
#   DEFAULT_OPUS_MODEL, which is also the no-label fallback, so repointing it
#   would silently change the model for every bead that names nothing. Adding a
#   version-suffixed key below changes nothing that already exists.
#
#   VERSION-SUFFIXED aliases name one model exactly. Adding one here is the
#   whole cost of making a new model dispatchable — and because
#   _resolve_bead_model also accepts any value present in this map, adding
#   ``"opus-5": "claude-opus-5"`` makes BOTH ``model:opus-5`` and
#   ``model:claude-opus-5`` resolve, with no second entry.
#
# Note the harness CLI does its own aliasing: ``claude --model opus`` resolves
# to the LATEST opus. We pin instead, so a bead's model is reproducible and a
# new release cannot change what an already-approved bead runs.
MODEL_ALIASES: dict[str, str] = {
    "opus": DEFAULT_OPUS_MODEL,
    "sonnet": DEFAULT_SONNET_MODEL,
    "haiku": "claude-haiku-4-5-20251001",
    # Current generation, addressable by exact version.
    "opus-5": "claude-opus-5",
    "sonnet-5": "claude-sonnet-5",
    "fable-5": "claude-fable-5",
    "haiku-4-5": "claude-haiku-4-5-20251001",
    # Prior generation, still nameable now that the bare aliases are pinned.
    "opus-4-8": DEFAULT_OPUS_MODEL,
    "sonnet-4-6": DEFAULT_SONNET_MODEL,
}


def _resolve_bead_model(labels: list[str]) -> str | None:
    """Resolve a per-bead model override from a ``model:<name>`` label.

    Returns the resolved model id when a ``model:`` label is present, or None
    when absent — in which case the caller passes no ``--model`` and
    launch_session_cli's existing workspace-then-default chain resolves exactly
    as it does today. Accepts either a known alias (``opus``/``sonnet``/``haiku``)
    or a full model id already present in :data:`MODEL_ALIASES`'s values.

    Raises :class:`ValueError` naming the offending label when the value is
    neither — a typo must fail the dispatch loudly rather than silently fall
    back to the expensive default.
    """
    for label in labels:
        if not label.startswith("model:"):
            continue
        value = label[len("model:"):].strip()
        if value in MODEL_ALIASES:
            return MODEL_ALIASES[value]
        if value in MODEL_ALIASES.values():
            return value
        raise ValueError(
            f"Unknown model label {label!r}: '{value}' is not a known model. "
            f"Valid model: labels are {sorted(MODEL_ALIASES)} "
            f"(or a full model id). Refusing to dispatch with a silent fallback."
        )
    return None

# Deferred restart flag — set by _maybe_restart_dispatcher(), executed at end of cycle
_restart_scheduled = False


def _read_rig_image() -> str:
    """Read default container image from .beads/config.yaml."""
    # Default to the STATE volume rather than a relative ".beads" beside the
    # cwd, which resolves differently depending on where a process starts
    # (auto-qk4ip). BEADS_DIR still overrides for a caller that knows better.
    from tools.data_paths import DATA_ROOT as _DATA_ROOT
    config_path = Path(os.environ.get("BEADS_DIR", _DATA_ROOT / ".beads")) / "config.yaml"
    try:
        for line in config_path.read_text().splitlines():
            m = re.match(r"^image:\s*(.+)$", line)
            if m:
                return m.group(1).strip()
    except Exception:
        pass
    return DEFAULT_IMAGE


_rig_image = _read_rig_image()

# Grace period before staleness checks can trigger a kill (seconds).
# Gives the container time to boot and start writing JSONL.
BOOT_GRACE_PERIOD = 60

# Auto-pause after this many consecutive cross-bead merge failures
MERGE_FAILURE_PAUSE_THRESHOLD = 3
_consecutive_merge_failures = 0

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
    reason: str = ""


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
    # Latched once the JSONL tail shows a tool_use; extends the stale
    # threshold to STALE_THRESHOLD_TOOL_SECS.
    _extended: bool = False


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
    _extended: bool = False


@dataclass
class RunningAgentic:
    """Tracks live-stats state for a running agentic dispatch container.

    Agentic launches happen in the dashboard process and only ever exist
    in dispatch_runs as a DB row — there is no in-memory ``RunningAgent``
    for the dispatcher to hang per-tick state on. ``poll_and_collect_agentic``
    creates one of these the first time it sees a RUNNING agentic row, mirrors
    the live-stats collection that ``RunningAgent`` and ``RunningLibrarian``
    get in the main loop, and drops the entry on completion. Same field
    contract as the other two so ``_collect_live_stats_for`` can treat them
    interchangeably.
    """
    run_id: str            # also the tmux/container name (Round 5 contract)
    container_name: str
    container_id: str      # resolved lazily from container_name via docker inspect
    output_dir: str
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


# ── Failure classification ────────────────────────────────────────

_AUTH_ERROR_PATTERNS = re.compile(
    r"authentication_error|OAuth token has expired|Invalid authentication credentials|"
    r'"type"\s*:\s*"authentication_error"|401',
    re.IGNORECASE,
)


def _has_auth_error(output_dir: str) -> bool:
    """Check session JSONL tail and docker stderr for auth error patterns.

    Reads the last 4KB of the session JSONL file and the .docker-stderr
    file (captured before container removal) looking for known auth
    failure signatures.
    """
    # Check session JSONL (last 4KB)
    session_dir = Path(output_dir) / "sessions"
    if session_dir.exists():
        jsonl_files = list(session_dir.glob("**/*.jsonl"))
        if jsonl_files:
            try:
                fsize = jsonl_files[0].stat().st_size
                with open(jsonl_files[0], "rb") as fh:
                    offset = max(0, fsize - 4096)
                    fh.seek(offset)
                    tail = fh.read().decode("utf-8", errors="replace")
                if _AUTH_ERROR_PATTERNS.search(tail):
                    return True
            except OSError:
                pass

    # Check docker stderr (fallback for 5-second crash runs)
    stderr_path = Path(output_dir) / ".docker-stderr"
    if stderr_path.exists():
        try:
            stderr_text = stderr_path.read_text(errors="replace")
            if _AUTH_ERROR_PATTERNS.search(stderr_text):
                return True
        except OSError:
            pass

    return False


def classify_failure(output_dir: str, duration_secs: float) -> str:
    """Classify an agent failure into one of: auth, fast_crash, timeout, agent_failure.

    Called after collecting results from a non-zero exit or missing/failed decision.
    """
    if _has_auth_error(output_dir):
        return "auth"

    if duration_secs < 15:
        decision_path = Path(output_dir) / "decision.json"
        if not decision_path.exists():
            return "fast_crash"

    # Timeout is now determined by the caller via JSONL staleness, not wall-clock.
    # Check JSONL staleness here as a fallback classification.
    jsonl_file = _find_jsonl_file(output_dir)
    if jsonl_file:
        try:
            stale_secs = time.time() - jsonl_file.stat().st_mtime
            if stale_secs > STALE_THRESHOLD_SECS:
                return "timeout"
        except OSError:
            pass
    elif duration_secs > BOOT_GRACE_PERIOD:
        # No JSONL file found after boot grace — likely crashed before writing
        return "timeout"

    return "agent_failure"


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
            # Golden-rule gate, host-side closer (auto-w41na): this bd
            # runs on the HOST, where the cap-bin shim never rides — the
            # highest-volume closer in the system was the one ungated
            # caller (found live by packaging: a re-run that declined to
            # fabricate host evidence still got its bead closed here).
            # Same rule inline: runtime-critical + no proof ref = no
            # close; downgrade to BLOCKED semantics instead.
            labels = []
            try:
                show = run_bd(["show", bead_id, "--json"]) or "[]"
                row = json.loads(show)
                labels = (row[0] if isinstance(row, list) else row).get(
                    "labels") or []
            except Exception:
                pass
            proof_re = r"functional-proof: *[A-Za-z0-9/][A-Za-z0-9/_.:-]{5,}"
            if ("runtime-critical" in labels
                    and not re.search(proof_re, reason or "", re.I)
                    and not re.search(
                        proof_re, run_bd(["show", bead_id]) or "", re.I)):
                _retry_bd(["update", bead_id, "-s", "open"])
                # Refusal is the one moment the gate KNOWS host evidence
                # is outstanding — flip the readiness axis too, or the
                # reopened bead stays dispatch-eligible and the pipeline
                # re-dispatches work it structurally cannot finish
                # (packaging's observed loop). approved -> host-verify,
                # replacing not stacking; the coordinator flips it back
                # explicitly if build work remains.
                run_bd(["update", bead_id,
                        "--remove-label", "readiness:approved",
                        "--add-label", "readiness:host-verify"])
                run_bd(["update", bead_id, "--append-notes",
                        "golden-rule gate (host closer): refusing DONE close "
                        "— runtime-critical bead has no functional-proof "
                        "reference. Provide real-run evidence "
                        "(functional-proof: <ref>) or remove the label with "
                        "a recorded justification. Readiness moved to "
                        "host-verify: not dispatch-eligible until the "
                        "evidence lands or a coordinator flips it back."])
                print(f"  Golden-rule gate: host close REFUSED for {bead_id}")
                return True
            _retry_bd(["close", bead_id, "--reason", reason])
        elif status == "BLOCKED":
            _retry_bd(["update", bead_id, "-s", "open"])
            run_bd(["update", bead_id, "--append-notes", f"Blocked: {reason}"])
        elif status == "FAILED":
            _retry_bd(["update", bead_id, "-s", "open"])
            run_bd(["update", bead_id, "--append-notes", f"Failed: {reason}"])
        elif status == "TIMEOUT":
            _retry_bd(["update", bead_id, "-s", "open"])
            run_bd(["update", bead_id, "--append-notes", f"Timeout: {reason}"])
        elif status == "MERGE_FAILED":
            _retry_bd(["update", bead_id, "-s", "open"])
            run_bd(["update", bead_id, "--append-notes", f"Merge failed (will retry): {reason}"])
        else:
            # Unknown status — log and release
            _retry_bd(["update", bead_id, "-s", "open"])
            run_bd(["update", bead_id, "--append-notes", f"Released (unknown status {status}): {reason}"])
    except BdCommandError as e:
        print(f"  Close: FAILED — bd close returned: {e}", file=sys.stderr)
        print(f"  STALE BEAD WARNING: {bead_id} may need manual cleanup "
              f"(intended status: {status})", file=sys.stderr)
        return False

    action = "closed" if status == "DONE" else "reopened"
    print(f"  Close: OK → {bead_id} {action}")
    return True


# ── Image routing ────────────────────────────────────────────────


def _build_label_image_map() -> dict[str, str]:
    """Map bead label → container image, sourced from ``autonomy.workspace#1``.

    Each workspace's ``dispatch_labels`` list contributes one entry per label.
    Adding dispatch routing for a new workspace is a Settings update only.
    """
    mapping: dict[str, str] = {}
    for cfg in load_workspaces().values():
        for label in cfg.dispatch_labels:
            mapping[label] = cfg.image
    return mapping


def project_for_bead(bead: dict) -> WorkspaceV1 | None:
    """Return the workspace whose ``dispatch_labels`` match any bead label.

    Workspaces are scanned in Setting-enumeration order; first match wins.
    Returns None when no workspace claims the bead — caller falls back to
    the rig default.
    """
    labels = set(bead.get("labels") or ())
    if not labels:
        return None
    for cfg in load_workspaces().values():
        if labels.intersection(cfg.dispatch_labels):
            return cfg
    return None


def _workspace_for_graph_project(graph_project: str) -> WorkspaceV1 | None:
    """Return the first workspace whose owning graph_project matches."""
    for cfg in load_workspaces().values():
        if cfg.graph_project == graph_project:
            return cfg
    return None


def image_for_bead(bead: dict) -> str:
    """Select container image. Rig default, with per-bead label override."""
    project = project_for_bead(bead)
    if project is not None:
        return project.image
    return _rig_image


# ── Non-blocking agent lifecycle ─────────────────────────────────


def start_agent(
    bead_id: str,
    image: str = DEFAULT_IMAGE,
    *,
    harness: str = "claude",
    graph_project: str | None = None,
    graph_tags: tuple[str, ...] = (),
    workspace_id: str | None = None,
    model: str | None = None,
) -> RunningAgent | None:
    """Launch an agent container in detached mode. Returns immediately.

    Calls launch.sh --detach which:
    1. Creates worktree and generates prompt
    2. Starts container with docker run -d
    3. Returns container metadata as key=value pairs

    ``graph_project`` and ``graph_tags`` flow through to the container as
    ``GRAPH_SCOPE`` / ``GRAPH_TAGS`` env vars and are written into
    ``.session_meta.json`` so ingest can scope the resulting session source.

    ``model`` is forwarded as ``--model`` ONLY when set. When None, no flag is
    passed and launch_session_cli's ``args.model or workspace_model or default``
    chain resolves exactly as it does today — a bead with no model override
    inherits its workspace's model, or the built-in default when the workspace
    declares none.

    Returns RunningAgent on success, None on failure.
    """
    print(f"  Starting agent for {bead_id} (image: {image})...")

    cmd = [
        str(LAUNCH_SCRIPT),
        bead_id,
        f"--image={image}",
        f"--harness={harness}",
        "--detach",
    ]
    if graph_project:
        cmd.append(f"--graph-project={graph_project}")
    if workspace_id:
        cmd.append(f"--workspace-id={workspace_id}")
    if graph_tags:
        cmd.append(f"--graph-tags={','.join(graph_tags)}")
    if model:
        cmd.append(f"--model={model}")

    try:
        result = subprocess.run(
            cmd,
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

    # Capture docker stderr before removing the container (fallback for
    # fast crashes where session JSONL may not have been written yet)
    if exit_code != 0:
        try:
            logs_result = subprocess.run(
                ["docker", "logs", "--tail", "20", agent.container_name],
                capture_output=True, text=True, timeout=10,
            )
            if logs_result.stderr.strip():
                (Path(output_dir) / ".docker-stderr").write_text(
                    logs_result.stderr[-4096:]  # cap at 4KB
                )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

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
    return working_tree_clean_and_summary(REPO_ROOT, untracked="normal", timeout=10)


def functional_proof_missing(dispatch_result) -> bool:
    """True when a runtime-critical bead's decision.json carries no
    concrete functional artifact (golden-rule gate, auto-w41na).

    Concrete means a functional_artifacts entry whose path is a real
    token — non-empty, no angle-bracket placeholders. Shape only: the
    gate never stats paths, so content-addressed attachment ids and
    host-side paths both qualify; the record judges substance.
    """
    if "runtime-critical" not in (dispatch_result.labels or []):
        return False
    artifacts = (dispatch_result.decision or {}).get(
        "functional_artifacts") or []
    concrete = [
        a for a in artifacts
        if isinstance(a, dict) and str(a.get("path", "")).strip()
        and "<" not in str(a.get("path", ""))
    ]
    if concrete:
        return False
    # Pipeline-driven proof (auto-hwrho): a passing functional_check.log,
    # produced by the run's own functional_check.sh via smoke.py and recorded
    # in <output_dir>/smoke_result.json's functional block, also satisfies the
    # gate. Same standard — a REAL run's transcript — reached by convention
    # instead of a hand-written decision.json entry.
    return not functional_check_proven(dispatch_result)


def functional_check_proven(dispatch_result) -> bool:
    """True when the run's per-bead functional check ran and passed.

    Reads <output_dir>/smoke_result.json (written by smoke.py --functional-only
    / --output-dir) and accepts it as functional proof only when the functional
    block reports present + pass and the referenced functional_check.log exists
    on disk. Shape only — the log's substance is judged by the record.
    """
    out = getattr(dispatch_result, "output_dir", "")
    if not out:
        return False
    try:
        smoke = json.loads((Path(out) / "smoke_result.json").read_text())
    except Exception:
        return False
    fn = smoke.get("functional") or {}
    log = fn.get("log")
    return bool(fn.get("present") and fn.get("pass")
                and log and Path(log).exists())


def run_functional_check(dispatch_result) -> dict | None:
    """Execute the run's per-bead functional_check.sh via smoke.py, before the
    pre-merge gate (golden-rule gate feeder, auto-hwrho).

    Convention-based: a dispatched run declares pipeline-driven functional
    proof by writing an executable functional_check.sh into its output dir.
    This drives smoke.py --output-dir <dir> --functional-only (which skips the
    dashboard tiers, runs the script, tees stdout to functional_check.log, and
    emits a functional block) and persists smoke_result.json — the same file
    the post-merge dashboard smoke writes — so functional_proof_missing() can
    accept the log. No-op (returns None) when there is no output dir, no
    smoke.py, or no functional_check.sh.
    """
    out = getattr(dispatch_result, "output_dir", "")
    smoke_script = REPO_ROOT / "tools/dashboard/smoke.py"
    if not out or not smoke_script.exists():
        return None
    if not (Path(out) / "functional_check.sh").exists():
        return None
    try:
        raw = subprocess.run(
            [sys.executable, str(smoke_script),
             "--output-dir", out, "--functional-only"],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=140,
        )
        smoke = json.loads(raw.stdout) if raw.stdout.strip() else {
            "pass": False, "functional": {"present": True, "pass": False,
                                          "detail": "no smoke output"}}
        (Path(out) / "smoke_result.json").write_text(json.dumps(smoke))
        fn = smoke.get("functional") or {}
        print(f"  Functional check: present={fn.get('present')} "
              f"pass={fn.get('pass')} (log: {fn.get('log')})")
        return smoke
    except Exception as fc_err:
        print(f"  WARN: functional check error: {fc_err}", file=sys.stderr)
        return None



def merge_branch(branch: str, bead_id: str, reason: str) -> tuple[bool, str]:
    """Merge a branch into the current HEAD, handling dirty working trees.

    If the working tree is dirty, attempts git stash before merging and
    restores afterward. Returns (success, error_message).
    """
    is_clean, dirty_files = check_working_tree_clean()
    stashed = False

    if not is_clean:
        # Detect UU (unmerged) files — these can't be stashed and indicate
        # a persistent problem requiring manual resolution
        uu_files = [line.strip() for line in dirty_files.split(";")
                    if line.strip().startswith("UU ")]
        if uu_files:
            return False, f"UNMERGED_FILES: {'; '.join(uu_files[:3])}"

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
            if merge_ok:
                # Stash pop conflicts with merged code — revert the merge to
                # restore clean pre-merge state, then pop stash again
                print(f"  Stash pop conflicts with merged code — reverting merge")
                subprocess.run(
                    ["git", "reset", "--hard", "HEAD~1"],
                    capture_output=True, text=True, timeout=15,
                    cwd=str(REPO_ROOT),
                )
                # Pop should succeed now (tree is back to pre-merge state)
                pop2 = subprocess.run(
                    ["git", "stash", "pop"],
                    capture_output=True, text=True, timeout=15,
                    cwd=str(REPO_ROOT),
                )
                if pop2.returncode != 0:
                    print(f"  WARNING: second stash pop also failed: {pop2.stderr.strip()}")
                    print(f"  Stashed changes preserved in git stash list")
                return False, (
                    f"STASH_POP_CONFLICT: host local edits conflict with merged "
                    f"code from {bead_id}. {pop_result.stderr.strip()}"
                )
            else:
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
        # Capture the call chain so a missing-worktree incident can be
        # attributed to the responsible code path months later. See the
        # 2026-05-02 incident audit (auto-0502-123849) and the
        # ``_log_worktree_removed`` helper in workspace_manager.py for
        # the broader contract: every deletion of a worktree directory
        # must produce an INFO log line at the source.
        import traceback as _tb
        chain = " ← ".join(
            f"{f.name}@{Path(f.filename).name}:{f.lineno}"
            for f in _tb.extract_stack()[-5:-1]
        )
        result = subprocess.run(
            ["git", "worktree", "remove", worktree_path, "--force"],
            capture_output=True, text=True, timeout=15,
            cwd=str(REPO_ROOT),
        )
        if result.returncode == 0:
            logger.info(
                "workspace cleanup: REMOVED %s  method=git-worktree-remove(dispatcher)  caller=%s",
                worktree_path, chain,
            )
        else:
            logger.warning(
                "workspace cleanup: git worktree remove FAILED for %s rc=%d err=%s caller=%s",
                worktree_path, result.returncode,
                (result.stderr or "").strip(), chain,
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

_DISPATCH_STATE_PATH = DATA_ROOT / "dispatch.state"
_START_DISPATCHER_SCRIPT = REPO_ROOT / "agents" / "start-dispatcher.sh"
_DISPATCHER_WATCH_PATHS = (
    "agents/dispatcher.py",
    "agents/compose.py",
    "agents/launch.sh",
    "agents/session_launcher.py",
    "agents/dispatch_db.py",
)


def _dispatcher_files_changed(branch_base: str) -> bool:
    """Return True if the merged commit touched dispatcher or agent infrastructure files."""
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
            for watch in _DISPATCHER_WATCH_PATHS:
                if path == watch or path.startswith(watch):
                    return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return False


def _maybe_restart_dispatcher(branch_base: str) -> None:
    """Schedule a dispatcher restart if merged commit touched dispatcher/agent files.

    Does NOT restart immediately — sets a flag that is checked at the end of
    dispatch_cycle(), after all bookkeeping (release_bead, _record_run, etc.)
    is complete. This prevents the old bug where the dispatcher killed itself
    mid-collection, orphaning bead state.
    """
    global _restart_scheduled
    if not _dispatcher_files_changed(branch_base):
        return
    print("  Dispatcher files changed — restart scheduled after cycle completes")
    _restart_scheduled = True


def _pause_dashboard_dispatch(reason: str = "") -> None:
    """Write dashboard=true pause flag to data/dispatch.state.

    If reason is provided, also stores it as dashboard_reason so the
    dashboard UI can show WHY the queue was paused.
    """
    try:
        state: dict = {}
        if _DISPATCH_STATE_PATH.exists():
            try:
                state = json.loads(_DISPATCH_STATE_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        state["dashboard"] = True
        if reason:
            state["dashboard_reason"] = reason
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


def process_decision(dispatch_result: DispatchResult) -> str:
    """Process agent decision and update bead state. Returns effective status."""
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
        return "FAILED"

    status = decision.get("status", "FAILED")
    reason = decision.get("reason", "No reason provided")
    stash_pop_blocked = False  # Tracks STASH_POP_CONFLICT for readiness blocking
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

    # Per-bead functional check (auto-hwrho): if the run wrote a
    # functional_check.sh into its output dir, execute it now — before the
    # pre-merge gate — so a passing functional_check.log counts as the
    # functional artifact the gate demands. No-op when absent.
    if status == "DONE":
        run_functional_check(dispatch_result)

    # Auto-merge to master on DONE if agent committed
    # ── Golden-rule pre-merge gate (auto-w41na) ──────────────────────
    # A runtime-critical bead merges only with functional proof: evidence
    # the change ran on a REAL user path, listed in decision.json as
    #   functional_artifacts: [{"path": ..., "kind": ..., "note": ...}]
    # or produced by the run's own functional_check.sh (auto-hwrho).
    # A green test suite is not functional proof for a runtime change.
    # Mirrors the bd-close shim (tools/beads/bd); same label, same rule,
    # enforced here for the dispatched path the shim never sees.
    if status == "DONE" and functional_proof_missing(dispatch_result):
        if True:
            status = "BLOCKED"
            reason = (
                "golden-rule gate: runtime-critical bead has no "
                "functional artifact in decision.json — a real run's "
                "screenshot/tail/transcript is required before merge"
            )
            run_bd(["update", bead_id, "--append-notes",
                    "golden-rule gate BLOCKED merge: decision.json has no "
                    "concrete functional_artifacts entry. Provide evidence "
                    "of the change exercised on a real user path "
                    "(screenshot, log tail, transcript) and re-dispatch, "
                    "or remove the runtime-critical label with a recorded "
                    "justification."])
            print(f"  Golden-rule gate: BLOCKED {bead_id} (no functional artifact)")

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
                    # Pass --output-dir so the dashboard smoke also runs any
                    # per-bead functional_check.sh and rides the functional
                    # block on the same smoke_result.json (auto-hwrho).
                    smoke_cmd = [sys.executable, str(smoke_script)]
                    if dispatch_result.output_dir:
                        smoke_cmd += ["--output-dir", dispatch_result.output_dir]
                    smoke_raw = subprocess.run(
                        smoke_cmd,
                        capture_output=True, text=True,
                        cwd=str(REPO_ROOT), timeout=180,
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
                        # Build a human-readable reason with bead ID and failing checks
                        failed_checks = [
                            c.get("name", "?") for c in smoke.get("checks", [])
                            if not c.get("pass")
                        ]
                        smoke_reason = f"smoke failed on {bead_id}"
                        if failed_checks:
                            smoke_reason += f": {' '.join(failed_checks)}"
                        reason = "Post-merge smoke test failed — dashboard dispatch paused"
                        _pause_dashboard_dispatch(reason=smoke_reason)
                    else:
                        print(f"  Smoke test PASSED ({smoke.get('duration_ms', '?')}ms)")
                except Exception as smoke_err:
                    # Crash must not prevent bead from closing as DONE
                    print(f"  WARN: smoke test error for {bead_id}: {smoke_err}",
                          file=sys.stderr)
                    run_bd(["update", bead_id, "--append-notes",
                            f"smoke test errored (non-blocking): {smoke_err}"])

            # ── Auto-restart dispatcher if agent infra files changed ──────────
            _maybe_restart_dispatcher(dispatch_result.branch_base)
        else:
            print(f"  Merge: FAILED — {merge_err}")
            run_bd(["update", bead_id, "--append-notes",
                    f"merge failed on {dispatch_result.branch}: {merge_err}"])
            if merge_err.startswith("STASH_POP_CONFLICT:"):
                # Host working tree conflict — not retryable by agent.
                # BLOCKED (not MERGE_FAILED) to prevent auto-retry loop.
                # No MERGE_RETRY_CONTEXT: agent retries won't fix host's
                # local edits. Only the host cleaning up resolves this.
                status = "BLOCKED"
                reason = f"Host working tree conflict (not retryable): {merge_err[:200]}"
                stash_pop_blocked = True
                dispatch_result.reason = f"STASH_POP_CONFLICT: {merge_err[:200]}"
            else:
                # Store retry context for smart merge retry
                retry_note = (
                    f"MERGE_RETRY_CONTEXT\n"
                    f"branch: {dispatch_result.branch}\n"
                    f"commit: {dispatch_result.commit_hash}\n"
                    f"merge_error: {merge_err[:500]}"
                )
                run_bd(["update", bead_id, "--append-notes", retry_note])
                status = "MERGE_FAILED"
                reason = f"Merge conflict (will retry): {merge_err[:200]}"

    # Release the bead with appropriate state
    release_bead(bead_id, status, reason)

    # Stash pop conflict: block readiness to prevent re-dispatch.
    # Unlike agent BLOCKED (which reopens for retry), stash pop conflicts
    # are host-side issues that won't resolve on retry.
    if stash_pop_blocked:
        run_bd(["set-state", bead_id, "readiness=blocked",
                "--reason", "Stash pop conflict — host working tree needs cleanup"])

    # Clean up worktree (branch persists for review)
    cleanup_worktree(dispatch_result.worktree_path)

    # Delete branch if it was merged successfully
    if status == "DONE" and dispatch_result.branch:
        subprocess.run(
            ["git", "branch", "-d", dispatch_result.branch],
            capture_output=True, text=True, timeout=5,
            cwd=str(REPO_ROOT),
        )

    return status


# ── Dispatch completion nag ───────────────────────────────────────


def _notify_dispatch_nag(
    agent: RunningAgent,
    effective_status: str,
    result: DispatchResult,
) -> None:
    """Send CrossTalk dispatch-nag to all opted-in sessions. Best-effort."""
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from tools.dashboard.dao import dashboard_db
        dashboard_db.init_db()
        targets = dashboard_db.get_dispatch_nag_sessions()
        if not targets:
            return

        # Derive notification fields
        bead_id = agent.bead_id
        duration = int(time.time() - agent.started_at)
        dur_min, dur_sec = divmod(duration, 60)
        dur_str = f"{dur_min}m{dur_sec:02d}s"

        # Get bead title via bd show (best-effort)
        title = bead_id
        try:
            out = run_bd(["show", bead_id, "--json"])
            if out:
                info = json.loads(out)
                title = info.get("title", bead_id)
        except Exception:
            pass

        # Get lines changed from git diff (best-effort)
        lines_changed = ""
        if result.commit_hash:
            try:
                diff_stat = subprocess.run(
                    ["git", "diff", "--shortstat", f"{result.commit_hash}~1", result.commit_hash],
                    capture_output=True, text=True, timeout=5,
                    cwd=str(REPO_ROOT),
                )
                if diff_stat.stdout.strip():
                    lines_changed = diff_stat.stdout.strip()
            except Exception:
                pass

        # Build two-line message: identity + outcome, then outcome-specific detail
        if effective_status == "DONE":
            line2 = dur_str
            if lines_changed:
                line2 += f" · {lines_changed}"
        elif effective_status == "TIMEOUT":
            line2 = result.reason or f"JSONL stale after {dur_str}"
        elif effective_status == "MERGE_FAILED":
            line2 = result.reason or "Merge conflict (auto-retrying)"
        else:
            # FAILED, BLOCKED, etc.
            line2 = result.reason or result.error or f"Failed after {dur_str}"

        msg = f"{bead_id} {effective_status} — {title}\n{line2}"

        _send_dispatch_nag_crosstalk(targets, msg)
    except Exception as e:
        print(f"  WARN: dispatch nag failed: {e}", file=sys.stderr)


def _send_dispatch_nag_crosstalk(targets: list[str], message: str) -> None:
    """Send a dispatch nag message to multiple sessions via tmux paste-buffer."""
    import secrets as _secrets

    # Hard guard: never paste into real tmux sessions from a pytest process.
    # The dispatcher mocks in test_dispatcher_nonblocking don't cover the
    # nag path, so without this guard a unit-test run sprays MagicMock
    # repr strings into every opted-in operator session.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return

    iso_now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    envelope = (
        f'<crosstalk from="dispatcher"\n'
        f'           label="Dispatch Notification"\n'
        f'           source="" turn="0"\n'
        f'           timestamp="{iso_now}">\n'
        f'{message}\n'
        f'</crosstalk>'
    )

    for tmux_name in targets:
        try:
            buf = f"nag_{_secrets.token_hex(4)}"
            path = f"/tmp/dispatch_nag_{_secrets.token_hex(4)}.txt"
            Path(path).write_text(envelope, encoding="utf-8")
            subprocess.run(
                ["tmux", "load-buffer", "-b", buf, path],
                capture_output=True, timeout=5,
            )
            subprocess.run(
                ["tmux", "paste-buffer", "-p", "-b", buf, "-t", tmux_name],
                capture_output=True, timeout=5,
            )
            subprocess.run(
                ["tmux", "delete-buffer", "-b", buf],
                capture_output=True, timeout=5,
            )
            time.sleep(0.3)
            subprocess.run(
                ["tmux", "send-keys", "-t", tmux_name, "", "Enter"],
                capture_output=True, timeout=5,
            )
            Path(path).unlink(missing_ok=True)
            print(f"  dispatch nag -> {tmux_name}", file=sys.stderr)
        except Exception as exc:
            # Best-effort delivery, but NOT silent: this swallowed every
            # failure, so a nag that never arrived looked identical to one
            # that was never attempted. That ambiguity cost a debugging
            # session — the notify functions logged, the send did not.
            print(
                f"  WARN: dispatch nag send failed for {tmux_name}: {exc}",
                file=sys.stderr,
            )


def _notify_agentic_dispatch_nag(
    run_id: str,
    status: str,
    reason: str,
    agentic_source_id: str | None,
) -> None:
    """Announce an agentic dispatch's completion. Best-effort.

    ``_notify_dispatch_nag`` covers the bead-agent path only -- all five of
    its call sites take a ``RunningAgent`` and it resolves its title via
    ``bd show``, neither of which an agentic run has. So agentic dispatches
    finished silently.

    Two audiences. ``dispatch_nag`` subscribers hear about every completion,
    which is what that flag has always meant. The session that launched this
    run hears about its own without subscribing: ``api_agent_action_dispatch``
    records it on the agentic source row as ``dispatched_by_session``, and
    ``"dashboard"`` is the sentinel for a browser click with no session
    behind it.
    """
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from tools.dashboard.dao import dashboard_db
        from tools.dashboard.server import _resolve_agentic_identity
        dashboard_db.init_db()
        targets = dashboard_db.get_dispatch_nag_sessions()

        identity = _resolve_agentic_identity(agentic_source_id)
        origin = identity.get("dispatched_by_session") or ""
        if origin and origin != "dashboard" and origin not in targets:
            targets.append(origin)
        if not targets:
            print(
                f"  agentic nag: no targets for {run_id} "
                f"(origin={origin!r}, subscribers=0)",
                file=sys.stderr,
            )
            return

        label = identity.get("action_label") or run_id
        title = identity.get("title") or ""
        msg = f"{label} {status}"
        if title and title != label:
            msg += f" — {title}"
        if reason:
            msg += f"\n{reason}"
        _send_dispatch_nag_crosstalk(targets, msg)
    except Exception as e:
        print(f"  WARN: agentic dispatch nag failed: {e}", file=sys.stderr)


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


# Upper bound on the backward scan for the last complete JSONL line. A single
# line beyond this is pathological; stop rather than read an unbounded tail.
_TAIL_SCAN_CAP = 8 * 1024 * 1024

# How long a session's JSONL may go unwritten before the run is reaped.
#
# This is a THINKING budget, not a liveness check. Between two turns the agent
# writes nothing: the model is composing the next one, and a hard problem can
# occupy it for minutes with no observable output anywhere -- not in the JSONL,
# not in docker logs, and not on any screen, because the agentic harness runs
# `claude -p` with no TTY. A budget shorter than the model's real thinking time
# therefore kills healthy runs and reports them exactly like a hang.
#
# Measured 2026-08-16: auto-42rsi was reaped five consecutive times at 300s
# while every other bead passed. Under observation the same bead showed
# between-turn pauses of 112s and 152s and then resumed normally, reaching
# implementation. The pauses scale with how hard the bead is to think about, so
# a fixed 300s silently penalises exactly the work that most needs the time.
STALE_THRESHOLD_SECS = 600
# Extended budget once a tool call is known to be in flight. A tool runs for as
# long as it runs -- a test suite, a build -- and that is not thinking time.
STALE_THRESHOLD_TOOL_SECS = 1800


def _has_running_tool(jsonl_file: Path) -> bool:
    """Return True if the last JSONL entry is an assistant turn with a tool_use block.

    Reads back to the last newline so a long-running tool call (pytest,
    builds) isn't mistaken for a stalled agent. Any I/O or parse error returns
    False so the caller falls back to the default 300s stale threshold.

    The window EXPANDS until it contains a line boundary rather than using a
    fixed 8KB tail. While a tool runs, the last line IS the assistant
    ``tool_use`` — the ``tool_result`` line is not written until the tool
    finishes — and the size of that line is model-controlled: a large ``Write``
    input or a fat MCP argument easily exceeds 8KB. With a fixed tail the
    window would start mid-JSON, ``json.loads`` would raise, and a legitimately
    running tool would silently drop to the 300s threshold and be killed
    mid-flight. Verified against auto-42rsi's runs 2026-08-16: a 30KB in-flight
    ``tool_use`` line returned False before this change.
    """
    try:
        with open(jsonl_file, "rb") as f:
            size = f.seek(0, 2)
            window, chunk = 8192, b""
            while True:
                f.seek(max(0, size - window))
                chunk = f.read().rstrip(b"\n")
                # Everything after the last newline is the final COMPLETE line.
                if b"\n" in chunk or size <= window or window >= _TAIL_SCAN_CAP:
                    break
                window *= 4
        data = chunk.rpartition(b"\n")[2].decode("utf-8", errors="replace")
        for line in [data]:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                return False
            if entry.get("type") != "assistant":
                return False
            content = entry.get("message", {}).get("content", [])
            if isinstance(content, list):
                return any(
                    isinstance(b, dict) and b.get("type") == "tool_use"
                    for b in content
                )
            return False
        return False
    except Exception:
        return False


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


def _resolve_container_id(name: str) -> str | None:
    """Map a container name to its long Docker ID for cgroup path building.

    Cgroup files live under ``/sys/fs/cgroup/system.slice/docker-<id>.scope/``,
    keyed on the long hex ID — passing a name produces an ``OSError`` on the
    cgroup read. Cheap one-shot ``docker inspect``; callers cache the result
    on their holder so this only runs once per agent's lifetime.
    """
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}}", name],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        cid = result.stdout.strip()
        return cid or None
    except (subprocess.TimeoutExpired, FileNotFoundError):
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


def _dashboard_base_url() -> str:
    return os.environ.get("GRAPH_API") or "https://localhost:8080"


def _monitor_post(path: str, body: dict, *, tmux_name: str) -> None:
    """POST to a dashboard /api/monitor/* endpoint. Best-effort — if the
    dashboard is unreachable, log a warning and continue. Direct DB writes
    are NOT an acceptable fallback: they bypass session_monitor's in-process
    state (inotify watches + SSE broadcasts). See graph://f4b1bb26-a1."""
    import json as _json
    import ssl
    import urllib.request

    url = _dashboard_base_url().rstrip("/") + path
    data = _json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    ctx = ssl.create_default_context()
    if url.startswith(("https://localhost", "https://127.0.0.1")):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            resp.read()
    except Exception as e:
        print(f"  WARNING: monitor {path} for {tmux_name} failed: {e}",
              file=sys.stderr)


def _register_dispatch_session(agent: "RunningAgent", jsonl_file: Path) -> None:
    """Register a dispatch session with the monitor (idempotent, best-effort)."""
    tmux_name = Path(agent.output_dir).name if agent.output_dir else agent.bead_id
    project = jsonl_file.parent.name if jsonl_file.is_file() else "autonomy"
    body = {
        "tmux_name": tmux_name,
        "type": "dispatch",
        "jsonl_path": str(jsonl_file),
        "bead_id": agent.bead_id,
        "project": project,
        "run_dir": str(agent.output_dir) if agent.output_dir else None,
    }
    _monitor_post("/api/monitor/register", body, tmux_name=tmux_name)


def _register_librarian_session(
    lib: "RunningLibrarian", jsonl_file: Path,
) -> None:
    """Register a librarian session with the monitor (idempotent, best-effort)."""
    tmux_name = Path(lib.output_dir).name if lib.output_dir else lib.job_id
    project = jsonl_file.parent.name if jsonl_file.is_file() else "autonomy"
    body = {
        "tmux_name": tmux_name,
        "type": "librarian",
        "jsonl_path": str(jsonl_file),
        "project": project,
        "run_dir": str(lib.output_dir) if lib.output_dir else None,
    }
    _monitor_post("/api/monitor/register", body, tmux_name=tmux_name)


def _deregister_session_with_monitor(tmux_name: str) -> None:
    """Tell the monitor to mark a session dead. Best-effort over HTTP so the
    dispatch process doesn't crash when the dashboard is down."""
    _monitor_post(
        "/api/monitor/deregister", {"tmux_name": tmux_name}, tmux_name=tmux_name,
    )


def _register_agentic_session(
    run_id: str, output_dir: str, jsonl_file: Path,
) -> None:
    """Register an agentic dispatch session with the monitor.

    Agentic launches happen in the dashboard process
    (``api_agent_action_dispatch``), not the dispatcher's RunningAgent
    loop. Without explicit registration the monitor never sets up an
    inotify watch on the JSONL, no ``session:messages`` broadcasts
    fire, and the live-trace overlay can only show new turns after a
    full close/reopen. The agentic poll loop calls this once the
    JSONL appears; ``register_session`` on the monitor side is
    idempotent on the (tmux_name, jsonl_path) tuple.

    ``run_id`` is both the dispatch_runs row id AND the tmux/container
    name (Round 5 contract for kind='agentic' rows), so it doubles as
    the SSE session_id the front-end's session-store handler routes by.
    """
    tmux_name = run_id
    project = jsonl_file.parent.name if jsonl_file.is_file() else "autonomy"
    # The launcher stamps the exact provider identity alongside the JSONL.
    # Carry it across the dispatcher→dashboard IPC boundary so the monitor
    # chooses the right parser from byte zero (and the Dispatch card can show
    # its model before the first assistant response arrives).
    harness = "claude"
    model: str | None = None
    if output_dir:
        meta_path = Path(output_dir) / "sessions" / ".session_meta.json"
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, json.JSONDecodeError, TypeError):
            meta = {}
        if isinstance(meta, dict):
            harness = str(meta.get("harness") or "claude")
            raw_model = meta.get("model")
            if isinstance(raw_model, str) and raw_model:
                model = raw_model
    body = {
        "tmux_name": tmux_name,
        "type": "agentic",
        "jsonl_path": str(jsonl_file),
        "project": project,
        "run_dir": output_dir or None,
        "harness": harness,
        "model": model,
    }
    _monitor_post("/api/monitor/register", body, tmux_name=tmux_name)


def _read_stats_via_monitor(
    tmux_name: str,
    holder: "RunningAgent | RunningLibrarian",
) -> tuple[str | None, int, int, int, str | None]:
    """Read snippet/tokens/entries from the monitor's DB view.

    Returns (snippet, context_tokens, entry_count, turn_delta, last_activity).
    turn_delta is the delta in entry_count since the last poll — the
    dispatcher's dispatch_runs table is still keyed on per-tick deltas.
    """
    try:
        from tools.dashboard.dao.dashboard_db import get_session
    except Exception:
        return None, 0, 0, 0, None
    try:
        row = get_session(tmux_name)
    except Exception:
        return None, 0, 0, 0, None
    if not row:
        return None, 0, 0, 0, None
    snippet = (row.get("last_message") or "") or None
    if snippet:
        snippet = snippet[:300]
    context_tokens = int(row.get("context_tokens") or 0)
    entry_count = int(row.get("entry_count") or 0)
    prev_count = getattr(holder, "_prev_entry_count", 0)
    turn_delta = max(0, entry_count - prev_count)
    holder._prev_entry_count = entry_count
    last_activity = row.get("last_activity")
    if last_activity is not None:
        # tmux_sessions.last_activity is REAL (float epoch). dispatch_runs
        # stores it as DATETIME. str(float) rounds-trips back to REAL via
        # SQLite's NUMERIC affinity and the server then TypeErrors on
        # datetime.fromisoformat(float). Format as ISO string here so the
        # column holds the intended datetime shape end-to-end.
        last_activity = datetime.fromtimestamp(
            float(last_activity), tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
    return snippet, context_tokens, entry_count, turn_delta, last_activity


def _collect_live_stats_for(
    holder,
    *,
    register_session,
    fallback_run_id: str,
    error_label: str,
) -> None:
    """Shared live-stats collection for any holder shaped like RunningAgent.

    Holder contract: attributes ``container_id``, ``output_dir``,
    ``jsonl_offset``, ``prev_cpu_usec``, ``prev_cpu_poll_time``. The monitor
    helper sets ``_prev_entry_count`` on it.

    The three callers (bead agent, librarian, agentic) differ only in how
    they register the JSONL with the monitor and what label to log on
    failure, so those are passed in. Card stats (snippet, context_tokens,
    entry_count, last_activity) come from the monitor's DB view —
    graph://554a08c6-887 explains why dashboard.db is the single source of
    truth for JSONL tail state. Cgroup memory and CPU are still read direct
    from the host's cgroup files; the resolver upstream is responsible for
    ensuring ``container_id`` is the long hex ID, not a name.
    """
    try:
        run_id = Path(holder.output_dir).name if holder.output_dir else fallback_run_id

        jsonl_file = _find_jsonl_file(holder.output_dir)
        if jsonl_file:
            register_session(jsonl_file)

        snippet, context_tokens, entry_count, turn_delta, last_activity = (
            _read_stats_via_monitor(run_id, holder)
        )
        tool_delta = 0  # tool-use counting lives in monitor.get_session_stats

        mem_mb = _read_cgroup_mem_mb(holder.container_id)

        cpu_usec = _read_cgroup_cpu_usec(holder.container_id)
        cpu_pct: float | None = None
        now = time.time()
        if cpu_usec is not None and holder.prev_cpu_usec > 0:
            elapsed_usec = (now - holder.prev_cpu_poll_time) * 1_000_000
            if elapsed_usec > 0:
                cpu_pct = (cpu_usec - holder.prev_cpu_usec) / elapsed_usec * 100.0
                cpu_pct = max(0.0, cpu_pct)
        if cpu_usec is not None:
            holder.prev_cpu_usec = cpu_usec
            holder.prev_cpu_poll_time = now

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
            jsonl_offset=holder.jsonl_offset,
        )
    except Exception as e:
        print(
            f"  WARNING: live stats collection failed for {error_label}: {e}",
            file=sys.stderr,
        )


def _collect_live_stats(agent: RunningAgent) -> None:
    """Bead-agent adapter — see ``_collect_live_stats_for``."""
    _collect_live_stats_for(
        agent,
        register_session=lambda jl: _register_dispatch_session(agent, jl),
        fallback_run_id=agent.bead_id,
        error_label=agent.bead_id,
    )


# ── Session ingestion ───────────────────────────────────────────


def _ingest_session_safe(
    agent: RunningAgent,
    result: DispatchResult,
    effective_status: str,
) -> None:
    """Run :func:`_ingest_session` as a non-raising post-decision step.

    By the time this is called, ``process_decision`` has already settled
    bead state (closed DONE, reopened on FAILED, etc.) and any merge has
    landed on master/hostsync. A downstream collection failure must not
    flip that state — it's a librarian-side issue, not implementation
    failure.

    This guards against the auto-qjme3 contradiction: ``graph sessions
    --all`` timed out *after* commit 4d9fb0c had already landed, the
    enclosing ``except`` in :func:`poll_and_collect` then called
    ``release_bead("FAILED")`` which reopened the merged bead.

    On failure the warning is logged and appended to the bead so it
    surfaces as a separate signal from the landed implementation status.
    """
    try:
        _ingest_session(result)
    except Exception as e:
        warning = f"post-land ingest warning ({type(e).__name__}): {str(e)[:200]}"
        print(
            f"  WARN ingest for {agent.bead_id} (status={effective_status}): {warning}",
            file=sys.stderr,
        )
        try:
            run_bd(["update", agent.bead_id, "--append-notes", warning])
        except Exception:
            pass


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


def _record_run(agent: RunningAgent, result: DispatchResult, *, effective_status: str | None = None) -> None:
    """Record dispatch run metadata to SQLite. Best-effort — never raises."""
    try:
        # Derive run_id from output dir name (e.g. auto-ahd-20260316-234902)
        run_id = Path(agent.output_dir).name if agent.output_dir else agent.bead_id

        decision = result.decision or {}
        status = effective_status or decision.get("status", "FAILED")
        reason = decision.get("reason", "No decision file")

        # Classify agent-side failures (non-zero exit, no decision, or decision != DONE)
        # MERGE_FAILED and TIMEOUT are dispatcher-side — skip classification
        fc: str | None = None
        if status == "TIMEOUT":
            fc = "timeout"
        elif (status == "BLOCKED"
              and "STASH_POP_CONFLICT" in (result.reason or "")):
            # Stash pop conflict is a merge/host issue, not an agent failure
            fc = "merge"
        elif status not in ("DONE", "MERGE_FAILED") and (
            result.exit_code != 0 or not result.decision
        ):
            duration = time.time() - agent.started_at
            fc = classify_failure(agent.output_dir, duration)

        # Auth failures are non-retryable — pause the entire dispatcher immediately
        if fc == "auth":
            pause_reason = {
                "reason": "auth",
                "paused_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "bead_id": agent.bead_id,
                "message": reason[:200],
            }
            set_dispatcher_paused(pause_reason)
            print(
                f"  AUTH FAILURE — dispatcher paused. "
                f"Resume via Dashboard or POST /api/dispatch/resume",
                file=sys.stderr,
            )

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
            failure_class=fc,
        )
        print(f"  Record: OK → {run_id}" + (f" [failure_class={fc}]" if fc else ""))
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
    output_dir = str(DATA_ROOT / "agent-runs" / run_id)

    container_name = f"librarian-{job_type}-{os.getpid()}-{job_id[:8]}"
    harness = "claude"
    workspace = _workspace_for_graph_project("autonomy")
    if workspace is not None:
        harness = workspace.harness

    container_id = launch_session(
        session_type="librarian",
        name=container_name,
        prompt=prompt,
        metadata={
            "job_id": job_id,
            "job_type": job_type,
            "graph_project": "autonomy",
        },
        detach=True,
        image=_rig_image,
        harness=harness,
        output_dir=output_dir,
        model=(workspace.model if workspace and workspace.model else
               (DEFAULT_SONNET_MODEL if harness == "claude" else None)),
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
            image=_rig_image,
            container_name=lib.container_name,
            output_dir=lib.output_dir,
            librarian_type=lib.job_type,
            kind="librarian",
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
            image=_rig_image,
            container_name=lib.container_name,
            exit_code=exit_code,
            output_dir=lib.output_dir,
            librarian_type=lib.job_type,
            kind="librarian",
        )
    except Exception as e:
        print(f"  WARNING: Failed to record librarian run to SQLite: {e}",
              file=sys.stderr)


# ── Merge failure tracking ────────────────────────────────────────


def _update_merge_failure_counter(effective_status: str, result: DispatchResult) -> None:
    """Track consecutive cross-bead merge failures and auto-pause dispatcher.

    Increments on MERGE_FAILED, resets on DONE. When the threshold is reached,
    pauses the dispatcher globally so the operator can fix the working tree.
    """
    global _consecutive_merge_failures

    # Count both MERGE_FAILED and stash-pop-conflict BLOCKED toward the
    # cross-bead merge failure counter (working tree is blocking merges).
    is_merge_failure = (
        effective_status == "MERGE_FAILED"
        or (effective_status == "BLOCKED"
            and "STASH_POP_CONFLICT" in (result.reason or ""))
    )

    if is_merge_failure:
        _consecutive_merge_failures += 1
        merge_err = (result.decision or {}).get("reason", "")
        print(f"  Merge failure #{_consecutive_merge_failures} "
              f"(threshold: {MERGE_FAILURE_PAUSE_THRESHOLD})")
        if _consecutive_merge_failures >= MERGE_FAILURE_PAUSE_THRESHOLD:
            set_dispatcher_paused({
                "reason": "merge_blocked",
                "paused_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "message": (
                    f"{_consecutive_merge_failures} consecutive merge failures "
                    f"— working tree may need manual cleanup"
                ),
                "last_error": merge_err[:200],
            })
            print(f"  AUTO-PAUSED: {_consecutive_merge_failures} consecutive merge failures")
    elif effective_status == "DONE":
        if _consecutive_merge_failures > 0:
            print(f"  Merge failure counter reset (was {_consecutive_merge_failures})")
        _consecutive_merge_failures = 0


# ── Poll and collect ─────────────────────────────────────────────


def poll_and_collect(running: list[RunningAgent]) -> None:
    """Poll running agents and collect results for any that completed or timed out.

    Modifies the running list in place — removes completed/timed-out agents.
    """
    completed: list[tuple[RunningAgent, int]] = []
    timed_out: list[tuple[RunningAgent, float]] = []  # (agent, stale_secs)

    for agent in running:
        elapsed = time.time() - agent.started_at
        finished, exit_code = poll_container(agent.container_id)

        if finished:
            print(f"  Completed: {agent.bead_id} (exit={exit_code}, {elapsed:.0f}s)")
            completed.append((agent, exit_code))
        else:
            # Check JSONL staleness unconditionally (with boot grace period)
            jsonl_file = _find_jsonl_file(agent.output_dir)
            stale = True
            stale_secs = -1.0
            if jsonl_file:
                try:
                    stale_secs = time.time() - jsonl_file.stat().st_mtime
                    # Tail the JSONL for a running tool call. Latched once True
                    # so subsequent ticks skip the file read.
                    if stale_secs > STALE_THRESHOLD_SECS:
                        if not agent._extended and _has_running_tool(jsonl_file):
                            agent._extended = True
                        threshold = (
                            STALE_THRESHOLD_TOOL_SECS if agent._extended
                            else STALE_THRESHOLD_SECS
                        )
                        stale = stale_secs > threshold
                    else:
                        stale = False
                except OSError:
                    pass
            if stale and elapsed > BOOT_GRACE_PERIOD:
                print(f"  Timeout: {agent.bead_id} ({elapsed:.0f}s, JSONL stale)")
                timed_out.append((agent, stale_secs))
            elif not stale:
                _collect_live_stats(agent)

    # Process normally-completed agents
    for agent, exit_code in completed:
        running.remove(agent)
        _deregister_session_with_monitor(
            Path(agent.output_dir).name if agent.output_dir else agent.bead_id
        )
        print(f"  Collecting: {agent.bead_id} (container: {agent.container_name})")

        # ── Phase 1: pre-decision. A failure here means we never landed,
        # so reopening the bead as FAILED is correct. ────────────────────
        try:
            result = collect_results(agent, exit_code)
            effective_status = process_decision(result)
        except Exception as e:
            error_msg = f"Collection error: {type(e).__name__}: {e}"
            print(f"  ERROR collecting {agent.bead_id}: {error_msg}")
            release_bead(agent.bead_id, "FAILED", error_msg[:200])
            _notify_dispatch_nag(agent, "FAILED", DispatchResult(
                bead_id=agent.bead_id, exit_code=exit_code, error=error_msg))
            _record_run(agent, DispatchResult(
                bead_id=agent.bead_id, exit_code=exit_code, error=error_msg),
                effective_status="FAILED")
            cleanup_worktree(agent.worktree_path)
            continue

        # ── Phase 2: post-decision. ``process_decision`` has already
        # settled bead state (closed DONE on a landed merge, reopened
        # otherwise) and resolved the worktree. A failure here must NOT
        # call ``release_bead("FAILED")`` — that's how auto-qjme3 got
        # reopened after commit 4d9fb0c had already landed on master.
        # Wrap each side-effect so a librarian-side hiccup surfaces as
        # a warning, not as implementation failure. ──────────────────────
        _notify_dispatch_nag(agent, effective_status, result)
        _update_merge_failure_counter(effective_status, result)
        _record_run(agent, result, effective_status=effective_status)
        _ingest_session_safe(agent, result, effective_status)
        # Enqueue review_report job after a successful DONE dispatch (best-effort)
        if effective_status == "DONE":
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

    # Handle timed-out agents — kill, then try to recover results
    for agent, stale_secs in timed_out:
        running.remove(agent)
        _deregister_session_with_monitor(
            Path(agent.output_dir).name if agent.output_dir else agent.bead_id
        )
        elapsed = time.time() - agent.started_at
        if stale_secs >= 0:
            timeout_reason = f"JSONL stale {int(stale_secs)}s after {int(elapsed)}s"
        else:
            timeout_reason = f"No JSONL output after {int(elapsed)}s"
        print(f"  Killing timed-out: {agent.bead_id} (container: {agent.container_name})")
        kill_container(agent.container_name)

        try:
            result = collect_results(agent, -1)
            if result.decision or result.commit_hash:
                print(f"  Recovered results from timed-out {agent.bead_id}")
                effective_status = process_decision(result)
                _notify_dispatch_nag(agent, effective_status, result)
                _record_run(agent, result, effective_status=effective_status)
            else:
                print(f"  No results from timed-out {agent.bead_id}, marking TIMEOUT")
                release_bead(agent.bead_id, "TIMEOUT", timeout_reason)
                result.reason = timeout_reason
                _notify_dispatch_nag(agent, "TIMEOUT", result)
                _record_run(agent, result, effective_status="TIMEOUT")
                cleanup_worktree(agent.worktree_path)
        except Exception as e:
            error_msg = f"Timeout collection error: {type(e).__name__}: {e}"
            print(f"  ERROR collecting timed-out {agent.bead_id}: {error_msg}")
            release_bead(agent.bead_id, "TIMEOUT", timeout_reason)
            _notify_dispatch_nag(agent, "TIMEOUT", DispatchResult(
                bead_id=agent.bead_id, exit_code=-1, error=error_msg,
                reason=timeout_reason))
            _record_run(agent, DispatchResult(
                bead_id=agent.bead_id, exit_code=-1, error=error_msg,
                reason=timeout_reason),
                effective_status="TIMEOUT")
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
        else:
            # Check JSONL staleness unconditionally (with boot grace period)
            jsonl_file = _find_jsonl_file(lib.output_dir)
            stale = True
            if jsonl_file:
                try:
                    stale_secs = time.time() - jsonl_file.stat().st_mtime
                    if stale_secs > STALE_THRESHOLD_SECS:
                        if not lib._extended and _has_running_tool(jsonl_file):
                            lib._extended = True
                        threshold = (
                            STALE_THRESHOLD_TOOL_SECS if lib._extended
                            else STALE_THRESHOLD_SECS
                        )
                        stale = stale_secs > threshold
                    else:
                        stale = False
                except OSError:
                    pass
            if stale and elapsed > BOOT_GRACE_PERIOD:
                print(f"  Librarian timeout: {lib.job_type}/{lib.job_id[:8]} "
                      f"({elapsed:.0f}s, JSONL stale)")
                timed_out.append(lib)
            elif not stale:
                _collect_live_stats_for_librarian(lib)

    for lib, exit_code in completed:
        running_librarians.remove(lib)
        _deregister_session_with_monitor(
            Path(lib.output_dir).name if lib.output_dir else lib.job_id
        )
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
        _deregister_session_with_monitor(
            Path(lib.output_dir).name if lib.output_dir else lib.job_id
        )
        print(f"  Killing timed-out librarian: {lib.container_name}")
        kill_container(lib.container_name)
        try:
            remove_container(lib.container_name)
            fail_job(lib.job_id)
            _record_librarian_run(lib, -1, "FAILED")
        except Exception as e:
            print(f"  ERROR handling timed-out librarian {lib.job_id[:8]}: {e}")


def _collect_live_stats_for_librarian(lib: RunningLibrarian) -> None:
    """Librarian adapter — see ``_collect_live_stats_for``."""
    _collect_live_stats_for(
        lib,
        register_session=lambda jl: _register_librarian_session(lib, jl),
        fallback_run_id=lib.job_id,
        error_label=f"librarian {lib.job_id[:8]}",
    )


# Agentic live-stats holders, keyed by run_id. Populated lazily inside
# poll_and_collect_agentic when a RUNNING agentic row is first observed,
# popped on completion. Each entry plays the role RunningAgent /
# RunningLibrarian play for the other dispatch kinds — they hold the
# prev_cpu_usec / prev_cpu_poll_time / jsonl_offset state that
# _collect_live_stats_for needs to compute deltas across ticks.
_agentic_holders: dict[str, RunningAgentic] = {}


def _collect_live_stats_for_agentic(holder: RunningAgentic) -> None:
    """Agentic adapter — see ``_collect_live_stats_for``."""
    _collect_live_stats_for(
        holder,
        register_session=lambda jl: _register_agentic_session(
            holder.run_id, holder.output_dir, jl,
        ),
        fallback_run_id=holder.run_id,
        error_label=f"agentic {holder.run_id[:12]}",
    )


# ── Agentic completion watcher ────────────────────────────────────


def _agentic_jsonl_metrics(jsonl_file: Path) -> tuple[str, int, int, str | None]:
    """Pull (last_snippet, turn_count, tool_count, last_activity_iso) from a JSONL.

    Cheap full-file scan — agentic JSONLs cap out around a few MB so this
    is acceptable for a once-per-completion call. Returns empty/zero
    defaults when the file is missing or unparseable so callers can keep
    going.
    """
    if not jsonl_file or not jsonl_file.exists():
        return "", 0, 0, None
    last_snippet = ""
    turn_count = 0
    tool_count = 0
    last_ts: str | None = None
    try:
        with open(jsonl_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = entry.get("timestamp") or ""
                if ts:
                    last_ts = ts
                msg = entry.get("message") or {}
                role = entry.get("type") or msg.get("role") or ""
                content = msg.get("content")
                if role in ("user", "assistant"):
                    turn_count += 1
                    if role == "assistant" and isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                if block.get("type") == "text":
                                    text = (block.get("text") or "")[:400]
                                    if text:
                                        last_snippet = text
                                elif block.get("type") == "tool_use":
                                    tool_count += 1
    except OSError:
        pass
    return last_snippet, turn_count, tool_count, last_ts


def poll_and_collect_agentic() -> None:
    """Finalise dispatch_runs rows for agentic containers that have exited.

    Agentic action launches happen in the dashboard process
    (api_agent_action_dispatch). They write a RUNNING row but never enter
    the dispatcher's in-memory ``running`` list, so the existing
    poll_and_collect path can't see them. This watcher closes the gap:

    1. Query ``dispatch_runs WHERE kind='agentic' AND status='RUNNING'``.
    2. For each row, ``docker inspect`` the container_name to check exit.
    3. On exit, derive last_snippet / turn_count / tool_count /
       last_activity from the JSONL under ``output_dir/sessions/...``,
       then UPSERT the row to status DONE/FAILED via ``insert_run``.

    Runs are keyed by ``dispatch_runs.id`` (the run_id, which equals
    container_name for agentic per Round 5) — never by ``bead_id``,
    which is empty for these rows.
    """
    try:
        conn = _open_dispatch_db()
    except Exception:
        return
    try:
        try:
            rows = conn.execute(
                "SELECT id, container_name, output_dir, started_at, image, "
                "agentic_source_id "
                "FROM dispatch_runs WHERE COALESCE(kind, 'bead') = 'agentic' "
                "AND status = 'RUNNING'"
            ).fetchall()
        except sqlite3.OperationalError:
            return
    finally:
        conn.close()

    if not rows:
        return

    for row in rows:
        run_id = row["id"] if hasattr(row, "keys") else row[0]
        container_name = (row["container_name"] if hasattr(row, "keys") else row[1]) or ""
        output_dir = (row["output_dir"] if hasattr(row, "keys") else row[2]) or ""
        started_at_str = row["started_at"] if hasattr(row, "keys") else row[3]
        image = (row["image"] if hasattr(row, "keys") else row[4]) or ""

        if not container_name:
            continue

        # Compute jsonl_file the same way the metrics path does — used both
        # by the completion-finalize block below and by the live-stats
        # collector via _find_jsonl_file (which globs identically).
        sessions_dir = Path(output_dir) / "sessions" if output_dir else None
        jsonl_files = (
            sorted(sessions_dir.rglob("*.jsonl")) if sessions_dir and sessions_dir.exists() else []
        )
        jsonl_file = jsonl_files[0] if jsonl_files else None

        # Get-or-create the live-stats holder for this run. Same shape as
        # RunningAgent so _collect_live_stats_for treats them identically;
        # cgroup paths need the long Docker ID, so we resolve the name
        # lazily and cache the result on the holder for subsequent ticks.
        holder = _agentic_holders.get(run_id)
        if holder is None:
            container_id = _resolve_container_id(container_name) or ""
            holder = RunningAgentic(
                run_id=run_id,
                container_name=container_name,
                container_id=container_id,
                output_dir=output_dir,
            )
            _agentic_holders[run_id] = holder

        # Run the same live-stats path bead/librarian agents get. This also
        # registers the session with the monitor (idempotently) once the
        # JSONL appears, so we can drop the standalone register-only branch
        # that used to live here — _collect_live_stats_for handles it.
        if holder.container_id:
            _collect_live_stats_for_agentic(holder)
        elif jsonl_file is not None:
            # Fall back to bare registration when we couldn't resolve the
            # container ID (e.g. container already gone). Stats are lost
            # but the monitor still gets its inotify watch, so the
            # live-trace overlay keeps working.
            _register_agentic_session(run_id, output_dir, jsonl_file)

        finished, exit_code = poll_container(container_name)
        if not finished:
            continue

        # Container has exited — drop the live-stats cache entry so we
        # don't leak memory across the dispatcher's lifetime.
        _agentic_holders.pop(run_id, None)

        last_snippet, turn_count, tool_count, last_ts = _agentic_jsonl_metrics(
            jsonl_file
        ) if jsonl_file else ("", 0, 0, None)

        # Convert started_at (string from sqlite) to epoch for insert_run.
        started_epoch = 0.0
        if started_at_str:
            try:
                if isinstance(started_at_str, (int, float)):
                    started_epoch = float(started_at_str)
                else:
                    started_epoch = datetime.strptime(
                        str(started_at_str), "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=timezone.utc).timestamp()
            except (ValueError, TypeError):
                started_epoch = 0.0

        completed_epoch = time.time()

        # Read the agent's decision.json (the authoritative outcome record).
        # Without this, every zero-exit container collapses to DONE/empty —
        # BLOCKED runs and informative reasons are lost in the dispatch_runs
        # row and on the trace page. Mirrors the bead-dispatch convention in
        # ``collect_results`` (decision.get("status") / decision.get("reason"))
        # so the same downstream extractors in ``insert_run`` (scores,
        # time_breakdown, failure_category, discovered_beads) work for
        # agentic runs.
        decision: dict | None = None
        if output_dir:
            decision_path = Path(output_dir) / "decision.json"
            if decision_path.exists():
                try:
                    parsed = json.loads(decision_path.read_text())
                    if isinstance(parsed, dict):
                        decision = parsed
                except (json.JSONDecodeError, OSError):
                    decision = None

        if decision is not None:
            status = str(decision.get("status") or
                         ("DONE" if exit_code == 0 else "FAILED"))
            reason = str(decision.get("reason") or "")
        elif exit_code == 0:
            status = "DONE"
            reason = "No decision file"
        else:
            status = "FAILED"
            reason = f"container exited with code {exit_code}"

        # Agentic decisions already carry the exact worktree result. Persist
        # it instead of replacing it with empty strings so Trace retains the
        # branch/base/commit identity after the container exits. A later
        # Worktrees merge row remains the authoritative immutable diff target.
        decision_commit = ""
        decision_branch = ""
        decision_base = ""
        if decision is not None:
            decision_commit = str(
                decision.get("commit") or decision.get("commit_hash") or ""
            )
            decision_branch = str(decision.get("branch") or "")
            decision_base = str(
                decision.get("base_commit") or decision.get("branch_base") or ""
            )

        try:
            insert_run(
                run_id=run_id,
                bead_id="",
                started_at=started_epoch,
                completed_at=completed_epoch,
                status=status,
                reason=reason,
                decision=decision,
                commit_hash=decision_commit,
                branch=decision_branch,
                branch_base=decision_base,
                image=image,
                container_name=container_name,
                exit_code=exit_code,
                output_dir=output_dir,
                kind="agentic",
                agentic_source_id=row["agentic_source_id"],
            )
        except Exception as e:
            print(
                f"  agentic completion: insert_run failed for {run_id}: {e}",
                file=sys.stderr,
            )
            continue

        _notify_agentic_dispatch_nag(
            run_id, status, reason, row["agentic_source_id"],
        )

        # Best-effort live-stats refresh so the SSE payload stops
        # showing the row as RUNNING before the next dashboard poll.
        try:
            update_live_stats(
                run_id=run_id,
                last_snippet=last_snippet or None,
                last_activity=last_ts,
                turn_delta=max(0, turn_count),
                tool_delta=max(0, tool_count),
            )
        except Exception:
            pass

        try:
            remove_container(container_name)
        except Exception:
            pass
        try:
            cleanup_session_worktrees(run_id, force=True, worktrees_dir=WORKTREES_DIR)
        except Exception as e:
            print(
                f"  agentic completion: workspace cleanup failed for {run_id}: {e}",
                file=sys.stderr,
            )
        # Mark the session dead in the dashboard's tmux_sessions registry.
        # Without this the session stays in a live state forever — the liveness
        # loop now skips ``type='agentic'`` rows (commit d08879c) so it
        # never marks them dead on its own. This is the explicit death
        # signal the skip comment promises ("container-exit collection
        # for agentic runs"). Mirrors the bead-dispatch path's calls to
        # the same helper after collect_results.
        _deregister_session_with_monitor(run_id)

        print(
            f"  Agentic completed: {run_id} (exit={exit_code}, "
            f"turns={turn_count}, tools={tool_count})"
        )


def _open_dispatch_db():
    """Open a SQLite handle on dispatch.db for the agentic watcher.

    Read-only would be ideal but completion writes go through the
    standard dispatch_db helpers; this handle is used only for the
    SELECT step.
    """
    db_path = DATA_ROOT / "dispatch.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


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

    # ── Phase 2b: Poll running agentic-action containers ───────
    # Agentic dispatches are launched out-of-band by the dashboard,
    # so they never enter ``running``. The watcher discovers them by
    # querying ``dispatch_runs WHERE kind='agentic' AND status='RUNNING'``
    # and finalises rows whose containers have exited (auto-gh2iv).
    poll_and_collect_agentic()

    # ── Pause gate: auth failure halts all new launches ────────
    if db_is_paused():
        reason = get_pause_reason() or {}
        print(f"  PAUSED ({reason.get('reason', '?')}): skipping new launches. "
              f"Resume via Dashboard or POST /api/dispatch/resume")
        return 0

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
        project = project_for_bead(bead)
        image = project.image if project is not None else _rig_image
        print(f"  Selected: {bead_id} — {title} (P{bead.get('priority', '?')}) [{image}]")

        if dashboard_paused and "dashboard" in bead_labels:
            print(f"  Skipping {bead_id}: dashboard dispatch paused (smoke test failure)")
            continue

        # Circuit breaker: skip beads with too many consecutive failures
        agent_fails, merge_fails = get_consecutive_failures(bead_id)
        if agent_fails >= 3:
            print(f"  Circuit breaker: {bead_id} has {agent_fails} consecutive "
                  f"agent failures, blocking")
            run_bd(["set-state", bead_id, "readiness=blocked",
                    "--reason", "Circuit breaker: 3 consecutive failures "
                    "— needs human review"])
            continue
        if merge_fails >= 5:
            print(f"  Circuit breaker: {bead_id} has {merge_fails} consecutive "
                  f"merge failures, blocking")
            run_bd(["set-state", bead_id, "readiness=blocked",
                    "--reason", "Circuit breaker: 5 consecutive merge failures "
                    "— needs human review"])
            continue

        if config.dry_run:
            print("  [DRY RUN] Would dispatch this bead")
            continue

        # Launch agent container (blocks until container starts).
        # When the bead's labels match no project (rig default beads),
        # fall back to the rig's owning org slug so the dispatched
        # session's .session_meta.json carries graph_org=autonomy and
        # ingest routes it to the autonomy DB. Without this, the meta
        # ships without a graph_org and downstream ingest passes that
        # cannot resolve a routing target end up filing the session in
        # personal.db (or, in fail-closed mode, skipping it entirely).
        graph_project = (
            project.graph_project if project is not None else "autonomy"
        )
        fallback_workspace = (
            _workspace_for_graph_project(graph_project)
            if project is None else None
        )
        harness = (
            project.harness
            if project is not None
            else (
                fallback_workspace.harness
                if fallback_workspace is not None
                else "claude"
            )
        )
        # Per-bead model override, resolved beside the harness. A ``model:<name>``
        # label wins over the workspace model (launch_session_cli precedence:
        # label > workspace > default). An unknown value fails the dispatch here
        # rather than silently running the default.
        try:
            bead_model = _resolve_bead_model(bead_labels)
        except ValueError as e:
            print(f"  Skipping {bead_id}: {e}", file=sys.stderr)
            run_bd(["set-state", bead_id, "readiness=blocked",
                    "--reason", str(e)])
            continue
        agent = start_agent(
            bead_id,
            image=image,
            harness=harness,
            graph_project=graph_project,
            graph_tags=project.default_tags if project is not None else (),
            workspace_id=(
                project.id if project is not None
                else (fallback_workspace.id if fallback_workspace is not None else None)
            ),
            model=bead_model,
        )
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

    # ── Deferred restart — execute only after all bookkeeping is done ────
    global _restart_scheduled
    if _restart_scheduled:
        _restart_scheduled = False
        print("  Executing scheduled dispatcher restart via os.execv (same PID)...")
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [
            sys.executable, "-u", "-m", "agents.dispatcher",
            "--loop",
            "--interval", str(config.interval),
            "--max-concurrent", str(config.max_concurrent),
        ])

    return dispatched


# ── Recovery ────────────────────────────────────────────────────


def _running_agent_containers() -> set[str]:
    """Return the set of currently-running agent-* container names.

    Used by reconcile to distinguish dead-agent rows from live ones without
    requiring a `docker inspect` per row.
    """
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=agent-",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return set()
        return {ln.strip() for ln in result.stdout.splitlines() if ln.strip()}
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return set()


def _collect_completed_run(row: dict) -> None:
    """Collection rescue path for a RUNNING dispatch_runs row whose container
    is gone but whose decision.json says the agent finished. Idempotent —
    short-circuits if the row is no longer RUNNING (a concurrent collector
    beat us via the dispatch_db status flip).
    """
    run_id = row.get("id") or ""
    bead_id = row.get("bead_id") or ""
    output_dir = row.get("output_dir") or ""
    branch = row.get("branch") or (f"agent/{bead_id}" if bead_id else "")
    image = row.get("image") or ""
    container_name = row.get("container_name") or (
        f"agent-{bead_id}" if bead_id else ""
    )

    branch_base = ""
    if output_dir:
        base_file = Path(output_dir) / ".branch_base"
        if base_file.exists():
            try:
                branch_base = base_file.read_text().strip()
            except OSError:
                pass
    worktree_path = ""
    if output_dir:
        wt_file = Path(output_dir) / ".worktree_path"
        if wt_file.exists():
            try:
                worktree_path = wt_file.read_text().strip()
            except OSError:
                pass

    # Per-run claim: flip RUNNING → COLLECTING atomically. If the UPDATE
    # affects 0 rows, another collector is already on it — bail.
    from agents.dispatch_db import _get_conn
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE dispatch_runs SET status='COLLECTING' "
            "WHERE id=? AND status='RUNNING'",
            (run_id,),
        )
        conn.commit()
        claimed = cur.rowcount > 0
    finally:
        conn.close()
    if not claimed:
        print(f"  reconcile rescue: {run_id} already claimed — skipping")
        return

    started_at = row.get("started_at")
    if isinstance(started_at, (int, float)):
        started_ts = float(started_at)
    elif isinstance(started_at, str) and started_at:
        try:
            started_ts = datetime.fromisoformat(
                started_at.rstrip("Z")
            ).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            started_ts = time.time()
    else:
        started_ts = time.time()

    agent = RunningAgent(
        bead_id=bead_id,
        container_name=container_name,
        container_id="",
        output_dir=output_dir,
        worktree_path=worktree_path,
        branch=branch,
        branch_base=branch_base,
        image=image,
        started_at=started_ts,
    )

    print(f"  reconcile rescue: collecting {run_id} (decision.json present)")
    try:
        result = collect_results(agent, 0)
        effective_status = process_decision(result)
        _record_run(agent, result, effective_status=effective_status)
        _ingest_session(result)
        if effective_status == "DONE":
            try:
                report_path = str(Path(agent.output_dir) / "experience_report.md") if agent.output_dir else ""
                decision_path = str(Path(agent.output_dir) / "decision.json") if agent.output_dir else ""
                payload = json.dumps({
                    "bead_id": agent.bead_id,
                    "report_path": report_path,
                    "decision_path": decision_path,
                    "run_id": run_id,
                })
                enqueue_job("review_report", payload=payload)
            except Exception as eq_err:
                print(
                    f"  WARNING: enqueue review_report failed for {run_id}: {eq_err}",
                    file=sys.stderr,
                )
    except Exception as e:
        # Restore RUNNING so a later pass can retry — don't strand the row.
        restore = _get_conn()
        try:
            restore.execute(
                "UPDATE dispatch_runs SET status='RUNNING' "
                "WHERE id=? AND status='COLLECTING'",
                (run_id,),
            )
            restore.commit()
        finally:
            restore.close()
        print(
            f"  ERROR rescue collect for {run_id}: {type(e).__name__}: {e}",
            file=sys.stderr,
        )


def reconcile_orphaned_runs() -> None:
    """Reconcile RUNNING dispatch_runs against live containers + decision.json.

    For every RUNNING row whose container is no longer in `docker ps`:
      - decision.json status=DONE → run the collection pipeline (rescue)
      - decision.json status=FAILED → record the agent's reason (real failure)
      - no decision.json → mark FAILED 'orphaned: no container at startup'

    Idempotent. Rows with status != 'RUNNING' are skipped — a prior pass that
    already flipped the row to COLLECTING/DONE/FAILED is the authoritative one.
    """
    try:
        running_rows = get_currently_running()
    except Exception as e:
        print(
            f"  WARNING: reconcile_orphaned_runs read failed: {e}",
            file=sys.stderr,
        )
        return
    if not running_rows:
        print("  reconcile_orphaned_runs: no RUNNING rows")
        return

    live_containers = _running_agent_containers()
    from agents.dispatch_db import _get_conn

    for row in running_rows:
        run_id = row.get("id") or ""
        container_name = row.get("container_name") or ""
        output_dir = row.get("output_dir") or ""

        # Live container — leave it alone for poll_and_collect to handle.
        if container_name and container_name in live_containers:
            continue
        # Idempotency guard — only operate on RUNNING rows.
        if (row.get("status") or "").upper() != "RUNNING":
            continue

        decision = None
        if output_dir:
            decision_path = Path(output_dir) / "decision.json"
            if decision_path.exists():
                try:
                    decision = json.loads(decision_path.read_text())
                except (OSError, json.JSONDecodeError):
                    decision = None

        decision_status = (
            (decision.get("status") or "").upper() if isinstance(decision, dict) else ""
        )

        if decision_status == "DONE":
            try:
                _collect_completed_run(row)
            except Exception as e:
                print(
                    f"  WARNING: rescue collect for {run_id} raised: {e}",
                    file=sys.stderr,
                )
            continue

        if decision_status == "FAILED":
            agent_reason = (
                (decision.get("reason") if isinstance(decision, dict) else None)
                or "Agent reported FAILED with no reason"
            )
            conn = _get_conn()
            try:
                conn.execute(
                    "UPDATE dispatch_runs SET status='FAILED', reason=? "
                    "WHERE id=? AND status='RUNNING'",
                    (agent_reason[:500], run_id),
                )
                conn.commit()
            finally:
                conn.close()
            print(
                f"  reconcile: marked {run_id} FAILED from decision.json: "
                f"{agent_reason[:120]}"
            )
            continue

        # No decision.json + no container → legitimate orphan
        conn = _get_conn()
        try:
            conn.execute(
                "UPDATE dispatch_runs SET status='FAILED', "
                "reason='orphaned: no container at startup' "
                "WHERE id=? AND status='RUNNING'",
                (run_id,),
            )
            conn.commit()
        finally:
            conn.close()
        print(f"  reconcile: marked {run_id} FAILED (orphaned, no decision.json)")


def reconcile_stale_monitor_rows() -> None:
    """Sweep stale live-state dispatch/librarian rows in tmux_sessions.

    When a dispatch container exits but the dispatcher's deregister POST
    failed (or the dispatcher was mid-restart), tmux_sessions retains a
    non-terminal row with no matching RUNNING dispatch_runs entry. The session
    monitor's liveness loop (correctly) does not sweep dispatch/librarian
    rows — their lifecycle is dispatcher-owned. This pass closes the gap.

    Idempotent. POSTs /api/monitor/deregister via the existing helper
    (auth-free post commit 8066651 — do not re-add Authorization headers).
    """
    import sqlite3 as _sq

    db_path = os.environ.get("DASHBOARD_DB", str(DATA_ROOT / "dashboard.db"))
    try:
        conn = _sq.connect(db_path)
        conn.row_factory = _sq.Row
        try:
            candidates = conn.execute(
                # Keyed on the one state column (FSM consolidation). The
                # legacy columns are dropped, so the NULL arm (a row this
                # out-of-process reader meets before any backfill stamped
                # it) conservatively counts as live — a spurious deregister
                # POST is idempotent and just marks it ENDED.
                "SELECT tmux_name FROM tmux_sessions "
                "WHERE type IN ('dispatch','librarian')"
                " AND (state NOT IN ('ENDED','FAILED') OR state IS NULL)"
            ).fetchall()
        finally:
            conn.close()
    except _sq.OperationalError as e:
        print(
            f"  WARNING: reconcile_stale_monitor_rows read failed: {e}",
            file=sys.stderr,
        )
        return

    if not candidates:
        print("  reconcile_stale_monitor_rows: no live dispatch/librarian rows")
        return

    try:
        from agents.dispatch_db import _get_conn as _disp_conn
        dconn = _disp_conn()
        try:
            running_run_ids = {
                r[0] for r in dconn.execute(
                    "SELECT id FROM dispatch_runs WHERE status='RUNNING'"
                ).fetchall()
            }
        finally:
            dconn.close()
    except Exception as e:
        print(
            f"  WARNING: reconcile_stale_monitor_rows dispatch read failed: {e}",
            file=sys.stderr,
        )
        return

    for row in candidates:
        tmux_name = row["tmux_name"]
        if tmux_name in running_run_ids:
            continue  # legitimately live — RUNNING run matches
        _monitor_post(
            "/api/monitor/deregister",
            {"tmux_name": tmux_name},
            tmux_name=tmux_name,
        )
        print(f"  reconcile: deregistered stale monitor row {tmux_name}")


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

    # 3. Mark orphaned librarian jobs as failed
    # Librarians are not recovered across restarts, so any 'running' job is orphaned.
    try:
        import sqlite3 as _sqlite3
        from agents.librarian_db import _get_conn as _get_lib_conn
        lib_conn = _get_lib_conn()
        lib_conn.row_factory = _sqlite3.Row
        try:
            stuck_libs = lib_conn.execute(
                "SELECT id, job_type FROM librarian_jobs WHERE status = 'running'"
            ).fetchall()
            for row in stuck_libs:
                print(f"  reconcile: marking librarian job {row['id'][:8]} ({row['job_type']}) "
                      f"as failed (no container at startup)")
                lib_conn.execute(
                    "UPDATE librarian_jobs SET status='failed', "
                    "completed_at=datetime('now') "
                    "WHERE id=? AND status='running'",
                    (row["id"],),
                )
            if stuck_libs:
                lib_conn.commit()
                print(f"  reconcile: cleaned {len(stuck_libs)} orphaned librarian job(s)")
            else:
                print("  reconcile: no orphaned librarian jobs")
        finally:
            lib_conn.close()
    except Exception as e:
        print(f"  WARNING: reconcile librarian jobs failed: {e}", file=sys.stderr)

    # 4. Clean orphaned worktrees with no new commits
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
            runs_dir = DATA_ROOT / "agent-runs"
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
            result = subprocess.run(
                ["git", "worktree", "remove", str(worktree), "--force"],
                capture_output=True, text=True, timeout=15,
                cwd=str(REPO_ROOT),
            )
            if result.returncode == 0:
                logger.info(
                    "workspace cleanup: REMOVED %s  method=git-worktree-remove(reconcile)  "
                    "caller=reconcile_worktrees@dispatcher.py",
                    worktree,
                )
            else:
                logger.warning(
                    "workspace cleanup: reconcile remove FAILED for %s rc=%d err=%s",
                    worktree, result.returncode, (result.stderr or "").strip(),
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
            runs_dir = DATA_ROOT / "agent-runs"
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
    pid_file = DATA_ROOT / "dispatcher.pid"
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

    # Pre-pass: rescue completed runs from decision.json and record real
    # agent failures BEFORE the broad orphan sweep. Without this, a
    # dispatcher restart between agent-completion and collection would lose
    # the agent's verdict and mark it 'orphaned'.
    reconcile_orphaned_runs()
    # Pre-pass: deregister stale live-state dispatch/librarian rows whose
    # dispatch_runs entry is no longer RUNNING.
    reconcile_stale_monitor_rows()
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
