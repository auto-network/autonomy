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
import logging
import mimetypes
import os
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


def _resolve_mission(target_uuid: str, grant: dict | None = None):
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

    # A grant with no bound participant cannot write -- _serve_write refuses
    # it, correctly, because an attributed record has nobody to attribute to.
    # The DOCUMENT is told, so the controls that cannot work are disabled with
    # a reason instead of being offered and failing on tap.
    meta = (grant or {}).get("meta") or {}
    may_write = bool(meta.get("participant_id"))
    # The same bound identity the write path attributes to, handed to the
    # composer so the screen can tell a question this guest asked from one
    # asked of them. Display only -- _serve_write re-reads it from the grant
    # and never from anything the page sends back.
    document = compose.compose_screen(
        target_uuid, framed=True, may_write=may_write,
        viewer=meta.get("participant_id"))
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


def _org_brand_for_invite(org: str | None) -> dict | None:
    """The org's OWN identity for the invite/join context (auto-r7kk4), served
    by the org over the E2E join channel — never registry-served, so the
    registry never learns org identity.

    Returns ``{org_name, org_color, org_description?, org_icon?}`` for a viewer's
    verified-org header. org_description (the byline) and org_icon are optional.

    org_icon is ALWAYS a bounded ``data:image/*;base64`` URI or ABSENT — NEVER a
    remote URL. A remote favicon fetched on the invite-view page would leak each
    visitor's IP/UA to the favicon host (a tracking vector on the exact page
    where we promise a registry-blind posture) and would fail the invite page's
    ``img-src data:`` CSP. So :func:`_resolve_org_brand`'s remote ``favicon_url``
    branch is deliberately dropped here (degrade to name/initial).
    """
    brand = _resolve_org_brand(org)
    if not brand or not isinstance(brand.get("name"), str):
        return None
    fields: dict = {"org_name": brand["name"], "org_color": brand["color"]}
    try:
        from tools.dashboard.org_identity import resolve_org_identity

        byline = (resolve_org_identity(org) or {}).get("byline")
        if isinstance(byline, str) and byline:
            fields["org_description"] = byline
    except Exception:
        pass
    icon = brand.get("favicon")
    # ONLY the bounded {mime, bytes} form becomes an icon; a remote favicon_url
    # is intentionally never emitted here (registry-blind + data:-only CSP).
    if (
        isinstance(icon, dict)
        and isinstance(icon.get("bytes"), bytes)
        and isinstance(icon.get("mime"), str)
    ):
        b64 = base64.b64encode(icon["bytes"]).decode("ascii")
        fields["org_icon"] = f"data:{icon['mime']};base64,{b64}"
    return fields


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
            return _resolve_mission(target_uuid, grant)
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

# Fleet enrollment is a separate authority domain from organization claims.
# It shares RelayKit's established channel but has its own grant type and
# operation vocabulary.
FLEET_JOIN_OPS = ("fleet.request", "fleet.resume")
FLEET_SYNC_OPS = ("fleet.sync.pull",)


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
            # r7kk4: enrich with the org's OWN identity (name/description/icon/
            # color) so the invitee's verified-org header is delivered by the
            # org over this E2E channel, never registry-served. Icon is a
            # bounded data: URI or absent — never a remote URL.
            brand = _org_brand_for_invite(org)
            if brand:
                result = {**result, **brand}
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
logger = logging.getLogger(__name__)
from tools.dashboard.log_throttle import StateChangeLogger

#: The dashboard hands one of its own bus events to this connector over the
#: loopback control listener it already drives. Not a registry control frame
#: and never reaching the relay: it is local delivery of news that a consumer
#: in THIS process then seals and fans out itself, because a guest's stream
#: key lives here and a frame sealed anywhere else is undecryptable to them.
#:
#: The proxy carries an opaque (topic, data) to a named consumer and knows
#: nothing else. What an event MEANS, which guests should see it and what a
#: frame looks like all belong to the consumer, exactly as reads and writes
#: already belong to the module that owns their target_type.
EVENT_OP = "event"

#: Consumers of that proxy, by name. Lazy in the same way and for the same
#: reason as the read and write dispatches below: a consumer is imported
#: only when an event is actually routed to it.
EVENT_CONSUMERS = ("mission",)

