"""Autonomy Dashboard — Starlette server.

Thin rendering layer over the bd and graph CLI tools.
Every view the dashboard shows, an agent can also produce via CLI.
"""

import asyncio
import fcntl
import json
import os
import pty
import signal
import struct
import subprocess
import termios
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route, Mount, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"


# ── CLI Subprocess Helper ─────────────────────────────────────

async def run_cli(cmd: list[str], timeout: int = 30) -> tuple[str, str, int]:
    """Run a CLI command async and return (stdout, stderr, returncode)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode(), stderr.decode(), proc.returncode
    except asyncio.TimeoutError:
        proc.kill()
        return "", "timeout", -1


async def run_cli_json(cmd: list[str], timeout: int = 30) -> list | dict:
    """Run CLI command and parse JSON output."""
    stdout, stderr, rc = await run_cli(cmd, timeout)
    if rc != 0 or not stdout.strip():
        return {"error": stderr or "no output", "returncode": rc}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"error": "invalid JSON", "raw": stdout[:500]}


# ── API Endpoints ─────────────────────────────────────────────

async def api_beads_ready(request):
    return JSONResponse(await run_cli_json(["bd", "ready", "--json"]))

async def api_beads_list(request):
    return JSONResponse(await run_cli_json(["bd", "list", "--json", "-n", "100"]))

async def api_bead_show(request):
    bead_id = request.path_params["id"]
    return JSONResponse(await run_cli_json(["bd", "show", bead_id, "--json"]))

async def api_bead_tree(request):
    bead_id = request.path_params["id"]
    return JSONResponse(await run_cli_json(["bd", "dep", "tree", bead_id, "--json"]))

async def api_bead_approve(request):
    """Set readiness=approved on a bead, releasing it for dispatch."""
    bead_id = request.path_params["id"]
    stdout, stderr, rc = await run_cli(["bd", "set-state", bead_id, "readiness=approved",
                                         "--reason", "dashboard: approved for dispatch"])
    if rc != 0:
        return JSONResponse({"error": stderr.strip(), "ok": False}, status_code=400)
    return JSONResponse({"ok": True, "bead_id": bead_id})

async def api_dispatch_status(request):
    """Show dispatched beads with their dispatch state.

    Reads the dispatch dimension (queued/launching/running/collecting/merging/done/failed)
    instead of relying on docker ps for state.
    """
    claimed = await run_cli_json(["bd", "query", "label=work:claimed", "--json"])
    # Also query beads with active dispatch states for richer status
    dispatching = await run_cli_json(["bd", "query", "label=dispatch:running OR label=dispatch:launching OR label=dispatch:collecting OR label=dispatch:merging OR label=dispatch:queued", "--json"])
    # Containers are still useful for runtime info (uptime, image)
    stdout, _, _ = await run_cli(["docker", "ps", "--filter", "name=agent-", "--format", '{"name":"{{.Names}}","status":"{{.Status}}","image":"{{.Image}}"}'])
    containers = []
    for line in stdout.strip().splitlines():
        if line:
            try:
                containers.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return JSONResponse({
        "claimed": claimed if isinstance(claimed, list) else [],
        "dispatching": dispatching if isinstance(dispatching, list) else [],
        "containers": containers,
    })

AGENT_RUNS_DIR = Path(__file__).parent.parent.parent / "data" / "agent-runs"

async def api_dispatch_runs(request):
    """List completed dispatch runs with their artifacts."""
    runs = []
    if AGENT_RUNS_DIR.exists():
        for run_dir in sorted(AGENT_RUNS_DIR.iterdir(), reverse=True):
            if not run_dir.is_dir():
                continue
            # Parse bead ID and timestamp from dir name: <bead>-YYYYMMDD-HHMMSS
            name = run_dir.name
            parts = name.rsplit("-", 2)
            if len(parts) < 3:
                continue
            bead_id = parts[0]
            timestamp = f"{parts[1]}-{parts[2]}"

            decision = None
            decision_path = run_dir / "decision.json"
            if decision_path.exists():
                try:
                    decision = json.loads(decision_path.read_text())
                except json.JSONDecodeError:
                    pass

            has_experience = (run_dir / "experience_report.md").exists()
            commit_hash = ""
            commit_path = run_dir / ".commit_hash"
            if commit_path.exists():
                commit_hash = commit_path.read_text().strip()
            branch = ""
            branch_path = run_dir / ".branch"
            if branch_path.exists():
                branch = branch_path.read_text().strip()

            runs.append({
                "bead_id": bead_id,
                "timestamp": timestamp,
                "dir": run_dir.name,
                "decision": decision,
                "has_experience_report": has_experience,
                "commit_hash": commit_hash,
                "branch": branch,
            })
    return JSONResponse(runs)

async def api_dispatch_trace(request):
    """Full trace for a completed dispatch run."""
    run_name = request.path_params["run"]
    run_dir = AGENT_RUNS_DIR / run_name
    if not run_dir.exists():
        return JSONResponse({"error": "run not found"}, status_code=404)

    # Decision
    decision = None
    decision_path = run_dir / "decision.json"
    if decision_path.exists():
        try:
            decision = json.loads(decision_path.read_text())
        except json.JSONDecodeError:
            pass

    # Experience report
    experience = ""
    exp_path = run_dir / "experience_report.md"
    if exp_path.exists():
        experience = exp_path.read_text()

    # Commit info
    commit_hash = ""
    commit_path = run_dir / ".commit_hash"
    if commit_path.exists():
        commit_hash = commit_path.read_text().strip()
    branch = ""
    branch_path = run_dir / ".branch"
    if branch_path.exists():
        branch = branch_path.read_text().strip()

    # Git diff (if branch still exists)
    diff = ""
    branch_base = ""
    base_path = run_dir / ".branch_base"
    if base_path.exists():
        branch_base = base_path.read_text().strip()
    if commit_hash and branch_base:
        stdout, _, rc = await run_cli(["git", "diff", f"{branch_base}..{commit_hash}"], timeout=10)
        if rc == 0:
            diff = stdout

    # Bead info
    parts = run_dir.name.rsplit("-", 2)
    bead_id = parts[0] if len(parts) >= 3 else run_dir.name
    bead = await run_cli_json(["bd", "show", bead_id, "--json"])

    # Session log availability
    has_session = bool(_find_session_files(run_dir.name))

    return JSONResponse({
        "run": run_dir.name,
        "bead_id": bead_id,
        "bead": bead,
        "decision": decision,
        "experience_report": experience,
        "commit_hash": commit_hash,
        "branch": branch,
        "diff": diff,
        "has_session": has_session,
    })

async def api_search(request):
    q = request.query_params.get("q", "")
    if not q:
        return JSONResponse({"error": "missing q parameter"})
    cmd = ["graph", "search", q, "--json", "--limit", request.query_params.get("limit", "20")]
    project = request.query_params.get("project")
    if project:
        cmd += ["--project", project]
    if request.query_params.get("or"):
        cmd += ["--or"]
    return JSONResponse(await run_cli_json(cmd))

async def api_sources(request):
    cmd = ["graph", "sources"]
    project = request.query_params.get("project")
    if project:
        cmd += ["--project", project]
    stype = request.query_params.get("type")
    if stype:
        cmd += ["--type", stype]
    cmd += ["--limit", request.query_params.get("limit", "30")]
    stdout, stderr, rc = await run_cli(cmd)
    return JSONResponse({"results": stdout, "error": stderr if rc != 0 else None})

async def api_source_read(request):
    source_id = request.path_params["id"]
    max_chars = request.query_params.get("max_chars", "50000")
    return JSONResponse(await run_cli_json(
        ["graph", "read", source_id, "--json", "--max-chars", max_chars, "--first"]
    ))

async def api_context(request):
    source_id = request.path_params["id"]
    turn = request.path_params["turn"]
    window = request.query_params.get("window", "3")
    stdout, stderr, rc = await run_cli(["graph", "context", source_id, turn, "--window", window])
    return JSONResponse({"content": stdout, "error": stderr if rc != 0 else None})

async def api_projects(request):
    stdout, stderr, rc = await run_cli(["graph", "projects"])
    return JSONResponse({"results": stdout, "error": stderr if rc != 0 else None})

async def api_stats(request):
    stdout, stderr, rc = await run_cli(["graph", "stats"])
    return JSONResponse({"results": stdout, "error": stderr if rc != 0 else None})

async def api_attention(request):
    cmd = ["graph", "attention"]
    last = request.query_params.get("last")
    if last:
        cmd += ["--last", last]
    search = request.query_params.get("search")
    if search:
        cmd += ["--search", search]
    stdout, stderr, rc = await run_cli(cmd)
    return JSONResponse({"results": stdout, "error": stderr if rc != 0 else None})

async def api_active_sessions(request):
    """Find currently active Claude Code sessions (JSONL files still being written)."""
    import time
    from pathlib import Path

    threshold = int(request.query_params.get("threshold", "300"))  # seconds
    projects_dir = Path.home() / ".claude" / "projects"
    now = time.time()
    sessions = []

    if projects_dir.exists():
        for jsonl in projects_dir.rglob("*.jsonl"):
            try:
                stat = jsonl.stat()
                age = now - stat.st_mtime
                if age < threshold:
                    # Get last line for latest activity
                    last_line = ""
                    with open(jsonl, "rb") as f:
                        f.seek(max(0, stat.st_size - 2000))
                        last_line = f.read().decode("utf-8", errors="replace")

                    # Extract latest user or assistant text
                    latest = ""
                    import json as _json
                    for line in reversed(last_line.strip().split("\n")):
                        try:
                            e = _json.loads(line)
                            if e.get("type") in ("user", "assistant") and not e.get("isSidechain"):
                                msg = e.get("message", {})
                                content = msg.get("content", "")
                                if isinstance(content, str) and len(content) > 5:
                                    latest = content[:150]
                                    break
                                elif isinstance(content, list):
                                    for c in content:
                                        if isinstance(c, dict) and c.get("type") == "text":
                                            latest = c["text"][:150]
                                            break
                                    if latest:
                                        break
                        except _json.JSONDecodeError:
                            continue

                    sessions.append({
                        "session_id": jsonl.stem,
                        "project": jsonl.parent.name,
                        "size_bytes": stat.st_size,
                        "age_seconds": round(age),
                        "active": age < 60,
                        "latest": latest,
                    })
            except OSError:
                continue

    sessions.sort(key=lambda s: s["age_seconds"])
    return JSONResponse(sessions)


async def api_terminals(request):
    """List active terminal sessions (tmux-backed)."""
    live = _list_dashboard_tmux()
    # Clean up tracking for dead sessions
    dead = [k for k in _active_terminals if k not in live]
    for k in dead:
        del _active_terminals[k]
    # Detect and cache type for any untracked sessions (e.g. after server restart)
    for name in live:
        if name not in _active_terminals:
            info = _detect_terminal_type(name)
            info["started"] = asyncio.get_event_loop().time()
            _active_terminals[name] = info
    return JSONResponse([
        {"id": name, "alive": True, **_active_terminals[name]}
        for name in live
    ])

async def api_terminal_kill(request):
    """Kill a terminal session."""
    name = request.path_params["id"]
    if _tmux_session_exists(name):
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
        _active_terminals.pop(name, None)
        return JSONResponse({"status": "killed", "id": name})
    return JSONResponse({"status": "not_found", "id": name})


async def api_terminal_rename(request):
    """Rename a terminal session's display name."""
    name = request.path_params["id"]
    body = await request.json()
    new_name = body.get("name", "").strip()
    if name in _active_terminals:
        _active_terminals[name]["name"] = new_name if new_name else None
        return JSONResponse({"ok": True, "id": name, "name": new_name})
    return JSONResponse({"error": "not found"}, status_code=404)


