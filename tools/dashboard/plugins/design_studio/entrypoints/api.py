"""Design Studio plugin API."""
from __future__ import annotations

import logging
import os
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from tools.dashboard import api_auth

_CATALOG_CACHE_TTL_SECONDS = 10
_SCREENSHOT_CACHE_TTL_SECONDS = 10
logger = logging.getLogger(__name__)
_catalog_cache: dict[str, Any] = {"expires_at": 0.0, "series": None, "source_key": None}
_screenshot_cache: dict[str, Any] = {"expires_at": 0.0, "revision_ids": set()}


def _status_filter(raw: str | None) -> set[str] | None:
    if not raw or raw == "all":
        return None
    return {part.strip() for part in raw.split(",") if part.strip()}


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _sort_key(row: dict, sort: str) -> tuple:
    if sort == "created":
        return (row.get("first_created_at") or "", row.get("title") or "")
    if sort == "title":
        return (row.get("title") or "", row.get("latest_created_at") or "")
    if sort == "revisions":
        return (_coerce_int(row.get("revision_count")), row.get("latest_created_at") or "")
    return (row.get("latest_created_at") or "", row.get("title") or "")


def _matches_query(row: dict, query: str) -> bool:
    if not query:
        return True
    haystack = " ".join(str(row.get(k) or "") for k in (
        "design_id",
        "latest_revision_id",
        "title",
        "description",
        "creator_session_id",
        "creator_session_label",
    )).lower()
    return query.lower() in haystack


def _safe_revision_id(raw: str) -> str:
    rev_id = str(raw or "").strip()
    if not rev_id or rev_id in {".", ".."} or "/" in rev_id or "\\" in rev_id:
        return ""
    return rev_id


def _thumbnail_url(revision_id: str) -> str:
    rev_id = _safe_revision_id(revision_id)
    if rev_id and rev_id in _screenshot_revision_ids():
        return f"/api/design-studio/revisions/{rev_id}/thumbnail"
    return ""


def _clear_thumbnail_cache() -> None:
    _screenshot_cache["revision_ids"] = None
    _screenshot_cache["expires_at"] = 0.0


def _screenshot_revision_ids() -> set[str]:
    """Revisions with any catalog image: the headless composite
    (thumbnail.jpg) or the operator's in-browser capture (screenshot.png)."""
    if os.environ.get("DASHBOARD_MOCK"):
        return set()
    now = time.monotonic()
    cached = _screenshot_cache.get("revision_ids")
    if cached is not None and now < float(_screenshot_cache.get("expires_at") or 0):
        return set(cached)
    from tools.data_paths import DATA_ROOT

    base = DATA_ROOT / "experiments"
    revision_ids: set[str] = set()
    if base.exists():
        for pattern in ("*/thumbnail.jpg", "*/screenshot.png"):
            revision_ids.update(
                path.parent.name
                for path in base.glob(pattern)
                if _safe_revision_id(path.parent.name)
            )
    _screenshot_cache["revision_ids"] = revision_ids
    _screenshot_cache["expires_at"] = now + _SCREENSHOT_CACHE_TTL_SECONDS
    return set(revision_ids)


def _form_factor(revision_id: str) -> str:
    """desktop | mobile | both from the renderer's metadata, else ''."""
    if os.environ.get("DASHBOARD_MOCK"):
        return ""
    from tools.dashboard import design_thumbnails

    meta = design_thumbnails.read_meta(revision_id)
    return str((meta or {}).get("form_factor") or "")


def _mock_design_rows() -> list[dict]:
    from tools.dashboard.dao import mock as dao_mock

    rows = []
    for design in dao_mock._designs():
        variants = design.get("variants") or []
        rows.append({
            "id": design.get("id"),
            "design_id": design.get("design_id") or design.get("id"),
            "title": design.get("title") or "Untitled Design",
            "description": design.get("description") or "",
            "status": design.get("status") or "pending",
            "revision_seq": _coerce_int(design.get("revision_seq"), 1),
            "created_at": design.get("created_at") or "",
            "creator_session_id": design.get("creator_session_id") or "",
            "creator_session_label": design.get("creator_session_label") or "",
            "org": design.get("org"),
            "variant_count": len(variants),
            "has_fixture": bool(design.get("fixture")),
            "thumbnail_url": design.get("thumbnail_url") or "",
            "form_factor": design.get("form_factor") or "",
            "shared": bool(design.get("shared")),
        })
    return rows


