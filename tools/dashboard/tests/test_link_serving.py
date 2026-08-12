"""C4 unit coverage: the I9 grant gate + the §6.1 target resolvers.

Everything here runs against real stores in a tmp GRAPH_DB /
designs DB — the grant cache is actual ``autonomy.network.link-grant``
Settings rows, targets are actual design revisions, notes, and owning-note
image attachments. The tunnel itself is exercised separately in
``test_link_serving_tunnel.py``; this file drives the handler directly.

Pinned behaviors:

* unknown, revoked (row removed), expired, and unresolvable tokens all
  return the SAME refusal bytes — a prober can't classify the failure;
* ``meta.ttl`` is enforced against ``issued_at`` (boundary inclusive);
* file grants are uniformly unavailable in the rich-render v1 protocol;
* a ``note`` grant serves only note sources and packages Markdown as data;
* ``present`` follows a design to its latest revision, ``design`` serves
  the exact revision and nothing newer.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import pytest

import agents.design_db as design_db
from tools.dashboard import link_serving
from tools.graph import ops as graph_ops
from tools.graph import settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_LINK_GRANT_REVISION,
    NETWORK_LINK_GRANT_SET_ID,
)
from tools.network.idkit import canonical_json

ORG = "netorg"
ISO = "%Y-%m-%dT%H:%M:%SZ"

FETCH = canonical_json({"op": "fetch", "v": 1})
HEAD = canonical_json({"op": "head", "v": 1})


def _token(n: int) -> str:
    return f"{n:032x}"


def _iso(epoch: float) -> str:
    return time.strftime(ISO, time.gmtime(epoch))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Tmp GRAPH_DB + tmp designs DB; yields nothing — state is the env."""
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "graph.db"))
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.setattr(design_db, "DB_PATH", tmp_path / "designs.db")
    monkeypatch.setattr(design_db, "_initialized", False)
    yield tmp_path
    GraphDB.close_all_pooled()


def put_grant(token: str, target_uuid: str, target_type: str, *,
              meta: dict | None = None, issued_at: str | None = None) -> None:
    settings_ops.add_setting(
        NETWORK_LINK_GRANT_SET_ID, NETWORK_LINK_GRANT_REVISION, token,
        {
            "token": token,
            "url": f"https://relay.auto.network/l/{token}",
            "target_uuid": target_uuid,
            "target_type": target_type,
            "meta": meta or {},
            "subject": {"kind": "operator", "id": "op-1"},
            "issued_at": issued_at or _iso(time.time()),
        },
        org=ORG,
    )


def drop_grant(token: str) -> None:
    for member in settings_ops.read_set(NETWORK_LINK_GRANT_SET_ID, org=ORG).members:
        if member.key == token:
            settings_ops.remove_setting(member.id, org=ORG)


def serve(token: str, message: bytes = FETCH, **handler_kwargs) -> bytes:
    handler = link_serving.make_grant_handler(ORG, **handler_kwargs)
    return asyncio.run(handler(token, message))


def parse(response: bytes) -> tuple[dict, bytes]:
    header, _, body = response.partition(b"\n")
    return json.loads(header), body


def sliced(body: bytes, descriptor: dict) -> bytes:
    start = descriptor["offset"]
    return body[start:start + descriptor["length"]]

def part(header, ref):
    """One generic part header by ref. The host no longer knows what a ref
    means, so tests address parts the way the note viewer does."""
    for entry in header.get("parts") or []:
        if entry["ref"] == ref:
            return entry
    raise KeyError(ref)


def image_parts(header):
    return [e for e in (header.get("parts") or [])
            if e["ref"] not in ("note", "markdown")]


def note_title(header, body):
    import json as _json
    return _json.loads(sliced(body, part(header, "note")).decode())["title"]



# ── the I9 gate ───────────────────────────────────────────────


