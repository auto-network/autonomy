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


def _get(endpoint: str) -> dict:
    """GET from the dashboard API. Returns parsed JSON response."""
    url = f"{GRAPH_API}{endpoint}"
    req = urllib.request.Request(url, method="GET")
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


def _post_multipart(endpoint: str, fields: dict, files: list[tuple[str, str, bytes, str]]) -> dict:
    """POST multipart/form-data. files: list of (field_name, filename, data, content_type)."""
    boundary = "----GraphMultipartBoundary"
    body = b""
    for key, value in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
    for field_name, filename, data, content_type in files:
        body += (f"--{boundary}\r\n"
                 f"Content-Disposition: form-data; name=\"{field_name}\"; filename=\"{filename}\"\r\n"
                 f"Content-Type: {content_type}\r\n\r\n").encode()
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    url = f"{GRAPH_API}{endpoint}"
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=60, context=_SSL_CTX)
        return json.loads(resp.read())
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


def api_note(args) -> None:
    """Create a note via API."""
    if getattr(args, 'content_stdin', None) == "-":
        text = sys.stdin.read().strip()
    elif args.text:
        text = " ".join(args.text)
    else:
        print("Error: note text required", file=sys.stderr)
        sys.exit(1)

    attach_paths = getattr(args, "attach", None) or []
    if attach_paths:
        import mimetypes
        from pathlib import Path
        fields = {"content": text}
        if args.tags:
            fields["tags"] = args.tags
        if args.project:
            fields["project"] = args.project
        if args.author:
            fields["author"] = args.author
        scope = os.environ.get("GRAPH_SCOPE")
        if scope and not args.project:
            fields["project"] = scope
        files = []
        for fp in attach_paths:
            p = Path(fp)
            if not p.is_file():
                print(f"Error: {fp} not found or not a file", file=sys.stderr)
                sys.exit(1)
            mime, _ = mimetypes.guess_type(p.name)
            files.append(("attachments", p.name, p.read_bytes(), mime or "application/octet-stream"))
        _print_output(_post_multipart("/api/graph/note", fields, files))
    else:
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

    attach_paths = getattr(args, "attach", None) or []
    if attach_paths:
        import mimetypes
        from pathlib import Path
        fields = {"source_id": args.source, "content": new_content}
        if args.integrate_ids:
            fields["integrate_ids"] = json.dumps(args.integrate_ids)
        files = []
        for fp in attach_paths:
            p = Path(fp)
            if not p.is_file():
                print(f"Error: {fp} not found or not a file", file=sys.stderr)
                sys.exit(1)
            mime, _ = mimetypes.guess_type(p.name)
            files.append(("attachments", p.name, p.read_bytes(), mime or "application/octet-stream"))
        _print_output(_post_multipart("/api/graph/note/update", fields, files))
    else:
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


def api_journal_write(args) -> None:
    """Write a journal entry via API."""
    raw = sys.stdin.read().strip() if args.content == "-" else args.content
    if not raw:
        print("Error: no JSON content provided", file=sys.stderr)
        sys.exit(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    _print_output(_post("/api/graph/journal", data))


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


def api_attach(args) -> None:
    """Attach a file via API (multipart form upload)."""
    import mimetypes
    from pathlib import Path

    file_path = Path(args.file_path)
    if not file_path.is_file():
        print(f"Error: {file_path} not found or not a file", file=sys.stderr)
        sys.exit(1)

    file_data = file_path.read_bytes()
    filename = file_path.name
    mime_type, _ = mimetypes.guess_type(filename)
    content_type = mime_type or "application/octet-stream"

    # Build multipart/form-data body
    boundary = "----GraphAttachBoundary"
    parts = []

    # File part
    parts.append(f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                 f"Content-Type: {content_type}\r\n\r\n")
    parts.append(None)  # placeholder for binary data
    parts.append(f"\r\n")

    # Source ID part
    source_id = getattr(args, "source", None)
    if source_id:
        parts.append(f"--{boundary}\r\n"
                     f'Content-Disposition: form-data; name="source_id"\r\n\r\n'
                     f"{source_id}\r\n")

    # Turn part
    turn = getattr(args, "turn", None)
    if turn is not None:
        parts.append(f"--{boundary}\r\n"
                     f'Content-Disposition: form-data; name="turn"\r\n\r\n'
                     f"{turn}\r\n")

    parts.append(f"--{boundary}--\r\n")

    # Assemble body bytes
    body = b""
    for part in parts:
        if part is None:
            body += file_data
        else:
            body += part.encode()

    url = f"{GRAPH_API}/api/graph/attach"
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=60, context=_SSL_CTX)
        result = json.loads(resp.read())
        _print_output(result)
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


