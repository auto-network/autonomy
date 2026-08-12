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
import copy
import hashlib
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
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


# ---------------------------------------------------------------- caller identity
# Auth binds to identifiers OpenAI's tunnel stamps on every request — NOT to
# anything the model types. Verified on the wire 2026-08-09 (design eeb23208-257):
# openai/session is per-chat and turn-stable; openai/subject is the user. These
# are trustworthy ONLY when the request arrives via OpenAI's tunnel, proven by the
# tunnel-service mTLS client cert (X-Forwarded-Client-Cert spiffe id). With the
# relay bound to loopback and fronted only by tunnel-client, that holds; set
# MCP_RELAY_REQUIRE_TUNNEL_CERT=0 only for local testing.
REQUIRE_TUNNEL_CERT = os.environ.get(
    "MCP_RELAY_REQUIRE_TUNNEL_CERT", "1") not in ("0", "false", "no", "")
_TUNNEL_SPIFFE_MARK = "/ns/tunnel-service/"

# STDIO transport: the relay runs as the tunnel-client's own child process and
# speaks JSON-RPC over stdin/stdout — there is NO TCP port to reach, so the
# process boundary (only the parent tunnel-client can write to our stdin) IS the
# trust boundary. This is the secure deployment: it closes the forgeable-header /
# shared-loopback hole that HTTP mode has in a host-networked estate, because an
# X-Forwarded-Client-Cert header on a loopback socket is not a boundary. Set by
# run_stdio(); when true we do not need (and do not read) HTTP identity headers.
STDIO_TRANSPORT = False


def extract_identity(headers, params) -> dict:
    """Pull OpenAI's tunnel-stamped identity from a request.

    Returns {openai_session, openai_subject, openai_org, tunnel_verified}.
    `_meta` is canonical; HTTP headers are the fallback. `tunnel_verified` is
    True only when the tunnel-service mTLS client cert is present — callers must
    refuse to trust the identity otherwise (unless REQUIRE_TUNNEL_CERT is off)."""
    meta = (params or {}).get("_meta") or {}
    xfcc = headers.get("X-Forwarded-Client-Cert") or ""
    return {
        "openai_session": meta.get("openai/session") or headers.get("X-Openai-Session"),
        "openai_subject": meta.get("openai/subject") or headers.get("X-Openai-Subject"),
        "openai_org": meta.get("openai/organization"),
        "tunnel_verified": _TUNNEL_SPIFFE_MARK in xfcc and "openai" in xfcc.lower(),
    }


def identity_trusted(identity: dict) -> bool:
    """Whether we can bind auth to this identity. Requires a session id, and then
    a trustworthy transport: STDIO (the process boundary — only the parent
    tunnel-client can reach us) is authoritative; over HTTP we fall back to the
    (weaker) tunnel mTLS header unless testing has disabled the requirement.

    NOTE: the HTTP path's header check is not a real boundary in a host-networked
    estate; STDIO is the supported secure deployment. See STDIO_TRANSPORT."""
    if not identity.get("openai_session"):
        return False
    if STDIO_TRANSPORT:
        return True
    return identity.get("tunnel_verified", False) or not REQUIRE_TUNNEL_CERT


# ---------------------------------------------------------------- authz mode
# Two modes. DASHBOARD (secure): auth binds to openai/session, the dashboard owns
# approval/org/audit, and the relay authorizes every call over an internal
# service token — engaged automatically when both env vars below are set. REGISTRY
# (legacy): the file-backed peer registry + peer_token. Default is REGISTRY so an
# unconfigured relay keeps working; production runs DASHBOARD host-side.
DASHBOARD_URL = os.environ.get("MCP_RELAY_DASHBOARD_URL", "").rstrip("/")
SERVICE_TOKEN = os.environ.get("MCP_RELAY_SERVICE_TOKEN", "")
DASHBOARD_MODE = bool(DASHBOARD_URL and SERVICE_TOKEN)

_TLS_UNVERIFIED = ssl.create_default_context()
_TLS_UNVERIFIED.check_hostname = False
_TLS_UNVERIFIED.verify_mode = ssl.CERT_NONE

# per-request Autonomy org for run_graph scoping (ThreadingHTTPServer → one
# request per thread, so a threadlocal is safe).
_req_ctx = threading.local()


def dashboard_post(path: str, body: dict, timeout: int = 15):
    """POST to the dashboard's internal API with the service token. Returns the
    decoded JSON dict, or None on any transport/HTTP error (fail-closed at the
    call site)."""
    url = DASHBOARD_URL + path
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {SERVICE_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_TLS_UNVERIFIED) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except Exception:
        return None