class TestGrantGate:
    def test_unknown_token_refused(self, env):
        assert serve(_token(1)) == link_serving.REFUSED

    def test_malformed_tokens_refused(self, env):
        for bad in ("", "zz" * 16, _token(2)[:-1], _token(2) + "0",
                    _token(2).upper()):
            assert serve(bad) == link_serving.REFUSED

    def test_revoked_indistinguishable_from_unknown(self, env, tmp_path):
        note = graph_ops.create_note("artifact bytes", title="Artifact", org=ORG)
        token = _token(3)
        put_grant(token, note["id"], "note")

        served = serve(token)
        header, body = parse(served)
        assert header["status"] == "ok" and b"artifact bytes" in body

        drop_grant(token)  # what link_revoke's executor does to the cache
        revoked = serve(token)
        assert revoked == serve(_token(4))  # unknown
        assert revoked == link_serving.REFUSED

    def test_expired_grant_refused(self, env):
        token = _token(5)
        put_grant(token, str(uuid.uuid4()), "note",
                  meta={"ttl": 60}, issued_at=_iso(time.time() - 120))
        assert serve(token) == link_serving.REFUSED

    def test_ttl_boundary_is_inclusive(self, env):
        issued = time.time() - 100
        token = _token(6)
        put_grant(token, str(uuid.uuid4()), "note",
                  meta={"ttl": 60}, issued_at=_iso(issued))
        grant_at = lambda now: link_serving.check_grant(token, org=ORG, now=now)
        assert grant_at(issued + 59) is not None
        assert grant_at(issued + 60) is None
        assert grant_at(issued + 61) is None

    def test_grant_without_ttl_does_not_expire(self, env):
        token = _token(7)
        put_grant(token, str(uuid.uuid4()), "note", issued_at=_iso(0))
        assert link_serving.check_grant(token, org=ORG) is not None

    def test_grant_valid_fails_closed_on_malformed_rows(self):
        now = time.time()
        base = {
            "token": _token(8), "target_uuid": str(uuid.uuid4()),
            "target_type": "note", "meta": {},
            "subject": {"kind": "operator", "id": "op-1"},
            "issued_at": _iso(now),
        }
        assert link_serving._grant_valid(dict(base), _token(8), now) is not None
        cases = [
            {"token": _token(9)},                        # key/payload mismatch
            {"target_type": "session"},                  # not a §6.1 kind
            {"target_uuid": "not-a-uuid"},
            {"meta": {"require_auth": True}},            # rung-2 reservation
            {"meta": {"ttl": True}},                     # bool is not a ttl
            {"meta": {"ttl": 0}},
            {"meta": {"ttl": 60}, "issued_at": "yesterday"},
            {"meta": "unlimited"},
        ]
        for override in cases:
            payload = {**base, **override}
            assert link_serving._grant_valid(payload, _token(8), now) is None, override

    def test_protocol_misuse_is_bad_request(self, env):
        assert serve(_token(10), b"\xff\xfe not json") == link_serving.BAD_REQUEST
        assert serve(_token(10), canonical_json({"op": "steal", "v": 1})) \
            == link_serving.BAD_REQUEST
        assert serve(_token(10), canonical_json({"op": "fetch", "v": 2})) \
            == link_serving.BAD_REQUEST
        assert serve(_token(10), canonical_json({"op": "fetch", "v": 1, "extra": 1})) \
            == link_serving.BAD_REQUEST
        assert serve(_token(10), canonical_json(["fetch"])) == link_serving.BAD_REQUEST


class TestHeadOp:
    """The object HEAD: same gate + resolution as fetch, headers only.

    A valid HEAD returns wire status ``ok`` with ``serialized_size`` but no
    body. Every gate failure returns the same refusal as fetch, so a prober
    cannot classify the failure or tell HEAD from fetch.
    """

    def test_head_returns_length_and_no_body(self, env, tmp_path):
        rev = design_db.create_design(
            title="large", variants=[{"id": "v1", "html": "x" * 5000}],
        )
        token = _token(20)
        put_grant(token, rev, "design")

        fetched = serve(token, FETCH)
        headed = serve(token, HEAD)
        fh, fbody = parse(fetched)
        hh, hbody = parse(headed)

        assert fh["status"] == "ok" and len(fbody) == 5000
        assert hh == {"v": 1, "status": "ok", "serialized_size": 5000}
        # …and NOTHING after the header newline: the artifact never streams.
        assert hbody == b""

    def test_head_refusal_matches_fetch_refusal(self, env):
        # Unknown, revoked, expired all collapse to the one refusal for HEAD
        # too — and it is byte-identical to the fetch refusal.
        assert serve(_token(21), HEAD) == link_serving.REFUSED
        assert serve(_token(21), HEAD) == serve(_token(21), FETCH)

    def test_head_expired_grant_refused(self, env):
        token = _token(22)
        put_grant(token, str(uuid.uuid4()), "note",
                  meta={"ttl": 60}, issued_at=_iso(time.time() - 120))
        assert serve(token, HEAD) == link_serving.REFUSED

    def test_fetch_and_head_share_exact_size_boundary(self, env, monkeypatch):
        rev = design_db.create_design(
            title="limit", variants=[{"id": "v1", "html": "x" * 64}],
        )
        token = _token(23)
        put_grant(token, rev, "design")
        monkeypatch.setattr(link_serving, "AUTONET_MAX_ARTIFACT_BYTES", 64)
        fh, fbody = parse(serve(token, FETCH))
        hh, _ = parse(serve(token, HEAD))
        assert fh["status"] == hh["status"] == "ok"
        assert len(fbody) == hh["serialized_size"] == 64

        monkeypatch.setattr(link_serving, "AUTONET_MAX_ARTIFACT_BYTES", 63)
        assert serve(token, FETCH) == link_serving.REFUSED
        assert serve(token, HEAD) == link_serving.REFUSED