async def api_primer(request):
    bead_id = request.path_params["id"]
    stdout, stderr, rc = await run_cli(["graph", "primer", bead_id])
    return JSONResponse({"content": stdout, "error": stderr if rc != 0 else None})


# ── Live Session Tailing ──────────────────────────────────────


def _tool_headline(name: str, inp: dict) -> str:
    """Extract a human-readable headline from a tool_use input dict.

    Returns a short string like ``Read path/to/file.py`` suitable for display
    in the collapsed summary of a tool call.
    """
    n = (name or "").lower()

    if n == "read":
        fp = inp.get("file_path", "")
        return f"`{fp}`" if fp else ""

    if n == "bash":
        cmd = inp.get("command", "")
        if len(cmd) > 60:
            cmd = cmd[:57] + "..."
        return f"`{cmd}`" if cmd else ""

    if n == "write":
        fp = inp.get("file_path", "")
        content = inp.get("content", "")
        n_lines = content.count("\n") + 1 if content else 0
        return f"`{fp}` ({n_lines} lines)" if fp else ""

    if n == "edit":
        fp = inp.get("file_path", "")
        old = inp.get("old_string", "")
        preview = old.replace("\n", " ").strip()
        if len(preview) > 40:
            preview = preview[:37] + "..."
        extra = f' "{preview}"' if preview else ""
        return f"`{fp}`{extra}" if fp else ""

    if n == "grep":
        pat = inp.get("pattern", "")
        path = inp.get("path", "")
        suffix = f" in `{path}`" if path else ""
        return f"`{pat}`{suffix}" if pat else ""

    if n == "glob":
        pat = inp.get("pattern", "")
        return f"`{pat}`" if pat else ""

    if n == "agent":
        desc = inp.get("description", "")
        return desc if desc else ""

    if n == "todowrite":
        todos = inp.get("todos", [])
        return f"{len(todos)} items" if todos else ""

    # Fallback: show first string-valued param, truncated
    for v in inp.values():
        if isinstance(v, str) and v:
            preview = v.replace("\n", " ").strip()
            if len(preview) > 50:
                preview = preview[:47] + "..."
            return preview
    return ""