#: Backoff for proxy_events_to_connectors()'s own retry loop. Module-level
#: so a test can shrink it instead of eating real wall-clock time waiting
#: out a production-sized backoff.
EVENT_PROXY_INITIAL_BACKOFF_S = 1.0
EVENT_PROXY_MAX_BACKOFF_S = 60.0


def _event_dispatch(consumer: str):
    """consumer name -> the module owning that consumer's events, or None."""
    if consumer not in EVENT_CONSUMERS:
        return None
    if consumer == "mission":
        from tools.dashboard.plugins.mission_control import relay_publisher

        return relay_publisher
    return None


def event_routes() -> dict:
    """topic -> the consumers wanting it, for the dashboard-side proxy.

    Asking each consumer what it subscribes to, rather than the proxy
    holding a list, is what keeps the pipe ignorant of its traffic.
    """
    routes: dict = {}
    for consumer in EVENT_CONSUMERS:
        module = _event_dispatch(consumer)
        for topic in getattr(module, "EVENT_TOPICS", ()):
            routes.setdefault(topic, []).append(consumer)
    return routes

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

    def _fleet_join(token: str, request: dict, channel_state: dict) -> bytes:
        grant = check_grant(token, org=org, now=clock())
        if grant is None or grant["target_type"] != "fleet:join":
            return REFUSED
        try:
            from tools.dashboard import fleet_enrollment_service

            result = fleet_enrollment_service.handle_request(
                grant,
                request,
                channel_state=channel_state,
                now_ms=int(clock() * 1000),
            )
            return canonical_json(result) + b"\n"
        except Exception:
            return REFUSED

    async def _fleet_sync(token: str, request: dict):
        from tools.network.fleet_route import FLEET_SYNC_TARGET_TYPES

        grant = await asyncio.to_thread(check_grant, token, org=org, now=clock())
        if grant is None or grant["target_type"] not in FLEET_SYNC_TARGET_TYPES:
            return REFUSED
        try:
            from tools.network.fleet_relay_sync import connector_runtime

            return await connector_runtime.handle(token, request)
        except Exception as exc:
            logger.warning("fleet relay sync request refused", exc_info=True)
            # The caller here already proved it holds a valid fleet:join
            # grant (checked above) -- unlike REFUSED elsewhere in this
            # module, there is no anonymous-prober oracle risk in telling
            # a roster-authenticated peer why the pull was refused (locked
            # machine, expired delegation, etc.) instead of a bare
            # "unavailable" it cannot distinguish from an unknown token.
            return canonical_json({
                "v": 1, "kind": "fleet.server-error", "error": str(exc),
            }) + b"\n"

    async def _handle(
        token: str, message: bytes, channel_state: dict
    ) -> bytes:
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
        if op in FLEET_JOIN_OPS:
            return await asyncio.to_thread(
                _fleet_join, token, request, channel_state
            )
        if op in FLEET_SYNC_OPS:
            return await _fleet_sync(token, request)
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

    async def handler(token: str, message: bytes) -> bytes:
        return await _handle(token, message, {})

    async def for_channel(_token: str):
        channel_state: dict = {}

        async def channel_handler(token: str, message: bytes) -> bytes:
            return await _handle(token, message, channel_state)

        return channel_handler

    handler.for_channel = for_channel

    return handler


