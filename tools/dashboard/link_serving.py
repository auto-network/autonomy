"""C4: target serving over the tunnel — the grant-gated artifact resolver (§6.1).

When a viewer channel opens on the B2 tunnel, the connector's *handler*
seam (``tools/network/relaykit/connector.py``) asks this module for the
bytes behind the channel's token. The flow per request:

1. **The I9 gate.** The token is validated against the LOCAL grant cache
   (``autonomy.network.link-grant`` Settings rows, written by the C3
   ``link_publish`` executor). Unknown token, malformed cache row,
   expired ``meta.ttl``, or a token whose row was removed by
   ``link_revoke`` → serve NOTHING. The registry is never consulted: a
   registry compromise alone cannot open content — the dashboard's own
   grant record is the gate.
2. **Target resolution.** The grant's ``(target_type, target_uuid)``
   maps to rendered bytes:

   * ``present`` → Present deck HTML (the deck's latest revision, same
     resolution the Present plugin uses);
   * ``design`` → the exact Design Studio revision's variant HTML;
   * ``note`` → a self-contained rich note render (all content
     HTML-escaped — the note body is data, never markup);
   * ``file`` → an agent-run artifact resolved through its graph
     attachment row, PATH-ALLOWLISTED: the attachment's ``file_path``
     must ``realpath``-resolve inside an allowed root
     (``data/agent-runs/`` by default), so neither ``..`` segments nor
     symlinks can walk the resolver out of the run dirs.

Wire protocol: channel fetch v1, exactly the shape of the reference
``connector.file_handler`` — request ``{"op": "fetch", "v": 1}``,
response one canonical-JSON header line (``{v, status, content_type}``),
a newline, then the body bytes.

Anti-enumeration: every refusal — unknown token, expired grant, revoked
grant, reserved ``require_auth`` grant, unresolvable target, disallowed
path — returns the SAME byte string (404, empty body). A prober on an
open channel learns nothing about which stage refused it (§5.3
discipline applied at the serving layer).

Runnable directly to hook the resolver into a live tunnel::

    python -m tools.dashboard.link_serving \
        --relay wss://auto.network --org <uuid> \
        --key-file session.hex --cert-file session.cert \
        [--graph-org <slug>] [--file-root DIR ...]
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import html
import json
import os
import re
import time
import uuid as uuid_mod
from pathlib import Path

from tools.graph import settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_LINK_GRANT_SET_ID,
    NETWORK_TOKEN_HEX_LEN,
    TARGET_TYPES,
)
from tools.network.idkit import canonical_json

REPO_ROOT = Path(__file__).resolve().parents[2]

#: file targets may only resolve inside these directories (bead: agent-run
#: output dirs). Overridable per-handler for tests / deployments.
DEFAULT_FILE_ROOTS = (str(REPO_ROOT / "data" / "agent-runs"),)

_TOKEN_RE = re.compile(r"^[0-9a-f]{%d}$" % NETWORK_TOKEN_HEX_LEN)
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _response(status: int, content_type: str, body: bytes = b"") -> bytes:
    header = canonical_json({"v": 1, "status": status, "content_type": content_type})
    return header + b"\n" + body


#: the ONE refusal every gate failure returns — anti-enumeration requires
#: unknown/expired/revoked/unresolvable to be byte-indistinguishable.
REFUSED = _response(404, "text/plain")
BAD_REQUEST = _response(400, "text/plain", b"bad request")


# ── the I9 gate ───────────────────────────────────────────────


def _grant_valid(payload, token: str, now: float):
    """Pure validity check for one cached grant payload → payload or None.

    Fails closed on every malformation: the Settings schema should make
    these unrepresentable, but the serving path re-checks because a cache
    row is the last line between a token and artifact bytes (I9).
    """
    if not isinstance(payload, dict) or payload.get("token") != token:
        return None
    if payload.get("target_type") not in TARGET_TYPES:
        return None
    target_uuid = payload.get("target_uuid")
    if not isinstance(target_uuid, str):
        return None
    try:
        uuid_mod.UUID(target_uuid)
    except (ValueError, AttributeError):
        return None

    meta = payload.get("meta")
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        return None
    # Rung-2 reservation, re-checked at serve time: an authenticated-only
    # grant must never be served by the anonymous rung-1 path.
    if meta.get("require_auth"):
        return None
    if "ttl" in meta:
        ttl = meta["ttl"]
        if type(ttl) is not int or ttl <= 0:
            return None
        issued_at = payload.get("issued_at")
        if not isinstance(issued_at, str):
            return None
        try:
            issued = calendar.timegm(time.strptime(issued_at, _ISO_FORMAT))
        except ValueError:
            return None
        if now >= issued + ttl:
            return None
    return payload


def check_grant(token: str, *, org: str | None = None, now: float | None = None):
    """THE I9 gate: token → valid LOCAL grant payload, or None.

    Only the dashboard's own ``autonomy.network.link-grant`` cache is
    consulted — never the registry. A row that isn't there (never
    published locally, or removed by ``link_revoke``) means the token
    serves nothing, no matter who asks.
    """
    if not isinstance(token, str) or not _TOKEN_RE.match(token):
        return None
    try:
        # Owning-scope read (P2): the serving gate must consult only THIS
        # org's own grant cache. A peer-published grant row must never be
        # servable through check_grant — token->bytes authorization cannot
        # compose across orgs. (Recovered from the retired write-guard
        # commit, which had bundled this read-scope fix with its write wrap.)
        members = settings_ops.read_owned_set(NETWORK_LINK_GRANT_SET_ID, org=org).members
    except Exception:
        return None  # unreadable cache → no grant → no bytes (fail closed)
    for member in members:
        if member.key == token:
            return _grant_valid(member.payload, token,
                                time.time() if now is None else now)
    return None


# ── target resolvers (§6.1) ───────────────────────────────────


def _variant_html(design: dict) -> str:
    """The revision's presented HTML: the selected variant, else the last."""
    variants = design.get("variants") or []
    if not variants:
        return ""
    selected = [v for v in variants if v.get("selected")]
    return (selected or variants)[-1].get("html") or ""