def _parse_jsonl_entry(line: str) -> dict | None:
    """Parse a single JSONL line into a display entry."""
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None

    entry_type = raw.get("type")
    timestamp = raw.get("timestamp", "")
    is_sidechain = raw.get("isSidechain", False)

    # Skip non-content entries
    if entry_type in ("queue-operation", "progress", "system"):
        return None
    if is_sidechain:
        return None

    message = raw.get("message", {})
    role = message.get("role", entry_type)
    content_raw = message.get("content", "")

    if entry_type == "user":
        text = ""
        if isinstance(content_raw, str):
            text = content_raw
        elif isinstance(content_raw, list):
            for block in content_raw:
                if isinstance(block, dict) and block.get("type") == "text":
                    text += block.get("text", "")
        if not text:
            return None
        return {
            "type": "user",
            "role": "user",
            "content": text[:2000],
            "timestamp": timestamp,
        }

    if entry_type == "assistant" and isinstance(content_raw, list):
        # Expand assistant content blocks into sub-entries
        blocks = []
        for block in content_raw:
            btype = block.get("type", "")
            if btype == "text":
                text = block.get("text", "").strip()
                if text:
                    blocks.append({
                        "type": "assistant_text",
                        "role": "assistant",
                        "content": text,
                        "timestamp": timestamp,
                    })
            elif btype == "tool_use":
                tool_input = block.get("input", {})
                tool_name = block.get("name", "?")
                headline = _tool_headline(tool_name, tool_input)
                # Truncate large inputs for display
                input_str = json.dumps(tool_input, indent=2)
                if len(input_str) > 3000:
                    input_str = input_str[:3000] + "\n... (truncated)"
                blocks.append({
                    "type": "tool_use",
                    "role": "assistant",
                    "tool_name": tool_name,
                    "tool_headline": headline,
                    "tool_id": block.get("id", ""),
                    "content": input_str,
                    "timestamp": timestamp,
                })
            elif btype == "thinking":
                thinking = block.get("thinking", "").strip()
                if thinking:
                    blocks.append({
                        "type": "thinking",
                        "role": "assistant",
                        "content": thinking[:1000],
                        "timestamp": timestamp,
                    })
        return blocks if blocks else None

    if entry_type == "tool_result":
        # Tool results can be large; extract just the summary
        tool_id = raw.get("toolUseId", "")
        result_content = ""
        if isinstance(content_raw, str):
            result_content = content_raw
        elif isinstance(content_raw, list):
            for block in content_raw:
                if isinstance(block, dict) and block.get("type") == "text":
                    result_content += block.get("text", "")
        if len(result_content) > 2000:
            result_content = result_content[:2000] + "\n... (truncated)"
        if not result_content:
            return None
        return {
            "type": "tool_result",
            "role": "tool",
            "tool_id": tool_id,
            "content": result_content,
            "timestamp": timestamp,
        }

    return None