class TestArtifactSerializer:
    def test_note_offsets_round_trip_and_do_not_overlap(self):
        artifact = {
            "kind": "note", "viewer": b"VIEWER",
            "parts": [
                {"ref": "note", "mime": "application/json", "bytes": b'{"title": "Title"}'},
                {"ref": "markdown", "mime": "text/markdown", "bytes": "snowman: \u2603".encode()},
                {"ref": "a", "mime": "image/png", "bytes": b"AAA"},
                {"ref": "b", "mime": "image/webp", "bytes": b"BBBB"},
            ],
        }
        header, body = link_serving._serialize_artifact(artifact)
        descriptors = [
            header["viewer"], part(header, "note"), part(header, "markdown"),
            *image_parts(header),
        ]
        assert sliced(body, header["viewer"]) == b"VIEWER"
        assert note_title(header, body) == "Title"
        assert sliced(body, part(header, "markdown")).decode() == "snowman: ☃"
        assert sliced(body, part(header, "a")) == b"AAA"
        assert sliced(body, part(header, "b")) == b"BBBB"
        intervals = sorted((d["offset"], d["offset"] + d["length"]) for d in descriptors)
        assert all(left[1] <= right[0] for left, right in zip(intervals, intervals[1:]))

    def test_org_favicon_is_an_authenticated_part(self):
        artifact = {
            "kind": "design",
            "viewer": b"<h1>viewer</h1>",
            "branding": {
                "name": "Example Org", "color": "#123456", "initial": "E",
                "favicon": {"mime": "image/png", "bytes": b"PNG-icon"},
            },
        }
        header, body = link_serving._serialize_artifact(artifact)
        assert sliced(body, header["viewer"]) == b"<h1>viewer</h1>"
        assert sliced(body, header["branding"]["favicon"]) == b"PNG-icon"
        assert header["branding"] == {
            "name": "Example Org", "color": "#123456", "initial": "E",
            "favicon": {
                "mime": "image/png", "offset": 15, "length": 8,
            },
        }

    @pytest.mark.parametrize("artifact", [
        {"kind": "note", "viewer": b"v", "content": {}},   # `content` is gone
        {"kind": "present", "viewer": b""},
        {"kind": "file", "viewer": b"v"},
        {"kind": "note", "viewer": b"v", "parts": [          # duplicate ref
            {"ref": "x", "mime": "image/png", "bytes": b"1"},
            {"ref": "x", "mime": "image/png", "bytes": b"2"},
        ]},
        {"kind": "note", "viewer": b"v", "parts": [{"ref": 1, "mime": "image/png", "bytes": b"1"}]},
        {"kind": "note", "viewer": b"v", "parts": [{"ref": "x", "mime": "image/png", "bytes": "1"}]},
        {"kind": "note", "viewer": b"v", "parts": {"ref": "x"}},
    ])
    def test_union_and_part_invariants_fail_closed(self, artifact):
        with pytest.raises((TypeError, ValueError)):
            link_serving._serialize_artifact(artifact)


