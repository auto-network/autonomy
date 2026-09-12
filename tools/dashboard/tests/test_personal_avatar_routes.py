"""Personal profile avatar routes (auto-vlt7j.2).

``POST`` / ``DELETE /api/identity/profile/avatar`` — the thin persistence
adapters that normalize an uploaded Personal photo through the shared
:mod:`tools.dashboard.profile_image` seam, store ONLY the canonical 512x512
WebP as a Personal attachment, and activate its id plus the bounded compact
64x64 ``data:`` URI on ``autonomy.user#1``. Pins:

* every ``ProfileImageError.code`` maps to a stable 400;
* the explicit crop is forwarded to the processor verbatim;
* a declared Content-Length over the bound is refused before buffering, and a
  dishonest/absent header cannot defeat the streaming bound;
* the canonical bytes land in the PERSONAL store even under an org header, and
  the organization database receives nothing;
* the stored WebP is 512x512 and the compact data URI is bounded;
* no original bytes, upload filename, path, or MIME escape;
* replacement preserves text fields; removal is idempotent and never deletes
  the immutable blob;
* no canonical identity → 409;
* a processing/attachment failure leaves the prior active references unchanged,
  and only a final profile-write failure may orphan the immutable blob, in the
  exact order attach → activate.
"""
from __future__ import annotations

import base64
import io
import json
import sqlite3

import pytest
from PIL import Image
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import identity_routes, personal_profile, profile_image
from tools.graph import ops as graph_ops
from tools.graph import settings_ops
from tools.graph.schemas.user import (
    USER_PROFILE_CANONICAL_LABEL,
    USER_PROFILE_REVISION,
    USER_PROFILE_SET_ID,
)
from tools.network.idkit import KeyPair

from tools.dashboard.tests.test_identity_routes import (
    HOST,
    _enroll,
    _store_identity,
)

ORG = "idorg"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB
    from tools.dashboard.dao import identity_sessions

    GraphDB.close_all_pooled()
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.create_org_db(
        "personal", type_="personal", path=orgs_dir / "personal.db").close()
    # A DIFFERENT ambient org proves personal routing is immune to it.
    GraphDB.create_org_db(
        ORG, type_="shared", path=orgs_dir / f"{ORG}.db").close()
    monkeypatch.setenv("GRAPH_ORG", ORG)
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET_FILE",
                       str(tmp_path / "session.secret"))
    monkeypatch.setenv("DASHBOARD_IDENTITY_SESSION_DB",
                       str(tmp_path / "identity-sessions.db"))
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    # The avatar routes gate on operator authority via
    # ``api_auth.require_global_api_authority`` (the org-icon precedent). The
    # bare ``Starlette(routes=...)`` app here carries none of the auth
    # middleware that would classify the caller, so — exactly as the
    # org-membership route tests do — grant authority and exercise the route
    # logic itself. Authority classification is covered by the api_auth tests.
    monkeypatch.setattr(
        identity_routes.api_auth, "require_global_api_authority",
        lambda request: None,
    )
    identity_routes._pending.clear()
    identity_sessions.reset_for_tests()
    with TestClient(Starlette(routes=identity_routes.ROUTES),
                    base_url=f"https://{HOST}") as client:
        client._orgs_dir = orgs_dir  # type: ignore[attr-defined]
        yield client
    identity_routes._pending.clear()
    identity_sessions.reset_for_tests()
    GraphDB.close_all_pooled()


@pytest.fixture
def root():
    return KeyPair.generate()


# ── image + upload helpers ─────────────────────────────────────