def make_ice_grant_handler(
    graph_org: str | None,
    *,
    channel_org: str | None = None,
    configuration_provider,
    key,
    channel_cert,
    peer_runtime,
    signaling_capacity,
    publisher=None,
    modules=None,
    now=None,
):
    """Add one bounded ICE capability to the ordinary grant handler.

    The credential issuer is injected because it belongs to the separate TURN
    credential work.  Policy is not injected and is never read from the
    viewer: it is derived only from this dashboard's verified, browser-signed
    local grant. ``meta.ice_policy`` is immutable per grant; omission means the
    operator-approved ``direct_allowed`` default. Changing the policy means
    publishing and signing a new grant, not mutating a live one.
    """
    from tools.network.relaykit.aiortc_responder import AiortcResponderFactory
    from tools.network.relaykit.ice_handler import IceRoutingHandler

    # The production grant path always carries live questions/presence through
    # Publisher.  Allowing None here creates a direct request/response path
    # that silently loses the live feed; isolated responder tests may still
    # omit it by constructing AiortcResponder directly.
    if publisher is None:
        raise ValueError("ICE grant handler requires a Publisher")

    clock = now or time.time
    application_handler = make_grant_handler(graph_org, now=clock)
    responder_org = channel_org if channel_org is not None else graph_org

    async def valid_grant(token: str) -> bool:
        grant = await asyncio.to_thread(
            check_grant, token, org=graph_org, now=clock()
        )
        return grant is not None

    async def policy_provider(token: str):
        grant = await asyncio.to_thread(
            check_grant, token, org=graph_org, now=clock()
        )
        if grant is None:
            return None
        meta = grant.get("meta") or {}
        return meta.get("ice_policy", "direct_allowed")

    def responder_factory_provider(token: str):
        return AiortcResponderFactory(
            token=token,
            owner=peer_runtime,
            key=key,
            cert=channel_cert,
            org=responder_org,
            application_handler=application_handler,
            authorization_check=valid_grant,
            publisher=publisher,
            modules=modules,
        )

    return IceRoutingHandler(
        application_handler,
        policy_provider=policy_provider,
        configuration_provider=configuration_provider,
        responder_factory_provider=responder_factory_provider,
        capacity=signaling_capacity,
    )


def _turn_configuration_from_control(reply):
    """Convert the Registry's authenticated ``issue-turn`` reply.

    The signaling state machine performs the authoritative value validation;
    this boundary only refuses a failed/malformed control reply and converts
    its JSON containers to the immutable in-process type it expects.
    """
    from tools.network.relaykit.ice_signaling import IceConfiguration

    if not isinstance(reply, dict) or reply.get("ok") is not True:
        raise ConnectionError("TURN credential issuance was refused")
    ice_servers = reply.get("ice_servers")
    expires_at = reply.get("expires_at")
    if not isinstance(ice_servers, list) or not all(
        isinstance(server, dict) for server in ice_servers
    ):
        raise ConnectionError("TURN credential issuer returned a malformed reply")
    return IceConfiguration(
        ice_servers=tuple(ice_servers),
        expires_at=expires_at,
    )


def _make_ice_serving_connector(
    relay,
    org,
    key,
    cert,
    channel_cert,
    graph_org,
    publisher,
    *,
    min_backoff,
    max_backoff,
    machine_key=None,
    connector_factory=None,
):
    """Construct the production connector with the existing ICE handler."""
    from tools.dashboard.service_gateway_stream import LocalCaddyStreamHandler
    from tools.network.relaykit.aiortc_responder import PeerRuntime
    from tools.network.relaykit.connector import TunnelConnector
    from tools.network.relaykit.ice_signaling import IceCapacity

    factory = connector_factory or TunnelConnector
    connector = None

    async def configuration_provider(_token, _policy):
        if connector is None:  # construction invariant, never viewer-driven
            raise ConnectionError("serving connector is not ready")
        reply = await connector.control("issue-turn", {})
        return _turn_configuration_from_control(reply)

    handler = make_ice_grant_handler(
        graph_org,
        channel_org=org,
        configuration_provider=configuration_provider,
        key=key,
        channel_cert=channel_cert,
        peer_runtime=PeerRuntime(64, per_token_limit=4),
        signaling_capacity=IceCapacity(64, per_token_limit=2),
        publisher=publisher,
    )
    stream_kwargs = {}
    if machine_key is not None:
        stream_kwargs = {
            "machine_key": machine_key,
            "caps": ("host-lease/1", "tls-stream/1", "dns-01/1"),
            "stream_handler": LocalCaddyStreamHandler(graph_org),
        }
    # Per-link serving (graph://807b4e11-3e9): resolve the link's channel
    # signing key from the vault so the handshake is authenticated by that key
    # instead of the org-root serve cert. A link with no channel key (legacy,
    # or a cold vault at publish) resolves to None and serves the old way.
    def _link_key_for(token):
        from tools.dashboard.link_channel_key import (
            ChannelKeyUnavailable,
            channel_key_for,
        )
        try:
            return channel_key_for(token, graph_org)
        except ChannelKeyUnavailable:
            return None

    connector = factory(
        relay,
        org,
        key,
        cert,
        handler,
        channel_cert=channel_cert,
        min_backoff=min_backoff,
        max_backoff=max_backoff,
        publisher=publisher,
        link_key_for=_link_key_for,
        **stream_kwargs,
    )
    return connector



