"""Global, deliberately low-value file dropbox for operator screenshots."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
import uuid

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from tools.data_paths import resolve_store
from tools.dashboard import api_auth
from tools.dashboard.approval_service import ApprovalServiceError


MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_ENROLLMENT_BODY_BYTES = 8 * 1024
MAX_LIST_LIMIT = 100
MAX_PENDING_ENROLLMENTS = 8
ENROLLMENTS_PER_MINUTE = 10
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_PREFIX_RE = re.compile(r"^[0-9a-f]{8,32}$")
_enrollment_attempts: dict[str, list[float]] = {}


def _root() -> Path:
    return resolve_store("dropbox")


def _safe_original_filename(value: str | None) -> str | None:
    if not value:
        return None
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch >= " " and ch != "\x7f").strip()
    return name[:200] or None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def store_object(
    data: bytes, *, content_type: str, original_filename: str | None,
) -> dict:
    item_id = uuid.uuid4().hex
    root = _root()
    relative = f"objects/{item_id}"
    object_path = root / relative
    metadata = {
        "id": item_id,
        "original_filename": _safe_original_filename(original_filename),
        "content_type": content_type,
        "size": len(data),
        "created_at": _utc_now(),
        "storage_path": relative,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    _write_atomic(object_path, data)
    _write_atomic(
        root / "metadata" / f"{item_id}.json",
        (json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )
    return metadata


def _read_metadata(path: Path) -> dict | None:
    try:
        row = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return None
    item_id = row.get("id") if isinstance(row, dict) else None
    if not isinstance(item_id, str) or not _ID_RE.fullmatch(item_id):
        return None
    expected = _root() / "objects" / item_id
    if row.get("storage_path") != f"objects/{item_id}" or not expected.is_file():
        return None
    required = {"content_type", "size", "created_at", "sha256"}
    if not required.issubset(row):
        return None
    return row


def list_objects(limit: int) -> list[dict]:
    metadata_dir = _root() / "metadata"
    if not metadata_dir.is_dir():
        return []
    rows = [
        row for path in metadata_dir.glob("*.json")
        if (row := _read_metadata(path)) is not None
    ]
    rows.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
    return rows[:limit]


def get_object(identifier: str) -> tuple[dict, Path]:
    if not _PREFIX_RE.fullmatch(identifier):
        raise FileNotFoundError(identifier)
    metadata_dir = _root() / "metadata"
    matches = list(metadata_dir.glob(f"{identifier}*.json")) if metadata_dir.is_dir() else []
    if len(matches) != 1:
        if len(matches) > 1:
            raise ValueError("dropbox id prefix is ambiguous")
        raise FileNotFoundError(identifier)
    row = _read_metadata(matches[0])
    if row is None:
        raise FileNotFoundError(identifier)
    return row, _root() / row["storage_path"]


def _session_read_error(request: Request) -> JSONResponse | None:
    principal = api_auth.principal_from_request(request)
    if principal.kind in {
        api_auth.ApiPrincipalKind.LOCAL_SESSION,
        api_auth.ApiPrincipalKind.ORG_SESSION,
    }:
        return None
    return JSONResponse({"error": "authenticated Autonomy session required"}, status_code=401)


def _enrollment_rate_limited(request: Request) -> bool:
    now = time.monotonic()
    key = request.client.host if request.client else "unknown"
    recent = [t for t in _enrollment_attempts.get(key, []) if now - t < 60]
    if len(recent) >= ENROLLMENTS_PER_MINUTE:
        _enrollment_attempts[key] = recent
        return True
    recent.append(now)
    _enrollment_attempts[key] = recent
    return False


def _enrollment_desk():
    from tools.dashboard import attention_routes
    desk = attention_routes.approval_runtime().enrollment_desk
    if desk is None:
        raise RuntimeError("the enrollment desk is not composed")
    return desk


async def create_enrollment(request: Request) -> JSONResponse:
    """Open a Central approval for an Autonomy Capture device.

    The returned ``id`` is the device's poll secret: it never leaves this
    machine except in this response (only its hash is kept, in auth.db).
    ``sourceApprovalId`` is the public Central approval id, which authorizes
    nothing."""
    if _enrollment_rate_limited(request):
        return JSONResponse({"error": "too many enrollment requests"}, status_code=429)
    desk = _enrollment_desk()
    await asyncio.to_thread(desk.prune)
    if await asyncio.to_thread(desk.pending_count) >= MAX_PENDING_ENROLLMENTS:
        return JSONResponse({"error": "too many pending enrollment requests"}, status_code=429)
    raw_body = bytearray()
    try:
        async for chunk in request.stream():
            raw_body.extend(chunk)
            if len(raw_body) > MAX_ENROLLMENT_BODY_BYTES:
                return JSONResponse(
                    {"error": "enrollment request exceeds 8 KiB"},
                    status_code=413,
                )
        body = json.loads(raw_body or b"{}")
    except (UnicodeDecodeError, ValueError, TypeError):
        return JSONResponse({"error": "valid JSON object required"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "JSON object required"}, status_code=400)
    poll_secret = secrets.token_urlsafe(24)
    label = body.get("label")
    try:
        approval_id = await asyncio.to_thread(
            desk.open, "dropbox", poll_secret, {
                "label": label.strip() if isinstance(label, str) and label.strip()
                else "External device",
                "requested_ttl_seconds": body.get("requested_ttl_seconds", 365 * 24 * 60 * 60),
            },
        )
    except ApprovalServiceError as exc:
        if exc.code == "invalid_request":
            return JSONResponse({"error": "invalid enrollment request"}, status_code=400)
        return JSONResponse({"error": "enrollment is unavailable"}, status_code=503)
    return JSONResponse({
        "id": poll_secret,
        "sourceApprovalId": approval_id,
        "status": "pending",
    }, status_code=202)


#: How often a held poll re-reads the approval.
POLL_RECHECK_SECONDS = 1.0


async def wait_enrollment(request: Request) -> JSONResponse:
    """The device's poll, by its poll secret. An unknown secret and a wrong
    one are the same 404. The bearer is in the minting response only."""
    desk = _enrollment_desk()
    approval_id = await asyncio.to_thread(desk.approval_for, request.path_params["id"])
    if approval_id is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        wait = min(float(request.query_params.get("wait", "0")), 60.0)
    except ValueError:
        wait = 0.0
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        answer = await asyncio.to_thread(desk.collect, approval_id)
        if answer["status"] != "pending" or time.monotonic() >= deadline:
            return JSONResponse({"id": request.path_params["id"], **answer},
                                headers={"Cache-Control": "no-store"})
        await asyncio.sleep(min(POLL_RECHECK_SECONDS, max(0.0, deadline - time.monotonic())))


async def upload(request: Request) -> JSONResponse:
    principal = api_auth.principal_from_request(request)
    if (
        principal.kind is not api_auth.ApiPrincipalKind.EXTERNAL_SERVICE
        or not principal.allows_api("POST", "/api/dropbox")
    ):
        return JSONResponse({"error": "invalid or revoked upload token"}, status_code=401)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if not (content_type.startswith("image/") or content_type == "application/octet-stream"):
        return JSONResponse(
            {"error": "content-type must be image/* or application/octet-stream"},
            status_code=400,
        )
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            return JSONResponse({"error": "upload exceeds 25 MiB"}, status_code=413)
        chunks.append(chunk)
    if size == 0:
        return JSONResponse({"error": "upload body is empty"}, status_code=400)
    metadata = await asyncio.to_thread(
        store_object,
        b"".join(chunks),
        content_type=content_type,
        original_filename=request.headers.get("x-autonomy-filename"),
    )
    return JSONResponse({"ok": True, **metadata}, status_code=201)


async def list_dropbox(request: Request) -> JSONResponse:
    if (error := _session_read_error(request)) is not None:
        return error
    try:
        limit = int(request.query_params.get("limit", "3"))
    except ValueError:
        return JSONResponse({"error": "limit must be an integer"}, status_code=400)
    if limit < 1 or limit > MAX_LIST_LIMIT:
        return JSONResponse(
            {"error": f"limit must be between 1 and {MAX_LIST_LIMIT}"},
            status_code=400,
        )
    rows = await asyncio.to_thread(list_objects, limit)
    return JSONResponse({"items": rows})


async def get_dropbox(request: Request):
    if (error := _session_read_error(request)) is not None:
        return error
    try:
        metadata, path = await asyncio.to_thread(
            get_object, request.path_params["id"],
        )
    except FileNotFoundError:
        return JSONResponse({"error": "dropbox object not found"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    filename = metadata.get("original_filename") or f"{metadata['id']}.bin"
    return FileResponse(
        path,
        media_type=metadata["content_type"],
        filename=filename,
        headers={
            "X-Autonomy-Dropbox-Id": metadata["id"],
            "X-Autonomy-SHA256": metadata["sha256"],
        },
    )


ROUTES = [
    Route("/api/dropbox/enrollments", create_enrollment, methods=["POST"]),
    Route("/api/dropbox/enrollments/{id}", wait_enrollment, methods=["GET"]),
    Route("/api/dropbox", upload, methods=["POST"]),
    Route("/api/dropbox", list_dropbox, methods=["GET"]),
    Route("/api/dropbox/{id}", get_dropbox, methods=["GET"]),
]