def _get_session_name() -> str:
    """Resolve the current session name from environment."""
    name = os.environ.get("AUTONOMY_SESSION")
    if name:
        return name
    bd_actor = os.environ.get("BD_ACTOR")
    if bd_actor and ":" in bd_actor:
        return bd_actor.split(":", 1)[1]
    print("Error: cannot determine session name. Set $AUTONOMY_SESSION or $BD_ACTOR.", file=sys.stderr)
    sys.exit(1)


def api_set_label(args) -> None:
    """Set session label via dashboard API."""
    session = _get_session_name()
    label = " ".join(args.text)
    _put(f"/api/session/{urllib.parse.quote(session)}/label", {"label": label})
    print(f"  \u2713 Label set: {label}")


def api_set_topics(args) -> None:
    """Set session topics via dashboard API."""
    session = _get_session_name()
    _put(f"/api/session/{urllib.parse.quote(session)}/topics", {"topics": args.topics})
    print(f"  \u2713 Topics set ({len(args.topics)} lines)")


def api_set_role(args) -> None:
    """Set session role via dashboard API."""
    session = _get_session_name()
    role = " ".join(args.role)
    _put(f"/api/session/{urllib.parse.quote(session)}/role", {"role": role})
    print(f"  \u2713 Role set: {role}")


def api_set_nag(args) -> None:
    """Enable or disable session nag via dashboard API."""
    session = _get_session_name()
    if getattr(args, "dispatch", False):
        # Dispatch completion nag
        enabled = not args.off
        _put(f"/api/session/{urllib.parse.quote(session)}/dispatch-nag", {"enabled": enabled})
        state = "enabled" if enabled else "disabled"
        print(f"  \u2713 Dispatch nag {state}")
    elif args.off:
        _delete(f"/api/session/{urllib.parse.quote(session)}/nag")
        print("  \u2713 Nag disabled")
    else:
        if not args.interval:
            print("Error: --interval required (or use --off)", file=sys.stderr)
            sys.exit(1)
        payload = {"enabled": True, "interval": args.interval}
        if args.message:
            payload["message"] = " ".join(args.message)
        _put(f"/api/session/{urllib.parse.quote(session)}/nag", payload)
        print(f"  \u2713 Nag enabled (every {args.interval}m)")


def api_collab_list(args) -> None:
    """List collab notes via dashboard API."""
    result = _get(f"/api/graph/collab?limit={args.limit}")
    _print_output(result)


def api_collab_tag(args) -> None:
    """Add collab tag via dashboard API."""
    source_id = args.source_id
    if not _SOURCE_ID_RE.match(source_id):
        print(f"Error: malformed source_id: {source_id!r}", file=sys.stderr)
        sys.exit(1)
    result = _put(f"/api/graph/collab/tag/{source_id}", {})
    _print_output(result)


def api_collab_tag_describe(args) -> None:
    """Set tag description via dashboard API."""
    tag_name = args.tag_name
    desc = args.description
    if desc == "-":
        desc = sys.stdin.read().strip()
    data = {
        "description": desc,
        "actor": os.environ.get("BD_ACTOR", "user"),
    }
    result = _put(f"/api/graph/collab/tag-describe/{tag_name}", data)
    _print_output(result)


