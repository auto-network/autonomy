#!/usr/bin/env python3
"""MCP relay — exposes the Autonomy graph to ChatGPT via OpenAI Secure MCP Tunnel.

A stdlib-only, stateless MCP server (spec 2026-07-28, with legacy 2025-xx
handshake compatibility) that translates tool calls into `graph` CLI
invocations. Binds loopback; reachability from ChatGPT comes solely from an
`openai/tunnel-client` daemon pointed at it.

Peer model: every caller must hold an approved peer token. A new peer
introduces itself with the `hello` tool, the operator approves it once
(`gateway.py approve <name> --scopes read,send`), and every later call passes
the minted token. Scopes gate tool classes: read / write / send.

Usage:
  gateway.py run [--port 8787] [--data-dir DIR]
  gateway.py peers
  gateway.py approve <peer-name> --scopes read,send[,write]
  gateway.py revoke <peer-name>
"""

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROTOCOL_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26"]
SERVER_INFO = {"name": "autonomy-mcp-relay", "version": "0.1.0"}
META_KEY = "io.modelcontextprotocol/serverInfo"
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "mcp_relay"
GRAPH_BIN = os.environ.get("GRAPH_BIN", "graph")
GRAPH_TIMEOUT = int(os.environ.get("GRAPH_TIMEOUT", "45"))

# session names / source ids: no leading dash (argv flag injection), sane charset
IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$")
MAX_MESSAGE_CHARS = 8000
MAX_NOTE_CHARS = 20000