class TestOrgBrandResolver:
    def test_local_dashboard_favicon_becomes_bounded_bytes(
        self, env, tmp_path, monkeypatch,
    ):
        static = tmp_path / "static"
        icon = static / "orgs" / "example.png"
        icon.parent.mkdir(parents=True)
        icon.write_bytes(b"png-bytes")
        monkeypatch.setattr(link_serving, "_DASHBOARD_STATIC", static)
        monkeypatch.setattr(
            "tools.dashboard.org_identity.resolve_org_identity",
            lambda _org: {
                "name": "Example Org", "color": "#123456", "initial": "E",
                "favicon": "/static/orgs/example.png",
            },
        )
        assert link_serving._resolve_org_brand("example") == {
            "name": "Example Org", "color": "#123456", "initial": "E",
            "favicon": {"mime": "image/png", "bytes": b"png-bytes"},
        }

    def test_uploaded_data_url_becomes_favicon_bytes(self, env, monkeypatch):
        monkeypatch.setattr(
            "tools.dashboard.org_identity.resolve_org_identity",
            lambda _org: {
                "name": "Example Org", "color": "#123456", "initial": "E",
                "favicon": "data:image/png;base64,cG5nLWJ5dGVz",
            },
        )
        assert link_serving._resolve_org_brand("example") == {
            "name": "Example Org", "color": "#123456", "initial": "E",
            "favicon": {"mime": "image/png", "bytes": b"png-bytes"},
        }

    def test_favicon_path_cannot_escape_static_root(self, env, tmp_path, monkeypatch):
        static = tmp_path / "static"
        static.mkdir()
        (tmp_path / "secret.png").write_bytes(b"secret")
        monkeypatch.setattr(link_serving, "_DASHBOARD_STATIC", static)
        monkeypatch.setattr(
            "tools.dashboard.org_identity.resolve_org_identity",
            lambda _org: {
                "name": "Example Org", "color": "#123456", "initial": "E",
                "favicon": "/static/../secret.png",
            },
        )
        assert link_serving._resolve_org_brand("example") == {
            "name": "Example Org", "color": "#123456", "initial": "E",
        }


# ── file resolver: the path allowlist ─────────────────────────


def _attach(path, *, mime: str | None = None, source_id: str | None = None) -> str:
    """Insert a raw attachment row pointing at *path*; returns its UUID."""
    from tools.graph.db import GraphDB
    from tools.graph.models import Attachment

    db = GraphDB(os.environ["GRAPH_DB"])
    try:
        att = Attachment(filename=os.path.basename(str(path)), mime_type=mime,
                         file_path=str(path), source_id=source_id)
        db.insert_attachment(att)
    finally:
        db.close()
    return att.id


class TestDeferredFileResolver:
    def test_file_grant_is_unavailable_in_v1(self, env, tmp_path):
        artifact = tmp_path / "report.html"
        artifact.write_bytes(b"<h1>report</h1>")
        token = _token(20)
        put_grant(token, _attach(artifact, mime="text/html"), "file")
        assert serve(token) == link_serving.REFUSED
        assert serve(token, HEAD) == link_serving.REFUSED


# ── note / design / present resolvers ─────────────────────────