def _sqlite_design_rows() -> list[dict]:
    from agents.design_db import _get_conn

    conn = _get_conn()
    try:
        variant_counts = Counter(
            row["revision_id"]
            for row in conn.execute("SELECT revision_id FROM revision_variants").fetchall()
        )
        rows = conn.execute("""\
            SELECT
              d.id,
              COALESCE(d.design_id, d.id) AS design_id,
              d.title,
              d.description,
              d.status,
              COALESCE(d.revision_seq, 1) AS revision_seq,
              d.created_at,
              d.creator_session_id,
              d.creator_session_label,
              d.org,
              CASE WHEN d.fixture IS NOT NULL AND d.fixture != '' THEN 1 ELSE 0 END AS has_fixture
            FROM designs d
        """).fetchall()
        out = []
        for row in rows:
            item = {k: row[k] for k in row.keys()}
            item["variant_count"] = variant_counts.get(item["id"], 0)
            out.append(item)
        return out
    finally:
        conn.close()


def _design_rows() -> list[dict]:
    if os.environ.get("DASHBOARD_MOCK"):
        return _mock_design_rows()
    return _sqlite_design_rows()


def _design_org(identifier: str) -> str | None:
    """The owning org for a design_id or revision id — a design's revisions share
    one org, so a non-null value is preferred when present."""
    if os.environ.get("DASHBOARD_MOCK"):
        for row in _design_rows():
            if (str(row.get("id")) == identifier
                    or str(row.get("design_id") or row.get("id")) == identifier):
                return row.get("org")
        return None
    from agents.design_db import _get_conn

    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT org FROM designs WHERE (id = ? OR design_id = ?)"
            " ORDER BY (org IS NULL) LIMIT 1",
            (identifier, identifier),
        ).fetchone()
        return row["org"] if row else None
    finally:
        conn.close()


def _clear_catalog_cache() -> None:
    _catalog_cache["series"] = None
    _catalog_cache["expires_at"] = 0.0
    _catalog_cache["source_key"] = None


def _set_design_series_status(design_id: str, status: str) -> dict | None:
    if status not in {"pending", "dismissed", "completed"}:
        raise ValueError("invalid status")
    if os.environ.get("DASHBOARD_MOCK"):
        rows = [
            row for row in _design_rows()
            if str(row.get("design_id") or row.get("id")) == design_id
            or str(row.get("id")) == design_id
        ]
        if not rows:
            return None
        updated = [dict(row, status=status) for row in rows]
        return _series_from_rows(updated)[0]

    from agents.design_db import _get_conn

    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT COALESCE(design_id, id) AS design_id FROM designs "
            "WHERE id = ? OR design_id = ? ORDER BY revision_seq DESC LIMIT 1",
            (design_id, design_id),
        ).fetchone()
        if not row:
            return None
        canonical = row["design_id"] or design_id
        conn.execute(
            "UPDATE designs SET status = ? "
            "WHERE id = ? OR COALESCE(design_id, id) = ?",
            (status, canonical, canonical),
        )
        conn.commit()
    finally:
        conn.close()
    _clear_catalog_cache()
    rows = [
        row for row in _design_rows()
        if str(row.get("design_id") or row.get("id")) == canonical
        or str(row.get("id")) == canonical
    ]
    return _series_from_rows(rows)[0] if rows else None