# ── the dashboard-side half of the event proxy ────────────────


async def proxy_events_to_connectors(bus, *, org=None, stop=None,
                                     control=None, max_pending: int = 256) -> None:
    """Carry this dashboard's own bus events to the connector holding the
    guest channels, over the loopback control listener it already drives.

    The dashboard cannot seal a frame: a guest's stream key lives in the
    connector's process, so news has to cross one process boundary no
    matter what. This crosses it privately. The alternative -- the
    connector subscribing back to the dashboard's public event stream --
    made a conversation between two processes on one machine the only part
    of that conversation that had to pass the public gate, and it broke,
    silently, the day that gate closed.

    KNOWS NOTHING ABOUT ITS TRAFFIC. Which topics matter and what an event
    means belong to the consumers; this asks them (``event_routes``) and
    forwards an opaque payload, exactly as reads and writes are owned by
    the module that owns their target_type.

    NEVER BLOCKS THE PRODUCER. The bus gives us our own queue, so a slow or
    absent connector cannot delay the request that emitted the event, and
    the control call is synchronous with its own timeout so it is made off
    the event loop entirely.

    SELF-SUPERVISING: this runs for the life of the process as a single
    fire-and-forget task (server.py has no retry of its own), so a failure
    here used to be permanent — one lost race between a hot reload and
    ``event_routes()``'s dynamic consumer import (observed live: the
    module's file mid-write by a concurrent commit) killed live guest
    delivery for the rest of the process's life, silently, while HTTP
    writes kept working untouched. Any failure — including that one — is
    now caught and retried with capped exponential backoff instead of
    ending the task. ``CancelledError`` is a ``BaseException``, not caught
    here, so shutdown's ``task.cancel()`` still stops this promptly.
    """
    if control is None:
        from tools.dashboard.link_serving_supervisor import control

    backoff = EVENT_PROXY_INITIAL_BACKOFF_S
    while stop is None or not stop.is_set():
        try:
            await _run_event_proxy_once(bus, org, stop, control, max_pending)
        except Exception:
            logger.error(
                "event proxy: attempt failed, retrying in %.1fs",
                backoff, exc_info=True,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, EVENT_PROXY_MAX_BACKOFF_S)
            continue
        # A clean return means either routes is permanently empty (no
        # consumers registered at all) or stop was set — neither is a
        # failure, so don't retry.
        return


def _wants(predicate, topic, data) -> bool:
    """A consumer filter must never take the proxy down: a raising filter
    counts as "wants it" so the connector's own filter still applies."""
    try:
        return bool(predicate(topic, data))
    except Exception:
        logger.debug("event filter raised; forwarding", exc_info=True)
        return True


async def _run_event_proxy_once(bus, org, stop, control, max_pending: int) -> None:
    """One attempt at :func:`proxy_events_to_connectors` — no retry of its
    own. Split out so the supervising loop above can restart a fresh
    subscription (a fresh ``event_routes()`` call, a fresh queue) rather
    than trying to resume whatever state a failed attempt left behind."""
    routes = event_routes()
    if not routes:
        return
    # A consumer may declare ``wants_event(topic, data) -> bool`` to filter
    # dashboard-side, BEFORE the control round-trip. The mission consumer
    # subscribes to ``setting.changed`` but only for one set_id; without the
    # filter every Settings write on the machine crossed to the connector
    # only to be discarded there.
    filters = {
        consumer: getattr(_event_dispatch(consumer), "wants_event", None)
        for consumers in routes.values() for consumer in consumers
    }
    backlog = StateChangeLogger(
        interval_s=60.0,
        summary="event proxy still behind ({repeats} event(s) dropped in the last {interval:.0f}s)",
    )
    queue = bus.subscribe()
    try:
        while stop is None or not stop.is_set():
            topic, data, _seq = await queue.get()
            consumers = routes.get(topic)
            if not consumers:
                continue
            consumers = [
                c for c in consumers
                if filters.get(c) is None or _wants(filters[c], topic, data)
            ]
            if not consumers:
                continue
            if queue.qsize() > max_pending:
                # A live update is best-effort by design: a guest recovers a
                # gap by refetching on its own channel, which is
                # authoritative. So drop rather than grow without bound
                # behind a tunnel that may be down for hours. Say "behind"
                # once, then a count per minute, and "caught up" once.
                backlog.emit(
                    logger, logging.WARNING, "event-proxy", "behind",
                    "event proxy is behind by %d events; dropping",
                    queue.qsize(),
                )
                continue
            backlog.emit(
                logger, logging.INFO, "event-proxy", "current",
                "event proxy caught up; forwarding again",
            )
            for consumer in consumers:
                try:
                    await asyncio.to_thread(
                        control, org, EVENT_OP,
                        {"consumer": consumer, "topic": topic,
                         "data": data, "org": org},
                    )
                except Exception:
                    # No connector, no tunnel, or it refused. The guest
                    # simply does not get this frame. Debug, because with no
                    # tunnel up this is every event and it is not news.
                    logger.debug(
                        "event proxy could not deliver to %s", consumer,
                        exc_info=True,
                    )
    finally:
        bus.unsubscribe(queue)

