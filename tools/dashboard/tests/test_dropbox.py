"""Global dropbox storage, credential boundary, and cross-org retrieval."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.testclient import TestClient

from tools.dashboard import (
    api_auth,
    approvals_routes,
    dropbox_routes,
    external_service_approvals,
    unlock_routes,
)
from tools.dashboard.dao import approval_requests, auth_db
from tools.dashboard.server import authenticate_service, authenticate_session_request


@pytest.fixture
def dropbox_env(tmp_path, monkeypatch):
    saved = auth_db._conn
    auth_db.init_db(tmp_path / "auth.db")
    monkeypatch.setattr(approval_requests, "DB_PATH", tmp_path / "approvals.db")
    monkeypatch.setenv("AUTONOMY_DROPBOX_DIR", str(tmp_path / "dropbox"))
    dropbox_routes._enrollment_attempts.clear()
    try:
        yield tmp_path
    finally:
        if auth_db._conn is not None and auth_db._conn is not saved:
            auth_db._conn.close()
        auth_db._conn = saved


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _app() -> Starlette:
    return Starlette(
        routes=dropbox_routes.ROUTES,
        middleware=[Middleware(
            api_auth.ApiIdentityMiddleware,
            authenticate_bearer=authenticate_session_request,
            verify_cookie=lambda _cookie: None,
            cookie_name=unlock_routes.SESSION_COOKIE,
            authenticate_service=authenticate_service,
        )],
    )


def test_immutable_storage_records_complete_receipt(dropbox_env):
    first = dropbox_routes.store_object(
        b"png-one", content_type="image/png",
        original_filename="../../IMG 0001.png",
    )
    second = dropbox_routes.store_object(
        b"png-one", content_type="image/png", original_filename=None,
    )

    assert first["id"] != second["id"]
    assert first["sha256"] == hashlib.sha256(b"png-one").hexdigest()
    assert first["size"] == 7
    assert first["original_filename"] == "IMG 0001.png"
    assert first["storage_path"] == f"objects/{first['id']}"
    assert dropbox_routes.list_objects(2)[0]["id"] == second["id"]
    row, path = dropbox_routes.get_object(first["id"][:12])
    assert row == first
    assert path.read_bytes() == b"png-one"


def test_listing_ignores_uncommitted_or_corrupt_objects(dropbox_env):
    root = dropbox_env / "dropbox"
    (root / "objects").mkdir(parents=True)
    (root / "metadata").mkdir(parents=True)
    (root / "objects" / ("a" * 32)).write_bytes(b"orphan")
    (root / "metadata" / (("b" * 32) + ".json")).write_text("not json")
    assert dropbox_routes.list_objects(10) == []


def test_service_token_is_exactly_upload_only_and_expiry_aware(dropbox_env):
    upload_token = "shortcut-upload-token"
    auth_db.insert_scoped_service_token(
        _hash(upload_token), "dropbox-upload:test",
        capabilities=[{"method": "POST", "path": "/api/dropbox"}],
        application_scope="dropbox",
        resource_audience="global_operator_dropbox",
        source_approval_id="test",
        expires_at=time.time() + 60,
    )
    session_a = "org-session-a"
    session_b = "org-session-b"
    auth_db.insert_token(_hash(session_a), "auto-a", org="autonomy")
    auth_db.insert_token(_hash(session_b), "auto-b", org="personal")

    with TestClient(_app()) as client:
        uploaded = client.post(
            "/api/dropbox",
            content=b"image bytes",
            headers={
                "Authorization": f"Bearer {upload_token}",
                "Content-Type": "image/png",
                "X-Autonomy-Filename": "iphone.png",
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        receipt = uploaded.json()

        # A normal session bearer is deliberately not an upload credential.
        assert client.post(
            "/api/dropbox",
            content=b"not accepted",
            headers={
                "Authorization": f"Bearer {session_a}",
                "Content-Type": "image/png",
            },
        ).status_code == 401

        # The same credential cannot list or fetch what it wrote.
        assert client.get(
            "/api/dropbox", headers={"Authorization": f"Bearer {upload_token}"},
        ).status_code == 401
        assert client.get(
            f"/api/dropbox/{receipt['id']}",
            headers={"Authorization": f"Bearer {upload_token}"},
        ).status_code == 401

        # Two differently org-stamped normal sessions see the same global item.
        for token in (session_a, session_b):
            listed = client.get(
                "/api/dropbox?limit=3",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert listed.status_code == 200
            assert listed.json()["items"][0]["id"] == receipt["id"]
        fetched = client.get(
            f"/api/dropbox/{receipt['id'][:12]}",
            headers={"Authorization": f"Bearer {session_a}"},
        )
        assert fetched.status_code == 200
        assert fetched.content == b"image bytes"
        assert fetched.headers["x-autonomy-sha256"] == receipt["sha256"]

    expired = "expired-upload-token"
    auth_db.insert_scoped_service_token(
        _hash(expired), "dropbox-upload:expired",
        capabilities=[{"method": "POST", "path": "/api/dropbox"}],
        application_scope="dropbox",
        resource_audience="global_operator_dropbox",
        source_approval_id="expired",
        expires_at=time.time() - 1,
    )
    with TestClient(_app()) as client:
        response = client.post(
            "/api/dropbox", content=b"x",
            headers={"Authorization": f"Bearer {expired}", "Content-Type": "image/png"},
        )
        assert response.status_code == 401


def test_scoped_service_bearer_is_generic_over_exact_api_routes(dropbox_env):
    token = "future-integration-token"
    capabilities = [
        {"method": "POST", "path": "/api/missions/mission-1/events"},
        {"method": "GET", "path": "/api/data-feeds/weather"},
    ]
    auth_db.insert_scoped_service_token(
        _hash(token),
        "external-service:future",
        capabilities=capabilities,
        application_scope="future_integration",
        resource_audience="mission:mission-1",
        source_approval_id="approval-future",
        expires_at=time.time() + 60,
    )

    def request(method, path):
        return Request({
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        })

    principal = authenticate_service(
        request("POST", "/api/missions/mission-1/events"),
    )
    assert principal is not None
    assert principal.kind is api_auth.ApiPrincipalKind.EXTERNAL_SERVICE
    assert principal.application_scope == "future_integration"
    assert principal.resource_audience == "mission:mission-1"
    assert principal.source_approval_id == "approval-future"
    assert principal.allows_api("POST", "/api/missions/mission-1/events")
    assert principal.allows_api("GET", "/api/data-feeds/weather")

    assert authenticate_service(
        request("GET", "/api/missions/mission-1/events"),
    ) is None
    assert authenticate_service(request("GET", "/api/sessions")) is None
    auth_db.revoke_token("external-service:future")
    assert authenticate_service(
        request("POST", "/api/missions/mission-1/events"),
    ) is None


@pytest.mark.asyncio
async def test_approval_executor_mints_only_expiring_dropbox_service(dropbox_env):
    request, staged = external_service_approvals.registered_request(
        application_scope="dropbox",
        source_approval_id="enroll-1",
        requester_label="Jeremy's iPhone",
        requested_ttl_seconds=86400,
        resource_audience="global_operator_dropbox",
        capabilities=[{"method": "POST", "path": "/api/dropbox"}],
    )
    assert request["capabilities"] == [{"method": "POST", "path": "/api/dropbox"}]
    assert request["resource_audience"] == "global_operator_dropbox"

    result = await external_service_approvals.execute(
        {"id": "enroll-1", "request": request, "staged": staged},
        {"approved": True, "ttl_seconds": 3600},
    )
    assert result["ok"] is True
    assert result["sourceApprovalId"] == "enroll-1"
    assert result["capabilities"] == [{"method": "POST", "path": "/api/dropbox"}]
    assert auth_db.resolve_scoped_service_token(
        _hash(result["token"]), method="POST", path="/api/dropbox",
    )["name"] == "dropbox-upload:enroll-1"
    assert auth_db.resolve_scoped_service_token(
        _hash(result["token"]), method="GET", path="/api/dropbox",
    ) is None
    assert auth_db.resolve_token(_hash(result["token"])) is None


def test_external_access_kind_rejects_caller_selected_capabilities(dropbox_env):
    with pytest.raises(ValueError, match="registered enrollment route"):
        external_service_approvals.reject_direct_create(
            "untrusted caller",
            {
                "resource_audience": "sessions",
                "capabilities": [{"method": "GET", "path": "/api/sessions"}],
            },
        )


def test_public_enrollment_creates_central_approval_and_returns_credential(
    dropbox_env, monkeypatch,
):
    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        approvals_routes.web_push, "register_approval_pending", noop,
    )
    monkeypatch.setattr(approvals_routes.event_bus, "broadcast", noop)

    with TestClient(_app()) as client:
        created = client.post(
            "/api/dropbox/enrollments",
            json={
                "label": "Jeremy's iPhone",
                "requested_ttl_seconds": 86400,
                # These authority-bearing fields must be ignored in favor of
                # the server registration.
                "resource_audience": "sessions",
                "capabilities": [{"method": "GET", "path": "/api/sessions"}],
            },
        )
        assert created.status_code == 202, created.text
        enrollment_id = created.json()["id"]
        assert created.json()["sourceApprovalId"] == enrollment_id
        row = approval_requests.get(enrollment_id)
        assert row is not None
        assert row["kind"] == "external_service_access"
        assert row["request"]["sourceApprovalId"] == enrollment_id
        assert row["request"]["resource_audience"] == "global_operator_dropbox"
        assert row["request"]["capabilities"] == [
            {"method": "POST", "path": "/api/dropbox"},
        ]

        execution = asyncio.run(
            external_service_approvals.execute(
                row, {"approved": True, "ttl_seconds": 3600},
            )
        )
        assert approval_requests.set_result(
            enrollment_id, {"approved": True, "execution": execution},
        )
        enrolled = client.get(f"/api/dropbox/enrollments/{enrollment_id}")
        assert enrolled.status_code == 200
        receipt = enrolled.json()
        assert receipt["status"] == "approved"
        assert receipt["sourceApprovalId"] == enrollment_id
        assert receipt["capabilities"] == [
            {"method": "POST", "path": "/api/dropbox"},
        ]
        assert auth_db.resolve_scoped_service_token(
            _hash(receipt["token"]), method="POST", path="/api/dropbox",
        )["name"] == f"dropbox-upload:{enrollment_id}"


def test_metadata_is_json_and_never_contains_a_token(dropbox_env):
    row = dropbox_routes.store_object(
        b"safe", content_type="application/octet-stream",
        original_filename="capture.bin",
    )
    metadata = (
        dropbox_env / "dropbox" / "metadata" / f"{row['id']}.json"
    ).read_text()
    parsed = json.loads(metadata)
    assert parsed == row
    assert "token" not in metadata.lower()