def _update_revision_metadata(
    revision_id: str,
    *,
    title: str | None = None,
    description: str | None = None,
) -> dict | None:
    rev_id = _safe_revision_id(revision_id)
    if not rev_id:
        return None
    updates: list[str] = []
    values: list[Any] = []
    if title is not None:
        updates.append("title = ?")
        values.append(title)
    if description is not None:
        updates.append("description = ?")
        values.append(description)
    if not updates:
        raise ValueError("no metadata fields")

    if os.environ.get("DASHBOARD_MOCK"):
        rows = [row for row in _design_rows() if str(row.get("id") or "") == rev_id]
        if not rows:
            return None
        row = dict(rows[0])
        if title is not None:
            row["title"] = title
        if description is not None:
            row["description"] = description
        return row

    from agents.design_db import _get_conn

    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM designs WHERE id = ?",
            (rev_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE designs SET " + ", ".join(updates) + " WHERE id = ?",
            (*values, rev_id),
        )
        conn.commit()
        updated = conn.execute("""\
            SELECT
              d.id,
              COALESCE(d.design_id, d.id) AS design_id,
              d.title,
              d.description,
              d.status,
              COALESCE(d.revision_seq, 1) AS revision_seq,
              d.created_at,
              d.creator_session_id,
              d.creator_session_label,
              CASE WHEN d.fixture IS NOT NULL AND d.fixture != '' THEN 1 ELSE 0 END AS has_fixture
            FROM designs d
            WHERE d.id = ?
        """, (rev_id,)).fetchone()
        if not updated:
            return None
        item = {k: updated[k] for k in updated.keys()}
        variant_count = conn.execute(
            "SELECT COUNT(*) FROM revision_variants WHERE revision_id = ?",
            (rev_id,),
        ).fetchone()[0]
        item["variant_count"] = int(variant_count or 0)
        item["thumbnail_url"] = _thumbnail_url(rev_id)
        return item
    finally:
        conn.close()


def _all_series() -> list[dict]:
    if os.environ.get("DASHBOARD_MOCK"):
        return _series_from_rows(_design_rows())
    now = time.monotonic()
    source_key = (id(_design_rows), id(_thumbnail_url))
    cached = _catalog_cache.get("series")
    if (cached is not None
            and _catalog_cache.get("source_key") == source_key
            and now < float(_catalog_cache.get("expires_at") or 0)):
        return [dict(row) for row in cached]
    series = _series_from_rows(_design_rows())
    _catalog_cache["series"] = [dict(row) for row in series]
    _catalog_cache["source_key"] = source_key
    _catalog_cache["expires_at"] = now + _CATALOG_CACHE_TTL_SECONDS
    return series


def _shared_ids_for_org(org: str | None) -> set[str]:
    """Design/revision ids an active link grant reaches, for one org."""
    if os.environ.get("DASHBOARD_MOCK") or not org:
        return set()
    try:
        from tools.dashboard import design_shares

        return design_shares.shared_design_ids(org)
    except Exception:
        logger.debug("design-studio: share state unavailable for org %r", org, exc_info=True)
        return set()


