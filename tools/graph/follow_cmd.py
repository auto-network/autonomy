"""``graph follow add|list|status|remove`` — the follower's local management
of the organizations this node mirrors read-only.

A follow is a per-operator declaration (``autonomy.org.follow#1``, home
``personal``) plus a local read-only mirror ``data/orgs/<slug>.db`` the
credential-free follow loop fills from the org's standing ``org:follow`` public
link (design of record graph://5f2f5a49-00d §10.4). These verbs are inherently
local — they create and drop mirror files on this node — so they act on the
local stores directly rather than through the dashboard API.

``graph follow publish`` (the org side) lives in ``link_cmd``; this module is
the *follower* side.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def _fail(msg: str):
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(1)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _split_link_url(url: str) -> tuple[str, str, str, str]:
    """Split a published ``org:follow`` link into
    ``(registry_base, rendezvous, token, link_pub)``.

    ``url`` is ``https://host[:port]/l/<token>#<fragment_key>``. The fragment is
    the link's public key (the whole authentication); the base + ``/l/<token>``
    is the rendezvous; the base is the registry/relay origin the envelope is
    fetched from.
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        _fail(f"not a link URL: {url!r}")
    link_pub = parts.fragment.strip()
    if not link_pub:
        _fail(
            "the link URL carries no #fragment key — paste it exactly as "
            "published, bare, with nothing selected around it"
        )
    path = parts.path.rstrip("/")
    token = path.rsplit("/", 1)[-1] if path else ""
    if not token:
        _fail(f"the link URL names no token: {url!r}")
    registry_base = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    rendezvous = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    return registry_base, rendezvous, token, link_pub