def _find_session_files(run_name: str) -> list[Path]:
    """Find JSONL session files for a run, checking multiple locations."""
    # 1. Run directory sessions — use rglob because Claude Code writes JSONL
    #    into a subdirectory (e.g. sessions/-workspace-repo/<hash>.jsonl)
    run_dir = AGENT_RUNS_DIR / run_name
    sessions_dir = run_dir / "sessions"
    if sessions_dir.exists():
        files = sorted(sessions_dir.rglob("*.jsonl"), key=lambda f: f.stat().st_mtime)
        if files:
            return files

    return []


async def api_dispatch_tail(request):
    """Tail JSONL session data for a dispatch run.

    Returns parsed entries after a byte offset for incremental polling.
    GET /api/dispatch/tail/{run}?after=N
    """
    run_name = request.path_params["run"]
    after = int(request.query_params.get("after", "0"))

    session_files = _find_session_files(run_name)
    if not session_files:
        # Try docker exec fallback for running containers
        entries, new_offset, is_live = await _tail_from_container(run_name, after)
        if entries is not None:
            return JSONResponse({
                "entries": entries,
                "offset": new_offset,
                "is_live": is_live,
            })
        return JSONResponse({"entries": [], "offset": 0, "is_live": False})

    # Read from the largest/most recent session file
    session_file = session_files[-1]
    file_size = session_file.stat().st_size
    is_live = (import_time() - session_file.stat().st_mtime) < 120

    if after >= file_size:
        return JSONResponse({"entries": [], "offset": file_size, "is_live": is_live})

    entries = []
    with open(session_file, "rb") as f:
        f.seek(after)
        data = f.read()
        new_offset = after + len(data)

        text = data.decode("utf-8", errors="replace")
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parsed = _parse_jsonl_entry(line)
            if parsed is None:
                continue
            if isinstance(parsed, list):
                entries.extend(parsed)
            else:
                entries.append(parsed)

    return JSONResponse({
        "entries": entries,
        "offset": new_offset,
        "is_live": is_live,
    })