def _series_from_rows(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("design_id") or row.get("id"))].append(row)

    shared_by_org: dict[str, set[str]] = {}
    series = []
    for design_id, revisions in grouped.items():
        revisions.sort(key=lambda r: (
            _coerce_int(r.get("revision_seq"), 1),
            r.get("created_at") or "",
            r.get("id") or "",
        ))
        latest = revisions[-1]
        statuses = Counter(str(r.get("status") or "pending") for r in revisions)
        creators = [
            str(r.get("creator_session_id") or "")
            for r in revisions
            if r.get("creator_session_id")
        ]
        # A revision may omit creator metadata when it was appended through a
        # direct API client. Preserve the design's most recent real session
        # link, matching agents.design_db.get_design(), instead of making the
        # catalog presence and session-viewer return path disappear.
        linked_revision = next(
            (r for r in reversed(revisions) if r.get("creator_session_id")),
            {},
        )
        created_values = [str(r.get("created_at") or "") for r in revisions if r.get("created_at")]
        first_created = min(created_values) if created_values else ""
        latest_created = max(created_values) if created_values else ""
        thumbnail_url = str(latest.get("thumbnail_url") or "")
        thumbnail_revision_id = str(latest.get("id") or "") if thumbnail_url else ""
        if not thumbnail_url:
            for revision in reversed(revisions):
                thumbnail_url = str(revision.get("thumbnail_url") or _thumbnail_url(str(revision.get("id") or "")))
                if thumbnail_url:
                    thumbnail_revision_id = str(revision.get("id") or "")
                    break
        form_factor = ""
        if thumbnail_revision_id:
            thumb_row = next((r for r in revisions if str(r.get("id") or "") == thumbnail_revision_id), {})
            form_factor = str(thumb_row.get("form_factor") or "") or _form_factor(thumbnail_revision_id)
        design_org = next(
            (r.get("org") for r in reversed(revisions) if r.get("org")), None)
        share_org = design_org or "autonomy"
        if share_org not in shared_by_org:
            shared_by_org[share_org] = _shared_ids_for_org(share_org)
        shared = any(bool(r.get("shared")) for r in revisions) or bool(
            ({design_id} | {str(r.get("id") or "") for r in revisions}) & shared_by_org[share_org]
        )
        series.append({
            "design_id": design_id,
            "org": design_org,
            "latest_revision_id": latest.get("id"),
            "title": latest.get("title") or "Untitled Design",
            "description": latest.get("description") or "",
            "status": latest.get("status") or "pending",
            "status_counts": dict(statuses),
            "revision_count": len(revisions),
            "variant_count": sum(_coerce_int(r.get("variant_count")) for r in revisions),
            "latest_variant_count": _coerce_int(latest.get("variant_count")),
            "has_fixture": any(bool(r.get("has_fixture")) for r in revisions),
            "first_created_at": first_created,
            "latest_created_at": latest_created,
            "creator_session_id": linked_revision.get("creator_session_id") or "",
            "creator_session_label": linked_revision.get("creator_session_label") or "",
            "org": latest.get("org"),
            "creator_session_count": len(set(creators)),
            "thumbnail_url": thumbnail_url,
            "thumbnail_revision_id": thumbnail_revision_id,
            "form_factor": form_factor,
            "shared": shared,
        })
    return series


def _summarize(series: list[dict]) -> dict:
    statuses = Counter(str(row.get("status") or "pending") for row in series)
    return {
        "series": len(series),
        "revisions": sum(_coerce_int(row.get("revision_count")) for row in series),
        "variants": sum(_coerce_int(row.get("variant_count")) for row in series),
        "pending_series": statuses.get("pending", 0),
        "dismissed_series": statuses.get("dismissed", 0),
        "completed_series": statuses.get("completed", 0),
    }


async def list_designs(request: Request) -> JSONResponse:
    query = (request.query_params.get("q") or "").strip()
    statuses = _status_filter(request.query_params.get("status"))
    sort = request.query_params.get("sort") or "updated"
    direction = request.query_params.get("direction") or "desc"
    limit = max(1, min(_coerce_int(request.query_params.get("limit"), 250), 500))

    # Queries are also the lightweight reverse-lookup path used by the session
    # viewer; bypass the ten-second gallery cache so navigating from a freshly
    # linked design cannot briefly lose its return control.
    # Org-scope first (invariant 1): an org caller sees only its own org's
    # designs; the operator sees all. Everything below — summary counts included
    # — is computed over the caller's visible set, so no cross-org total leaks.
    base = _series_from_rows(_design_rows()) if query else _all_series()
    all_series = [
        row for row in base
        if not api_auth.caller_org_scope_hides(request, row.get("org"))
    ]
    filtered = [
        row for row in all_series
        if (statuses is None or row.get("status") in statuses)
        and _matches_query(row, query)
    ]
    reverse = direction != "asc"
    filtered.sort(key=lambda row: _sort_key(row, sort), reverse=reverse)

    return JSONResponse({
        "designs": filtered[:limit],
        "summary": _summarize(all_series),
        "filtered_count": len(filtered),
        "limit": limit,
    })


