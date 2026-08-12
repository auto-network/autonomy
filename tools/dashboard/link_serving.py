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
import contextlib
import json
import mimetypes
import re
import secrets
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
# On-demand attachment download cap (wire protocol v1). An attachment above
# this is listed in the manifest with ``oversize: true`` and is not
# downloadable in v1. This is a serving policy, not a structural limit.
AUTONET_MAX_ATTACHMENT_BYTES = 16 * 1024 * 1024 * 1024
_NOTE_VIEWER_DIR = Path(__file__).resolve().parent / "relay_viewer"
#: Build output, NOT a source file. Generated on demand and gitignored.
#: It used to be committed, which made the generated artifact look like the
#: real thing -- it is the one that is a megabyte, full of working code, and
#: named in this module -- while its source looked like a stub with four
#: marker comments. Three commits duly edited the output and not the
#: template, so the template silently stopped producing the artifact and a
#: rebuild reverted a shipped feature. Nothing to edit, nothing to diverge.
_NOTE_VIEWER = _NOTE_VIEWER_DIR / ".build" / "note-viewer.html"
_NOTE_VIEWER_TEMPLATE = _NOTE_VIEWER_DIR / "note-viewer.template.html"
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


def _resolve_mission(target_uuid: str):
    """One composed screen, from Mission Control's own compose function.

    This branch stays a plain producer dispatch: a grant carries a
    target_type and the types genuinely resolve differently (design pins
    one revision, present follows a stable design, note assembles an
    artifact). What it must NOT do is know how a mission document is
    built -- that lives in the owning module, and the same function
    serves the dashboard path, so the two surfaces cannot drift.

    Unlike design (pinned to the exact approved revision, see
    _resolve_design's docstring), this always composes the CURRENT
    revision on every open, matching note's reload-shows-latest
    behavior: a mission page reflects the live state of the work, not a
    frozen snapshot from whenever the link was approved.
    """
    from tools.dashboard.plugins.mission_control import compose

    document = compose.compose_screen(target_uuid, framed=True)
    if document is None:
        return None
    return {"kind": "mission", "viewer": document}