def authorize(identity: dict, tool: str, args: dict, *, poster=dashboard_post) -> dict:
    """Dashboard-mode per-request authorization. Resolves the session binding and
    enforces level + per-target crosstalk grants. `poster` is injectable for
    tests. Returns {allowed, org?, level?, status?, reason?}."""
    if not identity_trusted(identity):
        return {"allowed": False, "reason": "no trusted OpenAI session identity"}
    osession = identity["openai_session"]
    # Per-request check uses the NON-popping status endpoint — authorizing a tool
    # call must never open an approval popup (only hello does that).
    resolved = poster("/api/mcp/session/status", {"openai_session": osession})
    if resolved is None:
        return {"allowed": False, "reason": "dashboard unreachable"}
    if resolved.get("status") != "approved":
        return {"allowed": False, "status": resolved.get("status"),
                "reason": f"session {resolved.get('status')}"}
    org, level = resolved.get("autonomy_org"), resolved.get("level")
    scope = TOOL_SCOPES.get(tool)
    if scope == "write" and level != "readwrite":
        return {"allowed": False, "reason": "peer is read-only in this org"}
    # crosstalk_send does NOT pass through here — it is dispatched to the
    # dashboard's /api/mcp/crosstalk/relay endpoint, which enforces the per-target
    # grant itself. authorize() gates only the read/write graph tools.
    return {"allowed": True, "org": org, "level": level}


def peer_label(identity: dict) -> str:
    """Display/attribution id for a dashboard-mode caller (not an auth input)."""
    return "chatgpt:" + sess_tag(identity)


def sess_tag(identity: dict) -> str:
    """A non-reversible correlation tag for logs/attribution — NEVER the raw
    openai/session, which is bearer-equivalent in dashboard mode."""
    sess = identity.get("openai_session") or ""
    return hashlib.sha256(sess.encode()).hexdigest()[:12] if sess else "none"


def hello_dashboard(identity: dict, args: dict, *, poster=dashboard_post) -> dict:
    """hello in dashboard mode: resolve/create the session binding (opening the
    operator popup when needed) and report status to ChatGPT."""
    if not identity_trusted(identity):
        return {"structured": {"status": "untrusted"}, "is_error": True,
                "text": "Could not establish a trusted OpenAI session identity — "
                        "this connector must be reached through OpenAI's tunnel."}
    if not str(args.get("intent") or "").strip():
        return {"structured": {"status": "needs_intent"}, "is_error": True,
                "text": "Call hello again WITH an 'intent' — one specific sentence on what you "
                        "want to do and why. The operator can't approve access without a reason."}
    resolved = poster("/api/mcp/session/resolve", {
        "openai_session": identity["openai_session"],
        "openai_subject": identity.get("openai_subject") or "",
        "openai_org": identity.get("openai_org") or "",
        "intent": str(args.get("intent") or ""),
    })
    if resolved is None:
        return {"structured": {"status": "error"}, "is_error": True,
                "text": "The Autonomy dashboard is unreachable; try again shortly."}
    status = resolved.get("status")
    if status == "approved":
        text = (f"Approved for org '{resolved.get('autonomy_org')}' "
                f"({resolved.get('level')}). The tools are usable now.")
    elif status == "pending":
        text = ("Access requested. Approve this chat in the Autonomy dashboard "
                "(an approval popup is waiting), then call hello again.")
    elif status == "denied":
        text = "This chat's access request was declined by the operator."
    else:
        text = f"Session status: {status}."
    return {"structured": resolved, "text": text,
            "is_error": status not in ("approved", "pending")}


def authz_denial_text(tool: str, authz: dict) -> str:
    status = authz.get("status")
    if status == "pending":
        return ("Access is pending operator approval in the Autonomy dashboard. "
                "Retry after it's approved.")
    if status == "peer_not_linked":
        return "This chat isn't linked yet — call hello and get approved first."
    return f"Not authorized: {authz.get('reason', 'denied')}. Call hello to (re)request access."


