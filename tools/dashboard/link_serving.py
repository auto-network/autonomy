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
   maps to a typed, part-addressed artifact:

   * ``present`` → Present deck HTML (the deck's latest revision, same
     resolution the Present plugin uses);
   * ``design`` → the exact Design Studio revision's variant HTML;
   * ``note`` → a fixed viewer plus Markdown and owning-note image parts.

   ``file`` remains reserved and is not served by this protocol version.

Wire protocol: channel fetch v1.

* ``{"op": "fetch", "v": 1}`` → one canonical-JSON header line
  (``{v,status,kind,viewer,content?,branding?}``), a newline, then the serialized
  body. Every byte range is an in-bounds ``{offset,length}`` slice.
* ``{"op": "head", "v": 1}`` →
  ``{v:1,status:"ok",serialized_size}``, a newline, and NO body. It runs
  the identical grant gate + target resolution as ``fetch`` but never
  transfers the artifact.
  This is what the publisher's own end-to-end liveness probe issues (the
  final step of a link publish): the dashboard acts as its own viewer and
  HEADs the freshly published token to confirm the tunnel actually serves.

Anti-enumeration: every refusal — unknown token, expired grant, revoked
grant, reserved ``require_auth`` grant, unresolvable target, unsupported
target, or oversize artifact — returns the same
``{v:1,status:"unavailable"}`` byte string with an empty body.

Runnable directly to hook the resolver into a live tunnel::

    python -m tools.dashboard.link_serving \
        --relay wss://auto.network --org <uuid> \
        --key-file session.hex --cert-file session.cert \
        [--graph-org <slug>]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import calendar
import json
import mimetypes
import re
import time
import uuid as uuid_mod
from pathlib import Path

from tools.graph import settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_LINK_GRANT_REVISION,
    NETWORK_LINK_GRANT_SET_ID,
    NETWORK_TOKEN_HEX_LEN,
    TARGET_TYPES,
)
from tools.network.idkit import canonical_json

AUTONET_MAX_ARTIFACT_BYTES = 48 * 1024 * 1024
AUTONET_MAX_FAVICON_BYTES = 512 * 1024
AUTONET_MAX_TITLE_CHARS = 500
_NOTE_VIEWER = Path(__file__).resolve().parent / "relay_viewer" / "note-viewer.html"
_DASHBOARD_STATIC = Path(__file__).resolve().parent / "static"

_TOKEN_RE = re.compile(r"^[0-9a-f]{%d}$" % NETWORK_TOKEN_HEX_LEN)
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _response(status, body: bytes = b"") -> bytes:
    header = canonical_json({"v": 1, "status": status})
    return header + b"\n" + body


#: the ONE refusal every gate failure returns — anti-enumeration requires
#: unknown/expired/revoked/unresolvable to be byte-indistinguishable.
REFUSED = _response("unavailable")
BAD_REQUEST = _response(400, b"bad request")


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
        members = settings_ops.read_owned_set(
            NETWORK_LINK_GRANT_SET_ID,
            org=org,
            target_revision=NETWORK_LINK_GRANT_REVISION,
        ).members
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
    return {"kind": "present", "viewer": html_text.encode("utf-8")}


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
    return {"kind": "design", "viewer": html_text.encode("utf-8")}