class TestNoteResolver:
    def test_note_read_explicitly_disables_peers(self, env, monkeypatch):
        seen = {}

        def fake_read(*_args, **kwargs):
            seen.update(kwargs)
            return None

        monkeypatch.setattr(graph_ops, "read_source_full", fake_read)
        assert link_serving._resolve_note(str(uuid.uuid4()), ORG) is None
        assert seen["org"] == ORG
        assert seen["peers"] == []

    def test_note_is_content_data_not_server_rendered_html(self, env):
        note = graph_ops.create_note(
            "Line one\n\n<script>alert('xss')</script>",
            title="Ship <notes>", org=ORG,
        )
        token = _token(30)
        put_grant(token, note["id"], "note")
        header, body = parse(serve(token))
        assert header["status"] == "ok"
        assert header["kind"] == "note"
        assert note_title(header, body) == "Ship <notes>"
        markdown = sliced(body, part(header, "markdown")).decode()
        assert markdown == "Line one\n\n<script>alert('xss')</script>"
        assert sliced(body, header["viewer"]) == link_serving._note_viewer_bytes()
        assert b"Line one" not in sliced(body, header["viewer"])

    def test_current_owned_images_only(self, env, tmp_path):
        note = graph_ops.create_note("initial", title="Images", org=ORG)
        current = tmp_path / "current.png"
        old = tmp_path / "old.png"
        current.write_bytes(b"PNG-current")
        old.write_bytes(b"PNG-old")
        updated = graph_ops.update_note(
            note["id"], "![current]({1})", attachments=[str(current)], org=ORG,
        )
        old_ref = _attach(old, mime="image/png", source_id=note["id"])
        current_ref = updated["attachments"][0]["id"]
        token = _token(33)
        put_grant(token, note["id"], "note")

        header, body = parse(serve(token))
        markdown = sliced(body, part(header, "markdown")).decode()
        assert f"cid:{current_ref}" in markdown
        assert old_ref not in markdown
        assert [p["ref"] for p in image_parts(header)] == [current_ref]
        assert sliced(body, image_parts(header)[0]) == b"PNG-current"
        assert b"PNG-old" not in body

    def test_attachment_must_belong_to_note_and_be_image(self, env, tmp_path):
        note = graph_ops.create_note("initial", title="Images", org=ORG)
        other = graph_ops.create_note("other", title="Other", org=ORG)
        path = tmp_path / "foreign.png"
        path.write_bytes(b"FOREIGN")
        foreign_ref = _attach(path, mime="image/png", source_id=other["id"])
        graph_ops.update_note(
            note["id"], f"![foreign](graph://{foreign_ref})", org=ORG,
        )
        token = _token(34)
        put_grant(token, note["id"], "note")
        header, body = parse(serve(token))
        assert image_parts(header) == []
        assert b"FOREIGN" not in body
        assert f"cid:{foreign_ref}" in sliced(
            body, part(header, "markdown")
        ).decode()

    def test_note_embed_is_not_resolved(self, env):
        note = graph_ops.create_note(
            "Before ![[aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa]] after",
            title="No embed", org=ORG,
        )
        token = _token(35)
        put_grant(token, note["id"], "note")
        header, body = parse(serve(token))
        markdown = sliced(body, part(header, "markdown")).decode()
        assert "![[aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa]]" in markdown
        assert image_parts(header) == []

    def test_non_note_source_refused(self, env):
        from tools.graph.db import GraphDB
        from tools.graph.models import Source

        src = Source(type="session", platform="local", title="a transcript",
                     file_path="session:fixture")
        db = GraphDB(os.environ["GRAPH_DB"])
        try:
            db.insert_source(src)
        finally:
            db.close()
        token = _token(31)
        put_grant(token, src.id, "note")
        assert serve(token) == link_serving.REFUSED

    def test_missing_note_refused(self, env):
        token = _token(32)
        put_grant(token, str(uuid.uuid4()), "note")
        assert serve(token) == link_serving.REFUSED

    def test_note_image_obeys_exact_serialized_size_limit(
        self, env, tmp_path, monkeypatch
    ):
        image = tmp_path / "bounded.png"
        image.write_bytes(b"IMAGE-BYTES")
        note = graph_ops.create_note(
            "![bounded]({1})", title="Bounded", attachments=[str(image)], org=ORG,
        )
        token = _token(36)
        put_grant(token, note["id"], "note")
        _, body = parse(serve(token))

        monkeypatch.setattr(
            link_serving, "AUTONET_MAX_ARTIFACT_BYTES", len(body)
        )
        assert parse(serve(token))[1] == body
        monkeypatch.setattr(
            link_serving, "AUTONET_MAX_ARTIFACT_BYTES", len(body) - 1
        )
        assert serve(token) == link_serving.REFUSED