def crosstalk_send_dashboard(identity: dict, args: dict, state, *, poster=dashboard_post) -> dict:
    """Forward a crosstalk_send to the dashboard's single enforce-and-deliver
    endpoint. The dashboard authorizes (link + per-target grant) and delivers,
    stamping the source from this chat's minted handle — the relay never delivers
    and never sets the source. The relay does not hold: a message that needs
    approval is stored on the approval and delivered when the operator approves,
    so the model does NOT resend."""
    if not identity_trusted(identity):
        return {"structured": {"status": "untrusted"}, "is_error": True,
                "text": "No trusted OpenAI session identity."}
    target = str(args.get("session") or "").strip()
    message = str(args.get("message") or "").strip()
    if not valid_ident(target):
        return {"structured": {"error": "bad_target"}, "is_error": True,
                "text": "A valid target 'session' is required."}
    if not message:
        return {"structured": {"error": "empty_message"}, "is_error": True,
                "text": "A non-empty 'message' is required."}
    if not str(args.get("intent") or "").strip():
        return {"structured": {"error": "empty_intent"}, "is_error": True,
                "text": "Call crosstalk_send again WITH an 'intent' — one specific "
                        "sentence stating why you are messaging this session. The "
                        "operator sees it on the approval prompt and cannot answer "
                        "'should this chat message my agent?' without it."}
    res = poster("/api/mcp/crosstalk/relay", {
        "openai_session": identity["openai_session"], "target_session": target,
        "message": message, "intent": str(args.get("intent") or "")})
    if res is None:
        return {"structured": {"status": "error"}, "is_error": True,
                "text": "The Autonomy dashboard is unreachable; try again shortly."}
    status = res.get("status")
    if status == "delivered":
        state.log({"tool": "crosstalk_send", "sess": sess_tag(identity), "ok": True})
        return {"structured": {"status": "delivered", "from": res.get("from")},
                "text": f"Delivered to {target}.", "is_error": False}
    if status == "peer_not_linked":
        return {"structured": {"status": "peer_not_linked"}, "is_error": True,
                "text": "This chat isn't linked to an org yet. Call hello to request "
                        "access first; that's a separate approval from sending a message."}
    if status == "denied":
        return {"structured": {"status": "denied"}, "is_error": True,
                "text": "The operator declined to allow this message. It was not sent."}
    # pending: queued on the approval, delivered when the operator approves.
    state.log({"tool": "crosstalk_send", "sess": sess_tag(identity), "status": "pending"})
    return {"structured": {"status": "pending", "from": res.get("from")}, "is_error": False,
            "text": "Held for the operator's approval. A prompt is open on their "
                    "dashboard showing this exact message; it will be delivered as soon "
                    "as they approve. Do NOT resend — it is already queued."}


def crosstalk_inbox_dashboard(identity: dict, args: dict, state, *, poster=dashboard_post) -> dict:
    """Drain this chat's own inbox — the replies queued for its handle. The
    dashboard maps the chat's session to its handle and returns only that handle's
    messages, marking them collected (a repeat call returns nothing new)."""
    if not identity_trusted(identity):
        return {"structured": {"status": "untrusted"}, "is_error": True,
                "text": "No trusted OpenAI session identity."}
    res = poster("/api/mcp/crosstalk/collect",
                 {"openai_session": identity["openai_session"]})
    if res is None:
        return {"structured": {"status": "error"}, "is_error": True,
                "text": "The Autonomy dashboard is unreachable; try again shortly."}
    msgs = res.get("messages") or []
    state.log({"tool": "crosstalk_log", "sess": sess_tag(identity), "n": len(msgs)})
    if not msgs:
        return {"structured": {"messages": []}, "is_error": False,
                "text": "No new messages in your inbox."}
    lines = [f'{m.get("label") or m.get("from")}: {m.get("message")}' for m in msgs]
    return {"structured": {"messages": msgs}, "is_error": False, "text": "\n".join(lines)}


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
    """Run a graph CLI command. Returns (ok, output). In dashboard mode the
    request's bound org (threadlocal) is applied via GRAPH_ORG so every call is
    scoped to exactly the org the peer was approved for."""
    org = getattr(_req_ctx, "org", None)
    env = {**os.environ, "GRAPH_ORG": org} if org else None
    try:
        proc = subprocess.run(
            [GRAPH_BIN] + argv,
            capture_output=True, text=True, timeout=GRAPH_TIMEOUT, env=env,
        )
    except FileNotFoundError:
        print(f"graph CLI not found ({GRAPH_BIN}) argv={argv} PATH={os.environ.get('PATH','')}", file=sys.stderr, flush=True)
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
    else:
        text = (
            f"Peer '{name}' approved with scopes {result['scopes']}. "
            "Pass peer_token with every other tool call."
        )
    return result, text