def import_time():
    """Lazy import of time.time()."""
    import time
    return time.time()


async def _tail_from_container(run_name: str, after: int) -> tuple:
    """Try to tail session data from a running container via docker exec."""
    # Extract bead ID from run name
    parts = run_name.rsplit("-", 2)
    if len(parts) < 3:
        return None, 0, False

    bead_id = parts[0]

    # Find running container for this bead
    stdout, _, rc = await run_cli(
        ["docker", "ps", "--filter", f"name=agent-{bead_id}", "--format", "{{.Names}}"],
        timeout=5,
    )
    if rc != 0 or not stdout.strip():
        return None, 0, False

    container_name = stdout.strip().split("\n")[0]

    # Find session files inside container
    stdout, _, rc = await run_cli(
        ["docker", "exec", container_name, "sh", "-c",
         "ls -t /home/agent/.claude/projects/*/*.jsonl 2>/dev/null | head -1"],
        timeout=5,
    )
    if rc != 0 or not stdout.strip():
        return None, 0, False

    session_path = stdout.strip()

    # Read from offset using tail -c (O(1) seek, unlike dd bs=1 which is O(N))
    if after > 0:
        # tail -c +N starts reading at byte N (1-indexed)
        stdout, _, rc = await run_cli(
            ["docker", "exec", container_name, "sh", "-c",
             f"tail -c +{after + 1} '{session_path}'"],
            timeout=10,
        )
    else:
        stdout, _, rc = await run_cli(
            ["docker", "exec", container_name, "cat", session_path],
            timeout=10,
        )

    if rc != 0:
        return None, 0, False

    entries = []
    new_offset = after + len(stdout.encode("utf-8"))
    for line in stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parsed = _parse_jsonl_entry(line)
        if parsed is None:
            continue
        if isinstance(parsed, list):
            entries.extend(parsed)
        else:
            entries.append(parsed)

    return entries, new_offset, True


