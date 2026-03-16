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
    """Add 'approved' label to a bead, releasing it for dispatch."""
    bead_id = request.path_params["id"]
    stdout, stderr, rc = await run_cli(["bd", "label", "add", bead_id, "approved"])
    if rc != 0:
        return JSONResponse({"error": stderr.strip(), "ok": False}, status_code=400)
    return JSONResponse({"ok": True, "bead_id": bead_id})

async def api_dispatch_status(request):
    """Show currently claimed beads and running agent containers."""
    claimed = await run_cli_json(["bd", "query", "label=work:claimed", "--json"])
    # Get running agent containers
    stdout, _, _ = await run_cli(["docker", "ps", "--filter", "name=agent-", "--format", '{"name":"{{.Names}}","status":"{{.Status}}","image":"{{.Image}}"}'])
    containers = []
    for line in stdout.strip().splitlines():
        if line:
            try:
                containers.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return JSONResponse({"claimed": claimed if isinstance(claimed, list) else [], "containers": containers})

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

    return JSONResponse({
        "run": run_dir.name,
        "bead_id": bead_id,
        "bead": bead,
        "decision": decision,
        "experience_report": experience,
        "commit_hash": commit_hash,
        "branch": branch,
        "diff": diff,
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
    return JSONResponse([
        {"id": name, "alive": True, **_active_terminals.get(name, {"cmd": "?"})}
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


async def api_primer(request):
    bead_id = request.path_params["id"]
    stdout, stderr, rc = await run_cli(["graph", "primer", bead_id])
    return JSONResponse({"content": stdout, "error": stderr if rc != 0 else None})


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
    else:
        # Create a new tmux session
        cmd_str = params.get("cmd", "/bin/bash")
        _term_counter += 1
        if not term_id:
            term_id = f"auto-t{_term_counter}"
        tmux_name = term_id

        # Resolve special container commands
        repo_root = str(Path(__file__).parents[2])
        claude_creds = str(Path.home() / ".claude")
        claude_json = str(Path.home() / ".claude.json")
        if cmd_str == "autonomy-agent-claude":
            cmd_str = (
                f"docker run -it --rm --name {tmux_name}"
                f" -v {claude_creds}:/home/agent/.claude"
                f" -v {claude_json}:/home/agent/.claude.json:ro"
                f" -v {repo_root}/data/graph.db:/data/graph.db:ro"
                f" -v {repo_root}:/repo"
                f" -w /repo"
                f" autonomy-agent"
                f" --dangerously-skip-permissions"
            )
        elif cmd_str == "autonomy-agent-bash":
            cmd_str = (
                f"docker run -it --rm --name {tmux_name}"
                f" --entrypoint /bin/bash"
                f" -v {repo_root}:/repo:ro"
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

    # Track it
    _active_terminals[tmux_name] = {
        "cmd": params.get("cmd", attach or "bash"),
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
    Route("/api/primer/{id}", api_primer),

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