# Pending peers are surfaced by polling `gateway.py peers` (and, later, a
# dashboard approval view). The relay does NOT push any CrossTalk — a broadcast
# per hello would inject into every live session's context and burn fleet-wide
# tokens for a purely local event.


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
            "Request access to Autonomy for this chat. State your intent in plain "
            "language; the human operator approves this specific chat in the "
            "Autonomy dashboard and chooses which of THEIR orgs to grant, at "
            "read-only or read/write. You never choose or name an org — that is "
            "the operator's decision. After calling hello, just try the tools: "
            "they work once approved (and return a clear 'pending approval' "
            "message before that). Call hello AGAIN only when you need different "
            "or additional access (e.g. a tool was denied) — each call asks the "
            "operator to approve. Your identity is established automatically; no "
            "name or token is needed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": "REQUIRED. What you want to do and why, in plain language — this "
                                   "is the ONLY thing that tells the operator why they're being asked "
                                   "to approve, so it must be specific and non-empty.",
                },
                "peer_name": {
                    "type": "string",
                    "description": "Legacy only (registry mode); ignored when the dashboard authorizes.",
                },
            },
            "required": ["intent"],
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
                "intent": {
                    "type": "string",
                    "description": (
                        "One specific sentence stating why you are messaging this "
                        "session. Shown to the operator on the approval prompt; the "
                        "message is not delivered until they approve it."
                    ),
                },
            },
            "required": ["peer_token", "session", "message", "intent"],
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
]


# Registry-mode arguments that carry no meaning in dashboard mode: identity is the
# tunnel-stamped openai/session, not a self-asserted token or name. peer_name in
# particular was the original skeleton-key field (every chat asserted the same one)
# — never advertise it to a dashboard-mode client.
_REGISTRY_ONLY_ARGS = ("peer_token", "peer_name")