class TestAttachmentManifest:
    """The metadata-only download manifest (wire protocol v1, auto-b94x6)."""

    def test_lists_every_slot_attachment_with_no_bytes(self, env, tmp_path):
        img1 = tmp_path / "inline.png"
        doc = tmp_path / "notes.txt"
        img2 = tmp_path / "gallery.png"
        img1.write_bytes(b"PNG-INLINE")
        doc.write_bytes(b"DOCUMENT-BYTES")
        img2.write_bytes(b"PNG-GALLERY")
        note = graph_ops.create_note(
            "![inline]({1})", title="Gallery",
            attachments=[str(img1), str(doc), str(img2)], org=ORG,
        )
        token = _token(40)
        put_grant(token, note["id"], "note")
        header, body = parse(serve(token))

        manifest = header["attachments"]
        assert len(manifest) == 3
        assert [e["ref"] for e in manifest] == [
            a["id"] for a in note["attachments"]
        ]
        import re as _re
        for entry in manifest:
            assert set(entry) == {
                "ref", "name", "mime", "raw_sha256", "total_size", "oversize"
            }
            assert _re.fullmatch(r"[0-9a-f]{64}", entry["raw_sha256"])
            assert entry["total_size"] > 0
            assert entry["oversize"] is False
        names = {e["name"] for e in manifest}
        assert names == {"inline.png", "notes.txt", "gallery.png"}

        # The inline image still renders as a bundled part; the two
        # non-inline attachments are offered for download only — their bytes
        # never appear in the artifact body.
        assert [p["ref"] for p in image_parts(header)] == [
            note["attachments"][0]["id"]
        ]
        assert b"PNG-INLINE" in body
        assert b"DOCUMENT-BYTES" not in body
        assert b"PNG-GALLERY" not in body

    def test_membership_is_slots_not_source_id(self, env, tmp_path):
        # Identical image bytes stored under two notes deduplicate into one row
        # that keeps the FIRST note's source_id. The second note must still
        # both LIST the attachment for download AND bundle it inline: both use
        # slot membership, never the row source_id.
        shared = tmp_path / "shared.png"
        shared.write_bytes(b"PNG-SHARED-BYTES")
        note1 = graph_ops.create_note(
            "![a]({1})", title="N1", attachments=[str(shared)], org=ORG,
        )
        note2 = graph_ops.create_note(
            "![b]({1})", title="N2", attachments=[str(shared)], org=ORG,
        )
        ref1 = note1["attachments"][0]["id"]
        ref2 = note2["attachments"][0]["id"]
        assert ref1 == ref2  # deduplicated into a single row
        row = graph_ops.get_attachment(ref1, org=ORG, peers=[])
        assert row["source_id"] == note1["id"]  # row belongs to note1

        token = _token(41)
        put_grant(token, note2["id"], "note")
        header, body = parse(serve(token))
        # Download manifest membership by slots.
        assert [e["ref"] for e in header["attachments"]] == [ref1]
        # Inline bundling also by slots — the shared image is NOT dropped from
        # note2 even though the row's source_id is note1 (the source_id bug).
        assert [p["ref"] for p in image_parts(header)] == [ref1]
        assert sliced(body, image_parts(header)[0]) == b"PNG-SHARED-BYTES"

    def test_marks_oversize_above_the_cap(self, env, tmp_path, monkeypatch):
        big = tmp_path / "big.bin"
        big.write_bytes(b"X" * 100)
        note = graph_ops.create_note(
            "body", title="Big", attachments=[str(big)], org=ORG,
        )
        monkeypatch.setattr(link_serving, "AUTONET_MAX_ATTACHMENT_BYTES", 50)
        token = _token(42)
        put_grant(token, note["id"], "note")
        header, _ = parse(serve(token))
        manifest = header["attachments"]
        assert len(manifest) == 1
        assert manifest[0]["total_size"] == 100
        assert manifest[0]["oversize"] is True

    def test_artifact_size_independent_of_attachment_bytes(self, env, tmp_path):
        small = tmp_path / "aa.bin"
        big = tmp_path / "bb.bin"
        small.write_bytes(b"S" * 10)
        big.write_bytes(b"B" * 100_000)
        note_s = graph_ops.create_note(
            "body", title="T", attachments=[str(small)], org=ORG,
        )
        note_b = graph_ops.create_note(
            "body", title="T", attachments=[str(big)], org=ORG,
        )
        put_grant(_token(43), note_s["id"], "note")
        put_grant(_token(44), note_b["id"], "note")
        _, body_s = parse(serve(_token(43)))
        header_b, body_b = parse(serve(_token(44)))
        # No attachment bytes ride in the body, so its size is independent of
        # the attachment file size even as the manifest reports the true size.
        assert len(body_s) == len(body_b)
        assert header_b["attachments"][0]["total_size"] == 100_000
        assert b"B" * 100_000 not in body_b

    def test_empty_manifest_for_note_without_attachments(self, env):
        note = graph_ops.create_note("plain body", title="Plain", org=ORG)
        token = _token(45)
        put_grant(token, note["id"], "note")
        header, _ = parse(serve(token))
        assert header["attachments"] == []

    def test_attachment_with_noncanonical_hash_is_omitted(self, env, tmp_path):
        # raw_sha256 is load-bearing (client verify + resume identity); an
        # attachment whose stored hash is not canonical 64-hex is failed
        # closed — omitted from the manifest, never emitted with a bad hash.
        from tools.graph.db import GraphDB

        f = tmp_path / "corrupt.bin"
        f.write_bytes(b"DATA")
        note = graph_ops.create_note(
            "body", title="Corrupt", attachments=[str(f)], org=ORG,
        )
        ref = note["attachments"][0]["id"]
        db = GraphDB(os.environ["GRAPH_DB"])
        try:
            db.conn.execute(
                "UPDATE attachments SET hash = ? WHERE id = ?", ("NOTHEX", ref)
            )
            db.conn.commit()
        finally:
            db.close()
        token = _token(46)
        put_grant(token, note["id"], "note")
        header, _ = parse(serve(token))
        assert header["attachments"] == []