def _delete(endpoint: str) -> dict:
    """DELETE to the dashboard API. Returns parsed JSON response."""
    url = f"{GRAPH_API}{endpoint}"
    req = urllib.request.Request(url, method="DELETE")
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


def api_tag_add(args) -> None:
    """Add tag(s) to source(s) via API."""
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    for sid in args.source_ids:
        if not _SOURCE_ID_RE.match(sid):
            print(f"Error: malformed source_id: {sid!r}", file=sys.stderr)
            sys.exit(1)
        for tag in tags:
            if not _TAGS_RE.match(tag):
                print(f"Error: malformed tag: {tag!r}", file=sys.stderr)
                sys.exit(1)
            result = _put(f"/api/graph/tag/{sid}/{tag}", {})
            _print_output(result)


def api_tag_remove(args) -> None:
    """Remove tag(s) from source(s) via API."""
    tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    for sid in args.source_ids:
        if not _SOURCE_ID_RE.match(sid):
            print(f"Error: malformed source_id: {sid!r}", file=sys.stderr)
            sys.exit(1)
        for tag in tags:
            if not _TAGS_RE.match(tag):
                print(f"Error: malformed tag: {tag!r}", file=sys.stderr)
                sys.exit(1)
            result = _delete(f"/api/graph/tag/{sid}/{tag}")
            _print_output(result)


def api_tag_merge(args) -> None:
    """Merge tags via API."""
    data = {
        "from": args.from_tag,
        "to": args.to_tag,
        "reason": args.reason or "",
        "force": args.force,
    }
    result = _post("/api/graph/tag/merge", data)
    _print_output(result)


def api_thought(args) -> None:
    """Create a thought capture via dashboard API."""
    content = " ".join(args.text) if args.text else ""
    if args.content_stdin == "-" or not content:
        if args.content_stdin == "-":
            content = sys.stdin.read().strip()
        if not content:
            print("No content provided", file=sys.stderr)
            sys.exit(1)
    data: dict = {
        "content": content,
        "actor": os.environ.get("BD_ACTOR", "user"),
    }
    if hasattr(args, "source") and args.source:
        data["source_id"] = args.source
    if hasattr(args, "turn") and args.turn:
        data["turn_number"] = args.turn
    if hasattr(args, "thread") and args.thread:
        data["thread_id"] = args.thread
    result = _post("/api/graph/thought", data)
    _print_output(result)


def api_thoughts(args) -> None:
    """List captures via API."""
    params = [f"limit={args.limit}"]
    if hasattr(args, 'thread') and args.thread:
        params.append(f"thread={args.thread}")
    if hasattr(args, 'since') and args.since:
        params.append(f"since={args.since}")
    _print_output(_get(f"/api/graph/thoughts?{'&'.join(params)}"))


def api_threads(args) -> None:
    """List threads via API."""
    params = [f"limit={args.limit}"]
    if hasattr(args, 'all') and args.all:
        params.append("all=1")
    elif hasattr(args, 'status') and args.status:
        params.append(f"status={args.status}")
    _print_output(_get(f"/api/graph/threads?{'&'.join(params)}"))


def api_thread_action(args, action: str, thread_id: str, target: str | None = None) -> None:
    """Thread actions via API proxy."""
    data = {"action": action, "thread_id": thread_id}
    if target:
        data["target"] = target
    _print_output(_post("/api/graph/thread/action", data))


def api_thread(args) -> None:
    """Create a thread via dashboard API."""
    parts = args.thread_args or []
    if not parts:
        # list mode — read-only, no proxy needed
        return
    title = " ".join(parts)
    data: dict = {
        "title": title,
        "priority": args.priority,
        "actor": os.environ.get("BD_ACTOR", "user"),
    }
    result = _post("/api/graph/thread", data)
    _print_output(result)