def _advertised_tools() -> list:
    """The tool list ChatGPT caches at connector-create time. In dashboard mode a
    chat is identified by the tunnel-stamped openai/session, so the registry-mode
    auth/name arguments are dead weight — strip them from every schema so the model
    is never told to send a field the dashboard ignores."""
    if not DASHBOARD_MODE:
        return TOOL_DEFS
    out = []
    for tool in TOOL_DEFS:
        tool = copy.deepcopy(tool)
        schema = tool.get("inputSchema") or {}
        props = schema.get("properties", {})
        for arg in _REGISTRY_ONLY_ARGS:
            props.pop(arg, None)
        if schema.get("required"):
            schema["required"] = [r for r in schema["required"]
                                  if r not in _REGISTRY_ONLY_ARGS]
        out.append(tool)
    return out


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
        # DiscoverResult (2026-07-28) requires: supportedVersions, capabilities,
        # cacheScope, resultType, ttlMs. Field is supportedVersions (NOT
        # protocolVersions); missing cacheScope/ttlMs makes strict clients
        # (e.g. ChatGPT connector create) reject the whole server.
        return jsonrpc_result(msg_id, {
            "supportedVersions": PROTOCOL_VERSIONS,
            "capabilities": {"tools": {"listChanged": False}},
            "cacheScope": "private",
            "ttlMs": 300000,
            "instructions": (
                "Autonomy knowledge-graph relay. Use it to search and read "
                "Jeremy's Autonomy graph, tail live agent sessions, send "
                "CrossTalk messages to sessions, and write notes."
            ),
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
            "tools": _advertised_tools(),
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

        # --- DASHBOARD mode: identity = openai/session, authz + org via dashboard
        if DASHBOARD_MODE:
            identity = extract_identity(headers, params)
            if name == "hello":
                res = hello_dashboard(identity, args)
                state.log({"tool": "hello", "sess": sess_tag(identity),
                           "status": (res.get("structured") or {}).get("status")})
                return tool_call_result(msg_id, res["structured"], res["text"],
                                        is_error=res.get("is_error", False))
            if name == "crosstalk_send":
                # Forward to the dashboard's enforce-and-deliver endpoint; the
                # dashboard authorizes, delivers, and stamps the source. Not the
                # generic authorize() path.
                res = crosstalk_send_dashboard(identity, args, state)
                return tool_call_result(msg_id, res["structured"], res["text"],
                                        is_error=res.get("is_error", False))
            if name == "crosstalk_log":
                # A chat's mailbox is its OWN queued replies (drain + mark), not a
                # broad read of the org's crosstalk. Own-inbox only.
                res = crosstalk_inbox_dashboard(identity, args, state)
                return tool_call_result(msg_id, res["structured"], res["text"],
                                        is_error=res.get("is_error", False))
            authz = authorize(identity, name, args)
            if not authz["allowed"]:
                state.log({"tool": name, "sess": sess_tag(identity),
                           "ok": False, "err": authz.get("reason")})
                return tool_call_result(
                    msg_id, {"error": "not_authorized", "status": authz.get("status")},
                    authz_denial_text(name, authz), is_error=True)
            _req_ctx.org = authz.get("org")
            dpeer = {"name": peer_label(identity), "scopes": []}
            try:
                structured, text = TOOL_HANDLERS[name](state.registry, args, dpeer)
                state.log({"tool": name, "sess": sess_tag(identity),
                           "org": authz.get("org"), "ok": True})
                return tool_call_result(msg_id, structured, text)
            except ToolError as e:
                state.log({"tool": name, "ok": False, "err": str(e)[:200]})
                return tool_call_result(msg_id, {"error": str(e)}, str(e), is_error=True)
            except Exception as e:
                state.log({"tool": name, "ok": False, "err": f"internal: {e}"})
                return tool_call_result(msg_id, {"error": "internal"},
                                        f"Internal relay error: {e}", is_error=True)
            finally:
                _req_ctx.org = None

        # --- REGISTRY mode (legacy peer_token + file registry)
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
        # request-level debug log (method + tool + response error-ness). We do NOT
        # log identity headers or _meta: in dashboard mode openai/session is
        # bearer-equivalent, so it must never hit disk.
        try:
            params = msg.get("params") or {}
            _dbg = {
                "method": msg.get("method"),
                "tool": params.get("name"),
                "hdr_method": self.headers.get("Mcp-Method"),
                "hdr_name": self.headers.get("Mcp-Name"),
                "proto": self.headers.get("MCP-Protocol-Version"),
            }
        except Exception:
            _dbg = {"method": "?"}
        response = handle_message(self.state, msg, self.headers)
        try:
            _dbg["resp"] = "error" if (response and "error" in response) else (
                "result" if response else "202")
            self.state.log({"debug_request": _dbg})
        except Exception:
            pass
        if response is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._send_json(200, response)


# ---------------------------------------------------------------- CLI

def _is_loopback_url(url: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    return host in ("127.0.0.1", "::1", "localhost")


def cmd_run(args):
    data_dir = Path(args.data_dir)
    # MEDIUM hardening: the dashboard call disables TLS verification (self-signed
    # localhost cert), so it must ONLY ever target loopback — otherwise the relay
    # would hand its service token to anything presenting any cert. Fail fast.
    if DASHBOARD_MODE and not _is_loopback_url(DASHBOARD_URL):
        print(f"refusing to run: MCP_RELAY_DASHBOARD_URL ({DASHBOARD_URL}) is not "
              "loopback, but the dashboard call skips TLS verification. Point it at "
              "127.0.0.1 or add cert pinning before using a remote dashboard.",
              file=sys.stderr)
        sys.exit(2)
    RelayHandler.state = RelayState(data_dir)
    server = ThreadingHTTPServer((args.host, args.port), RelayHandler)
    print(f"autonomy-mcp-relay listening on http://{args.host}:{args.port}/mcp "
          f"({'DASHBOARD' if DASHBOARD_MODE else 'REGISTRY'} mode)")
    print(f"peer registry: {data_dir / 'peers.json'}")
    print(f"call log:      {data_dir / 'relay.jsonl'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_stdio(args):
    """Run over stdin/stdout as the tunnel-client's child (no TCP surface). MCP
    stdio framing: one JSON-RPC message per line, no embedded newlines."""
    global STDIO_TRANSPORT
    STDIO_TRANSPORT = True
    state = RelayState(Path(args.data_dir))

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    for raw in sys.stdin.buffer:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            emit(jsonrpc_error(None, -32700, "Parse error"))
            continue
        if not isinstance(msg, dict):
            emit(jsonrpc_error(None, -32600, "Batch requests not supported"))
            continue
        # No HTTP headers over stdio — identity comes from params._meta only.
        response = handle_message(state, msg, {})
        if response is not None:  # notifications (no id) get no reply
            emit(response)


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

    p_run = sub.add_parser("run", help="run the relay over HTTP (loopback)")
    p_run.add_argument("--host", default="127.0.0.1")
    p_run.add_argument("--port", type=int, default=8787)
    p_run.set_defaults(func=cmd_run)

    p_stdio = sub.add_parser(
        "stdio", help="run over stdin/stdout as the tunnel-client child (no TCP; "
                      "the secure deployment)")
    p_stdio.set_defaults(func=cmd_stdio)

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