def _png(w=300, h=300, color=(40, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _full_crop() -> dict:
    return {"x": 0.0, "y": 0.0, "size": 1.0}


def _upload(client, *, image=None, crop=None, filename="photo.png",
            mime="image/png", **kwargs):
    return client.post(
        "/api/identity/profile/avatar",
        files={"avatar": (filename, image if image is not None else _png(), mime)},
        data={"crop": json.dumps(crop if crop is not None else _full_crop())},
        **kwargs,
    )


def _profile(client) -> dict | None:
    r = client.get("/api/identity/profile")
    assert r.status_code == 200, r.text
    return r.json()["profile"]


def _att_rows(orgs_dir, db_name="personal"):
    conn = sqlite3.connect(str(orgs_dir / f"{db_name}.db"))
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM attachments ORDER BY id")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# ── happy path: store canonical, return compact ────────────────


def test_upload_stores_canonical_and_returns_compact(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    r = _upload(env)
    assert r.status_code == 200, r.text
    body = r.json()
    # The response leaks NOTHING but the two active references + ok.
    assert set(body) == {"ok", "avatar_attachment_id", "avatar_icon_data_uri"}
    assert body["ok"] is True
    assert body["avatar_icon_data_uri"].startswith("data:image/webp;base64,")
    assert len(body["avatar_icon_data_uri"]) <= profile_image.COMPACT_MAX_URI_CHARS

    # The profile now references the avatar; text baseline preserved.
    prof = _profile(env)
    assert prof["avatar_attachment_id"] == body["avatar_attachment_id"]
    assert prof["avatar_icon_data_uri"] == body["avatar_icon_data_uri"]
    assert prof["display_name"] == "Jeremy Spilman"
    assert prof["persisted"] is True

    # The stored attachment is a 512x512 WebP under the discarded-original name.
    rows = _att_rows(env._orgs_dir)  # type: ignore[attr-defined]
    assert len(rows) == 1
    assert rows[0]["filename"] == "profile-avatar.webp"
    att = graph_ops.get_attachment(rows[0]["id"], org="personal")
    with open(att["file_path"], "rb") as fh:
        stored = fh.read()
    canonical = Image.open(io.BytesIO(stored))
    assert canonical.format == "WEBP"
    assert canonical.size == (512, 512)
    assert len(stored) <= profile_image.CANONICAL_MAX_BYTES


def test_upload_lands_in_personal_store_under_org_header(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    r = env.post(
        "/api/identity/profile/avatar?org=idorg",
        files={"avatar": ("photo.png", _png(), "image/png")},
        data={"crop": json.dumps(_full_crop())},
        headers={"X-Graph-Org": "idorg"},
    )
    assert r.status_code == 200, r.text
    # The canonical bytes are in the personal DB...
    assert len(_att_rows(env._orgs_dir)) == 1  # type: ignore[attr-defined]
    # ...and the organization database received nothing.
    assert _att_rows(env._orgs_dir, ORG) == []  # type: ignore[attr-defined]


# ── explicit crop is forwarded verbatim ────────────────────────


def test_crop_is_forwarded_to_processor(env, root, monkeypatch):
    _store_identity(env, root, name="Jeremy Spilman")
    seen: dict = {}
    real = profile_image.process_profile_image

    def _spy(raw, crop):
        seen["raw_len"] = len(raw)
        seen["crop"] = crop
        return real(raw, crop)

    # The route imports the module lazily and calls
    # ``profile_image.process_profile_image``; patching the module attribute
    # is what the route observes.
    monkeypatch.setattr(profile_image, "process_profile_image", _spy)
    crop = {"x": 0.1, "y": 0.2, "size": 0.5}
    assert _upload(env, crop=crop).status_code == 200
    assert seen["crop"] == crop


# ── processor error-code mapping ───────────────────────────────


@pytest.mark.parametrize("code", [
    "empty", "too_large", "undecodable", "unsupported_format",
    "too_small", "too_many_pixels", "invalid_crop", "output_too_large",
])
def test_processor_error_code_maps_to_400(env, root, monkeypatch, code):
    _store_identity(env, root, name="Jeremy Spilman")

    def _raise(raw, crop):
        raise profile_image.ProfileImageError(code, f"boom: {code}")

    monkeypatch.setattr(profile_image, "process_profile_image", _raise)
    r = _upload(env)
    assert r.status_code == 400, r.text
    assert r.json()["code"] == code
    # Nothing was written on any rejection.
    assert _profile(env)["avatar_attachment_id"] is None
    assert _att_rows(env._orgs_dir) == []  # type: ignore[attr-defined]


def test_real_invalid_crop_is_400(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    r = _upload(env, crop={"x": 0.6, "y": 0.0, "size": 0.5})
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_crop"


def test_real_undecodable_is_400_and_writes_nothing(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    r = _upload(env, image=b"not an image" * 8)
    assert r.status_code == 400
    assert r.json()["code"] == "undecodable"
    assert _profile(env)["avatar_attachment_id"] is None
    assert _att_rows(env._orgs_dir) == []  # type: ignore[attr-defined]


def test_real_too_small_is_400(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    r = _upload(env, image=_png(64, 64))
    assert r.status_code == 400
    assert r.json()["code"] == "too_small"


def test_present_but_empty_file_is_empty_code(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    r = _upload(env, image=b"")
    assert r.status_code == 400
    assert r.json()["code"] == "empty"


# ── request-body bounds ────────────────────────────────────────


@pytest.mark.asyncio
async def test_declared_content_length_refused_before_buffering():
    """A declared Content-Length over the bound is refused WITHOUT reading the
    body — the stream is never iterated."""
    max_bytes = identity_routes._max_avatar_request_bytes()

    class _FakeRequest:
        def __init__(self):
            self.headers = {"content-length": str(max_bytes + 1)}

        def stream(self):
            raise AssertionError("stream must not be consumed")

    with pytest.raises(identity_routes._RequestTooLarge):
        await identity_routes._read_bounded_body(_FakeRequest(), max_bytes)


@pytest.mark.asyncio
async def test_streaming_bound_trips_without_honest_header():
    """With no Content-Length, the running total still trips the bound so a
    missing/dishonest header cannot cause unbounded buffering."""
    max_bytes = 4096

    class _FakeRequest:
        def __init__(self):
            self.headers = {}  # no content-length at all

        async def _iter(self):
            sent = 0
            while sent <= max_bytes + 4096:
                yield b"A" * 1024
                sent += 1024

        def stream(self):
            return self._iter()

    with pytest.raises(identity_routes._RequestTooLarge):
        await identity_routes._read_bounded_body(_FakeRequest(), max_bytes)


def test_oversized_real_upload_is_refused(env, root):
    """An end-to-end body larger than the bound comes back 413, not a decode
    error — the bounded read fires before the multipart parse."""
    _store_identity(env, root, name="Jeremy Spilman")
    over = b"A" * (identity_routes._max_avatar_request_bytes() + 4096)
    r = env.post(
        "/api/identity/profile/avatar",
        headers={"content-type": "multipart/form-data; boundary=zzz"},
        content=over,
    )
    assert r.status_code == 413
    assert r.json()["code"] == "request_too_large"


# ── malformed bodies ───────────────────────────────────────────


def test_non_multipart_body_is_bad_request(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    r = env.post("/api/identity/profile/avatar", json={"crop": _full_crop()})
    assert r.status_code == 400
    assert r.json()["code"] == "bad_request"


def test_missing_avatar_and_missing_crop(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    missing_avatar = env.post(
        "/api/identity/profile/avatar",
        files={"other": ("x.txt", b"x", "text/plain")},
        data={"crop": json.dumps(_full_crop())},
    )
    assert missing_avatar.status_code == 400
    assert missing_avatar.json()["code"] == "missing_avatar"
    missing_crop = env.post(
        "/api/identity/profile/avatar",
        files={"avatar": ("photo.png", _png(), "image/png")},
    )
    assert missing_crop.status_code == 400
    assert missing_crop.json()["code"] == "missing_crop"


@pytest.mark.parametrize("duplicate", ["avatar", "crop"])
def test_duplicate_required_multipart_part_is_rejected(env, root, duplicate):
    """The request has one unambiguous avatar and one crop."""
    _store_identity(env, root, name="Jeremy Spilman")
    boundary = "profile-duplicate-boundary"
    parts = [
        ("avatar", "first.png", "image/png", _png()),
        ("crop", None, None, json.dumps(_full_crop()).encode()),
    ]
    if duplicate == "avatar":
        parts.append(("avatar", "second.png", "image/png", _png()))
    else:
        parts.append(("crop", None, None, json.dumps(_full_crop()).encode()))
    body = bytearray()
    for name, filename, content_type, value in parts:
        body.extend(f"--{boundary}\r\n".encode())
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        body.extend((disposition + "\r\n").encode())
        if content_type is not None:
            body.extend(f"Content-Type: {content_type}\r\n".encode())
        body.extend(b"\r\n" + value + b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())

    response = env.post(
        "/api/identity/profile/avatar",
        content=bytes(body),
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"


def test_malformed_crop_json_is_invalid_crop(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    r = env.post(
        "/api/identity/profile/avatar",
        files={"avatar": ("photo.png", _png(), "image/png")},
        data={"crop": "{not json"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_crop"


# ── no canonical identity ──────────────────────────────────────


def test_upload_refused_without_identity(env):
    r = _upload(env)
    assert r.status_code == 409
    assert r.json()["code"] == "no_personal_identity"
    assert _att_rows(env._orgs_dir) == []  # type: ignore[attr-defined]


def test_delete_refused_without_identity(env):
    r = env.delete("/api/identity/profile/avatar")
    assert r.status_code == 409


# ── replacement preserves text; removal idempotent ─────────────


def test_replacement_preserves_text_fields(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    env.patch("/api/identity/profile",
              json={"biography": "Builder.", "initials": "JX"})
    first = _upload(env).json()["avatar_attachment_id"]
    second = _upload(env, image=_png(color=(9, 9, 9))).json()["avatar_attachment_id"]
    assert second != first
    prof = _profile(env)
    assert prof["avatar_attachment_id"] == second
    # Every text field survived the replacement.
    assert prof["display_name"] == "Jeremy Spilman"
    assert prof["biography"] == "Builder."
    assert prof["initials_override"] == "JX"


def test_delete_clears_both_references_idempotently(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    env.patch("/api/identity/profile", json={"biography": "Builder."})
    up = _upload(env).json()
    att_id = up["avatar_attachment_id"]

    r = env.delete("/api/identity/profile/avatar")
    assert r.status_code == 200
    prof = _profile(env)
    assert prof["avatar_attachment_id"] is None
    assert prof["avatar_icon_data_uri"] is None
    assert prof["biography"] == "Builder."  # text preserved

    # The immutable blob still exists — only the reference was dropped.
    rows = _att_rows(env._orgs_dir)  # type: ignore[attr-defined]
    assert any(row["id"] == att_id for row in rows)

    # A second delete is a no-op success.
    assert env.delete("/api/identity/profile/avatar").status_code == 200


def test_delete_idempotent_when_no_avatar(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    env.patch("/api/identity/profile", json={"biography": "hi"})
    r = env.delete("/api/identity/profile/avatar")
    assert r.status_code == 200
    # No spurious avatar row was written.
    assert _profile(env)["avatar_attachment_id"] is None


# ── failure ordering & orphan rule ─────────────────────────────


def test_final_write_failure_preserves_prior_reference_and_orphans_blob(
        env, root, monkeypatch):
    _store_identity(env, root, name="Jeremy Spilman")
    first = _upload(env).json()
    before = len(_att_rows(env._orgs_dir))  # type: ignore[attr-defined]

    def _boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(personal_profile, "set_avatar", _boom)
    r = _upload(env, image=_png(color=(1, 2, 3)))
    assert r.status_code == 500
    assert r.json()["code"] == "store_failed"

    # The prior active reference is untouched.
    assert _profile(env)["avatar_attachment_id"] == first["avatar_attachment_id"]
    # ...but the new immutable blob was already stored — the documented orphan.
    assert len(_att_rows(env._orgs_dir)) == before + 1  # type: ignore[attr-defined]


def test_attach_then_activate_order(env, root, monkeypatch):
    _store_identity(env, root, name="Jeremy Spilman")
    order: list[str] = []
    real_attach = graph_ops.attach_file
    real_set = personal_profile.set_avatar

    def _attach(*a, **k):
        order.append("attach")
        return real_attach(*a, **k)

    def _set(*a, **k):
        order.append("set_avatar")
        return real_set(*a, **k)

    # The route imports graph_ops as ``tools.graph.ops`` inside the thread.
    monkeypatch.setattr(graph_ops, "attach_file", _attach)
    monkeypatch.setattr(personal_profile, "set_avatar", _set)
    assert _upload(env).status_code == 200
    assert order == ["attach", "set_avatar"]


# ── status compatibility ───────────────────────────────────────


def test_status_reflects_avatar(env, root):
    _store_identity(env, root, name="Jeremy Spilman")
    up = _upload(env).json()
    body = env.get("/api/identity/status").json()
    assert body["profile"]["avatar_attachment_id"] == up["avatar_attachment_id"]
