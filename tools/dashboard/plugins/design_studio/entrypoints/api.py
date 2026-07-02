"""Design Studio plugin API."""
from __future__ import annotations

import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route


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
    from agents.design_db import REPO_ROOT

    base = (REPO_ROOT / "data" / "experiments").resolve()
    candidate = (base / rev_id / "screenshot.png").resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate


def _thumbnail_url(revision_id: str) -> str:
    path = _screenshot_path(revision_id)
    if path and path.is_file():
        return f"/api/design-studio/revisions/{revision_id}/thumbnail"
    return ""


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
              CASE WHEN d.fixture IS NOT NULL AND d.fixture != '' THEN 1 ELSE 0 END AS has_fixture,
              COUNT(rv.id) AS variant_count
            FROM designs d
            LEFT JOIN revision_variants rv ON rv.revision_id = d.id
            GROUP BY d.id
        """).fetchall()
        return [{k: row[k] for k in row.keys()} for row in rows]
    finally:
        conn.close()


def _design_rows() -> list[dict]:
    if os.environ.get("DASHBOARD_MOCK"):
        return _mock_design_rows()
    return _sqlite_design_rows()


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
            "creator_session_id": latest.get("creator_session_id") or "",
            "creator_session_label": latest.get("creator_session_label") or "",
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

    all_series = _series_from_rows(_design_rows())
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


def badge_counter() -> int:
    try:
        return _summarize(_series_from_rows(_design_rows())).get("pending_series", 0)
    except Exception:
        return 0


routes: list[Route] = [
    Route("/api/design-studio/designs", list_designs, methods=["GET"]),
    Route("/api/design-studio/designs/{design_id}", get_design_series, methods=["GET"]),
    Route("/api/design-studio/revisions/{revision_id}/thumbnail", get_revision_thumbnail, methods=["GET"]),
]