async def _latest_from_container(run_name: str) -> dict | None:
    """Get latest assistant text from container using tail (not cat of entire file)."""
    parts = run_name.rsplit("-", 2)
    if len(parts) < 3:
        return None

    bead_id = parts[0]

    stdout, _, rc = await run_cli(
        ["docker", "ps", "--filter", f"name=agent-{bead_id}", "--format", "{{.Names}}"],
        timeout=5,
    )
    if rc != 0 or not stdout.strip():
        return None

    container_name = stdout.strip().split("\n")[0]

    stdout, _, rc = await run_cli(
        ["docker", "exec", container_name, "sh", "-c",
         "ls -t /home/agent/.claude/projects/*/*.jsonl 2>/dev/null | head -1"],
        timeout=5,
    )
    if rc != 0 or not stdout.strip():
        return None

    session_path = stdout.strip()

    # Only read last 4KB — enough to find the latest assistant text
    stdout, _, rc = await run_cli(
        ["docker", "exec", container_name, "sh", "-c",
         f"tail -c 4096 '{session_path}'"],
        timeout=5,
    )
    if rc != 0:
        return None

    for line in reversed(stdout.strip().split("\n")):
        line = line.strip()
        if not line:
            continue
        parsed = _parse_jsonl_entry(line)
        if parsed is None:
            continue
        entries = parsed if isinstance(parsed, list) else [parsed]
        for e in reversed(entries):
            if e.get("type") == "assistant_text":
                return {
                    "text": e["content"][:100],
                    "timestamp": e.get("timestamp", ""),
                    "type": "assistant_text",
                    "is_live": True,
                }

    return None


async def api_dispatch_latest(request):
    """Return just the most recent entry for snippet display.

    GET /api/dispatch/latest/{run}
    """
    run_name = request.path_params["run"]
    session_files = _find_session_files(run_name)

    if not session_files:
        # Try container fallback — use _latest_from_container to avoid
        # catting the entire session file every poll
        result = await _latest_from_container(run_name)
        if result:
            return JSONResponse(result)
        return JSONResponse({"text": "", "timestamp": "", "type": "", "is_live": False})

    session_file = session_files[-1]
    is_live = (import_time() - session_file.stat().st_mtime) < 120

    # Read last ~4KB to find latest assistant text
    file_size = session_file.stat().st_size
    read_from = max(0, file_size - 4096)
    with open(session_file, "rb") as f:
        f.seek(read_from)
        data = f.read().decode("utf-8", errors="replace")

    # Parse lines in reverse to find latest assistant text
    for line in reversed(data.strip().split("\n")):
        line = line.strip()
        if not line:
            continue
        parsed = _parse_jsonl_entry(line)
        if parsed is None:
            continue
        if isinstance(parsed, list):
            for entry in reversed(parsed):
                if entry.get("type") == "assistant_text":
                    return JSONResponse({
                        "text": entry["content"][:100],
                        "timestamp": entry.get("timestamp", ""),
                        "type": "assistant_text",
                        "is_live": is_live,
                    })
        elif parsed.get("type") == "assistant_text":
            return JSONResponse({
                "text": parsed["content"][:100],
                "timestamp": parsed.get("timestamp", ""),
                "type": "assistant_text",
                "is_live": is_live,
            })

    return JSONResponse({"text": "", "timestamp": "", "type": "", "is_live": is_live})


# ── WebSocket Terminal ─────────────────────────────────────────

# Track active terminal sessions
_active_terminals: dict[str, dict] = {}
_term_counter = 0


def _tmux_session_exists(name: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", name],
                          capture_output=True).returncode == 0


def _list_dashboard_tmux() -> list[str]:
    """List all tmux sessions created by the dashboard (prefixed 'auto-')."""
    result = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return [s for s in result.stdout.strip().split("\n") if s.startswith("auto-")]


def _detect_terminal_type(tmux_name: str) -> dict:
    """Detect terminal session type for untracked sessions (e.g. after server restart)."""
    detected_cmd = "/bin/bash"
    detected_env = "host"
    # Check if a docker container with this name is running -> container session
    cr = subprocess.run(
        ["docker", "inspect", "-f", "{{.Path}}", tmux_name],
        capture_output=True, text=True,
    )
    if cr.returncode == 0:
        detected_env = "container"
        entrypoint = cr.stdout.strip()
        if "bash" in entrypoint or entrypoint == "sh":
            detected_cmd = "autonomy-agent-bash"
        else:
            detected_cmd = "autonomy-agent-claude"
    else:
        # Host session -- check tmux pane for claude
        pr = subprocess.run(
            ["tmux", "display-message", "-t", tmux_name, "-p",
             "#{pane_start_command} #{pane_current_command}"],
            capture_output=True, text=True,
        )
        pane_info = pr.stdout.strip().lower() if pr.returncode == 0 else ""
        if "claude" in pane_info:
            detected_cmd = "claude --dangerously-skip-permissions"
    return {"cmd": detected_cmd, "env": detected_env}