async def _dispatch_event(publisher, args: dict) -> dict:
    """Route one proxied bus event to its consumer.

    Mirrors :func:`_serve_write`: resolve the owner, hand over the opaque
    payload, interpret nothing. A consumer that faults serves nothing to
    the guest and never takes the connector down with it.
    """
    if publisher is None:
        return {"ok": False, "error": "this connector has no publisher"}
    module = _event_dispatch(args.get("consumer"))
    if module is None:
        return {"ok": False, "error": "no such event consumer"}
    data = args.get("data")
    try:
        sent = await module.publish_event(
            publisher, args.get("topic"), data if isinstance(data, dict) else {},
            org=args.get("org"),
        )
    except Exception:
        logger.warning("event consumer %s faulted", args.get("consumer"), exc_info=True)
        return {"ok": False, "error": "consumer faulted"}
    return {"ok": True, "sent": sent}


async def _serve_control_listener(connector, ctl_path: str,
                                  publisher=None) -> None:
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
    # Public, non-secret identity for this exact connector process.  The
    # Dashboard uses it to recognize a reconnect/relaunch and replay the full
    # Settings-derived hostname set once.  Do not use the control auth token as
    # an identity: it is authority and must remain private to the descriptor.
    instance = _secrets.token_hex(16)

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
                elif request.get("op") == EVENT_OP:
                    args = request.get("args") or {}
                    reply = await _dispatch_event(publisher, args)
                elif request.get("op") == "connector-status":
                    # Supervisor-local readiness probe.  This never becomes
                    # a registry control frame: it reports whether the
                    # connector has completed the tunnel hello right now.
                    from tools.network.fleet_relay_sync import connector_runtime
                    from tools.network import build_version

                    reply = {
                        "ok": True,
                        "serving": connector.connected.is_set(),
                        "connector_instance": instance,
                        # Capabilities accepted by the registry for this live
                        # tunnel. Local consumers must gate optional control
                        # operations on negotiation instead of interpreting a
                        # uniform remote refusal as a capability signal.
                        "accepted_caps": list(connector.accepted_caps),
                        # Added live 2026-08-23 while diagnosing "locked for
                        # Fleet sync" persisting across an unlock that
                        # logged no error -- lets a caller ask this exact
                        # process directly whether fleet-runtime configure()
                        # ever actually landed here, instead of inferring it
                        # from dashboard-side logs alone.
                        "fleet_runtime_configured": connector_runtime.scheduler is not None,
                        # The commit THIS process loaded at import time, not
                        # what's on disk now -- see build_version.py. Added
                        # the same day, same debugging cycle, one hop later:
                        # a stale-code worker looks identical to a correctly
                        # -configured one on every check above this line.
                        "process_commit": build_version.PROCESS_COMMIT,
                        # The disk HEAD at process boot — the honest "which
                        # code generation is this process" answer (see
                        # _BOOT_COMMIT; process_commit can postdate stale
                        # imports). The supervisor adopts an incumbent only
                        # when this matches the current disk head.
                        "boot_commit": _BOOT_COMMIT,
                        # Live pull/blob streams right now. The supervisor
                        # DRAINS a stale incumbent (waits for zero, or a
                        # deadline) instead of severing mid-transfer — a
                        # first-contact fleet pull needs minutes in one
                        # connector generation (2026-09-06 merge churn).
                        "active_streams": connector_runtime.active_streams,
                        # Seconds since any stream last yielded a frame; the
                        # supervisor honors a lame duck only when this is
                        # recent (a stuck counter is not a stream).
                        "stream_activity_age_s": (
                            connector_runtime.stream_activity_age_s()
                        ),
                        # How many sync pulls this process has turned away while
                        # unarmed, and when the first was -- the profile sync
                        # flag's "764 requests refused since 8pm". Zero on a
                        # freshly armed process (configure() resets it).
                        "locked_refusals": connector_runtime.locked_refusals,
                        "locked_refusal_since": (
                            connector_runtime.first_locked_refusal_at
                        ),
                        # The direct (tailnet/LAN) listener THIS process has
                        # bound, or null: the fleet verdict's proof that the
                        # direct tier is up without an operator session.
                        "direct_listener": connector_runtime.direct_listener,
                    }
                elif request.get("op") == "serve-host":
                    # Dashboard-local desired-state seam. Unlike forwarding a
                    # one-shot host-register frame, serve_host records the
                    # reservation in the connector and its half-life keeper
                    # renews it across the full tunnel lifetime/reconnects.
                    args = request.get("args") or {}
                    if set(args) != {"reservation", "host"} or not all(
                        isinstance(args.get(field), str) and args[field]
                        for field in ("reservation", "host")
                    ):
                        reply = {"ok": False, "error": "invalid serve-host request"}
                    else:
                        reply = await connector.serve_host(
                            args["reservation"], args["host"]
                        )
                elif request.get("op") == "release-host":
                    args = request.get("args") or {}
                    if set(args) != {"reservation"} or not isinstance(
                        args.get("reservation"), str
                    ):
                        reply = {"ok": False, "error": "invalid release-host request"}
                    else:
                        reply = await connector.release_host(args["reservation"])
                elif request.get("op") == "fleet-runtime":
                    from tools.network.fleet_relay_sync import connector_runtime

                    try:
                        reply = await asyncio.to_thread(
                            connector_runtime.configure, request.get("args") or {}
                        )
                    except Exception as exc:
                        # Unlike the generic connector.control() branch below,
                        # this had no exception handling of its own -- any
                        # failure here fell through to the outer
                        # `except Exception: pass` and silently closed the
                        # connection with zero diagnostics on either side
                        # (the dashboard only ever saw "connector closed the
                        # control connection", never why). Report it instead.
                        reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
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


