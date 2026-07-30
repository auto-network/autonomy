"""Headless window/resume acceptance for attachment download v1 (auto-ed336)."""

from __future__ import annotations

import asyncio
import hashlib
import inspect

import pytest

from tools.dashboard import link_serving
from tools.graph import ops as graph_ops
from tools.graph import settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_LINK_GRANT_SET_ID,
)
from tools.network.idkit import canonical_json
from tools.network.relaykit.attachment_download import (
    CHUNK,
    EOF,
    LAST_IN_WINDOW,
    WINDOW,
    AttachmentAlreadyActive,
    AttachmentAssemblyError,
    AttachmentDisconnected,
    AttachmentDownloader,
    AttachmentRefused,
    MemoryAttachmentSink,
    MemoryCursorStore,
    cursor_id,
)

from .test_attachment_fetch import ORG, _note_with_attachment, _token, env, put_grant


def _manifest(ref: str) -> dict:
    attachment = graph_ops.get_attachment(ref, org=ORG, peers=[])
    return {
        "ref": ref,
        "raw_sha256": attachment["hash"],
        "total_size": attachment["size_bytes"],
        "oversize": False,
    }


def _grant_fetch(token: str):
    handler = link_serving.make_grant_handler(ORG)

    async def fetch(request: dict):
        response = handler(token, canonical_json(request))
        if inspect.isawaitable(response):
            response = await response
        return response

    return fetch


class DisconnectingFetch:
    """Wrap the real handler and drop before one selected body frame."""

    def __init__(self, fetch):
        self.fetch = fetch
        self.cut_at: int | None = None
        self.requests: list[dict] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def __call__(self, request: dict):
        self.requests.append(dict(request))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        response = await self.fetch(request)

        async def messages():
            close = getattr(response, "aclose", None)
            try:
                async for message in response:
                    if len(message) >= 9 and not message.startswith(b"{"):
                        offset = int.from_bytes(message[:8], "big")
                        end = offset + len(message[9:])
                        if (
                            self.cut_at is not None
                            and offset <= self.cut_at < end
                        ):
                            self.cut_at = None
                            raise ConnectionError("forced channel drop")
                    yield message
            finally:
                self.in_flight -= 1
                if close is not None:
                    await close()

        return messages()


def _driver(manifest, token, note_id, sink, cursors, fetch):
    return AttachmentDownloader(
        manifest,
        link_token=token,
        note_id=note_id,
        sink=sink,
        cursors=cursors,
        fetch_window=fetch,
    )