_GRAPH_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(graph://([^)]+)\)")


class _ArtifactTooLarge(ValueError):
    pass


def _note_viewer_bytes() -> bytes:
    return _NOTE_VIEWER.read_bytes()


def _resolve_org_brand(org: str | None) -> dict | None:
    """Resolve authenticated org branding without exposing dashboard paths.

    Local dashboard favicons and data URLs become bounded artifact bytes.
    Public HTTPS icons remain URLs and load under the share page's no-referrer
    policy. Any malformed or unreadable icon degrades to the org's existing
    color/initial identity; branding must never make content unservable.
    """
    try:
        from tools.dashboard.org_identity import resolve_org_identity

        identity = resolve_org_identity(org)
        name = identity.get("name")
        color = identity.get("color")
        initial = identity.get("initial")
        if not isinstance(name, str) or not name:
            return None
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            color = "#4b5563"
        if not isinstance(initial, str) or len(initial) != 1:
            initial = next((ch.upper() for ch in name if ch.isalnum()), "?")
        brand = {"name": name[:200], "color": color, "initial": initial}
        favicon = identity.get("favicon")
        if not isinstance(favicon, str) or not favicon:
            return brand
        if favicon.startswith("https://") and len(favicon) <= 2048:
            brand["favicon_url"] = favicon
            return brand

        mime = None
        icon_bytes = None
        data_match = re.fullmatch(
            r"data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\r\n]+)", favicon
        )
        if data_match:
            mime = data_match.group(1).lower()
            try:
                encoded = "".join(data_match.group(2).split())
                icon_bytes = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                icon_bytes = None
        elif favicon.startswith("/static/"):
            relative = favicon.removeprefix("/static/")
            candidate = (_DASHBOARD_STATIC / relative).resolve()
            static_root = _DASHBOARD_STATIC.resolve()
            try:
                candidate.relative_to(static_root)
            except ValueError:
                candidate = None
            if candidate is not None and candidate.is_file():
                mime = mimetypes.guess_type(candidate.name)[0]
                with candidate.open("rb") as icon_file:
                    icon_bytes = icon_file.read(AUTONET_MAX_FAVICON_BYTES + 1)

        if (
            isinstance(mime, str) and mime.startswith("image/")
            and isinstance(icon_bytes, bytes)
            and 0 < len(icon_bytes) <= AUTONET_MAX_FAVICON_BYTES
        ):
            brand["favicon"] = {"mime": mime, "bytes": icon_bytes}
        return brand
    except Exception:
        return None


def _resolve_note(target_uuid: str, org: str | None):
    """Build an owning-scope note payload with only current image parts."""
    from tools.graph import ops as graph_ops

    data = graph_ops.read_source_full(
        target_uuid, max_chars=0, org=org, peers=[]
    )
    if not data:
        return None
    source = data.get("source") or {}
    # A 'note' grant serves notes only: resolving to any other source kind
    # (a session transcript, a bead) would serve more than was approved.
    if source.get("type") != "note":
        return None
    markdown = "\n\n".join(
        entry.get("content") or "" for entry in data.get("entries") or []
    )
    viewer = _note_viewer_bytes()
    serialized_size = len(viewer)
    if serialized_size > AUTONET_MAX_ARTIFACT_BYTES:
        raise _ArtifactTooLarge
    source_id = source.get("id")
    if not isinstance(source_id, str) or not source_id:
        return None
    parts_by_ref: dict[str, dict] = {}

    def replace_image(match: re.Match) -> str:
        nonlocal serialized_size
        alt, requested_ref = match.group(1), match.group(2).strip()
        cid_ref = requested_ref
        try:
            attachment = graph_ops.get_attachment(
                requested_ref, org=org, peers=[]
            )
            mime = attachment.get("mime_type") if attachment else None
            if (
                attachment
                and attachment.get("source_id") == source_id
                and isinstance(mime, str)
                and mime.startswith("image/")
            ):
                canonical_ref = attachment.get("id") or requested_ref
                file_path = attachment.get("file_path")
                if isinstance(file_path, str) and canonical_ref not in parts_by_ref:
                    remaining = AUTONET_MAX_ARTIFACT_BYTES - serialized_size
                    with Path(file_path).open("rb") as image_file:
                        part_bytes = image_file.read(remaining + 1)
                    if len(part_bytes) > remaining:
                        raise _ArtifactTooLarge
                    parts_by_ref[canonical_ref] = {
                        "ref": canonical_ref,
                        "mime": mime,
                        "bytes": part_bytes,
                    }
                    serialized_size += len(part_bytes)
                if canonical_ref in parts_by_ref:
                    cid_ref = canonical_ref
        except _ArtifactTooLarge:
            raise
        except Exception:
            # Keep a cid reference without a matching part. The viewer renders
            # a local placeholder and continues the rest of the note.
            pass
        return f"![{alt}](cid:{cid_ref})"

    markdown = _GRAPH_IMAGE_RE.sub(replace_image, markdown)
    serialized_size += len(markdown.encode("utf-8"))
    if serialized_size > AUTONET_MAX_ARTIFACT_BYTES:
        raise _ArtifactTooLarge
    title = source.get("title") or "Note"
    if not isinstance(title, str):
        title = "Note"
    return {
        "kind": "note",
        "viewer": viewer,
        "content": {
            "title": title[:AUTONET_MAX_TITLE_CHARS],
            "markdown": markdown,
            "parts": list(parts_by_ref.values()),
        },
    }


def _serialize_artifact(artifact: dict) -> tuple[dict, bytes]:
    """Serialize one resolved artifact into the v1 part-addressed body."""
    if not isinstance(artifact, dict):
        raise TypeError("artifact must be an object")
    kind = artifact.get("kind")
    if kind not in ("note", "design", "present"):
        raise ValueError("unsupported artifact kind")
    if not set(artifact).issubset({"kind", "viewer", "content", "branding"}):
        raise ValueError("artifact carries unknown fields")
    viewer = artifact.get("viewer")
    if not isinstance(viewer, bytes) or not viewer:
        raise ValueError("viewer must be non-empty bytes")

    chunks = [viewer]
    cursor = len(viewer)
    header = {
        "v": 1,
        "status": "ok",
        "kind": kind,
        "viewer": {"offset": 0, "length": len(viewer)},
    }

    content = artifact.get("content")
    if kind != "note":
        if content is not None:
            raise ValueError("design and present forbid content")
    else:
        if not isinstance(content, dict):
            raise ValueError("note requires content")

        title = content.get("title")
        markdown = content.get("markdown")
        parts = content.get("parts")
        if not isinstance(title, str) or len(title) > AUTONET_MAX_TITLE_CHARS:
            raise ValueError("invalid note title")
        if not isinstance(markdown, str) or not isinstance(parts, list):
            raise ValueError("invalid note content")

        markdown_bytes = markdown.encode("utf-8")
        markdown_slice = {"offset": cursor, "length": len(markdown_bytes)}
        chunks.append(markdown_bytes)
        cursor += len(markdown_bytes)

        part_headers = []
        seen_refs = set()
        for part in parts:
            if not isinstance(part, dict):
                raise ValueError("invalid part")
            ref, mime, part_bytes = part.get("ref"), part.get("mime"), part.get("bytes")
            if (
                not isinstance(ref, str) or not ref or ref in seen_refs
                or not isinstance(mime, str) or not mime
                or not isinstance(part_bytes, bytes)
            ):
                raise ValueError("invalid part")
            seen_refs.add(ref)
            part_headers.append({
                "ref": ref, "mime": mime,
                "offset": cursor, "length": len(part_bytes),
            })
            chunks.append(part_bytes)
            cursor += len(part_bytes)

        header["content"] = {
            "title": title,
            "markdown": markdown_slice,
            "parts": part_headers,
        }

    branding = artifact.get("branding")
    if branding is not None:
        if not isinstance(branding, dict) or not set(branding).issubset({
            "name", "color", "initial", "favicon", "favicon_url",
        }):
            raise ValueError("invalid branding")
        name, color, initial = (
            branding.get("name"), branding.get("color"), branding.get("initial")
        )
        if (
            not isinstance(name, str) or not name or len(name) > 200
            or not isinstance(color, str)
            or not re.fullmatch(r"#[0-9a-fA-F]{6}", color)
            or not isinstance(initial, str) or len(initial) != 1
        ):
            raise ValueError("invalid branding identity")
        wire_brand = {"name": name, "color": color, "initial": initial}
        favicon = branding.get("favicon")
        favicon_url = branding.get("favicon_url")
        if favicon is not None and favicon_url is not None:
            raise ValueError("branding has two favicons")
        if favicon is not None:
            if not isinstance(favicon, dict) or set(favicon) != {"mime", "bytes"}:
                raise ValueError("invalid branding favicon")
            mime, icon_bytes = favicon.get("mime"), favicon.get("bytes")
            if (
                not isinstance(mime, str) or not mime.startswith("image/")
                or not isinstance(icon_bytes, bytes) or not icon_bytes
                or len(icon_bytes) > AUTONET_MAX_FAVICON_BYTES
            ):
                raise ValueError("invalid branding favicon")
            wire_brand["favicon"] = {
                "mime": mime, "offset": cursor, "length": len(icon_bytes),
            }
            chunks.append(icon_bytes)
            cursor += len(icon_bytes)
        elif favicon_url is not None:
            if (
                not isinstance(favicon_url, str) or len(favicon_url) > 2048
                or not favicon_url.startswith("https://")
            ):
                raise ValueError("invalid branding favicon URL")
            wire_brand["favicon_url"] = favicon_url
        header["branding"] = wire_brand
    return header, b"".join(chunks)


def resolve_target(grant: dict, *, org: str | None = None):
    """(target_type, target_uuid) → typed artifact, or None.

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
        # file is deliberately deferred from the rich-render v1 grammar.
    except Exception:
        return None  # resolver errors serve nothing, not stack traces
    return None


# ── the connector handler (the C4 seam) ───────────────────────


def make_grant_handler(org: str | None = None, *, now=None):
    """Build the ``handler(token, message)`` the B2 connector serves with.

    *org* scopes the grant cache and graph lookups; *now* (an epoch-seconds
    callable) is the TTL clock, injectable for tests.
    """
    clock = now or time.time

    def _serve(token: str, head: bool = False) -> bytes:
        grant = check_grant(token, org=org, now=clock())
        if grant is None:
            return REFUSED
        resolved = resolve_target(grant, org=org)
        if resolved is None:
            return REFUSED
        branding = _resolve_org_brand(org)
        if branding is not None:
            resolved = {**resolved, "branding": branding}
        try:
            header, body = _serialize_artifact(resolved)
        except (TypeError, ValueError, OverflowError):
            return REFUSED
        if len(body) > AUTONET_MAX_ARTIFACT_BYTES:
            return REFUSED
        if head:
            return canonical_json({
                "v": 1, "status": "ok", "serialized_size": len(body),
            }) + b"\n"
        return canonical_json(header) + b"\n" + body

    async def handler(token: str, message: bytes) -> bytes:
        try:
            request = json.loads(message)
        except ValueError:
            return BAD_REQUEST
        if not isinstance(request, dict):
            return BAD_REQUEST
        if set(request) != {"v", "op"} or request.get("v") != 1:
            return BAD_REQUEST
        op = request.get("op")
        if op not in ("fetch", "head"):
            return BAD_REQUEST
        # Settings + sqlite + file reads are blocking; keep them off the
        # tunnel's event loop so one slow lookup can't stall siblings.
        return await asyncio.to_thread(_serve, token, op == "head")

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
    parser.add_argument("--min-backoff", type=float, default=0.2)
    parser.add_argument("--max-backoff", type=float, default=5.0)
    args = parser.parse_args()

    with open(args.key_file) as fh:
        key = KeyPair.from_private_hex(fh.read().strip())
    with open(args.cert_file) as fh:
        cert = DelegationCert.from_json(fh.read().strip())

    handler = make_grant_handler(args.graph_org)
    connector = TunnelConnector(
        args.relay, args.org, key, cert, handler,
        min_backoff=args.min_backoff, max_backoff=args.max_backoff,
    )
    asyncio.run(connector.run())


if __name__ == "__main__":
    main()