def _fetch_envelope(registry_base: str, token: str, *, timeout: float = 30.0) -> dict:
    """GET the link envelope (``/v1/links/{token}/envelope``): the org uuid,
    target type and meta the follow row needs. Module-level so tests swap it."""
    url = f"{registry_base.rstrip('/')}/v1/links/{token}/envelope"
    ctx = ssl.create_default_context()
    if os.environ.get("AUTONOMY_INSECURE_TLS"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        _fail(f"link envelope fetch failed ({exc.code}): the link may be "
              f"unknown, expired or revoked")
    except Exception as exc:  # noqa: BLE001
        _fail(f"could not reach the link's registry at {registry_base}: {exc}")
    try:
        return json.loads(body)
    except ValueError:
        _fail("link envelope response was not JSON")


def cmd_follow_add(args) -> None:
    """graph follow add <url>

    Follow an organization's public surface. Fetches the link envelope for the
    org's identity, writes the ``autonomy.org.follow#1`` row, creates the local
    read-only mirror, and lets the follow loop pull it (it fills within the
    poll cadence)."""
    from tools.graph import settings_ops
    from tools.graph.schemas.org_follow import (
        ORG_FOLLOW_REVISION, ORG_FOLLOW_SET_ID,
    )

    url = args.target
    registry_base, rendezvous, token, link_pub = _split_link_url(url)
    envelope = _fetch_envelope(registry_base, token)
    if envelope.get("target_type") != "org:follow":
        _fail(
            f"that link is a {envelope.get('target_type')!r} link, not an "
            f"org:follow link — only org:follow links can be followed"
        )
    org_uuid = envelope.get("org")
    meta = envelope.get("meta") or {}
    slug = meta.get("org") if isinstance(meta, dict) else None
    if not org_uuid or not isinstance(org_uuid, str):
        _fail("link envelope carried no org uuid")
    if not slug or not isinstance(slug, str):
        _fail("link envelope carried no org slug (meta.org)")

    payload = {
        "org_uuid": org_uuid,
        "rendezvous": rendezvous,
        "link_pub": link_pub,
        "registry_url": registry_base,
        "enabled": True,
        "added_at": _now_iso(),
    }
    try:
        settings_ops.add_setting(
            ORG_FOLLOW_SET_ID, ORG_FOLLOW_REVISION, slug, payload, org=None,
        )
    except Exception as exc:  # noqa: BLE001
        _fail(f"could not write the follow row: {exc}")

    # Create the mirror now so it is readable immediately; the follow loop
    # fills it. A slug collision (a local org of a different id) is refused
    # here with both ids named.
    try:
        from tools.network.fleet_sync_scheduler import materialize_follow_scopes

        created = materialize_follow_scopes()
    except Exception as exc:  # noqa: BLE001
        created = []
        print(f"  (mirror creation deferred: {exc})", file=sys.stderr)

    print(f"✓ following {slug} ({org_uuid})")
    if slug in created:
        print(f"  created mirror data/orgs/{slug}.db — the follow loop will "
              f"fill it from the org's public surface")
    else:
        print("  mirror already present; the follow loop keeps it current")


def _follow_rows() -> list:
    from tools.graph import settings_ops
    from tools.graph.schemas.org_follow import (
        ORG_FOLLOW_REVISION, ORG_FOLLOW_SET_ID,
    )

    members = settings_ops.read_set(
        ORG_FOLLOW_SET_ID, org=None, peers=[],
        target_revision=ORG_FOLLOW_REVISION,
    )
    return sorted(members, key=lambda m: m.key)


def cmd_follow_list(args) -> None:
    """graph follow list — the organizations this node follows."""
    rows = _follow_rows()
    if not rows:
        print("  not following any organization")
        return
    for m in rows:
        p = m.payload or {}
        state = "enabled" if p.get("enabled") else "disabled"
        print(f"  {m.key}  [{state}]")
        print(f"    org_uuid: {p.get('org_uuid')}")
        print(f"    rendezvous: {p.get('rendezvous')}")
        print(f"    added: {p.get('added_at')}")


def _mirror_summary(slug: str) -> dict:
    """The mirror's cursor, public row count and last status, read-only."""
    from tools.graph.db import GraphDB, _org_db_path
    from tools.network.fleet_sync import follow_mirror

    path = _org_db_path(slug)
    out: dict = {"exists": Path(path).exists()}
    if not out["exists"]:
        return out
    try:
        db = GraphDB(path, mode="ro")
    except Exception:
        return out
    try:
        conn = db.conn
        cur = follow_mirror.read_follow_cursor(conn)
        out["cursor"] = cur[1] if cur else None
        out["genesis"] = cur[0] if cur else None
        out["status"] = follow_mirror.read_follow_status(conn)
        try:
            out["rows"] = conn.execute(
                "SELECT count(*) FROM sources WHERE publication_state IN "
                "('published','canonical')"
            ).fetchone()[0]
        except Exception:
            out["rows"] = None
    finally:
        db.close()
    return out


def cmd_follow_status(args) -> None:
    """graph follow status — last pull, row count and projection per follow."""
    rows = _follow_rows()
    if not rows:
        print("  not following any organization")
        return
    for m in rows:
        p = m.payload or {}
        summary = _mirror_summary(m.key)
        print(f"  {m.key}")
        if not summary.get("exists"):
            print("    mirror: not yet created")
            continue
        rows_n = summary.get("rows")
        cursor = summary.get("cursor")
        print(f"    projection: public   rows: {rows_n}   cursor: {cursor}")
        status = summary.get("status")
        if not status or not status.get("at"):
            print("    last pull: never")
            continue
        at = status.get("at")
        when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at)) if at else "?"
        outcome = status.get("outcome")
        if outcome == "ok":
            print(f"    last pull: ok at {when}")
        elif outcome == "unreachable":
            print(f"    unreachable since {when}: {status.get('error') or ''}")
        else:
            retry = status.get("retry_after")
            retry_when = (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(retry))
                if retry else "?"
            )
            print(f"    last refusal: {status.get('refusal') or outcome} at "
                  f"{when}; retry after {retry_when}")


def cmd_follow_remove(args) -> None:
    """graph follow remove <slug> [--keep-mirror] — stop following an org."""
    from tools.graph import settings_ops
    from tools.graph.db import GraphDB, _org_db_path

    slug = args.target
    rows = _follow_rows()
    member = next((m for m in rows if m.key == slug), None)
    if member is None:
        _fail(f"not following {slug!r}")
    try:
        settings_ops.deprecate_setting(member.id, org=None)
    except Exception as exc:  # noqa: BLE001
        _fail(f"could not drop the follow row: {exc}")

    if getattr(args, "keep_mirror", False):
        print(f"✓ stopped following {slug} (mirror kept, read-only)")
        return

    path = Path(_org_db_path(slug))
    with_removed = False
    if path.exists():
        try:
            GraphDB.close_pooled_path(path)
        except Exception:
            pass
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(path) + suffix)
            try:
                candidate.unlink()
                with_removed = with_removed or (suffix == "")
            except FileNotFoundError:
                continue
            except Exception as exc:  # noqa: BLE001
                print(f"  (could not remove {candidate.name}: {exc})",
                      file=sys.stderr)
    print(f"✓ stopped following {slug}" + (
        " and removed its mirror" if with_removed else ""))