@pytest.mark.asyncio
async def test_three_disconnects_resume_only_committed_suffix(env, tmp_path):
    data = (bytes(range(251)) * ((12 * CHUNK + 123) // 251 + 1))[
        : 12 * CHUNK + 123
    ]
    note, ref = _note_with_attachment(tmp_path, data)
    token = _token(40)
    put_grant(token, note["id"], "note")
    manifest = _manifest(ref)
    sink = MemoryAttachmentSink()
    cursors = MemoryCursorStore()
    fetch = DisconnectingFetch(_grant_fetch(token))
    downloader = _driver(
        manifest, token, note["id"], sink, cursors, fetch
    )

    for cut, committed in (
        (CHUNK + 117, CHUNK),
        (4 * CHUNK + 19, 4 * CHUNK),
        (9 * CHUNK + 7, 9 * CHUNK),
    ):
        fetch.cut_at = cut
        with pytest.raises(AttachmentDisconnected):
            await downloader.run()
        record = cursors.get(downloader.cursor_key)
        assert record["committed_offset"] == committed
        assert sink.size == committed

    result = await downloader.run()
    assert result.status == "complete"
    assert [r["offset"] for r in fetch.requests] == [
        0,
        CHUNK,
        4 * CHUNK,
        9 * CHUNK,
    ]
    assert all(r["length"] == WINDOW for r in fetch.requests)
    assert fetch.max_in_flight == 1
    assert sink.data == data
    assert hashlib.sha256(sink.data).hexdigest() == manifest["raw_sha256"]
    assert downloader.cursor_key not in cursors.records


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_field", ["raw_sha256", "total_size"])
async def test_resume_identity_mismatch_restarts_at_zero(
    env, tmp_path, changed_field
):
    data = b"a" * (CHUNK + 17)
    note, ref = _note_with_attachment(tmp_path, data)
    token = _token(41)
    put_grant(token, note["id"], "note")
    manifest = _manifest(ref)
    sink = MemoryAttachmentSink()
    await sink.write_at(0, b"x" * CHUNK)
    cursors = MemoryCursorStore()
    downloader = _driver(
        manifest,
        token,
        note["id"],
        sink,
        cursors,
        _grant_fetch(token),
    )
    record = downloader._cursor_record(CHUNK)
    if changed_field == "raw_sha256":
        record[changed_field] = "f" * 64
    else:
        record[changed_field] += CHUNK
    cursors.put(downloader.cursor_key, record)
    requests = []
    real_fetch = downloader.fetch_window

    async def tracked(request):
        requests.append(dict(request))
        return await real_fetch(request)

    downloader.fetch_window = tracked
    await downloader.run()
    assert requests[0]["offset"] == 0
    assert sink.data == data


@pytest.mark.asyncio
async def test_missing_sink_prefix_restarts_instead_of_padding_zeros(
    env, tmp_path
):
    data = b"z" * (CHUNK + 5)
    note, ref = _note_with_attachment(tmp_path, data)
    token = _token(42)
    put_grant(token, note["id"], "note")
    sink = MemoryAttachmentSink()
    cursors = MemoryCursorStore()
    requests = []
    real_fetch = _grant_fetch(token)

    async def tracked(request):
        requests.append(dict(request))
        return await real_fetch(request)

    downloader = _driver(
        _manifest(ref), token, note["id"], sink, cursors, tracked
    )
    cursors.put(
        downloader.cursor_key, downloader._cursor_record(CHUNK)
    )
    await downloader.run()
    assert requests[0]["offset"] == 0
    assert sink.data == data


@pytest.mark.asyncio
async def test_cursor_offset_past_total_restarts_at_zero(env, tmp_path):
    data = b"p" * (2 * CHUNK)
    note, ref = _note_with_attachment(tmp_path, data)
    token = _token(49)
    put_grant(token, note["id"], "note")
    sink = MemoryAttachmentSink()
    await sink.write_at(0, b"x" * len(data))
    cursors = MemoryCursorStore()
    requests = []
    real_fetch = _grant_fetch(token)

    async def tracked(request):
        requests.append(dict(request))
        return await real_fetch(request)

    downloader = _driver(
        _manifest(ref), token, note["id"], sink, cursors, tracked
    )
    bad_record = downloader._cursor_record(3 * CHUNK)
    cursors.put(downloader.cursor_key, bad_record)
    await downloader.run()
    assert requests[0]["offset"] == 0
    assert sink.data == data


@pytest.mark.asyncio
async def test_pause_commits_window_and_next_run_resumes(env, tmp_path):
    data = b"b" * (WINDOW + CHUNK + 3)
    note, ref = _note_with_attachment(tmp_path, data)
    token = _token(43)
    put_grant(token, note["id"], "note")
    sink = MemoryAttachmentSink()
    cursors = MemoryCursorStore()
    requests = []
    real_fetch = _grant_fetch(token)

    async def tracked(request):
        requests.append(dict(request))
        return await real_fetch(request)

    downloader = _driver(
        _manifest(ref), token, note["id"], sink, cursors, tracked
    )
    paused = await downloader.run(max_windows=1)
    assert (paused.status, paused.committed_offset) == ("paused", WINDOW)
    assert sink.size == WINDOW
    assert cursors.get(downloader.cursor_key)["committed_offset"] == WINDOW
    complete = await downloader.run()
    assert complete.status == "complete"
    assert [request["offset"] for request in requests] == [0, WINDOW]
    assert sink.data == data


def _remove_grant(token: str) -> None:
    members = settings_ops.read_owned_set(
        NETWORK_LINK_GRANT_SET_ID, org=ORG
    ).members
    grant = next(member for member in members if member.key == token)
    settings_ops.remove_setting(grant.id, org=ORG)


@pytest.mark.asyncio
async def test_revoke_mid_transfer_stops_at_committed_window(env, tmp_path):
    data = b"c" * (WINDOW + CHUNK)
    note, ref = _note_with_attachment(tmp_path, data)
    token = _token(44)
    put_grant(token, note["id"], "note")
    requests = []
    real_fetch = _grant_fetch(token)

    async def revoke_before_second_window(request):
        requests.append(dict(request))
        if len(requests) == 2:
            _remove_grant(token)
        return await real_fetch(request)

    sink = MemoryAttachmentSink()
    cursors = MemoryCursorStore()
    downloader = _driver(
        _manifest(ref),
        token,
        note["id"],
        sink,
        cursors,
        revoke_before_second_window,
    )
    with pytest.raises(AttachmentRefused) as refusal:
        await downloader.run()
    assert refusal.value.code == "not_authorized"
    assert [request["offset"] for request in requests] == [0, WINDOW]
    assert sink.size == WINDOW
    assert cursors.get(downloader.cursor_key)["committed_offset"] == WINDOW


def _one_message_stream(message: bytes):
    async def messages():
        yield message

    return messages()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        (CHUNK).to_bytes(8, "big") + bytes([LAST_IN_WINDOW]) + b"x",
        (0).to_bytes(8, "big") + bytes([LAST_IN_WINDOW]) + b"x",
        (0).to_bytes(8, "big") + bytes([EOF]) + b"x" * CHUNK,
        (0).to_bytes(8, "big") + bytes([0x04]) + b"x" * CHUNK,
        (0).to_bytes(8, "big") + b"\x00" + b"x" * (CHUNK + 1),
    ],
    ids=[
        "noncontiguous",
        "early-last",
        "early-eof",
        "unknown-flags",
        "oversized-chunk",
    ],
)
async def test_assembly_invariant_violation_commits_nothing(message):
    manifest = {
        "ref": "att-test",
        "raw_sha256": "a" * 64,
        "total_size": 2 * CHUNK,
    }
    sink = MemoryAttachmentSink()
    cursors = MemoryCursorStore()
    downloader = _driver(
        manifest,
        _token(45),
        "note-test",
        sink,
        cursors,
        lambda request: _one_message_stream(message),
    )
    with pytest.raises(AttachmentAssemblyError):
        await downloader.run()
    assert sink.size == 0
    assert cursors.get(downloader.cursor_key)["committed_offset"] == 0