_GRAPH_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(graph://([^)]+)\)")
# raw_sha256 is a SHA-256 of the file bytes: 64 lowercase hex chars (wire
# protocol v1). It is load-bearing for the client's whole-file verification
# and resume identity, so a non-canonical value is refused, never coerced.
_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")
# A manifest display name is bounded; consumers re-sanitize before any
# filesystem use (the same-origin parent owns path-safe naming).
_MANIFEST_NAME_MAX = 255


class _ArtifactTooLarge(ValueError):
    pass


_NOTE_VIEWER_CACHE: bytes | None = None


def _note_viewer_bytes() -> bytes:
    """The note viewer, built on demand from its template.

    Rebuilt whenever the template or a vendored asset is newer than the
    output, so an edit to the source is picked up without a build step in
    anyone's deploy path.
    """
    global _NOTE_VIEWER_CACHE
    from tools.dashboard.scripts import build_relay_note_viewer as builder

    sources = [_NOTE_VIEWER_TEMPLATE, *builder.vendor_files()]
    newest = max(path.stat().st_mtime for path in sources)
    if (
        _NOTE_VIEWER_CACHE is None
        or not _NOTE_VIEWER.exists()
        or _NOTE_VIEWER.stat().st_mtime < newest
    ):
        _NOTE_VIEWER.parent.mkdir(parents=True, exist_ok=True)
        _NOTE_VIEWER.write_text(builder.build())
        _NOTE_VIEWER_CACHE = _NOTE_VIEWER.read_bytes()
    return _NOTE_VIEWER_CACHE


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

    # The note's content-bound attachment slots are the single membership set
    # for BOTH inline image bundling and the download manifest. Using the row
    # source_id would be wrong for bytes deduplicated across notes into one
    # row: a note that shares an image with another note keeps that other
    # note's source_id, so a source_id check would drop its own inline image.
    slot_attachments = graph_ops.note_slot_attachments(
        source_id, org=org, peers=[]
    )
    slot_ids = {
        att["id"] for att in slot_attachments if isinstance(att.get("id"), str)
    }
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
            canonical_ref = attachment.get("id") if attachment else None
            if (
                attachment
                and canonical_ref in slot_ids
                and isinstance(mime, str)
                and mime.startswith("image/")
            ):
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

    # Metadata-only manifest of the note's downloadable attachments. Membership
    # is the note's content-bound attachment slots (never the row source_id,
    # which is shared for deduplicated bytes), so a note that shares bytes with
    # another note still offers its own attachments. No bytes are carried here;
    # on-demand fetch (wire protocol v1) streams them over the channel.
    attachments: list[dict] = []
    for att in slot_attachments:
        ref = att.get("id")
        if not isinstance(ref, str) or not ref:
            continue
        raw_sha256 = att.get("hash")
        size = att.get("size_bytes")
        # Fail closed: an attachment we cannot describe with a canonical
        # raw_sha256 and a real byte size is omitted rather than offered with
        # an unverifiable hash or a coerced size (bool excluded — it is an int
        # subclass but never a valid size).
        if not (isinstance(raw_sha256, str) and _SHA256_HEX_RE.fullmatch(raw_sha256)):
            continue
        if type(size) is not int or size < 0:
            continue
        name = att.get("filename")
        mime = att.get("mime_type")
        attachments.append({
            "ref": ref,
            "name": name[:_MANIFEST_NAME_MAX] if isinstance(name, str) else "",
            "mime": mime if isinstance(mime, str) and mime else "application/octet-stream",
            "raw_sha256": raw_sha256,
            "total_size": size,
            "oversize": size > AUTONET_MAX_ATTACHMENT_BYTES,
        })
    # The manifest is metadata carried in the artifact header, not the
    # size-capped body; it does not count toward AUTONET_MAX_ARTIFACT_BYTES
    # (which bounds body bytes). Its size is bounded by the note's own
    # attachment count.

    title = source.get("title") or "Note"
    if not isinstance(title, str):
        title = "Note"
    # Generic parts, not a note-shaped `content` block. Only the note viewer
    # knows that ref "note" carries a title or that ref "markdown" is
    # markdown -- which is what lets note change its own content contract
    # without a registry deploy.
    note_parts = [
        {
            "ref": "note",
            "mime": "application/json",
            "bytes": json.dumps(
                {"title": title[:AUTONET_MAX_TITLE_CHARS]}, ensure_ascii=False
            ).encode("utf-8"),
        },
        {"ref": "markdown", "mime": "text/markdown", "bytes": markdown.encode("utf-8")},
        *parts_by_ref.values(),
    ]
    return {
        "kind": "note",
        "viewer": viewer,
        "parts": note_parts,
        "attachments": attachments,
    }


def _serialize_artifact(artifact: dict) -> tuple[dict, bytes]:
    """Serialize one resolved artifact into the v1 part-addressed body."""
    if not isinstance(artifact, dict):
        raise TypeError("artifact must be an object")
    kind = artifact.get("kind")
    if kind not in ("note", "design", "present", "mission"):
        raise ValueError("unsupported artifact kind")
    if not set(artifact).issubset({"kind", "viewer", "parts", "attachments", "branding"}):
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

    # Generic parts: the serializer lays out bytes and records in-bounds
    # slices. It never learns what a ref means.
    parts = artifact.get("parts")
    if parts is not None:
        if not isinstance(parts, list):
            raise ValueError("parts must be a list")
        part_headers = []
        seen_refs = set()
        for part in parts:
            if not isinstance(part, dict) or not set(part).issubset({"ref", "mime", "bytes"}):
                raise ValueError("invalid artifact part")
            ref, mime, part_bytes = part.get("ref"), part.get("mime"), part.get("bytes")
            if (
                not isinstance(ref, str) or not ref or ref in seen_refs
                or not isinstance(mime, str) or not mime
                or not isinstance(part_bytes, bytes)
            ):
                raise ValueError("invalid artifact part")
            seen_refs.add(ref)
            part_headers.append({
                "ref": ref, "mime": mime, "offset": cursor, "length": len(part_bytes),
            })
            chunks.append(part_bytes)
            cursor += len(part_bytes)
        header["parts"] = part_headers

    # The attachment manifest is TOP LEVEL: the host activates its download
    # controller from the manifest's presence, not from a kind check.
    manifest = artifact.get("attachments")
    if manifest is not None:
        if not isinstance(manifest, list):
            raise ValueError("invalid attachment manifest")
        manifest_headers = []
        manifest_refs = set()
        for entry in manifest:
            if not isinstance(entry, dict) or not set(entry).issubset({
                "ref", "name", "mime", "raw_sha256", "total_size", "oversize",
            }):
                raise ValueError("invalid attachment manifest entry")
            ref = entry.get("ref")
            name = entry.get("name")
            mime = entry.get("mime")
            raw_sha256 = entry.get("raw_sha256")
            total_size = entry.get("total_size")
            oversize = entry.get("oversize")
            if (
                not isinstance(ref, str) or not ref or ref in manifest_refs
                or not isinstance(name, str) or len(name) > _MANIFEST_NAME_MAX
                or not isinstance(mime, str) or not mime
                or not isinstance(raw_sha256, str)
                or not _SHA256_HEX_RE.fullmatch(raw_sha256)
                or type(total_size) is not int or total_size < 0
                or not isinstance(oversize, bool)
            ):
                raise ValueError("invalid attachment manifest entry")
            manifest_refs.add(ref)
            manifest_headers.append({
                "ref": ref, "name": name, "mime": mime,
                "raw_sha256": raw_sha256, "total_size": total_size,
                "oversize": oversize,
            })
        header["attachments"] = manifest_headers

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
        if target_type == "mission":
            return _resolve_mission(target_uuid)
        # file is deliberately deferred from the rich-render v1 grammar.
    except Exception:
        return None  # resolver errors serve nothing, not stack traces
    return None


# ── the connector handler (the C4 seam) ───────────────────────


#: Ops an ``org:join`` grant serves (auto-4d6qm) — the membership claim
#: protocol over the same E2E viewer channel every share link uses. The
#: relay routes opaque frames; the claim (persona, profile, credential,
#: and the BEARER TOKEN) is channel ciphertext end-to-end to this node.
JOIN_OPS = ("context", "submit", "status")


def _claim_service():
    """The transport-agnostic claim service (auto-v3db2), imported lazily.

    Lazy so this dispatch lands before the service does: until then a
    join channel simply serves the uniform refusal, exactly as an
    unresolvable target would.
    """
    from tools.dashboard import claim_service

    return claim_service


def _serve_join(grant: dict, org: str | None, request: dict) -> bytes:
    """One ``org:join`` channel request → the claim_service envelope.

    The grant's ``invite_ref`` is the invite locator (a join channel is
    scoped to exactly one invitation), so a client cannot steer this
    channel at another invite: a submitted event naming a different
    ``invite_ref`` is refused here as defence in depth — the fold is
    authoritative regardless.
    """
    invite_ref = grant.get("invite_ref")
    if not isinstance(invite_ref, str):
        return REFUSED  # schema guarantees it on org:join; re-checked (I9)
    try:
        service = _claim_service()
    except Exception:
        return REFUSED  # service not present yet / import fault: serve nothing
    op = request["op"]
    try:
        if op == "context":
            result = service.context(org, invite_ref)
        elif op == "submit":
            wire = request.get("event")
            if not isinstance(wire, str) or not wire:
                return BAD_REQUEST
            raw = wire.encode("utf-8")
            try:
                payload_ref = json.loads(raw)["payload"]["invite_ref"]
            except (ValueError, KeyError, TypeError):
                return BAD_REQUEST
            if payload_ref != invite_ref:
                return REFUSED  # channel is scoped to its own invitation
            result = service.submit(org, raw)
        else:  # status
            persona_pub = request.get("persona_pub")
            if not isinstance(persona_pub, str) or not persona_pub:
                return BAD_REQUEST
            result = service.status(org, invite_ref, persona_pub)
    except Exception:
        return REFUSED  # service faults serve nothing, not stack traces
    try:
        return canonical_json({"v": 1, **result}) + b"\n"
    except Exception:
        return REFUSED


#: Ops any write-capable grant serves beyond fetch/head (auto-u0kxw). The
#: relay knows nothing about what a write MEANS for any given
#: target_type -- only how to reach the module that does (mirrors
#: resolve_target()'s read-side dispatch and _claim_service()'s
#: opaque-handoff shape for org:join). Same one op for every writable
#: target_type; new target_types add a dispatch entry, not new relay code.
WRITE_OPS = ("write",)

#: target_types allowed to accept writes at all. Disabled by default --
#: a target_type must be listed here explicitly to accept a write op, so
#: a viewer can never stuff data down a channel nothing is listening on.
#: Checked before the dispatch below is even consulted.
WRITE_ENABLED_TARGET_TYPES = frozenset({"mission"})


def _write_dispatch(target_type: str):
    """target_type → the module-owned async write handler, or None if
    this target_type doesn't accept writes. Lazy, same reason
    _claim_service() is lazy: a target_type gains write support without
    this dispatch needing to import it before it exists.
    """
    if target_type not in WRITE_ENABLED_TARGET_TYPES:
        return None
    if target_type == "mission":
        from tools.dashboard.plugins.mission_control.entrypoints import api as mc_api

        return mc_api.handle_relay_write
    return None


async def _serve_write(token: str, org: str | None, request: dict, clock) -> bytes:
    """One ``write`` request → the target_type-owned handler's envelope.

    The relay's only jobs: resolve the grant, resolve the identity this
    channel is allowed to write AS (from the grant's own bound
    ``meta.participant_id`` — never a value the client supplies), and
    forward the opaque ``body`` to whichever module owns writes for this
    grant's target_type. It never interprets ``body`` itself, so there is
    no field in it for a client to smuggle a different identity into.
    """
    grant = await asyncio.to_thread(check_grant, token, org=org, now=clock())
    if grant is None:
        return REFUSED
    handler = _write_dispatch(grant["target_type"])
    if handler is None:
        return REFUSED  # this target_type doesn't accept writes at all
    meta = grant.get("meta") or {}
    identity = meta.get("participant_id")
    if not isinstance(identity, str) or not identity:
        return REFUSED  # no bound identity: nothing to write as
    body = request.get("body")
    if not isinstance(body, dict):
        return BAD_REQUEST
    try:
        result = await handler(identity, grant["target_uuid"], body)
    except Exception:
        return REFUSED  # handler faults serve nothing, not stack traces
    if result is None:
        return BAD_REQUEST  # the handler rejected this body on its own terms
    try:
        return _app_json({"v": 1, "status": "ok", **result}) + b"\n"
    except Exception:
        return REFUSED


def _app_json(payload: dict) -> bytes:
    """Serialize an application read/write response.

    Deliberately NOT ``canonical_json``: that encoding exists for bytes
    something signs or hashes, and it forbids floats precisely because
    float repr is not canonical across languages. These envelopes are
    neither signed nor hashed, and Mission Control's payloads carry epoch
    float timestamps (``created_at``, ``answered_at``) -- passing them
    through canonical_json raises, and the guest gets a uniform refusal
    instead of their own question back. Caught by the end-to-end
    acceptance test, not by unit tests whose stub payloads were int-only.

    The artifact header, ``head``, and the join protocol keep using
    canonical_json: that is their established on-wire contract.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


#: The read mirror of WRITE_OPS (auto-t2lz1). A mission's own page is
#: authored against same-origin dashboard HTTP, none of which exists on
#: the relay's origin; this is the channel path those reads take instead.
#: Same discipline as the write side: the relay learns nothing about what
#: is being read, only which module owns reads for this target_type.
READ_OPS = ("read",)

#: target_types allowed to serve reads at all. Default-off, same reason
#: WRITE_ENABLED_TARGET_TYPES is.
READ_ENABLED_TARGET_TYPES = frozenset({"mission"})


def _read_dispatch(target_type: str):
    """target_type → the module-owned async read handler, or None."""
    if target_type not in READ_ENABLED_TARGET_TYPES:
        return None
    if target_type == "mission":
        from tools.dashboard.plugins.mission_control.entrypoints import api as mc_api

        return mc_api.handle_relay_read
    return None


async def _serve_read(token: str, org: str | None, request: dict, clock) -> bytes:
    """One ``read`` request → the target_type-owned handler's envelope.

    Same dispatch shape as :func:`_serve_write` -- resolve the grant, hand
    an opaque ``body`` to whichever module owns reads for this target_type,
    never interpret it -- but NOT the same identity rule.

    A write needs a bound participant because it is attributed: the whole
    point is that the record says who asked. A READ does not. Holding the
    link is the authorisation, and requiring meta.participant_id here meant
    every anonymous link could fetch its first screen (that arrives with the
    artifact) and then silently fail at everything else -- navigation
    refused, questions refused -- while looking completely intact.

    An unbound channel reads as the empty identity. Handlers receive it and
    are free to refuse anything that genuinely needs to know who is asking.
    """
    grant = await asyncio.to_thread(check_grant, token, org=org, now=clock())
    if grant is None:
        return REFUSED
    handler = _read_dispatch(grant["target_type"])
    if handler is None:
        return REFUSED  # this target_type serves no reads over the channel
    meta = grant.get("meta") or {}
    identity = meta.get("participant_id")
    if not isinstance(identity, str):
        identity = ""
    body = request.get("body")
    if not isinstance(body, dict):
        return BAD_REQUEST
    try:
        result = await handler(identity, grant["target_uuid"], body)
    except Exception:
        return REFUSED  # handler faults serve nothing, not stack traces
    if result is None:
        return BAD_REQUEST  # the handler rejected this body on its own terms
    try:
        return _app_json({"v": 1, "status": "ok", **result}) + b"\n"
    except Exception:
        return REFUSED


#: Ops that hand a viewer the key to a fan-out stream (auto-albp6.8).
SUBSCRIBE_OPS = ("subscribe",)

#: target_types whose channels may subscribe to a live stream. Same
#: default-off discipline as WRITE_ENABLED_TARGET_TYPES: a target_type
#: not listed here gets the uniform refusal, so no stream key is ever
#: minted for content nobody publishes.
STREAM_ENABLED_TARGET_TYPES = frozenset({"mission"})

#: token -> 32-byte stream key, for the life of this process. NOT the
#: per-channel key: this one is shared by every viewer of a link, which
#: is exactly what lets one sealed frame be fanned out by the relay
#: instead of re-sealed per viewer. It is minted on first subscribe,
#: never sent to the relay, and never appears in a published frame.
#: Revocation needs no key machinery: check_grant runs BEFORE the key is
#: handed over, so a revoked token never obtains it, and a viewer already
#: holding it simply receives no further frames.
_STREAM_KEYS: dict[str, bytes] = {}


def _stream_key(token: str) -> bytes:
    key = _STREAM_KEYS.get(token)
    if key is None:
        key = secrets.token_bytes(32)
        _STREAM_KEYS[token] = key
    return key


def forget_stream_key(token: str) -> None:
    """Drop a link's stream key -- called when a grant is revoked, so a
    later republish of the same token cannot reuse a key old viewers
    hold."""
    _STREAM_KEYS.pop(token, None)


async def _serve_subscribe(token: str, org: str | None, clock) -> bytes:
    """One ``subscribe`` request → this link's stream key, or the uniform
    refusal. The grant check happens FIRST and the key is handed over
    only after it passes."""
    grant = await asyncio.to_thread(check_grant, token, org=org, now=clock())
    if grant is None:
        return REFUSED
    if grant["target_type"] not in STREAM_ENABLED_TARGET_TYPES:
        return REFUSED  # byte-identical to the unknown-token refusal
    try:
        return canonical_json({
            "v": 1, "status": "ok", "stream_key": _stream_key(token).hex(),
        }) + b"\n"
    except Exception:
        return REFUSED


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

    def _join(token: str, request: dict) -> bytes:
        grant = check_grant(token, org=org, now=clock())
        if grant is None or grant["target_type"] != "org:join":
            return REFUSED  # a content token never serves the join protocol
        return _serve_join(grant, org, request)

    async def handler(token: str, message: bytes) -> bytes:
        try:
            request = json.loads(message)
        except ValueError:
            return BAD_REQUEST
        if not isinstance(request, dict):
            return BAD_REQUEST
        # Strict version match: bool is an int subclass and True == 1, so a
        # loose ``!= 1`` would accept ``{"v": true, ...}``. Require an int.
        if type(request.get("v")) is not int or request.get("v") != 1:
            return BAD_REQUEST
        op = request.get("op")
        # Settings + sqlite + file reads are blocking; keep them off the
        # tunnel's event loop so one slow lookup can't stall siblings.
        if op in JOIN_OPS:
            return await asyncio.to_thread(_join, token, request)
        if op in WRITE_OPS:
            return await _serve_write(token, org, request, clock)
        if op in READ_OPS:
            return await _serve_read(token, org, request, clock)
        if op in SUBSCRIBE_OPS:
            return await _serve_subscribe(token, org, clock)
        if op == "attachment.fetch":
            # A well-formed fetch returns a bounded async stream of body
            # frames (or a single error message); the connector streams it
            # record-by-record. Malformed shape is a hard bad request.
            from tools.dashboard import attachment_serving
            if not attachment_serving.valid_fetch_request(request):
                return BAD_REQUEST
            return attachment_serving.fetch_stream(
                token, request, org=org, now=clock
            )
        if op == "attachment.cancel":
            # The real cancel is the client not requesting the next window;
            # an explicit cancel is accepted and ends the exchange with no
            # response, leaving the channel up.
            from tools.dashboard import attachment_serving
            if not attachment_serving.valid_cancel_request(request):
                return BAD_REQUEST
            return None
        if op not in ("fetch", "head") or set(request) != {"v", "op"}:
            return BAD_REQUEST
        return await asyncio.to_thread(_serve, token, op == "head")

    return handler


async def _serve_control_listener(connector, ctl_path: str) -> None:
    """A loopback listener the dashboard drives to run D19 control ops on
    this connector's tunnel (register §3). One newline-delimited JSON
    request per connection — ``{auth, op, args}`` → the connector's reply
    (or ``{ok: false, error_kind: "no-tunnel", ...}`` when no tunnel is
    up). Bound to 127.0.0.1 only; a per-process random token gates it so a
    co-tenant process cannot drive the tunnel. The descriptor
    ``{port, auth}`` is written to *ctl_path* and removed on exit."""
    import json as _json
    import os as _os
    import secrets as _secrets

    auth = _secrets.token_hex(16)

    async def handle(reader, writer):
        try:
            line = await reader.readline()
            try:
                request = _json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                reply = {"ok": False, "error": "control request is not JSON"}
            else:
                if request.get("auth") != auth:
                    reply = {"ok": False, "error": "control auth rejected"}
                else:
                    try:
                        reply = await connector.control(
                            request.get("op"), request.get("args") or {})
                    except ConnectionError as exc:
                        reply = {"ok": False, "error_kind": "no-tunnel",
                                 "error": str(exc)}
            writer.write((_json.dumps(reply) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    tmp = ctl_path + ".tmp"
    with open(tmp, "w") as fh:
        _json.dump({"port": port, "auth": auth}, fh)
    _os.replace(tmp, ctl_path)
    try:
        async with server:
            await server.serve_forever()
    finally:
        with contextlib.suppress(OSError):
            _os.remove(ctl_path)


def _default_dashboard_url() -> str:
    """Where this connector reaches its own dashboard's event stream.

    Same resolution order as ``graph link publish``
    (``link_cmd._dash_base``) so one convention covers both, and so the
    supervisor -- which passes the whole environment through to the
    subprocess -- needs no new argument to enable live push.
    """
    import os as _os

    return (
        _os.environ.get("AUTONOMY_DASHBOARD")
        or _os.environ.get("GRAPH_API")
        or "https://localhost:8080"
    )


async def _run_connector_with_control(connector, ctl_path: str | None,
                                      publish_task_factory=None) -> None:
    tasks = []
    if ctl_path is not None:
        tasks.append(asyncio.create_task(_serve_control_listener(connector, ctl_path)))
    if publish_task_factory is not None:
        tasks.append(asyncio.create_task(publish_task_factory()))
    try:
        await connector.run()
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


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
    parser.add_argument("--control-file", default=None,
                        help="path to write the loopback control descriptor "
                             "(enables D19 publish/revoke over this tunnel)")
    parser.add_argument("--min-backoff", type=float, default=0.2)
    parser.add_argument("--max-backoff", type=float, default=5.0)
    parser.add_argument("--dashboard-url", default=None,
                        help="dashboard base URL whose event stream feeds live "
                             "mission updates to open guest channels (auto-8npih). "
                             "Defaults to AUTONOMY_DASHBOARD / GRAPH_API / "
                             "https://localhost:8080 — the same resolution order "
                             "graph link publish uses. Pass --dashboard-url '' to "
                             "disable live push; serving is unaffected either way.")
    args = parser.parse_args()

    with open(args.key_file) as fh:
        key = KeyPair.from_private_hex(fh.read().strip())
    with open(args.cert_file) as fh:
        cert = DelegationCert.from_json(fh.read().strip())

    from tools.network.relaykit.connector import Publisher

    handler = make_grant_handler(args.graph_org)
    # One Publisher shared by both halves of live push: the connector
    # reports channel attach/detach into it, and the Mission Control
    # publish loop emits through it. Without --dashboard-url it is still
    # constructed and still tracks listeners, it just has nothing
    # publishing into it.
    publisher = Publisher()
    connector = TunnelConnector(
        args.relay, args.org, key, cert, handler,
        min_backoff=args.min_backoff, max_backoff=args.max_backoff,
        publisher=publisher,
    )
    # Live push is ON by default. It was opt-in behind an explicit
    # --dashboard-url, which made it unreachable in production: the
    # supervisor builds this argv itself (link_serving_supervisor.
    # _connector_command) and never passed the flag, so no deployed
    # connector could ever publish. Defaulting to the same resolution
    # order `graph link publish` already uses means the supervisor gets
    # it for free -- it passes the whole environment to the subprocess.
    dashboard_url = (
        args.dashboard_url
        if args.dashboard_url is not None
        else _default_dashboard_url()
    )
    publish_task_factory = None
    if dashboard_url:
        from tools.dashboard.plugins.mission_control import relay_publisher

        def publish_task_factory():
            return relay_publisher.run(
                publisher, dashboard_url=dashboard_url, org=args.graph_org,
            )

    asyncio.run(_run_connector_with_control(
        connector, args.control_file, publish_task_factory,
    ))


if __name__ == "__main__":
    # Run the CANONICAL module's main(), not this __main__ copy.
    #
    # `python -m tools.dashboard.link_serving` executes this file as the
    # module `__main__`. Anything that later does `from tools.dashboard
    # import link_serving` -- relay_publisher does, to seal frames --
    # imports a SECOND, independent module object. Module-level state is
    # then duplicated: `_serve_subscribe` hands the guest a key out of one
    # `_STREAM_KEYS`, the publisher seals with a different key out of the
    # other, and every pushed frame is undecryptable. Same process, two
    # modules -- which is why "seal and subscribe must share a process"
    # was necessary but not sufficient.
    from tools.dashboard.link_serving import main as _canonical_main

    _canonical_main()