async def get_design_series(request: Request) -> JSONResponse:
    design_id = request.path_params["design_id"]
    rows = [
        row for row in _design_rows()
        if str(row.get("design_id") or row.get("id")) == design_id
        or str(row.get("id")) == design_id
    ]
    if not rows:
        return JSONResponse({"error": "not found"}, status_code=404)
    series = _series_from_rows(rows)[0]
    # Org-scope: a cross-org design is the same 404 as a nonexistent one.
    if api_auth.caller_org_scope_hides(request, series.get("org")):
        return JSONResponse({"error": "not found"}, status_code=404)
    revisions = sorted(rows, key=lambda r: (
        _coerce_int(r.get("revision_seq"), 1),
        r.get("created_at") or "",
        r.get("id") or "",
    ))
    series["revisions"] = revisions
    series["share"] = _share_state(series, revisions)
    return JSONResponse(series)


def _share_state(series: dict, revisions: list[dict]) -> dict:
    """Active link grants that reach this design — read model only."""
    if os.environ.get("DASHBOARD_MOCK"):
        return {"shared": False, "grants": []}
    try:
        from tools.dashboard import design_shares

        return design_shares.share_for_design(
            series.get("org") or "autonomy",
            str(series.get("design_id") or ""),
            [str(r.get("id") or "") for r in revisions],
        )
    except Exception:
        logger.exception("design-studio: share state unavailable")
        return {"shared": False, "grants": [], "error": "share state unavailable"}


async def get_revision_thumbnail(request: Request):
    revision_id = request.path_params["revision_id"]
    # Org-scope: another org's thumbnail is the same 404 as a missing one.
    if api_auth.caller_org_scope_hides(request, _design_org(revision_id)):
        return JSONResponse({"error": "thumbnail not found"}, status_code=404)
    from tools.dashboard import design_thumbnails

    path = design_thumbnails.thumbnail_path(revision_id)
    if not path or not path.is_file():
        return JSONResponse({"error": "thumbnail not found"}, status_code=404)
    media_type = "image/jpeg" if path.suffix == ".jpg" else "image/png"
    return FileResponse(path, media_type=media_type)


async def update_revision_metadata(request: Request) -> JSONResponse:
    revision_id = request.path_params["revision_id"]
    # Org-scope: refuse a cross-org revision as an indistinguishable 404 before
    # any read or write.
    if api_auth.caller_org_scope_hides(request, _design_org(revision_id)):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    allowed = {"title", "description"}
    unknown = set(body) - allowed
    if unknown:
        return JSONResponse(
            {"error": "unknown field(s)", "fields": sorted(unknown)},
            status_code=400,
        )
    if not any(key in body for key in allowed):
        return JSONResponse(
            {"error": "title or description is required"},
            status_code=400,
        )
    title = body.get("title") if "title" in body else None
    description = body.get("description") if "description" in body else None
    if title is not None:
        title = str(title).strip()
        if not title:
            return JSONResponse({"error": "title must not be blank"}, status_code=400)
    if description is not None:
        description = str(description).strip()
        if len(description) > 1000:
            return JSONResponse(
                {"error": "description exceeds 1000 characters"},
                status_code=400,
            )
    try:
        revision = _update_revision_metadata(
            revision_id,
            title=title,
            description=description,
        )
    except ValueError:
        return JSONResponse(
            {"error": "title or description is required"},
            status_code=400,
        )
    if revision is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    _clear_catalog_cache()
    try:
        from tools.dashboard.event_bus import event_bus
        await event_bus.broadcast(
            f"design:{revision.get('design_id') or revision_id}",
            {
                "revision_id": revision_id,
                "design_id": revision.get("design_id") or revision_id,
                "metadata_updated": True,
            },
        )
    except Exception:
        logger.exception("design-studio: failed to broadcast metadata update")
    return JSONResponse({"ok": True, "revision": revision})


async def update_design_status(request: Request) -> JSONResponse:
    design_id = request.path_params["design_id"]
    # Org-scope: refuse a cross-org design as an indistinguishable 404 before
    # any read or write.
    if api_auth.caller_org_scope_hides(request, _design_org(design_id)):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    status = str(body.get("status") or "").strip()
    if status not in {"pending", "dismissed", "completed"}:
        return JSONResponse({"error": "invalid status"}, status_code=400)
    try:
        series = _set_design_series_status(design_id, status)
    except ValueError:
        return JSONResponse({"error": "invalid status"}, status_code=400)
    if series is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    _clear_catalog_cache()
    try:
        from tools.dashboard.event_bus import event_bus
        await event_bus.broadcast(
            "plugin_badges",
            {"design_studio": {"badge": badge_counter()}},
        )
    except Exception:
        logger.exception("design-studio: failed to broadcast plugin badge update")
    return JSONResponse({"ok": True, "design": series})


