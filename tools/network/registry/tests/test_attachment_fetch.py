"""auto-1lrip: on-demand attachment.fetch over the share-link channel.

Drives the real grant handler (link_serving.make_grant_handler) with real
notes, attachments, and grants in a tmp GRAPH_DB. Covers:

* windowed reconstruction of a multi-window file and SHA-256 == the stored
  hash, with correct LAST_IN_WINDOW / EOF flags at window and file ends;
* byte-exact conformance to the frozen cross-language vectors
  (attachment_v1.json), including a mid-file window and an EOF window;
* seek-not-whole-file: a second-window fetch serves only that window;
* the D4 authorization attack set — each serves zero body bytes and a typed
  error: unknown/forged ref, another note's attachment, a non-attachment
  ref, out-of-range offset, a path-traversal file_path, and a non-note grant;
* malformed requests are hard bad requests; cancel is accepted; a zero-byte
  attachment yields one empty EOF frame.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import time
import uuid

import pytest

import agents.design_db as design_db
from tools.dashboard import attachment_serving, link_serving
from tools.graph import ops as graph_ops
from tools.graph import settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_LINK_GRANT_REVISION,
    NETWORK_LINK_GRANT_SET_ID,
)
from tools.network.idkit import canonical_json

ORG = "netorg"
ISO = "%Y-%m-%dT%H:%M:%SZ"
CHUNK = attachment_serving.CHUNK
WINDOW = attachment_serving.WINDOW

LAST_IN_WINDOW = 0x01
EOF = 0x02


def _token(n: int) -> str:
    return f"{n:032x}"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "graph.db"))
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.setattr(design_db, "DB_PATH", tmp_path / "designs.db")
    monkeypatch.setattr(design_db, "_initialized", False)
    yield tmp_path
    GraphDB.close_all_pooled()


def put_grant(token: str, target_uuid: str, target_type: str, *,
              meta: dict | None = None) -> None:
    settings_ops.add_setting(
        NETWORK_LINK_GRANT_SET_ID, NETWORK_LINK_GRANT_REVISION, token,
        {
            "token": token,
            "url": f"https://relay.auto.network/l/{token}",
            "target_uuid": target_uuid,
            "target_type": target_type,
            "meta": meta or {},
            "subject": {"kind": "operator", "id": "op-1"},
            "issued_at": time.strftime(ISO, time.gmtime(time.time())),
        },
        org=ORG,
    )


def _note_with_attachment(tmp_path, data: bytes, name: str = "blob.bin"):
    path = tmp_path / name
    path.write_bytes(data)
    note = graph_ops.create_note(
        "![a]({1})", title="N", attachments=[str(path)], org=ORG,
    )
    return note, note["attachments"][0]["id"]


def _messages(request_obj: dict, token: str) -> list[bytes]:
    """Drive one request through the real grant handler; collect messages."""
    handler = link_serving.make_grant_handler(ORG)

    async def run():
        resp = handler(token, canonical_json(request_obj))
        if inspect.isawaitable(resp):
            resp = await resp
        if resp is None:
            return []
        if isinstance(resp, (bytes, bytearray)):
            return [bytes(resp)]
        out = []
        async for msg in resp:
            out.append(bytes(msg))
        return out

    return asyncio.run(run())


def _decode_body(msg: bytes):
    offset = int.from_bytes(msg[:8], "big")
    flags = msg[8]
    return offset, flags, msg[9:]


def _fetch(token: str, ref: str, offset: int, length: int | None = None):
    req = {"v": 1, "op": "attachment.fetch", "ref": ref, "offset": offset}
    if length is not None:
        req["length"] = length
    return _messages(req, token)


# ── windowed reconstruction ────────────────────────────────────────


def test_windows_reconstruct_file_with_correct_flags(env, tmp_path):
    data = os.urandom(WINDOW + CHUNK + 123)  # spans two windows, short tail
    note, ref = _note_with_attachment(tmp_path, data)
    total = len(data)
    stored_hash = graph_ops.get_attachment(ref, org=ORG, peers=[])["hash"]
    token = _token(1)
    put_grant(token, note["id"], "note")

    assembled = bytearray()
    offset = 0
    saw_eof = False
    while not saw_eof:
        msgs = _fetch(token, ref, offset)
        assert msgs, "a window must produce at least one frame"
        expected = offset
        for i, msg in enumerate(msgs):
            o, flags, chunk = _decode_body(msg)
            assert o == expected  # contiguous, starts at requested offset
            assert len(chunk) <= CHUNK
            assembled.extend(chunk)
            expected += len(chunk)
            last = i == len(msgs) - 1
            assert bool(flags & LAST_IN_WINDOW) == last
            if flags & EOF:
                assert last and expected == total
                saw_eof = True
        # window served no more than the cap and no more than remained
        assert expected - offset <= WINDOW
        offset = expected

    assert bytes(assembled) == data
    assert hashlib.sha256(assembled).hexdigest() == stored_hash


def test_first_window_ends_mid_file_without_eof(env, tmp_path):
    data = os.urandom(WINDOW + CHUNK)  # exactly one full window then more
    note, ref = _note_with_attachment(tmp_path, data)
    token = _token(2)
    put_grant(token, note["id"], "note")
    msgs = _fetch(token, ref, 0)
    o, flags, _ = _decode_body(msgs[-1])
    assert flags & LAST_IN_WINDOW  # window boundary
    assert not (flags & EOF)       # but not end of file
    assert sum(len(_decode_body(m)[2]) for m in msgs) == WINDOW


# ── seek, not whole-file read ──────────────────────────────────────


def test_second_window_serves_only_that_window(env, tmp_path):
    data = os.urandom(2 * WINDOW + 7)
    note, ref = _note_with_attachment(tmp_path, data)
    token = _token(3)
    put_grant(token, note["id"], "note")
    msgs = _fetch(token, ref, WINDOW)  # the SECOND window
    first_offset, _, _ = _decode_body(msgs[0])
    assert first_offset == WINDOW  # seeked; did not restart at 0
    served = b"".join(_decode_body(m)[2] for m in msgs)
    assert len(served) == WINDOW
    assert served == data[WINDOW:2 * WINDOW]


# ── conformance to the frozen cross-language vectors ───────────────


def test_conforms_to_frozen_attachment_v1_vectors(env, tmp_path):
    # The exact payload attachment_v1.json is generated from.
    chunk_a = bytes(range(256)) * (CHUNK // 256)
    chunk_b = bytes(reversed(range(256))) * (CHUNK // 256)
    raw = chunk_a + chunk_b + b"end"
    note, ref = _note_with_attachment(tmp_path, raw, name="vector.bin")
    token = _token(4)
    put_grant(token, note["id"], "note")

    # POSITIVE[0] two_messages_end_mid_file: offset 0, length 2 MiB.
    msgs = _fetch(token, ref, 0, 2 * CHUNK)
    assert [_decode_body(m)[:2] for m in msgs] == [(0, 0), (CHUNK, LAST_IN_WINDOW)]
    assert _decode_body(msgs[0])[2] == chunk_a
    assert _decode_body(msgs[1])[2] == chunk_b

    # POSITIVE[1] resume_window_reaches_eof: offset 2 MiB, length 1 MiB.
    msgs = _fetch(token, ref, 2 * CHUNK, CHUNK)
    assert len(msgs) == 1
    o, flags, chunk = _decode_body(msgs[0])
    assert (o, flags, chunk) == (2 * CHUNK, LAST_IN_WINDOW | EOF, b"end")


# ── D4 authorization attack set (each: zero body bytes + typed error) ──


def _assert_error(msgs, code: str):
    """Exactly one error message with the given code, and no body frame."""
    assert len(msgs) == 1
    err = json.loads(msgs[0])
    assert err["v"] == 1 and err["op"] == "error" and err["code"] == code


def test_unknown_ref_refused(env, tmp_path):
    note, _ = _note_with_attachment(tmp_path, os.urandom(CHUNK))
    token = _token(10)
    put_grant(token, note["id"], "note")
    _assert_error(_fetch(token, "att-does-not-exist", 0), "not_authorized")


def test_other_notes_attachment_refused(env, tmp_path):
    note1, _ = _note_with_attachment(tmp_path, os.urandom(CHUNK), name="a.bin")
    note2, ref2 = _note_with_attachment(tmp_path, os.urandom(CHUNK), name="b.bin")
    token = _token(11)
    put_grant(token, note1["id"], "note")  # grant is for note1
    _assert_error(_fetch(token, ref2, 0), "not_authorized")  # ref2 is note2's


def test_non_attachment_ref_refused(env, tmp_path):
    note, _ = _note_with_attachment(tmp_path, os.urandom(CHUNK))
    token = _token(12)
    put_grant(token, note["id"], "note")
    _assert_error(_fetch(token, note["id"], 0), "not_authorized")  # a note id


def test_out_of_range_offset_refused(env, tmp_path):
    note, ref = _note_with_attachment(tmp_path, os.urandom(CHUNK + 5))
    token = _token(13)
    put_grant(token, note["id"], "note")
    _assert_error(_fetch(token, ref, 8 * CHUNK), "out_of_range")


def test_path_traversal_file_path_refused(env, tmp_path):
    from tools.graph.db import GraphDB

    note, ref = _note_with_attachment(tmp_path, os.urandom(CHUNK))
    escape = tmp_path / "outside.secret"
    escape.write_bytes(b"SECRET")
    db = GraphDB(os.environ["GRAPH_DB"])
    try:
        db.conn.execute(
            "UPDATE attachments SET file_path = ? WHERE id = ?",
            (str(escape), ref),
        )
        db.conn.commit()
    finally:
        db.close()
    token = _token(14)
    put_grant(token, note["id"], "note")
    msgs = _fetch(token, ref, 0)
    _assert_error(msgs, "not_found")
    assert b"SECRET" not in b"".join(msgs)


def test_non_note_grant_refused(env, tmp_path):
    note, ref = _note_with_attachment(tmp_path, os.urandom(CHUNK))
    token = _token(15)
    put_grant(token, str(uuid.uuid4()), "design")
    # A design grant must never serve an attachment, even a real note ref.
    _assert_error(_fetch(token, ref, 0), "not_authorized")


def test_oversize_attachment_refused(env, tmp_path, monkeypatch):
    note, ref = _note_with_attachment(tmp_path, os.urandom(CHUNK + 1))
    monkeypatch.setattr(attachment_serving, "MAX_ATTACHMENT_BYTES", CHUNK)
    token = _token(16)
    put_grant(token, note["id"], "note")
    _assert_error(_fetch(token, ref, 0), "oversize")


# ── malformed shape, cancel, zero-byte ─────────────────────────────


@pytest.mark.parametrize("req", [
    {"v": 1, "op": "attachment.fetch", "ref": "r", "offset": 1},          # not CHUNK-aligned
    {"v": 1, "op": "attachment.fetch", "ref": "r", "offset": -CHUNK},     # negative
    {"v": 1, "op": "attachment.fetch", "ref": "r", "offset": True},       # bool
    {"v": 1, "op": "attachment.fetch", "ref": "", "offset": 0},           # empty ref
    {"v": 1, "op": "attachment.fetch", "ref": "r", "offset": 0, "length": 0},       # zero length
    {"v": 1, "op": "attachment.fetch", "ref": "r", "offset": 0, "length": CHUNK + 1},  # not aligned
    {"v": 1, "op": "attachment.fetch", "ref": "r", "offset": 0, "extra": 1},         # extra key
])
def test_malformed_fetch_is_bad_request(env, req):
    token = _token(20)
    put_grant(token, str(uuid.uuid4()), "note")
    assert _messages(req, token) == [link_serving.BAD_REQUEST]


def test_cancel_is_accepted_with_no_response(env, tmp_path):
    note, ref = _note_with_attachment(tmp_path, os.urandom(CHUNK))
    token = _token(21)
    put_grant(token, note["id"], "note")
    assert _messages(
        {"v": 1, "op": "attachment.cancel", "ref": ref}, token
    ) == []
    assert _messages(
        {"v": 1, "op": "attachment.cancel", "ref": ""}, token
    ) == [link_serving.BAD_REQUEST]


def test_zero_byte_attachment_yields_one_empty_eof_frame(env, tmp_path):
    note, ref = _note_with_attachment(tmp_path, b"", name="empty.bin")
    token = _token(22)
    put_grant(token, note["id"], "note")
    msgs = _fetch(token, ref, 0)
    assert len(msgs) == 1
    o, flags, chunk = _decode_body(msgs[0])
    assert (o, flags, chunk) == (0, LAST_IN_WINDOW | EOF, b"")


# ── version type confusion (bool is an int subclass) ───────────────


def test_boolean_version_is_bad_request_not_a_body(env, tmp_path):
    note, ref = _note_with_attachment(tmp_path, os.urandom(CHUNK))
    token = _token(23)
    put_grant(token, note["id"], "note")
    # {"v": true, ...} must NOT slip past the version gate and serve bytes.
    fetch = {"v": True, "op": "attachment.fetch", "ref": ref,
             "offset": 0, "length": CHUNK}
    assert _messages(fetch, token) == [link_serving.BAD_REQUEST]
    cancel = {"v": True, "op": "attachment.cancel", "ref": ref}
    assert _messages(cancel, token) == [link_serving.BAD_REQUEST]


# ── operational faults become a typed error, not a channel teardown ──


class _FakeHandle:
    def __init__(self, reads):
        self._reads = list(reads)
        self.closed = False

    def read(self, n):
        item = self._reads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


def test_db_fault_during_authorization_yields_unavailable(env, tmp_path, monkeypatch):
    note, ref = _note_with_attachment(tmp_path, os.urandom(CHUNK))
    token = _token(24)
    put_grant(token, note["id"], "note")

    def boom(*a, **k):
        raise OSError("database is locked")

    monkeypatch.setattr(graph_ops, "note_slot_attachments", boom)
    # Must be one typed error, not an exception that tears the channel down.
    _assert_error(_fetch(token, ref, 0), "unavailable")


def test_open_fault_after_authorization_yields_unavailable(env, tmp_path, monkeypatch):
    note, ref = _note_with_attachment(tmp_path, os.urandom(CHUNK))
    token = _token(25)
    put_grant(token, note["id"], "note")

    def boom(path, offset):
        raise FileNotFoundError(path)

    monkeypatch.setattr(attachment_serving, "_open_at", boom)
    _assert_error(_fetch(token, ref, 0), "unavailable")


def test_read_fault_yields_unavailable(env, tmp_path, monkeypatch):
    note, ref = _note_with_attachment(tmp_path, os.urandom(CHUNK))
    token = _token(26)
    put_grant(token, note["id"], "note")
    monkeypatch.setattr(
        attachment_serving, "_open_at",
        lambda path, offset: _FakeHandle([OSError("read error")]),
    )
    _assert_error(_fetch(token, ref, 0), "unavailable")


def test_file_shrinks_after_one_chunk_yields_prefix_then_unavailable(
    env, tmp_path, monkeypatch
):
    note, ref = _note_with_attachment(tmp_path, os.urandom(2 * CHUNK))
    token = _token(27)
    put_grant(token, note["id"], "note")
    # First read is a full chunk; the file then "shrinks" (short read).
    monkeypatch.setattr(
        attachment_serving, "_open_at",
        lambda path, offset: _FakeHandle([b"a" * CHUNK, b""]),
    )
    msgs = _fetch(token, ref, 0)
    assert len(msgs) == 2
    o, flags, chunk = _decode_body(msgs[0])
    assert (o, flags, chunk) == (0, 0, b"a" * CHUNK)  # valid prefix frame
    err = json.loads(msgs[1])
    assert err["op"] == "error" and err["code"] == "unavailable"


# ── length above the window cap clamps (frozen protocol), never rejects ──


def test_length_above_window_clamps_to_window(env, tmp_path):
    data = os.urandom(WINDOW + CHUNK)
    note, ref = _note_with_attachment(tmp_path, data)
    token = _token(28)
    put_grant(token, note["id"], "note")
    msgs = _fetch(token, ref, 0, 100 * CHUNK)  # far above the 8 MiB cap
    served = b"".join(_decode_body(m)[2] for m in msgs)
    assert len(served) == WINDOW  # clamped, not the whole file, not refused
    o, flags, _ = _decode_body(msgs[-1])
    assert flags & LAST_IN_WINDOW and not (flags & EOF)
