"""PUT /api/orgs/{slug}/charter (auto-bkoe6) + the icon routes (auto-j1y0z).

The Charter route owns the org's text/color identity: it allowlists those
fields, read-merges the current row so it preserves the server-owned icon
fields, and upserts the org's own identity row at canonical state. The icon
routes own the portable icon: POST normalizes an upload through the shared
image seam and stores it in this org's attachment store; DELETE clears the
active references. ``GET /api/orgs/{slug}``'s resolver (``org_ops.show_org``)
returns the resulting payload.
"""
from __future__ import annotations

import io
import json

import pytest
from PIL import Image
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from tools.graph.schemas.org import ORG_REVISION

from tools.dashboard import org_membership_routes
from tools.graph import org_ops
from tools.graph.db import GraphDB


def _png(w=300, h=300, color=(40, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _upload_icon(client, slug, *, image=None, crop=None):
    return client.post(
        f"/api/orgs/{slug}/icon",
        files={"icon": ("photo.png", image or _png(), "image/png")},
        data={"crop": json.dumps(crop or {"x": 0.0, "y": 0.0, "size": 1.0})},
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    GraphDB.close_all_pooled()
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setattr(
        org_membership_routes.api_auth,
        "require_global_api_authority",
        lambda request: None,
    )
    # A founded-with-identity org: the revision-1 seed the charter must outrank.
    org_ops.create_org(
        "charterorg",
        identity_payload={"name": "Charter Org", "byline": "seed byline"},
    )
    app = Starlette(routes=org_membership_routes.ROUTES)
    with TestClient(app) as c:
        yield c
    GraphDB.close_all_pooled()


def _identity(slug: str) -> dict:
    detail = org_ops.show_org(slug)
    assert detail is not None and detail["identity"] is not None
    return detail["identity"]


def test_put_round_trips_through_show_org(client):
    body = {
        "name": "Autonomy Network",
        "byline": "AGI platform",
        "description": "What the org is, in its own words.",
        "color": "#6C63FF",
    }
    r = client.put("/api/orgs/charterorg/charter", json=body)
    assert r.status_code == 200, r.text
    identity = _identity("charterorg")
    assert identity["payload"] == body
    assert identity["schema_revision"] == 3
    assert identity["publication_state"] == "canonical"


def test_second_put_updates_the_same_row(client):
    first = client.put(
        "/api/orgs/charterorg/charter", json={"name": "One"}
    ).json()["setting_id"]
    second = client.put(
        "/api/orgs/charterorg/charter", json={"name": "Two"}
    ).json()["setting_id"]
    assert first == second, "upsert must evolve one base row, not append"
    assert _identity("charterorg")["payload"]["name"] == "Two"


def test_overlong_byline_is_refused_with_the_schema_error(client):
    r = client.put(
        "/api/orgs/charterorg/charter",
        json={"name": "X", "byline": "y" * 61},
    )
    assert r.status_code == 400
    assert "over the 60" in r.json()["error"]
    # The seed row is untouched by the refused write.
    assert _identity("charterorg")["payload"]["byline"] == "seed byline"


def test_missing_name_is_refused(client):
    r = client.put("/api/orgs/charterorg/charter", json={"byline": "b"})
    assert r.status_code == 400
    assert "name" in r.json()["error"]


def test_unknown_org_is_404(client):
    r = client.put("/api/orgs/nosuch/charter", json={"name": "X"})
    assert r.status_code == 404


def test_seeded_identity_reads_back_before_any_put(client):
    """The seeded identity row reads back at the schema's CURRENT revision.

    This asserted 1 and was renamed from test_revision_1_... because the
    number is not the point: org identity rows used to be written at a
    hardcoded revision 1 while the schema had moved to 3, which made the
    orgs migration non-idempotent (reported 2026-09-22, fixed at
    8b75bbda). The fix landed while this whole file was uncollectable —
    Pillow was missing from the session image — so nothing caught the
    stale assertion. It now reads the constant, so the next revision
    bump cannot strand it again.
    """
    identity = _identity("charterorg")
    assert identity["schema_revision"] == ORG_REVISION
    assert identity["payload"]["name"] == "Charter Org"


# ── icon upload / remove ─────────────────────────────────────────────


def test_icon_upload_stores_both_fields_and_clears_favicon(client):
    # Seed a legacy favicon so we can prove the upload clears it.
    org_membership_routes  # noqa: B018 (module in scope for monkeypatch)
    from tools.graph import settings_ops
    settings_ops.upsert_by_key(
        "autonomy.org", 3, "charterorg",
        {"name": "Charter Org", "favicon": "/static/legacy.png"},
        org="charterorg", state="canonical",
    )
    r = _upload_icon(client, "charterorg")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["icon_attachment_id"]
    assert body["icon_data_uri"].startswith("data:image/webp;base64,")
    payload = _identity("charterorg")["payload"]
    assert payload["icon_attachment_id"] == body["icon_attachment_id"]
    assert payload["icon_data_uri"] == body["icon_data_uri"]
    assert "favicon" not in payload           # legacy reference cleared
    assert payload["name"] == "Charter Org"   # unrelated field preserved


def test_icon_response_never_leaks_filename_or_bytes(client):
    r = _upload_icon(client, "charterorg")
    assert set(r.json()) == {"ok", "icon_attachment_id", "icon_data_uri"}


def test_icon_upload_requires_authority(client, monkeypatch):
    monkeypatch.setattr(
        org_membership_routes.api_auth,
        "require_global_api_authority",
        lambda request: JSONResponse({"error": "nope"}, status_code=403),
    )
    r = _upload_icon(client, "charterorg")
    assert r.status_code == 403


def test_icon_upload_unknown_org_is_404(client):
    r = _upload_icon(client, "nosuch")
    assert r.status_code == 404


def test_icon_upload_requires_icon_and_crop(client):
    # A multipart body with no ``icon`` part (a stray field forces multipart
    # encoding) is missing_icon, not a generic bad request.
    missing_icon = client.post(
        "/api/orgs/charterorg/icon",
        files={"other": ("x.txt", b"x", "text/plain")},
        data={"crop": json.dumps({"x": 0, "y": 0, "size": 1})},
    )
    assert missing_icon.status_code == 400
    assert missing_icon.json()["code"] == "missing_icon"
    missing_crop = client.post(
        "/api/orgs/charterorg/icon",
        files={"icon": ("p.png", _png(), "image/png")},
    )
    assert missing_crop.status_code == 400
    assert missing_crop.json()["code"] == "missing_crop"


def test_icon_upload_rejects_non_multipart_body(client):
    r = client.post(
        "/api/orgs/charterorg/icon",
        data={"crop": json.dumps({"x": 0, "y": 0, "size": 1})},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "bad_request"


def test_icon_upload_rejects_bad_image_before_write(client):
    r = client.post(
        "/api/orgs/charterorg/icon",
        files={"icon": ("p.png", b"not an image" * 8, "image/png")},
        data={"crop": json.dumps({"x": 0, "y": 0, "size": 1})},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "undecodable"
    # No icon reference was written.
    assert "icon_attachment_id" not in _identity("charterorg")["payload"]


def test_icon_upload_rejects_invalid_crop(client):
    r = _upload_icon(client, "charterorg", crop={"x": 0.6, "y": 0, "size": 0.5})
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_crop"


def test_icon_scoped_to_target_org(client):
    org_ops.create_org("otherorg", identity_payload={"name": "Other"})
    _upload_icon(client, "charterorg")
    assert "icon_attachment_id" in _identity("charterorg")["payload"]
    assert "icon_attachment_id" not in (_identity("otherorg")["payload"])


def test_icon_temp_file_is_cleaned_up(client, monkeypatch):
    seen: list[str] = []
    real_unlink = org_membership_routes._safe_unlink

    def _spy(path):
        seen.append(path)
        real_unlink(path)

    monkeypatch.setattr(org_membership_routes, "_safe_unlink", _spy)
    r = _upload_icon(client, "charterorg")
    assert r.status_code == 200
    import os
    assert seen and seen[0]                     # a temp file was created
    assert not os.path.exists(seen[0])          # ...and cleaned up


def test_icon_final_write_failure_preserves_prior_reference(client, monkeypatch):
    # Land a first icon, then fail the second upload's final write.
    first = _upload_icon(client, "charterorg").json()
    from tools.graph import settings_ops
    real = settings_ops.upsert_by_key

    def _boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(settings_ops, "upsert_by_key", _boom)
    r = _upload_icon(client, "charterorg", image=_png(color=(1, 2, 3)))
    assert r.status_code == 500
    monkeypatch.setattr(settings_ops, "upsert_by_key", real)
    payload = _identity("charterorg")["payload"]
    assert payload["icon_attachment_id"] == first["icon_attachment_id"]


def test_delete_clears_all_icon_references(client):
    # A legacy favicon AND the new fields both clear on remove.
    from tools.graph import settings_ops
    settings_ops.upsert_by_key(
        "autonomy.org", 3, "charterorg",
        {"name": "Charter Org", "favicon": "/static/legacy.png"},
        org="charterorg", state="canonical",
    )
    _upload_icon(client, "charterorg")
    r = client.delete("/api/orgs/charterorg/icon")
    assert r.status_code == 200
    payload = _identity("charterorg")["payload"]
    assert "icon_attachment_id" not in payload
    assert "icon_data_uri" not in payload
    assert "favicon" not in payload
    assert payload["name"] == "Charter Org"    # preserved


def test_delete_requires_authority(client, monkeypatch):
    monkeypatch.setattr(
        org_membership_routes.api_auth,
        "require_global_api_authority",
        lambda request: JSONResponse({"error": "nope"}, status_code=403),
    )
    assert client.delete("/api/orgs/charterorg/icon").status_code == 403


def test_delete_is_idempotent_when_no_icon(client):
    assert client.delete("/api/orgs/charterorg/icon").status_code == 200


# ── charter cannot touch server-owned icon fields ────────────────────


def test_charter_rejects_icon_fields_in_body(client):
    for field in ("favicon", "icon_attachment_id", "icon_data_uri"):
        r = client.put(
            "/api/orgs/charterorg/charter",
            json={"name": "X", field: "whatever"},
        )
        assert r.status_code == 400, field
        assert r.json()["code"] == "icon_field_forbidden"


def test_charter_preserves_icon_and_icon_preserves_charter(client):
    # Upload an icon, then edit the charter — the icon must survive.
    up = _upload_icon(client, "charterorg").json()
    r = client.put(
        "/api/orgs/charterorg/charter",
        json={"name": "Renamed", "byline": "new tagline"},
    )
    assert r.status_code == 200, r.text
    payload = _identity("charterorg")["payload"]
    assert payload["name"] == "Renamed"
    assert payload["byline"] == "new tagline"
    assert payload["icon_attachment_id"] == up["icon_attachment_id"]
    assert payload["icon_data_uri"] == up["icon_data_uri"]

    # Now re-upload the icon — the charter text must survive.
    up2 = _upload_icon(client, "charterorg", image=_png(color=(9, 9, 9))).json()
    payload = _identity("charterorg")["payload"]
    assert payload["name"] == "Renamed"
    assert payload["byline"] == "new tagline"
    assert payload["icon_attachment_id"] == up2["icon_attachment_id"]