class TestManifestSerializerValidation:
    """_serialize_artifact enforces the frozen manifest schema (auto-b94x6)."""

    @staticmethod
    def _artifact(entries: list) -> dict:
        return {
            "kind": "note", "viewer": b"V",
            "parts": [{"ref": "markdown", "mime": "text/markdown", "bytes": b"m"}],
            "attachments": entries,
        }

    def test_valid_entry_round_trips(self):
        entry = {
            "ref": "r1", "name": "doc.txt", "mime": "text/plain",
            "raw_sha256": "a" * 64, "total_size": 0, "oversize": False,
        }
        header, _ = link_serving._serialize_artifact(self._artifact([entry]))
        assert header["attachments"] == [entry]

    @pytest.mark.parametrize("bad", [
        {"raw_sha256": "deadbeef"},        # too short
        {"raw_sha256": ""},                 # empty
        {"raw_sha256": "A" * 64},          # uppercase (non-canonical)
        {"raw_sha256": "a" * 63},          # 63 chars
        {"raw_sha256": "g" * 64},          # non-hex char
        {"total_size": True},               # bool is an int subclass
        {"total_size": -1},                 # negative
        {"ref": ""},                        # empty ref
        {"mime": ""},                       # empty mime
        {"name": "x" * 256},               # name too long
        {"extra": True},                    # unknown key
    ])
    def test_invalid_entry_rejected(self, bad):
        entry = {
            "ref": "r1", "name": "n", "mime": "text/plain",
            "raw_sha256": "a" * 64, "total_size": 1, "oversize": False,
        }
        entry.update(bad)
        with pytest.raises(ValueError):
            link_serving._serialize_artifact(self._artifact([entry]))

    def test_duplicate_ref_rejected(self):
        entry = {
            "ref": "dup", "name": "n", "mime": "text/plain",
            "raw_sha256": "a" * 64, "total_size": 1, "oversize": False,
        }
        with pytest.raises(ValueError):
            link_serving._serialize_artifact(self._artifact([entry, dict(entry)]))


class TestDesignResolvers:
    @pytest.fixture
    def deck(self, env):
        """A two-revision design; returns (design_id, rev1_id, rev2_id)."""
        rev1 = design_db.create_design(
            title="OSS Insights binder",
            variants=[{"id": "v1", "html": "<html><body>rev one</body></html>"}],
        )
        rev2 = design_db.create_design(
            title="OSS Insights binder",
            design_id=rev1,
            variants=[
                {"id": "v2a", "html": "<html><body>rev two draft</body></html>"},
                {"id": "v2b", "html": "<html><body>rev two final</body></html>"},
            ],
        )
        return rev1, rev1, rev2

    def test_present_serves_latest_revision(self, deck):
        design_id, _rev1, _rev2 = deck
        token = _token(40)
        put_grant(token, design_id, "present")
        header, body = parse(serve(token))
        assert header["status"] == "ok"
        assert header["kind"] == "present"
        assert "content" not in header
        assert sliced(body, header["viewer"]) == b"<html><body>rev two final</body></html>"

    def test_design_serves_exact_revision_only(self, deck):
        _design_id, rev1, rev2 = deck
        token = _token(41)
        put_grant(token, rev1, "design")
        header, body = parse(serve(token))
        assert header["kind"] == "design" and "content" not in header
        assert sliced(body, header["viewer"]) == b"<html><body>rev one</body></html>"

        token2 = _token(42)
        put_grant(token2, rev2, "design")
        header2, body2 = parse(serve(token2))
        assert sliced(body2, header2["viewer"]) == b"<html><body>rev two final</body></html>"

    def test_selected_variant_wins(self, deck):
        _design_id, _rev1, rev2 = deck
        design_db.submit_results(rev2, [{"id": "v2a", "rank": 1}])
        token = _token(43)
        put_grant(token, rev2, "design")
        header, body = parse(serve(token))
        assert sliced(body, header["viewer"]) == b"<html><body>rev two draft</body></html>"

    def test_unknown_design_refused(self, deck):
        token = _token(44)
        put_grant(token, str(uuid.uuid4()), "present")
        assert serve(token) == link_serving.REFUSED