TOOL_SCOPES = {
    "hello": None,
    "search": "read",
    "read": "read",
    "tail": "read",
    "sessions": "read",
    "crosstalk_log": "read",
    "note": "write",
    "crosstalk_send": "send",
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- peer registry

class PeerRegistry:
    """File-backed peer store. Single-process; guarded by a lock for the
    threaded HTTP server."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "peers.json"
        self.lock = threading.Lock()
        data_dir.mkdir(parents=True, exist_ok=True)

    def _load(self):
        if not self.path.exists():
            return {"peers": {}}
        return json.loads(self.path.read_text())

    def _save(self, data):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def hello(self, peer_name: str) -> dict:
        with self.lock:
            data = self._load()
            peer = data["peers"].get(peer_name)
            if peer is None:
                peer = {
                    "name": peer_name,
                    "approved": False,
                    "scopes": [],
                    "token": None,
                    "created_at": now_iso(),
                    "approved_at": None,
                    "last_seen": now_iso(),
                }
                data["peers"][peer_name] = peer
                self._save(data)
                return {"status": "pending", "created": True}
            peer["last_seen"] = now_iso()
            self._save(data)
            if not peer["approved"]:
                return {"status": "pending", "created": False}
            return {
                "status": "approved",
                "peer_token": peer["token"],
                "scopes": peer["scopes"],
            }

    def resolve_token(self, token: str):
        if not token:
            return None
        with self.lock:
            data = self._load()
            for peer in data["peers"].values():
                if peer["approved"] and peer["token"] and secrets.compare_digest(peer["token"], token):
                    peer["last_seen"] = now_iso()
                    self._save(data)
                    return peer
        return None

    def approve(self, peer_name: str, scopes: list) -> dict:
        with self.lock:
            data = self._load()
            peer = data["peers"].get(peer_name)
            if peer is None:
                peer = {"name": peer_name, "created_at": now_iso(), "last_seen": None}
                data["peers"][peer_name] = peer
            peer["approved"] = True
            peer["scopes"] = sorted(set(scopes))
            peer["approved_at"] = now_iso()
            if not peer.get("token"):
                peer["token"] = secrets.token_hex(24)
            self._save(data)
            return peer

    def revoke(self, peer_name: str) -> bool:
        with self.lock:
            data = self._load()
            peer = data["peers"].get(peer_name)
            if peer is None:
                return False
            peer["approved"] = False
            peer["token"] = None
            peer["scopes"] = []
            self._save(data)
            return True

    def list_peers(self) -> list:
        with self.lock:
            return list(self._load()["peers"].values())


# ---------------------------------------------------------------- graph calls

def run_graph(argv: list) -> tuple:
    """Run a graph CLI command. Returns (ok, output)."""
    try:
        proc = subprocess.run(
            [GRAPH_BIN] + argv,
            capture_output=True, text=True, timeout=GRAPH_TIMEOUT,
        )
    except FileNotFoundError:
        return False, f"graph CLI not found ({GRAPH_BIN})"
    except subprocess.TimeoutExpired:
        return False, f"graph command timed out after {GRAPH_TIMEOUT}s"
    if proc.returncode != 0:
        return False, (proc.stderr.strip() or proc.stdout.strip() or "graph command failed")[:2000]
    return True, proc.stdout


def valid_ident(value: str) -> bool:
    return bool(value) and len(value) <= 128 and bool(IDENT_RE.match(value))


# ---------------------------------------------------------------- tool handlers
# Each returns (structured: dict, text: str) or raises ToolError.

class ToolError(Exception):
    pass


def tool_hello(registry, args, peer=None):
    name = str(args.get("peer_name", "")).strip()
    if not name or len(name) > 80 or not IDENT_RE.match(name):
        raise ToolError("peer_name required: 1-80 chars, alphanumeric plus ._:@-")
    result = registry.hello(name)
    if result["status"] == "pending":
        text = (
            f"Peer '{name}' is registered but awaiting operator approval. "
            "The operator must run: gateway.py approve " + name + " --scopes read,send  "
            "Then call hello again to receive your peer_token."
        )
        if result.get("created"):
            _notify_operator(name)
    else:
        text = (
            f"Peer '{name}' approved with scopes {result['scopes']}. "
            "Pass peer_token with every other tool call."
        )
    return result, text


def _notify_operator(peer_name):
    """Best-effort ping so a live session surfaces the pending approval."""
    run_graph(["crosstalk", "broadcast",
               f"MCP relay: new peer '{peer_name}' awaiting approval. "
               f"Run: python3 tools/mcp_relay/gateway.py approve {peer_name} --scopes read,send"])


def tool_search(registry, args, peer):
    query = str(args.get("query", "")).strip()
    if not query:
        raise ToolError("query is required")
    limit = min(int(args.get("limit", 8) or 8), 25)
    # --json emits one row per matching excerpt; fetch extra rows and keep the
    # first excerpt per source so `limit` means distinct sources.
    ok, out = run_graph(["search", query, "--json", "--limit", str(limit * 4)])
    if not ok:
        raise ToolError(out)
    rows = json.loads(out or "[]")
    results, seen = [], set()
    for r in rows:
        sid = r.get("source_id")
        if sid in seen:
            continue
        seen.add(sid)
        results.append({
            "source_id": sid,
            "title": r.get("source_title"),
            "type": r.get("source_type"),
            "excerpt": (r.get("content") or "")[:400],
            "created_at": r.get("source_created_at"),
        })
        if len(results) >= limit:
            break
    text = "\n".join(
        f"[{r['source_id']}] {r['title']} — {r['excerpt'][:120]}" for r in results
    ) or "No results."
    return {"results": results}, text


def tool_read(registry, args, peer):
    source_id = str(args.get("source_id", "")).strip()
    if not valid_ident(source_id):
        raise ToolError("source_id is required (id prefix or tmux session name)")
    max_chars = min(int(args.get("max_chars", 6000) or 6000), 30000)
    ok, out = run_graph(["read", source_id, "--max-chars", str(max_chars)])
    if not ok:
        raise ToolError(out)
    return {"source_id": source_id, "content": out}, out


def tool_tail(registry, args, peer):
    session = str(args.get("session", "")).strip()
    if not valid_ident(session):
        raise ToolError("session is required (tmux name like auto-0808-230541, or source id)")
    turns = min(int(args.get("turns", 10) or 10), 50)
    max_chars = min(int(args.get("max_chars", 1500) or 1500), 10000)
    ok, out = run_graph(["tail", session, str(turns), "--max-chars", str(max_chars)])
    if not ok:
        raise ToolError(out)
    return {"session": session, "content": out}, out


def tool_sessions(registry, args, peer):
    ok, out = run_graph(["sessions", "--status", "--topics"])
    if not ok:
        raise ToolError(out)
    return {"table": out}, out


def tool_crosstalk_log(registry, args, peer):
    since = str(args.get("since", "1h")).strip()
    if not re.match(r"^\d{1,4}[mhd]$", since):
        raise ToolError("since must look like 30m, 2h, or 1d")
    limit = min(int(args.get("limit", 30) or 30), 100)
    ok, out = run_graph(["crosstalk", "--since", since, "--limit", str(limit)])
    if not ok:
        raise ToolError(out)
    return {"log": out}, out


def tool_note(registry, args, peer):
    text = str(args.get("text", "")).strip()
    if not text:
        raise ToolError("text is required")
    if len(text) > MAX_NOTE_CHARS:
        raise ToolError(f"note too long (max {MAX_NOTE_CHARS} chars)")
    argv = ["note", text, "--author", f"chatgpt:{peer['name']}"]
    tags = str(args.get("tags", "")).strip()
    if tags:
        if not re.match(r"^[A-Za-z0-9_,-]+$", tags):
            raise ToolError("tags must be comma-separated slugs")
        argv += ["--tags", f"chatgpt,{tags}"]
    else:
        argv += ["--tags", "chatgpt"]
    ok, out = run_graph(argv)
    if not ok:
        raise ToolError(out)
    return {"result": out.strip()}, out.strip() or "Note created."


def tool_crosstalk_send(registry, args, peer):
    session = str(args.get("session", "")).strip()
    message = str(args.get("message", "")).strip()
    if not valid_ident(session):
        raise ToolError("session is required (tmux name from the sessions tool)")
    if not message:
        raise ToolError("message is required")
    if len(message) > MAX_MESSAGE_CHARS:
        raise ToolError(f"message too long (max {MAX_MESSAGE_CHARS} chars)")
    prefixed = f"[from ChatGPT peer '{peer['name']}' via MCP relay] {message}"
    ok, out = run_graph(["crosstalk", "send", session, prefixed])
    if not ok:
        raise ToolError(out)
    return {"delivered_to": session, "result": out.strip()}, (
        f"Delivered to {session}. The session will see it on its next turn; "
        "poll crosstalk_log or tail to read any reply."
    )


TOOL_HANDLERS = {
    "hello": tool_hello,
    "search": tool_search,
    "read": tool_read,
    "tail": tool_tail,
    "sessions": tool_sessions,
    "crosstalk_log": tool_crosstalk_log,
    "note": tool_note,
    "crosstalk_send": tool_crosstalk_send,
}

_TOKEN_PROP = {
    "peer_token": {
        "type": "string",
        "description": "Your approved peer token from the hello tool.",
    }
}

TOOL_DEFS = [
    {
        "name": "hello",
        "description": (
            "Introduce yourself to the Autonomy relay and obtain your peer_token. "
            "Call this once at the start of a conversation with a stable peer_name "
            "(e.g. 'jeremy-chatgpt'). If the peer is not yet approved, the human "
            "operator must approve it once; call hello again afterwards. Every "
            "other tool requires the returned peer_token."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "peer_name": {
                    "type": "string",
                    "description": "Stable identifier for this ChatGPT peer, e.g. 'jeremy-chatgpt'.",
                }
            },
            "required": ["peer_name"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "search",
        "description": (
            "Full-text search over Jeremy's Autonomy knowledge graph (100K+ thoughts: "
            "sessions, notes, beads, documents). Use when prior research, conversations "
            "or decisions may be relevant. Single high-signal terms work best."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_TOKEN_PROP,
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 8},
            },
            "required": ["peer_token", "query"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "read",
        "description": "Read the full content of a graph source found via search (by source_id).",
        "inputSchema": {
            "type": "object",
            "properties": {
                **_TOKEN_PROP,
                "source_id": {"type": "string"},
                "max_chars": {"type": "integer", "default": 6000, "maximum": 30000},
            },
            "required": ["peer_token", "source_id"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "tail",
        "description": (
            "Read the latest turns of a live Autonomy agent session. Use after "
            "crosstalk_send to read the session's reply."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_TOKEN_PROP,
                "session": {"type": "string", "description": "tmux name, e.g. auto-0808-230541"},
                "turns": {"type": "integer", "default": 10, "maximum": 50},
                "max_chars": {"type": "integer", "default": 1500, "maximum": 10000},
            },
            "required": ["peer_token", "session"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "sessions",
        "description": "List live Autonomy agent sessions (name, state, working label, topics).",
        "inputSchema": {
            "type": "object",
            "properties": {**_TOKEN_PROP},
            "required": ["peer_token"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "crosstalk_log",
        "description": (
            "Read recent CrossTalk messages between Autonomy sessions — your mailbox. "
            "Check this at the start of a turn to pick up messages sent to you since "
            "the user last spoke."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_TOKEN_PROP,
                "since": {"type": "string", "default": "1h", "description": "e.g. 30m, 2h, 1d"},
                "limit": {"type": "integer", "default": 30, "maximum": 100},
            },
            "required": ["peer_token"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "note",
        "description": (
            "Write a permanent note into the Autonomy knowledge graph (tagged as "
            "coming from ChatGPT). Use for conclusions, decisions or insights worth "
            "keeping across sessions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_TOKEN_PROP,
                "text": {"type": "string"},
                "tags": {"type": "string", "description": "optional comma-separated slugs"},
            },
            "required": ["peer_token", "text"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "crosstalk_send",
        "description": (
            "Send a message to a live Autonomy agent session (peer-to-peer agent "
            "communication). The session sees it as a CrossTalk message on its next "
            "turn. Combine with tail to converse: send, wait, tail."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_TOKEN_PROP,
                "session": {"type": "string", "description": "tmux name from the sessions tool"},
                "message": {"type": "string"},
            },
            "required": ["peer_token", "session", "message"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
]


# ---------------------------------------------------------------- MCP plumbing

def jsonrpc_error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def jsonrpc_result(msg_id, result):
    result.setdefault("resultType", "complete")
    result.setdefault("_meta", {})[META_KEY] = SERVER_INFO
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def tool_call_result(msg_id, structured, text, is_error=False):
    return jsonrpc_result(msg_id, {
        "content": [{"type": "text", "text": text}],
        "structuredContent": structured,
        "isError": is_error,
    })


class RelayState:
    def __init__(self, data_dir: Path):
        self.registry = PeerRegistry(data_dir)
        self.log_path = data_dir / "relay.jsonl"
        self.log_lock = threading.Lock()

    def log(self, record: dict):
        record["ts"] = now_iso()
        with self.log_lock:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(record) + "\n")


def handle_message(state: RelayState, msg: dict, headers) -> dict | None:
    """Handle one JSON-RPC message. Returns response dict, or None for
    notifications (no id)."""
    method = msg.get("method")
    msg_id = msg.get("id")
    is_notification = "id" not in msg

    # header/body mismatch detection (2026-07-28 requires mirrored headers;
    # absent headers are tolerated for older clients)
    hdr_method = headers.get("Mcp-Method")
    if hdr_method and hdr_method != method:
        return None if is_notification else jsonrpc_error(
            msg_id, -32020, f"Mcp-Method header '{hdr_method}' does not match body method '{method}'")

    if is_notification:
        return None

    if method == "server/discover":
        return jsonrpc_result(msg_id, {
            "protocolVersions": PROTOCOL_VERSIONS,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        })

    if method == "initialize":  # legacy pre-2026 handshake
        client_version = (msg.get("params") or {}).get("protocolVersion", "2025-06-18")
        version = client_version if client_version in PROTOCOL_VERSIONS else "2025-06-18"
        return jsonrpc_result(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        })

    if method == "ping":  # legacy
        return jsonrpc_result(msg_id, {})

    if method == "tools/list":
        return jsonrpc_result(msg_id, {
            "tools": TOOL_DEFS,
            "ttlMs": 300000,
            "cacheScope": "private",
        })

    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        hdr_name = headers.get("Mcp-Name")
        if hdr_name and hdr_name != name:
            return jsonrpc_error(
                msg_id, -32020, f"Mcp-Name header '{hdr_name}' does not match tool '{name}'")
        args = params.get("arguments") or {}
        if name not in TOOL_HANDLERS:
            return jsonrpc_error(msg_id, -32602, f"Unknown tool: {name}")

        scope = TOOL_SCOPES[name]
        peer = None
        if scope is not None:
            peer = state.registry.resolve_token(str(args.get("peer_token", "")))
            if peer is None:
                state.log({"tool": name, "peer": None, "ok": False, "err": "auth"})
                return tool_call_result(
                    msg_id, {"error": "not_authorized"},
                    "No valid peer_token. Call the hello tool first to register and "
                    "obtain a token (the operator must approve new peers once).",
                    is_error=True)
            if scope not in peer["scopes"]:
                state.log({"tool": name, "peer": peer["name"], "ok": False, "err": "scope"})
                return tool_call_result(
                    msg_id, {"error": "scope_denied", "required_scope": scope,
                             "granted_scopes": peer["scopes"]},
                    f"Peer '{peer['name']}' lacks the '{scope}' scope required by {name}. "
                    "The operator can extend scopes with the approve command.",
                    is_error=True)
        try:
            structured, text = TOOL_HANDLERS[name](state.registry, args, peer)
            state.log({"tool": name, "peer": peer["name"] if peer else args.get("peer_name"),
                       "ok": True})
            return tool_call_result(msg_id, structured, text)
        except ToolError as e:
            state.log({"tool": name, "peer": peer["name"] if peer else None,
                       "ok": False, "err": str(e)[:200]})
            return tool_call_result(msg_id, {"error": str(e)}, str(e), is_error=True)
        except Exception as e:  # defensive: never 500 the tunnel
            state.log({"tool": name, "ok": False, "err": f"internal: {e}"})
            return tool_call_result(msg_id, {"error": "internal"},
                                    f"Internal relay error: {e}", is_error=True)

    return jsonrpc_error(msg_id, -32601, f"Method not found: {method}")


class RelayHandler(BaseHTTPRequestHandler):
    state: RelayState = None  # set by serve()

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/healthz", "/readyz"):
            self._send_json(200, {"ok": True, "server": SERVER_INFO})
        else:
            # 404 (not 405): tunnel-client probes for OAuth protected-resource
            # metadata and treats 404-on-all-candidates as "plain MCP server,
            # ready"; other statuses keep readiness degraded.
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 512 * 1024:
                self._send_json(413, {"error": "request too large"})
                return
            raw = self.rfile.read(length)
            msg = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, jsonrpc_error(None, -32700, "Parse error"))
            return
        if not isinstance(msg, dict):
            self._send_json(400, jsonrpc_error(None, -32600, "Batch requests not supported"))
            return
        response = handle_message(self.state, msg, self.headers)
        if response is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send_json(200, response)


# ---------------------------------------------------------------- CLI

def cmd_run(args):
    data_dir = Path(args.data_dir)
    RelayHandler.state = RelayState(data_dir)
    server = ThreadingHTTPServer((args.host, args.port), RelayHandler)
    print(f"autonomy-mcp-relay listening on http://{args.host}:{args.port}/mcp")
    print(f"peer registry: {data_dir / 'peers.json'}")
    print(f"call log:      {data_dir / 'relay.jsonl'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_peers(args):
    registry = PeerRegistry(Path(args.data_dir))
    peers = registry.list_peers()
    if not peers:
        print("No peers registered.")
        return
    for p in peers:
        state = "APPROVED" if p.get("approved") else "pending "
        scopes = ",".join(p.get("scopes") or []) or "-"
        print(f"{state}  {p['name']:<30} scopes={scopes:<16} "
              f"created={p.get('created_at', '-')} last_seen={p.get('last_seen', '-')}")


def cmd_approve(args):
    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
    bad = set(scopes) - {"read", "write", "send"}
    if bad or not scopes:
        print(f"scopes must be a comma list from: read, write, send (got: {args.scopes})")
        sys.exit(1)
    registry = PeerRegistry(Path(args.data_dir))
    peer = registry.approve(args.peer, scopes)
    print(f"Approved '{peer['name']}' with scopes {peer['scopes']}.")
    print(f"Token (also returned to the peer via hello): {peer['token']}")


def cmd_revoke(args):
    registry = PeerRegistry(Path(args.data_dir))
    if registry.revoke(args.peer):
        print(f"Revoked '{args.peer}'. Its token is dead as of the next call.")
    else:
        print(f"No such peer: {args.peer}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR),
                        help=f"peer registry + log location (default {DEFAULT_DATA_DIR})")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="run the relay server")
    p_run.add_argument("--host", default="127.0.0.1")
    p_run.add_argument("--port", type=int, default=8787)
    p_run.set_defaults(func=cmd_run)

    p_peers = sub.add_parser("peers", help="list peers")
    p_peers.set_defaults(func=cmd_peers)

    p_approve = sub.add_parser("approve", help="approve a peer (one-time)")
    p_approve.add_argument("peer")
    p_approve.add_argument("--scopes", required=True, help="comma list: read,write,send")
    p_approve.set_defaults(func=cmd_approve)

    p_revoke = sub.add_parser("revoke", help="revoke a peer's token and scopes")
    p_revoke.add_argument("peer")
    p_revoke.set_defaults(func=cmd_revoke)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