async def ws_terminal(websocket: WebSocket):
    """WebSocket endpoint that bridges xterm.js to a tmux session.

    All terminals run inside tmux so they persist across page navigations.
    The WebSocket just attaches/detaches — the process keeps running.

    Query params:
      cmd     — command to run in a new tmux session (default: bash)
      attach  — existing tmux session name to attach to
      id      — terminal session ID (auto-generated if not provided)
    """
    global _term_counter
    await websocket.accept()

    params = websocket.query_params
    attach = params.get("attach")
    term_id = params.get("id")

    if attach:
        # Attach to existing tmux session
        if not _tmux_session_exists(attach):
            await websocket.send_text(f"\r\n\x1b[31mSession '{attach}' not found\x1b[0m\r\n")
            await websocket.close()
            return
        tmux_name = attach
        # Ensure mouse mode is off for reattached sessions too
        subprocess.run(["tmux", "set-option", "-t", tmux_name, "mouse", "off"],
                        capture_output=True)
    else:
        # Create a new tmux session
        cmd_str = params.get("cmd", "/bin/bash")
        _term_counter += 1
        if not term_id:
            term_id = f"auto-t{_term_counter}"
        tmux_name = term_id

        # Resolve special container commands
        repo_root = str(Path(__file__).parents[2])
        claude_creds_file = str(Path.home() / ".claude" / ".credentials.json")
        if cmd_str == "autonomy-agent-claude":
            cmd_str = (
                f"docker run -it --rm --name {tmux_name}"
                f" --network=host"
                f" -v {claude_creds_file}:/home/agent/.claude/.credentials.json:ro"
                f" -v {repo_root}/data/graph.db:/data/graph.db"
                f" -v {repo_root}/.beads:/data/.beads"
                f" -v {repo_root}:/workspace/repo:ro"
                f" -w /workspace/repo"
                f" autonomy-agent"
                f" --dangerously-skip-permissions"
            )
        elif cmd_str == "autonomy-agent-bash":
            cmd_str = (
                f"docker run -it --rm --name {tmux_name}"
                f" --network=host"
                f" --entrypoint /bin/bash"
                f" -v {repo_root}:/workspace/repo:ro"
                f" -v {repo_root}/.beads:/data/.beads"
                f" autonomy-agent"
            )

        # Create detached tmux session running the command
        # Enable set-clipboard so OSC 52 passes through to xterm.js
        subprocess.run([
            "tmux", "new-session", "-d", "-s", tmux_name,
            "-x", "120", "-y", "40",
            cmd_str,
        ], env={**os.environ, "TERM": "xterm-256color"})
        # Enable OSC 52 clipboard passthrough in this session
        subprocess.run(["tmux", "set-option", "-t", tmux_name, "set-clipboard", "on"],
                        capture_output=True)
        subprocess.run(["tmux", "set-option", "-t", tmux_name, "allow-passthrough", "on"],
                        capture_output=True)
        # Disable tmux mouse mode so right-click/middle-click reach the browser
        # instead of being captured by tmux (which shows its own context menu).
        # Paste is handled at the xterm.js layer using the browser clipboard API.
        subprocess.run(["tmux", "set-option", "-t", tmux_name, "mouse", "off"],
                        capture_output=True)

    # Track it — only set cmd/env on initial creation, not re-attach
    if tmux_name not in _active_terminals:
        if attach:
            # Reconnecting to a session we lost track of (server restart)
            info = _detect_terminal_type(tmux_name)
            info["started"] = asyncio.get_event_loop().time()
            _active_terminals[tmux_name] = info
        else:
            orig_cmd = params.get("cmd", "/bin/bash")
            is_container = "autonomy-agent" in orig_cmd
            _active_terminals[tmux_name] = {
                "cmd": orig_cmd,
                "env": "container" if is_container else "host",
                "started": asyncio.get_event_loop().time(),
            }

    # Now attach to the tmux session via a PTY
    master_fd, slave_fd = pty.openpty()
    winsize = struct.pack("HHHH", 40, 120, 0, 0)
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)

    proc = subprocess.Popen(
        ["tmux", "attach-session", "-t", tmux_name],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env={**os.environ, "TERM": "xterm-256color"},
        preexec_fn=os.setsid,
        close_fds=True,
    )
    os.close(slave_fd)

    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    alive = True

    async def read_pty():
        nonlocal alive
        try:
            while alive:
                await asyncio.sleep(0.02)
                try:
                    data = os.read(master_fd, 65536)
                    if data:
                        await websocket.send_text(data.decode("utf-8", errors="replace"))
                except BlockingIOError:
                    continue
                except OSError:
                    break
                if proc.poll() is not None:
                    break
        except Exception:
            pass
        alive = False

    reader_task = asyncio.create_task(read_pty())

    try:
        while alive:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if "text" in msg:
                data = msg["text"]
                if data.startswith("\x1b[8;"):
                    try:
                        parts = data[4:-1].split(";")
                        rows, cols = int(parts[0]), int(parts[1])
                        ws_bytes = struct.pack("HHHH", rows, cols, 0, 0)
                        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, ws_bytes)
                        os.kill(proc.pid, signal.SIGWINCH)
                    except (ValueError, IndexError, OSError):
                        pass
                else:
                    try:
                        os.write(master_fd, data.encode("utf-8"))
                    except OSError:
                        break
            elif "bytes" in msg:
                try:
                    os.write(master_fd, msg["bytes"])
                except OSError:
                    break
    except Exception:
        pass
    finally:
        alive = False
        reader_task.cancel()
        # DON'T kill the tmux session — it persists for reconnection
        # Just detach by killing the attach process
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass


async def page_terminal(request):
    return HTMLResponse(_load_template("base.html"))


# ── HTML Pages ────────────────────────────────────────────────

def _load_template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text()

async def page_index(request):
    return RedirectResponse(url="/beads")

async def page_beads(request):
    return HTMLResponse(_load_template("base.html"))

async def page_dispatch(request):
    return HTMLResponse(_load_template("base.html"))

async def page_sessions(request):
    return HTMLResponse(_load_template("base.html"))

async def page_search(request):
    return HTMLResponse(_load_template("base.html"))

async def page_source(request):
    return HTMLResponse(_load_template("base.html"))

async def page_bead(request):
    return HTMLResponse(_load_template("base.html"))


# ── App ───────────────────────────────────────────────────────

routes = [
    # Pages
    Route("/", page_index),
    Route("/beads", page_beads),
    Route("/dispatch", page_dispatch),
    Route("/sessions", page_sessions),
    Route("/search", page_search),
    Route("/source/{id}", page_source),
    Route("/bead/{id}", page_bead),
    Route("/terminal", page_terminal),
    Route("/terminal/{session_id}", page_terminal),

    # WebSocket
    WebSocketRoute("/ws/terminal", ws_terminal),

    # API
    Route("/api/beads/ready", api_beads_ready),
    Route("/api/beads/list", api_beads_list),
    Route("/api/bead/{id}", api_bead_show),
    Route("/api/bead/{id}/tree", api_bead_tree),
    Route("/api/bead/{id}/approve", api_bead_approve, methods=["POST"]),
    Route("/api/dispatch/status", api_dispatch_status),
    Route("/api/dispatch/runs", api_dispatch_runs),
    Route("/api/dispatch/trace/{run}", api_dispatch_trace),
    Route("/dispatch/trace/{run}", page_dispatch),
    Route("/api/search", api_search),
    Route("/api/sources", api_sources),
    Route("/api/source/{id}", api_source_read),
    Route("/api/context/{id}/{turn}", api_context),
    Route("/api/projects", api_projects),
    Route("/api/stats", api_stats),
    Route("/api/attention", api_attention),
    Route("/api/active", api_active_sessions),
    Route("/api/terminals", api_terminals),
    Route("/api/terminal/{id}/kill", api_terminal_kill),
    Route("/api/terminal/{id}/rename", api_terminal_rename, methods=["POST"]),
    Route("/api/primer/{id}", api_primer),
    Route("/api/dispatch/tail/{run}", api_dispatch_tail),
    Route("/api/dispatch/latest/{run}", api_dispatch_latest),

    # Static
    Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
]

app = Starlette(routes=routes)


def main():
    import uvicorn
    uvicorn.run(
        "tools.dashboard.server:app",
        host="0.0.0.0",
        port=8080,
        log_level="info",
        reload=True,
        reload_dirs=["tools/dashboard"],
    )