class TestMissionResolver:
    """Deliberately the opposite of TestDesignResolvers: a mission grant
    must NOT pin to a revision the way `design` does (a714c09a-ccd's "BUG"
    section) -- it always serves whatever is current, matching `note`."""

    @pytest.fixture(autouse=True)
    def _mission_db(self, env, tmp_path, monkeypatch):
        # `env` isolates GRAPH_DB/GRAPH_ORG (grants live there via
        # put_grant) -- without it this class writes into whatever graph
        # DB the ambient environment happens to have live, exactly the
        # cross-test collision TestDesignResolvers avoids by depending on
        # `env` through its own `deck` fixture.
        from tools.dashboard.dao import mission_control_db as mdb
        monkeypatch.setattr(mdb, "DB_PATH", tmp_path / "mission_control.db")
        mdb.init_db(tmp_path / "mission_control.db")
        self.mdb = mdb

    # A mission grant's schema now REQUIRES meta.participant_id
    # (auto-xwamk) -- every put_grant(..., "mission") call below carries
    # one. It's not exercised by these serving-layer tests (that's
    # auto-u0kxw's job, once the write op reads it), just present because
    # a mission grant without one is no longer a valid grant to construct.
    _PARTICIPANT = "guest:22222222-2222-4222-8222-222222222222"

    def test_mission_serves_current_revision_not_pinned(self):
        mission = self.mdb.create_mission("OSS Insights")
        mission_id = mission["mission_id"]
        self.mdb.push_site_revision(mission_id, "<html>rev one</html>", "first")
        token = _token(50)
        put_grant(token, mission_id, "mission", meta={"participant_id": self._PARTICIPANT})
        header, body = parse(serve(token))
        assert header["kind"] == "mission" and "content" not in header
        served = sliced(body, header["viewer"])
        # The author's document, byte for byte, inside the composed screen.
        assert b"<html>rev one</html>" in served
        assert served.startswith(b'<!doctype html>\n<base href="about:srcdoc">')

        # Same grant, same token -- push two more revisions and re-fetch.
        # A `design`-style pinned resolver would still show rev one here;
        # this must show the latest, unpinned, exactly like `note`.
        self.mdb.push_site_revision(mission_id, "<html>rev two</html>", "second")
        self.mdb.push_site_revision(mission_id, "<html>rev three</html>", "third")
        header2, body2 = parse(serve(token))
        served2 = sliced(body2, header2["viewer"])
        assert b"<html>rev three</html>" in served2
        assert b"<html>rev one</html>" not in served2

    def test_mission_with_no_revision_yet_refused(self):
        mission = self.mdb.create_mission("Empty Mission")
        token = _token(51)
        put_grant(token, mission["mission_id"], "mission", meta={"participant_id": self._PARTICIPANT})
        assert serve(token) == link_serving.REFUSED

    def test_unknown_mission_refused(self):
        token = _token(52)
        put_grant(token, str(uuid.uuid4()), "mission", meta={"participant_id": self._PARTICIPANT})
        assert serve(token) == link_serving.REFUSED

    def test_unknown_artifact_fields_are_rejected(self):
        """viewer, parts, attachments, branding. `content` was note's private
        shape and no longer exists at this layer."""
        with pytest.raises(ValueError, match="unknown fields"):
            link_serving._serialize_artifact(
                {"kind": "mission", "viewer": b"<html></html>", "content": {}}
            )


def test_check_grant_reads_owning_scope_not_composed(monkeypatch):
    """The serving gate reads owning scope (P2): a peer-published grant — one
    a peer-COMPOSED read WOULD surface — must never be servable through
    check_grant. Distinguishes the readers by RETURN VALUE (not by raising,
    which check_grant would swallow): composed returns a VALID peer grant,
    owning returns empty; owning scope must win. Regression for the read-scope
    fix recovered from the retired write-guard commit."""
    token = _token(4242)
    peer_payload = {
        "token": token,
        "target_uuid": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "target_type": "note",
        "meta": {},
        "subject": {"kind": "operator", "id": "peer-op"},
        "issued_at": _iso(time.time()),
    }

    class _Member:
        key = token
        payload = peer_payload

    class _Composed:
        members = [_Member()]

    class _Owned:
        members = []

    monkeypatch.setattr(settings_ops, "read_set",
                        lambda *_a, **_k: _Composed())
    monkeypatch.setattr(settings_ops, "read_owned_set",
                        lambda *_a, **_k: _Owned())
    # Composed would return the valid peer grant; owning scope makes it
    # unservable. If check_grant regressed to read_set this returns non-None.
    assert link_serving.check_grant(token, org=ORG) is None