async def _run_connector_with_control(connector, ctl_path: str | None,
                                      publisher=None) -> None:
    tasks = []
    if ctl_path is not None:
        tasks.append(asyncio.create_task(
            _serve_control_listener(connector, ctl_path, publisher)))
    # The fleet direct listener (tailnet/LAN peers dial this process
    # directly, bypassing the relay) is bound and kept matched to the
    # fleet-direct row here, on the connector's loop.
    from tools.network.fleet_relay_sync import connector_runtime as _fleet_rt

    tasks.append(asyncio.create_task(
        _fleet_rt.direct_listener_loop(), name="fleet-direct-listener"))
    try:
        await connector.run()
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


#: The git HEAD on disk when THIS connector process booted. Captured at main()
#: entry — before the serving stack imports — so it names the code generation
#: this process actually loaded. PROCESS_COMMIT cannot serve this purpose: it is
#: captured at build_version's own (possibly late) import and can report a
#: NEWER commit than the modules this process runs (observed live 2026-09-06: a
#: pre-fix connector reporting the post-fix commit). The supervisor's adoption
#: check compares boot_commit against current disk head and replaces stale
#: survivors instead of adopting them.
_BOOT_COMMIT: str | None = None


#: CPU-gated stack sampler: every window, if this process burned more than
#: the threshold share of one core, dump every thread's Python stack to the
#: log. Names a long CPU-bound job that runs OUTSIDE a serve stream (where
#: the stream stall dump never arms) — tonight's activation/reconcile-class
#: burners were invisible for hours because py-spy needs SYS_PTRACE the
#: container lacks. Idle processes never dump.
CPU_SAMPLER_WINDOW_S = 30.0
CPU_SAMPLER_THRESHOLD = 0.8


