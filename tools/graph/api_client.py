"""API client for routing graph write commands through the dashboard server.

When GRAPH_API env var is set (e.g. GRAPH_API=https://localhost:8080),
write commands POST to the dashboard server instead of opening the DB directly.
This enables the single-writer architecture: containers mount graph.db read-only
and all writes go through the host dashboard server.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

GRAPH_API = os.environ.get("GRAPH_API")

# Accept self-signed certs — dashboard uses Tailscale TLS
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# Validation patterns
_SOURCE_ID_RE = re.compile(r'^[0-9a-f-]+$', re.IGNORECASE)
_TAGS_RE = re.compile(r'^[a-zA-Z0-9_,:-]+$')


def is_api_mode() -> bool:
    """Return True if GRAPH_API is set and writes should go through the API."""
    return bool(GRAPH_API)


def _post(endpoint: str, data: dict) -> dict:
    """POST JSON to the dashboard graph API. Returns parsed JSON response."""
    url = f"{GRAPH_API}{endpoint}"
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30, context=_SSL_CTX)
        result = json.loads(resp.read())
        return result
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read())
            error_msg = err_body.get("error", str(e))
        except Exception:
            error_msg = str(e)
        print(f"API error: {error_msg}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Cannot reach graph API at {GRAPH_API}: {e.reason}", file=sys.stderr)
        sys.exit(1)


def _put(endpoint: str, data: dict) -> dict:
    """PUT JSON to the dashboard API. Returns parsed JSON response."""
    url = f"{GRAPH_API}{endpoint}"
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30, context=_SSL_CTX)
        result = json.loads(resp.read())
        return result
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read())
            error_msg = err_body.get("error", str(e))
        except Exception:
            error_msg = str(e)
        print(f"API error: {error_msg}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Cannot reach graph API at {GRAPH_API}: {e.reason}", file=sys.stderr)
        sys.exit(1)


def _print_output(result: dict) -> None:
    """Print the output field from an API response, mimicking direct CLI output."""
    output = result.get("output", "")
    if output:
        print(output, end="" if output.endswith("\n") else "\n")


def api_note(args) -> None:
    """Create a note via API."""
    if getattr(args, 'content_stdin', None) == "-":
        text = sys.stdin.read().strip()
    elif args.text:
        text = " ".join(args.text)
    else:
        print("Error: note text required", file=sys.stderr)
        sys.exit(1)

    data = {"content": text}
    if args.tags:
        data["tags"] = args.tags
    if args.project:
        data["project"] = args.project
    if args.author:
        data["author"] = args.author
    scope = os.environ.get("GRAPH_SCOPE")
    if scope and not args.project:
        data["project"] = scope

    _print_output(_post("/api/graph/note", data))


def api_note_update(args) -> None:
    """Update a note via API."""
    if getattr(args, 'content_stdin', None) == "-":
        new_content = sys.stdin.read().strip()
    else:
        new_content = " ".join(args.text) if args.text else ""

    if not new_content:
        print("Error: no content provided", file=sys.stderr)
        sys.exit(1)

    data = {
        "source_id": args.source,
        "content": new_content,
    }
    if args.integrate_ids:
        data["integrate_ids"] = args.integrate_ids

    _print_output(_post("/api/graph/note/update", data))


def api_comment_add(args) -> None:
    """Add a comment via API."""
    if getattr(args, 'content_stdin', None) == "-":
        content = sys.stdin.read().strip()
    else:
        content = " ".join(args.text) if args.text else ""

    if not content:
        print("Error: no comment content provided", file=sys.stderr)
        sys.exit(1)
    if not args.source:
        print("Error: source ID required", file=sys.stderr)
        sys.exit(1)

    data = {
        "source_id": args.source,
        "content": content,
        "actor": args.actor,
    }
    _print_output(_post("/api/graph/comment", data))


def api_comment_integrate(args) -> None:
    """Mark a comment as integrated via API."""
    data = {"comment_id": args.comment_id}
    _print_output(_post("/api/graph/comment/integrate", data))


def api_bead(args) -> None:
    """Create a bead with provenance via API."""
    desc = args.desc
    if desc == "-":
        desc = sys.stdin.read().strip()

    data = {
        "title": args.title,
        "priority": args.priority,
    }
    if desc:
        data["description"] = desc
    if args.type:
        data["type"] = args.type
    if args.source:
        data["source"] = args.source
    if args.turns:
        data["turns"] = args.turns
    if args.note:
        data["note"] = args.note

    _print_output(_post("/api/graph/bead", data))


def api_link(args) -> None:
    """Create a provenance link via API."""
    data = {
        "bead_id": args.bead,
        "source_id": args.source,
        "relationship": args.relation,
    }
    if args.turns:
        data["turn"] = args.turns
    if args.note:
        data["note"] = args.note

    _print_output(_post("/api/graph/link", data))


def api_sessions(args) -> None:
    """Ingest sessions via API."""
    data = {}
    if args.all:
        data["all"] = True
    if args.project:
        data["project"] = args.project
    if getattr(args, 'force', False):
        data["force"] = True

    _print_output(_post("/api/graph/sessions", data))


def api_set_label(args) -> None:
    """Set session label via dashboard API."""
    tmux_name = os.environ.get("DASHBOARD_SESSION")
    if not tmux_name:
        print("Error: $DASHBOARD_SESSION not set. Cannot identify current session.", file=sys.stderr)
        print("This command must be run inside a dashboard-managed tmux session.", file=sys.stderr)
        sys.exit(1)
    label = " ".join(args.text)
    result = _put(f"/api/session/{urllib.parse.quote(tmux_name)}/label", {"label": label})
    print(f"  \u2713 Label set: {label}")