def _resolve_present(target_uuid: str):
    """Present deck HTML — deck semantics, so a stable design id follows
    to its latest revision (mirrors the Present plugin's resolution)."""
    from agents.design_db import _get_conn, get_design

    design = get_design(target_uuid)
    if design:
        revisions = design.get("revisions") or []
        if revisions and revisions[-1] != design.get("id"):
            design = get_design(revisions[-1]) or design
    else:
        # A stable design id that is not itself a revision id: newest
        # revision wins (same fallback the Present plugin performs).
        conn = _get_conn()
        try:
            row = conn.execute(
                "SELECT id FROM designs WHERE design_id = ? "
                "ORDER BY revision_seq DESC LIMIT 1",
                (target_uuid,),
            ).fetchone()
        finally:
            conn.close()
        design = get_design(row["id"]) if row else None
    if not design:
        return None
    html_text = _variant_html(design)
    if not html_text:
        return None
    return html_text.encode("utf-8"), "text/html; charset=utf-8"


def _resolve_design(target_uuid: str):
    """The exact Design Studio revision's HTML — no latest-revision hop:
    the operator approved sharing this revision, not the design's future."""
    from agents.design_db import get_design

    design = get_design(target_uuid)
    if not design or design.get("id") != target_uuid:
        return None
    html_text = _variant_html(design)
    if not html_text:
        return None
    return html_text.encode("utf-8"), "text/html; charset=utf-8"


