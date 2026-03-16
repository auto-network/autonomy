"""Autonomy Dashboard — Starlette server.

Thin rendering layer over the bd and graph CLI tools.
Every view the dashboard shows, an agent can also produce via CLI.
"""

import asyncio
import json
import subprocess
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles

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

async def api_search(request):
    q = request.query_params.get("q", "")
    if not q:
        return JSONResponse({"error": "missing q parameter"})
    cmd = ["graph", "search", q, "--limit", request.query_params.get("limit", "20")]
    project = request.query_params.get("project")
    if project:
        cmd += ["--project", project]
    if request.query_params.get("or"):
        cmd += ["--or"]
    # graph search doesn't have --json yet, so we parse the text output
    # For now return raw text; we'll add --json to graph search later
    stdout, stderr, rc = await run_cli(cmd)
    return JSONResponse({"results": stdout, "error": stderr if rc != 0 else None})

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
    stdout, stderr, rc = await run_cli(["graph", "read", source_id, "--max-chars", max_chars])
    return JSONResponse({"content": stdout, "error": stderr if rc != 0 else None})

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


async def api_primer(request):
    bead_id = request.path_params["id"]
    stdout, stderr, rc = await run_cli(["graph", "primer", bead_id])
    return JSONResponse({"content": stdout, "error": stderr if rc != 0 else None})


# ── HTML Pages ────────────────────────────────────────────────

def _load_template(name: str) -> str:
    return (TEMPLATE_DIR / name).read_text()

async def page_index(request):
    return RedirectResponse(url="/beads")

async def page_beads(request):
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
    Route("/sessions", page_sessions),
    Route("/search", page_search),
    Route("/source/{id}", page_source),
    Route("/bead/{id}", page_bead),

    # API
    Route("/api/beads/ready", api_beads_ready),
    Route("/api/beads/list", api_beads_list),
    Route("/api/bead/{id}", api_bead_show),
    Route("/api/bead/{id}/tree", api_bead_tree),
    Route("/api/search", api_search),
    Route("/api/sources", api_sources),
    Route("/api/source/{id}", api_source_read),
    Route("/api/context/{id}/{turn}", api_context),
    Route("/api/projects", api_projects),
    Route("/api/stats", api_stats),
    Route("/api/attention", api_attention),
    Route("/api/active", api_active_sessions),
    Route("/api/primer/{id}", api_primer),

    # Static
    Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
]

app = Starlette(routes=routes)


def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