def _start_cpu_stack_sampler() -> None:
    import faulthandler
    import sys
    import threading

    def run() -> None:
        last_cpu = time.process_time()
        last_wall = time.monotonic()
        while True:
            time.sleep(CPU_SAMPLER_WINDOW_S)
            cpu, wall = time.process_time(), time.monotonic()
            share = (cpu - last_cpu) / max(1e-9, wall - last_wall)
            last_cpu, last_wall = cpu, wall
            if share >= CPU_SAMPLER_THRESHOLD:
                logger.warning(
                    "cpu sampler: process at %.0f%% of one core over the last "
                    "%.0fs — dumping all thread stacks", share * 100,
                    CPU_SAMPLER_WINDOW_S,
                )
                sys.stderr.flush()
                faulthandler.dump_traceback(all_threads=True)
                sys.stderr.flush()

    threading.Thread(target=run, name="cpu-stack-sampler", daemon=True).start()


def main() -> None:
    # UTC-timestamped lines: this file is append-mode and shared across
    # connector generations, so without wall-clock stamps a post-incident
    # read cannot even tell which generation wrote a line (bit us live
    # 2026-09-06 diagnosing the 02d833fd build wedge).
    logging.basicConfig(
        format="%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logging.Formatter.converter = time.gmtime
    # The fleet-sync loggers' INFO lines ARE the operational record of this
    # process (pull succeeded, journal pruned, attach/reconcile/backfill
    # progress); third-party loggers stay at WARNING.
    for name in ("tools.network.fleet_sync", "tools.network.fleet_relay_sync",
                 "tools.network.fleet_sync_scheduler"):
        logging.getLogger(name).setLevel(logging.INFO)
    _start_cpu_stack_sampler()
    # `kill -USR1 <pid>` dumps every thread's Python stack into this log —
    # the on-demand answer to "what is this process doing" with no ptrace,
    # no sudo, no restart (the container lacks SYS_PTRACE for py-spy).
    import faulthandler
    import signal as _signal
    faulthandler.register(_signal.SIGUSR1, all_threads=True)
    global _BOOT_COMMIT
    from tools.network import build_version
    _BOOT_COMMIT = build_version.disk_head()
    # One banner per generation: the shared append-mode log needs each
    # process to self-identify (pid + code generation) at birth.
    logging.getLogger(__name__).warning(
        "connector starting pid=%d boot_commit=%s",
        os.getpid(), (_BOOT_COMMIT or "unknown")[:12],
    )
    from tools.network.idkit import DelegationCert, KeyPair

    parser = argparse.ArgumentParser(
        description="auto.network tunnel connector serving grant-gated targets (C4)"
    )
    parser.add_argument("--relay", required=True, help="relay base URL, e.g. wss://auto.network")
    parser.add_argument("--org", required=True, help="org UUID on the registry")
    parser.add_argument("--key-file", required=True, help="file holding the private key hex")
    parser.add_argument("--cert-file", required=True, help="file holding the cert wire JSON")
    parser.add_argument(
        "--channel-cert-file", required=False, default=None,
        help="identity-neutral cert used only in viewer SERVER_HELLO",
    )
    parser.add_argument("--graph-org", default=None,
                        help="dashboard org slug scoping the grant cache")
    parser.add_argument("--control-file", default=None,
                        help="path to write the loopback control descriptor "
                             "(enables D19 publish/revoke over this tunnel)")
    parser.add_argument("--min-backoff", type=float, default=0.2)
    parser.add_argument("--max-backoff", type=float, default=5.0)
    args = parser.parse_args()

    # Defense in depth for manual launches and alternate process managers.
    # The normal production path has already checked this in
    # link_serving_supervisor, but this directly runnable module must not be a
    # bypass for the temporary Fleet-wide single-server assignment. Legacy
    # installations that have not initialized Fleet remain allowed by the
    # helper itself.
    from tools.network import fleet_tunnel_server

    tunnel_server = fleet_tunnel_server.state()
    if not tunnel_server.allowed:
        selected = (
            f"; selected machine is {tunnel_server.selected_machine_id}"
            if tunnel_server.selected_machine_id is not None
            else ""
        )
        parser.error(
            "this Fleet machine may not serve auto.network tunnels "
            f"({tunnel_server.reason}{selected})"
        )

    with open(args.key_file) as fh:
        key = KeyPair.from_private_hex(fh.read().strip())
    with open(args.cert_file) as fh:
        cert = DelegationCert.from_json(fh.read().strip())
    channel_cert = None
    if args.channel_cert_file:
        with open(args.channel_cert_file) as fh:
            channel_cert = DelegationCert.from_json(fh.read().strip())

    # Re-arm the Fleet runtime from the warm ramfs cache BEFORE serving: a
    # connector that restarts (a crash, or the watchdog respawning it after its
    # dashboard died) held its sync credential only in memory and came back
    # locked, refusing every pull until a human unlocked. The warm cache — the
    # same ramfs treatment the delegate key gets — lets it re-arm itself with
    # nobody present. Keyed by --org, so only the connector that was armed
    # re-arms; the personal fleet connector is the one that ever holds it.
    from tools.network.fleet_relay_sync import (
        ConnectorFleetRuntime,
        FleetRuntimeWarmCache,
        connector_runtime,
    )

    with contextlib.suppress(Exception):
        connector_runtime.attach_warm_cache(FleetRuntimeWarmCache(args.org))
        connector_runtime.rearm_from_cache()

    # The enrolled machine signer is node-wide, while the Fleet runtime cache
    # that carries it belongs to the PERSONAL tunnel. Organization connectors
    # previously looked only at their own org-keyed cache, found nothing, and
    # silently emitted hello v1 with zero capabilities. Reuse the already-
    # verified personal warm credential solely to recover its machine signer;
    # do not invent or persist another machine key.
    # auto-e2ufw: prefer the per-org SERVING key for the tunnel hello when the
    # browser delivered one; fall back to the fleet machine key (transitional)
    # otherwise. This changes only which machine identity the serving hello
    # presents — reachability node:announce is unaffected (it never uses this
    # key). The registry's serving-domain verify accepts either under the
    # per-org allow-set (empty set -> transitional accept).
    machine_key = (
        connector_runtime.serving_machine_key or connector_runtime.machine_key
    )
    if machine_key is None:
        with contextlib.suppress(Exception):
            from tools.dashboard.link_approvals import _load_binding

            personal_binding, _error = _load_binding(None)
            personal_org_uuid = (
                personal_binding.get("org_uuid") if personal_binding else None
            )
            if personal_org_uuid:
                personal_runtime = ConnectorFleetRuntime()
                personal_runtime.attach_warm_cache(
                    FleetRuntimeWarmCache(personal_org_uuid)
                )
                personal_runtime.rearm_from_cache()
                machine_key = (
                    personal_runtime.serving_machine_key
                    or personal_runtime.machine_key
                )

    from tools.network.relaykit.connector import Publisher

    # One Publisher shared by both halves of live push: the connector
    # reports channel attach/detach into it, and a mission event arriving
    # over the control listener emits through it.
    publisher = Publisher()

    # The Registry already owns TURN credential issuance on this connector's
    # authenticated org tunnel. Feed that existing control result into the
    # existing ICE handler; neither the viewer nor the grant chooses it.
    connector = _make_ice_serving_connector(
        args.relay,
        args.org,
        key=key,
        cert=cert,
        channel_cert=channel_cert,
        graph_org=args.graph_org,
        publisher=publisher,
        min_backoff=args.min_backoff, max_backoff=args.max_backoff,
        machine_key=machine_key,
    )
    # Live push is ON by default and needs no configuration: the dashboard
    # delivers each event over the control listener below, so there is no
    # address to resolve, no stream to subscribe to and no credential to
    # hold. This connector used to fetch the news back out of the
    # dashboard's own public event stream, which made a private exchange
    # between two processes on one machine the only part of that exchange
    # that had to pass the public gate -- and it broke silently the day
    # that gate closed.
    asyncio.run(_run_connector_with_control(
        connector, args.control_file, publisher,
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