_NOTE_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ margin: 0; background: #101418; color: #d8dee6;
         font: 16px/1.6 system-ui, -apple-system, sans-serif; }}
  article {{ max-width: 52rem; margin: 0 auto; padding: 3rem 1.5rem; }}
  h1 {{ font-size: 1.6rem; line-height: 1.3; color: #f0f4f8; }}
  pre {{ white-space: pre-wrap; overflow-wrap: break-word;
        font: 15px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace; }}
</style>
</head>
<body><article><h1>{title}</h1><pre>{body}</pre></article></body>
</html>
"""


def _resolve_note(target_uuid: str, org: str | None):
    """Rich note render: a self-contained page, every character escaped —
    note content is displayed, never interpreted as markup."""
    from tools.graph import ops as graph_ops

    data = graph_ops.read_source_full(target_uuid, max_chars=0, org=org)
    if not data:
        return None
    source = data.get("source") or {}
    # A 'note' grant serves notes only: resolving to any other source kind
    # (a session transcript, a bead) would serve more than was approved.
    if source.get("type") != "note":
        return None
    body = "\n\n".join(
        entry.get("content") or "" for entry in data.get("entries") or []
    )
    page = _NOTE_PAGE.format(
        title=html.escape(source.get("title") or "Note"),
        body=html.escape(body),
    )
    return page.encode("utf-8"), "text/html; charset=utf-8"


def _path_allowed(path: str, roots: tuple[str, ...]) -> str | None:
    """Resolve *path* and require it inside an allowed root → real path or None.

    ``realpath`` first, THEN the containment check: ``..`` segments and
    symlinks are resolved away before comparison, so a link inside a run
    dir pointing at ``/etc/passwd`` fails exactly like a literal
    traversal. The root itself is not servable — only entries within it.
    """
    if not isinstance(path, str) or not path:
        return None
    real = os.path.realpath(path)
    for root in roots:
        root_real = os.path.realpath(root)
        try:
            if real != root_real and os.path.commonpath([real, root_real]) == root_real:
                return real
        except ValueError:
            continue  # mixed drive/absolute forms cannot be contained
    return None


def _resolve_file(target_uuid: str, org: str | None, roots: tuple[str, ...]):
    """Agent-run artifact via its graph attachment row, allowlisted."""
    from tools.graph import ops as graph_ops

    attachment = graph_ops.get_attachment(target_uuid, org=org)
    if not isinstance(attachment, dict):
        return None
    real = _path_allowed(attachment.get("file_path"), roots)
    if real is None or not os.path.isfile(real):
        return None
    try:
        body = Path(real).read_bytes()
    except OSError:
        return None
    return body, attachment.get("mime_type") or "application/octet-stream"


def resolve_target(grant: dict, *, org: str | None = None,
                   file_roots: tuple[str, ...] = DEFAULT_FILE_ROOTS):
    """(target_type, target_uuid) → (body bytes, content type), or None.

    Takes a grant that already passed :func:`check_grant`; any resolution
    failure returns None so the caller emits the uniform refusal.
    """
    target_type = grant["target_type"]
    target_uuid = grant["target_uuid"]
    try:
        if target_type == "present":
            return _resolve_present(target_uuid)
        if target_type == "design":
            return _resolve_design(target_uuid)
        if target_type == "note":
            return _resolve_note(target_uuid, org)
        if target_type == "file":
            return _resolve_file(target_uuid, org, file_roots)
    except Exception:
        return None  # resolver errors serve nothing, not stack traces
    return None


# ── the connector handler (the C4 seam) ───────────────────────


def make_grant_handler(org: str | None = None, *,
                       file_roots=None, now=None):
    """Build the ``handler(token, message)`` the B2 connector serves with.

    *org* scopes the grant cache and graph lookups; *file_roots* overrides
    the file-target allowlist; *now* (an epoch-seconds callable) is the
    TTL clock, injectable for tests.
    """
    roots = tuple(file_roots) if file_roots else DEFAULT_FILE_ROOTS
    clock = now or time.time

    def _serve(token: str) -> bytes:
        grant = check_grant(token, org=org, now=clock())
        if grant is None:
            return REFUSED
        resolved = resolve_target(grant, org=org, file_roots=roots)
        if resolved is None:
            return REFUSED
        body, content_type = resolved
        return _response(200, content_type, body)

    async def handler(token: str, message: bytes) -> bytes:
        try:
            request = json.loads(message)
        except ValueError:
            return BAD_REQUEST
        if not isinstance(request, dict) or request.get("op") != "fetch":
            return BAD_REQUEST
        # Settings + sqlite + file reads are blocking; keep them off the
        # tunnel's event loop so one slow lookup can't stall siblings.
        return await asyncio.to_thread(_serve, token)

    return handler


def main() -> None:
    from tools.network.idkit import DelegationCert, KeyPair
    from tools.network.relaykit.connector import TunnelConnector

    parser = argparse.ArgumentParser(
        description="auto.network tunnel connector serving grant-gated targets (C4)"
    )
    parser.add_argument("--relay", required=True, help="relay base URL, e.g. wss://auto.network")
    parser.add_argument("--org", required=True, help="org UUID on the registry")
    parser.add_argument("--key-file", required=True, help="file holding the private key hex")
    parser.add_argument("--cert-file", required=True, help="file holding the cert wire JSON")
    parser.add_argument("--graph-org", default=None,
                        help="dashboard org slug scoping the grant cache")
    parser.add_argument("--file-root", action="append", default=None,
                        help="allowed root for file targets (repeatable; "
                             "default: data/agent-runs)")
    parser.add_argument("--min-backoff", type=float, default=0.2)
    parser.add_argument("--max-backoff", type=float, default=5.0)
    args = parser.parse_args()

    with open(args.key_file) as fh:
        key = KeyPair.from_private_hex(fh.read().strip())
    with open(args.cert_file) as fh:
        cert = DelegationCert.from_json(fh.read().strip())

    handler = make_grant_handler(args.graph_org, file_roots=args.file_root)
    connector = TunnelConnector(
        args.relay, args.org, key, cert, handler,
        min_backoff=args.min_backoff, max_backoff=args.max_backoff,
    )
    asyncio.run(connector.run())


if __name__ == "__main__":
    main()
