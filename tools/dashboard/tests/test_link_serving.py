"""C4 unit coverage: the I9 grant gate + the §6.1 target resolvers.

Everything here runs against real stores in a tmp GRAPH_DB /
designs DB — the grant cache is actual ``autonomy.network.link-grant``
Settings rows, targets are actual design revisions / notes / attachment
rows. The tunnel itself is exercised separately in
``test_link_serving_tunnel.py``; this file drives the handler directly.

Pinned behaviors:

* unknown, revoked (row removed), expired, and unresolvable tokens all
  return the SAME refusal bytes — a prober can't classify the failure;
* ``meta.ttl`` is enforced against ``issued_at`` (boundary inclusive);
* the file resolver only serves attachment paths that realpath-resolve
  inside an allowed root: ``..`` traversal, absolute escapes, symlink
  escapes, and the root dir itself are all refused;
* a ``note`` grant serves only note sources, with content HTML-escaped;
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


# ── the I9 gate ───────────────────────────────────────────────


class TestGrantGate:
    def test_unknown_token_refused(self, env):
        assert serve(_token(1)) == link_serving.REFUSED

    def test_malformed_tokens_refused(self, env):
        for bad in ("", "zz" * 16, _token(2)[:-1], _token(2) + "0",
                    _token(2).upper()):
            assert serve(bad) == link_serving.REFUSED

    def test_revoked_indistinguishable_from_unknown(self, env, tmp_path):
        root = tmp_path / "runs"
        root.mkdir()
        artifact = root / "out.txt"
        artifact.write_bytes(b"artifact bytes")
        token = _token(3)
        put_grant(token, _attach(artifact), "file")

        served = serve(token, file_roots=[str(root)])
        header, body = parse(served)
        assert header["status"] == 200 and body == b"artifact bytes"

        drop_grant(token)  # what link_revoke's executor does to the cache
        revoked = serve(token, file_roots=[str(root)])
        assert revoked == serve(_token(4), file_roots=[str(root)])  # unknown
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
        assert serve(_token(10), canonical_json(["fetch"])) == link_serving.BAD_REQUEST


class TestHeadOp:
    """The object HEAD: same gate + resolution as fetch, headers only.

    A valid HEAD returns status 200 with content_type and content_length
    but NO body (the liveness-probe shape — a large target costs no
    transfer). Every gate failure returns the SAME refusal as fetch, so a
    prober can neither classify the failure nor tell HEAD from fetch.
    """

    def test_head_returns_length_and_no_body(self, env, tmp_path):
        root = tmp_path / "runs"
        root.mkdir()
        artifact = root / "out.bin"
        artifact.write_bytes(b"x" * 5000)
        token = _token(20)
        put_grant(token, _attach(artifact), "file")

        fetched = serve(token, FETCH, file_roots=[str(root)])
        headed = serve(token, HEAD, file_roots=[str(root)])
        fh, fbody = parse(fetched)
        hh, hbody = parse(headed)

        assert fh["status"] == 200 and len(fbody) == 5000
        # Same status + content_type the fetch reports, plus the length…
        assert hh["status"] == 200
        assert hh["content_type"] == fh["content_type"]
        assert hh["content_length"] == 5000
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


# ── file resolver: the path allowlist ─────────────────────────


def _attach(path, *, mime: str | None = None) -> str:
    """Insert a raw attachment row pointing at *path*; returns its UUID."""
    from tools.graph.db import GraphDB
    from tools.graph.models import Attachment

    db = GraphDB(os.environ["GRAPH_DB"])
    try:
        att = Attachment(filename=os.path.basename(str(path)),
                         mime_type=mime, file_path=str(path))
        db.insert_attachment(att)
    finally:
        db.close()
    return att.id


class TestFileResolver:
    @pytest.fixture
    def runs_root(self, env):
        root = env / "agent-runs"
        (root / "run-1").mkdir(parents=True)
        (env / "outside.txt").write_bytes(b"SECRET")
        return root

    def _served(self, target: str, root) -> bytes:
        token = _token(20)
        put_grant(token, target, "file")
        try:
            return serve(token, file_roots=[str(root)])
        finally:
            drop_grant(token)

    def test_serves_inside_root_with_mime(self, runs_root):
        artifact = runs_root / "run-1" / "report.html"
        artifact.write_bytes(b"<h1>report</h1>")
        header, body = parse(self._served(_attach(artifact, mime="text/html"), runs_root))
        assert header == {"v": 1, "status": 200, "content_type": "text/html"}
        assert body == b"<h1>report</h1>"

    def test_absolute_path_outside_root_refused(self, runs_root):
        outside = runs_root.parent / "outside.txt"
        assert self._served(_attach(outside), runs_root) == link_serving.REFUSED

    def test_dotdot_traversal_refused(self, runs_root):
        sneaky = str(runs_root / "run-1" / ".." / ".." / "outside.txt")
        assert self._served(_attach(sneaky), runs_root) == link_serving.REFUSED

    def test_symlink_escape_refused(self, runs_root):
        link = runs_root / "run-1" / "innocent.txt"
        link.symlink_to(runs_root.parent / "outside.txt")
        assert self._served(_attach(link), runs_root) == link_serving.REFUSED

    def test_root_itself_refused(self, runs_root):
        assert self._served(_attach(runs_root), runs_root) == link_serving.REFUSED

    def test_missing_attachment_row_refused(self, runs_root):
        assert self._served(str(uuid.uuid4()), runs_root) == link_serving.REFUSED

    def test_prefix_sibling_dir_refused(self, runs_root):
        # /x/agent-runs-evil must not pass a /x/agent-runs allowlist.
        evil = runs_root.parent / (runs_root.name + "-evil")
        evil.mkdir()
        artifact = evil / "payload.txt"
        artifact.write_bytes(b"nope")
        assert self._served(_attach(artifact), runs_root) == link_serving.REFUSED


# ── note / design / present resolvers ─────────────────────────


class TestNoteResolver:
    def test_note_renders_escaped(self, env):
        note = graph_ops.create_note(
            "Line one\n\n<script>alert('xss')</script>",
            title="Ship <notes>", org=ORG,
        )
        token = _token(30)
        put_grant(token, note["id"], "note")
        header, body = parse(serve(token))
        text = body.decode("utf-8")
        assert header["status"] == 200
        assert header["content_type"].startswith("text/html")
        assert "<script>" not in text
        assert "&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;" in text
        assert "Ship &lt;notes&gt;" in text

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
        assert header["status"] == 200
        assert body == b"<html><body>rev two final</body></html>"

    def test_design_serves_exact_revision_only(self, deck):
        _design_id, rev1, rev2 = deck
        token = _token(41)
        put_grant(token, rev1, "design")
        _, body = parse(serve(token))
        assert body == b"<html><body>rev one</body></html>"

        token2 = _token(42)
        put_grant(token2, rev2, "design")
        _, body2 = parse(serve(token2))
        assert body2 == b"<html><body>rev two final</body></html>"

    def test_selected_variant_wins(self, deck):
        _design_id, _rev1, rev2 = deck
        design_db.submit_results(rev2, [{"id": "v2a", "rank": 1}])
        token = _token(43)
        put_grant(token, rev2, "design")
        _, body = parse(serve(token))
        assert body == b"<html><body>rev two draft</body></html>"

    def test_unknown_design_refused(self, deck):
        token = _token(44)
        put_grant(token, str(uuid.uuid4()), "present")
        assert serve(token) == link_serving.REFUSED


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
