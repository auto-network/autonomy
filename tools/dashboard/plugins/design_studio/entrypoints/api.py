"""Design Studio plugin API."""
from __future__ import annotations

import logging
import os
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

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


def _screenshot_path(revision_id: str) -> Path | None:
    rev_id = _safe_revision_id(revision_id)
    if not rev_id:
        return None
    from tools.data_paths import DATA_ROOT

    base = (DATA_ROOT / "experiments").resolve()
    candidate = (base / rev_id / "screenshot.png").resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate


def _thumbnail_url(revision_id: str) -> str:
    rev_id = _safe_revision_id(revision_id)
    if rev_id and rev_id in _screenshot_revision_ids():
        return f"/api/design-studio/revisions/{rev_id}/thumbnail"
    return ""


def _screenshot_revision_ids() -> set[str]:
    if os.environ.get("DASHBOARD_MOCK"):
        return set()
    now = time.monotonic()
    cached = _screenshot_cache.get("revision_ids")
    if cached is not None and now < float(_screenshot_cache.get("expires_at") or 0):
        return set(cached)
    from tools.data_paths import DATA_ROOT

    base = DATA_ROOT / "experiments"
    revision_ids = {
        path.parent.name
        for path in base.glob("*/screenshot.png")
        if _safe_revision_id(path.parent.name)
    } if base.exists() else set()
    _screenshot_cache["revision_ids"] = revision_ids
    _screenshot_cache["expires_at"] = now + _SCREENSHOT_CACHE_TTL_SECONDS
    return set(revision_ids)


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
            "variant_count": len(variants),
            "has_fixture": bool(design.get("fixture")),
            "thumbnail_url": design.get("thumbnail_url") or "",
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


def _series_from_rows(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("design_id") or row.get("id"))].append(row)

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
        if not thumbnail_url:
            for revision in reversed(revisions):
                thumbnail_url = str(revision.get("thumbnail_url") or _thumbnail_url(str(revision.get("id") or "")))
                if thumbnail_url:
                    break
        series.append({
            "design_id": design_id,
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
            "creator_session_count": len(set(creators)),
            "thumbnail_url": thumbnail_url,
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

    # Queries are also the lightweight reverse-lookup path used by the
    # session viewer. Bypass the ten-second gallery cache so navigating from a
    # freshly linked design cannot briefly lose its return control.
    all_series = _series_from_rows(_design_rows()) if query else _all_series()
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
    revisions = sorted(rows, key=lambda r: (
        _coerce_int(r.get("revision_seq"), 1),
        r.get("created_at") or "",
        r.get("id") or "",
    ))
    series["revisions"] = revisions
    return JSONResponse(series)


async def get_revision_thumbnail(request: Request):
    revision_id = request.path_params["revision_id"]
    path = _screenshot_path(revision_id)
    if not path or not path.is_file():
        return JSONResponse({"error": "thumbnail not found"}, status_code=404)
    return FileResponse(path, media_type="image/png")


async def update_revision_metadata(request: Request) -> JSONResponse:
    revision_id = request.path_params["revision_id"]
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


def badge_counter() -> int:
    try:
        return _summarize(_all_series()).get("pending_series", 0)
    except Exception:
        return 0


routes: list[Route] = [
    Route("/api/design-studio/designs", list_designs, methods=["GET"]),
    Route("/api/design-studio/designs/{design_id}/status", update_design_status, methods=["POST"]),
    Route("/api/design-studio/designs/{design_id}", get_design_series, methods=["GET"]),
    Route("/api/design-studio/revisions/{revision_id}/metadata", update_revision_metadata, methods=["PATCH", "PUT"]),
    Route("/api/design-studio/revisions/{revision_id}/thumbnail", get_revision_thumbnail, methods=["GET"]),
]