@pytest.mark.asyncio
async def test_one_active_writer_per_cursor():
    manifest = {
        "ref": "att-test",
        "raw_sha256": "a" * 64,
        "total_size": CHUNK,
    }
    sink = MemoryAttachmentSink()
    cursors = MemoryCursorStore()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_fetch(request):
        async def messages():
            entered.set()
            await release.wait()
            yield (
                (0).to_bytes(8, "big")
                + bytes([LAST_IN_WINDOW | EOF])
                + b"x" * CHUNK
            )

        return messages()

    first = _driver(
        manifest, _token(46), "note-test", sink, cursors, blocked_fetch
    )
    second = _driver(
        manifest, _token(46), "note-test", sink, cursors, blocked_fetch
    )
    task = asyncio.create_task(first.run())
    await entered.wait()
    with pytest.raises(AttachmentAlreadyActive):
        await second.run()
    release.set()
    await task


@pytest.mark.asyncio
async def test_task_cancellation_discards_only_uncommitted_tail():
    manifest = {
        "ref": "att-test",
        "raw_sha256": "a" * 64,
        "total_size": 2 * WINDOW,
    }
    sink = MemoryAttachmentSink()
    cursors = MemoryCursorStore()
    blocked = asyncio.Event()

    async def interrupted_window(request):
        yield (0).to_bytes(8, "big") + b"\x00" + b"x" * CHUNK
        yield CHUNK.to_bytes(8, "big") + b"\x00" + b"tail"
        blocked.set()
        await asyncio.Event().wait()

    downloader = _driver(
        manifest,
        _token(50),
        "note-test",
        sink,
        cursors,
        interrupted_window,
    )
    task = asyncio.create_task(downloader.run())
    await blocked.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sink.size == CHUNK
    assert cursors.get(downloader.cursor_key)["committed_offset"] == CHUNK


@pytest.mark.asyncio
async def test_cancel_clears_staged_bytes_and_cursor():
    manifest = {
        "ref": "att-test",
        "raw_sha256": "a" * 64,
        "total_size": 2 * WINDOW,
    }
    sink = MemoryAttachmentSink()
    cursors = MemoryCursorStore()

    async def one_window(request):
        for offset in range(0, WINDOW, CHUNK):
            flags = LAST_IN_WINDOW if offset + CHUNK == WINDOW else 0
            yield (
                offset.to_bytes(8, "big")
                + bytes([flags])
                + b"x" * CHUNK
            )

    downloader = _driver(
        manifest, _token(47), "note-test", sink, cursors, one_window
    )
    result = await downloader.run(max_windows=1)
    assert result.status == "paused"
    await downloader.cancel()
    assert sink.size == 0
    assert downloader.cursor_key not in cursors.records


def test_cursor_id_does_not_store_bearer_token():
    token = _token(48)
    value = cursor_id(token)
    assert value == (
        "030d17857a87e496dcd49eb185f728f4"
        "a47336a3db79ef8263f55a87601fb129"
    )
    assert token not in value