async def list_shared_remote(request: Request) -> JSONResponse:
    """Designs shared WITH this dashboard's org(s) that live on another
    member's machine: link grants (replicated through org sync) whose target
    is not a local design. The gallery shows them as link-out tiles; the
    HTML never syncs, the auto.network link serves it."""
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"shares": []})
    from tools.dashboard import design_shares

    local_ids: set[str] = set()
    orgs: set[str] = set()
    for row in _design_rows():
        local_ids.add(str(row.get("id") or ""))
        local_ids.add(str(row.get("design_id") or row.get("id") or ""))
        if row.get("org"):
            orgs.add(str(row["org"]))
    scope = api_auth.organization_scope_from_request(request)
    orgs = {scope} if scope else (orgs | {"autonomy"})
    shares = []
    for org in sorted(orgs):
        for grant in design_shares.active_design_grants(org):
            if grant.get("target_uuid") in local_ids:
                continue
            shares.append(dict(grant, org=org))
    return JSONResponse({"shares": shares})


async def render_revision_thumbnail(request: Request) -> JSONResponse:
    """Queue a headless thumbnail render for one revision (no LLM)."""
    revision_id = request.path_params["revision_id"]
    if api_auth.caller_org_scope_hides(request, _design_org(revision_id)):
        return JSONResponse({"error": "not found"}, status_code=404)
    if not _safe_revision_id(revision_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    from tools.dashboard import design_thumbnails

    queued = design_thumbnails.queue.enqueue(revision_id)
    status = design_thumbnails.queue.status()
    if not status["available"]:
        return JSONResponse(
            {"error": "thumbnail renderer unavailable: agent-browser is not installed on this host",
             "status": status},
            status_code=503,
        )
    if not status["running"]:
        return JSONResponse(
            {"error": "thumbnail renderer is not running", "status": status},
            status_code=503,
        )
    return JSONResponse({"ok": True, "queued": queued, "status": status}, status_code=202)


async def upload_thumbnail_artifacts(request: Request) -> JSONResponse:
    """Store thumbnails rendered somewhere with a browser (a session
    container running ``design_thumbnails --remote``) when this host has
    none. Body: ``{"files": {name: base64}, "meta": {...}}``."""
    revision_id = request.path_params["revision_id"]
    denied = api_auth.require_authenticated_api_caller(request)
    if denied is not None:
        return denied
    if api_auth.caller_org_scope_hides(request, _design_org(revision_id)):
        return JSONResponse({"error": "not found"}, status_code=404)
    if not _safe_revision_id(revision_id) or _design_org(revision_id) is None and not _revision_exists(revision_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    import base64

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    raw_files = body.get("files") if isinstance(body, dict) else None
    meta = body.get("meta") if isinstance(body, dict) else None
    if not isinstance(raw_files, dict) or not isinstance(meta, dict):
        return JSONResponse({"error": "files and meta are required"}, status_code=400)
    files: dict[str, bytes] = {}
    try:
        for name, encoded in raw_files.items():
            files[str(name)] = base64.b64decode(str(encoded), validate=True)
    except Exception:
        return JSONResponse({"error": "files must be base64"}, status_code=400)
    from tools.dashboard import design_thumbnails

    try:
        stored = design_thumbnails.write_artifacts(revision_id, files, meta)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    _clear_catalog_cache()
    _clear_thumbnail_cache()
    try:
        from tools.dashboard.event_bus import event_bus
        await event_bus.broadcast(
            f"design:{stored.get('design_id') or revision_id}",
            {"revision_id": revision_id, "design_id": stored.get("design_id") or revision_id,
             "thumbnail_updated": True, "form_factor": stored.get("form_factor")},
        )
    except Exception:
        logger.exception("design-studio: failed to broadcast thumbnail update")
    return JSONResponse({"ok": True, "meta": stored})


def _revision_exists(revision_id: str) -> bool:
    return any(str(row.get("id") or "") == revision_id for row in _design_rows())


async def render_status(request: Request) -> JSONResponse:
    from tools.dashboard import design_thumbnails

    return JSONResponse(design_thumbnails.queue.status())


async def render_backfill(request: Request) -> JSONResponse:
    """Queue every design whose latest revision has no composed thumbnail."""
    denied = api_auth.require_global_api_authority(request)
    if denied is not None:
        return denied
    from tools.dashboard import design_thumbnails

    queued = design_thumbnails.queue.enqueue_missing()
    return JSONResponse({"ok": True, "queued": queued, "status": design_thumbnails.queue.status()}, status_code=202)


def badge_counter() -> int:
    """Designs a live session is working on right now — not the backlog.
    The pending count was a permanent three-digit badge; this is zero when
    nothing is being designed."""
    if os.environ.get("DASHBOARD_MOCK"):
        try:
            return _summarize(_all_series()).get("pending_series", 0)
        except Exception:
            return 0
    try:
        from tools.dashboard import design_lifecycle

        return design_lifecycle.live_design_count()
    except Exception:
        return 0


_SESSION_ICON = (
    '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" '
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" '
    'aria-hidden="true">'
    '<path d="M7 3.75H3.75V7M13 3.75h3.25V7M16.25 13v3.25H13M7 16.25H3.75V13"></path>'
    '<circle cx="10" cy="10" r="2.15"></circle>'
    '</svg>'
)


def session_contributions(session_ids: list[str], request: Request) -> dict[str, list[dict]]:
    """Contribute one latest linked-design action per requested session."""
    requested = set(session_ids)
    result: dict[str, list[dict]] = {session_id: [] for session_id in session_ids}
    if not requested:
        return result
    designs = sorted(
        _series_from_rows(_design_rows()),
        key=lambda row: (row.get("latest_created_at") or "", row.get("design_id") or ""),
        reverse=True,
    )
    claimed: set[str] = set()
    for design in designs:
        session_id = str(design.get("creator_session_id") or "")
        if (
            session_id not in requested
            or session_id in claimed
            or api_auth.caller_org_scope_hides(request, design.get("org"))
        ):
            continue
        revision_id = str(design.get("latest_revision_id") or "")
        if not revision_id:
            continue
        title = str(design.get("title") or "Untitled Design")
        result[session_id].append({
            "id": f"design:{design.get('design_id') or revision_id}",
            "kind": "action",
            "label": "Design Studio",
            "title": f"Open Design Studio: {title}",
            "href": (
                f"/design/{revision_id}?from_session="
                f"{quote(session_id, safe='')}"
            ),
            "icon_svg": _SESSION_ICON,
            "accent": "#818cf8",
        })
        claimed.add(session_id)
    return result


routes: list[Route] = [
    Route("/api/design-studio/designs", list_designs, methods=["GET"]),
    Route("/api/design-studio/designs/{design_id}/status", update_design_status, methods=["POST"]),
    Route("/api/design-studio/designs/{design_id}", get_design_series, methods=["GET"]),
    Route("/api/design-studio/revisions/{revision_id}/metadata", update_revision_metadata, methods=["PATCH", "PUT"]),
    Route("/api/design-studio/revisions/{revision_id}/thumbnail", get_revision_thumbnail, methods=["GET"]),
    Route("/api/design-studio/revisions/{revision_id}/render", render_revision_thumbnail, methods=["POST"]),
    Route("/api/design-studio/revisions/{revision_id}/thumbnail-artifacts", upload_thumbnail_artifacts, methods=["PUT"]),
    Route("/api/design-studio/render/status", render_status, methods=["GET"]),
    Route("/api/design-studio/shared", list_shared_remote, methods=["GET"]),
    Route("/api/design-studio/render/backfill", render_backfill, methods=["POST"]),
]
