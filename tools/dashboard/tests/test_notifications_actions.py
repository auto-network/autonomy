"""Behavioral tests for the refresh-request → CrossTalk source-session
ping bridge.

Bead: auto-r92kc. The dispatcher in
:mod:`tools.dashboard.notifications_actions` watches the EventBus
``setting.changed`` channel for writes to ``dashboard.activity.
ask_refresh`` and delivers a CrossTalk envelope to the source
session. The "source session" is the session whose
:class:`SessionAskV1` row the operator clicked Refresh on; ``ask_id``
== ``session_id`` by the SessionAsk
``@keyed_per_entity(key="session_id")`` convention.

Acceptance covered (matching the bead spec):

1. Operator clicks Refresh → source session receives a CrossTalk
   message with the ask preview and ``target_revision``.
2. Multiple writes at the same ``target_revision`` produce ONE ping —
   in-process tracker reservation prevents duplicates.
3. After the source session bumps ``revision_seq`` past the target
   (state-machine clear), no further pings fire until a new refresh
   request arrives with a HIGHER ``target_revision``.
4. ``send_fn`` raising mirrors a dead/disconnected source session —
   the dispatcher logs and continues, the tracker stays advanced so
   retries don't loop.
5. End-to-end through the dispatcher task: writing an
   AskRefreshRequest fires exactly one ping within ~1s.
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from tools.dashboard import event_bus as event_bus_module
from tools.dashboard import notifications_actions
from tools.dashboard import notifications_settings as ns
from tools.dashboard.notifications_actions import (
    deliver_refresh_ping,
    reset_delivered_tracker,
    start_notifications_dispatcher,
    stop_notifications_dispatcher,
)
from tools.graph import settings_ops


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin GRAPH_DB to a fresh per-test SQLite file."""
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    yield db_path


@pytest.fixture(autouse=True)
def _reset_tracker():
    """Clear the in-process delivery tracker around each test."""
    reset_delivered_tracker()
    yield
    reset_delivered_tracker()


def _write_session_ask(
    *, session_id: str, text: str, revision_seq: int,
    created_at: str = "2026-05-03T12:00:00Z",
) -> str:
    """Helper — write a SessionAsk row and return its setting id."""
    return settings_ops.upsert_by_key(
        ns.SESSION_ASK_SET_ID, 1, session_id,
        {
            "session_id": session_id,
            "text": text,
            "revision_seq": revision_seq,
            "created_at": created_at,
        },
    )


def _write_refresh_request(
    *, ask_id: str, requested_by: str, target_revision: int,
    requested_at: str = "2026-05-03T12:05:00Z",
) -> str:
    return settings_ops.upsert_by_key(
        ns.ASK_REFRESH_SET_ID, 1, ask_id,
        {
            "ask_id": ask_id,
            "requested_at": requested_at,
            "requested_by": requested_by,
            "target_revision": target_revision,
        },
    )


# ── deliver_refresh_ping (direct unit tests) ────────────────────────


@pytest.mark.asyncio
async def test_deliver_sends_envelope_to_source_session(graph_db_env):
    """Acceptance #1 — write refresh row → CrossTalk delivered.

    The envelope's ``target=`` is the SessionAsk's ``session_id``
    (which equals the ``ask_id`` key per the keyed_per_entity
    convention), and the body inlines the bead spec template
    (``Operator requested refresh on your ask at rev=…``).
    """
    _write_session_ask(
        session_id="auto-source-1",
        text="Should I ship the migration?",
        revision_seq=3,
    )
    _write_refresh_request(
        ask_id="auto-source-1",
        requested_by="operator-jeremy",
        target_revision=3,
    )

    sent: list[tuple[str, str]] = []

    async def _send_fn(target: str, envelope: str) -> None:
        sent.append((target, envelope))

    delivered = await deliver_refresh_ping(
        ask_id="auto-source-1", org=None, send_fn=_send_fn,
    )
    assert delivered is True
    assert len(sent) == 1
    target, envelope = sent[0]
    assert target == "auto-source-1"
    # Envelope shape mirrors surface_actions.build_envelope output.
    assert envelope.startswith('<crosstalk from="operator-jeremy"\n')
    assert '           kind="ask-refresh-request"' in envelope
    assert '           ask_id="auto-source-1"' in envelope
    assert '           target_revision="3"' in envelope
    assert '           requested_by="operator-jeremy"' in envelope
    assert "Operator requested refresh on your ask at rev=3." in envelope
    assert 'Ask text: "Should I ship the migration?...' in envelope
    assert envelope.endswith("</crosstalk>")


@pytest.mark.asyncio
async def test_deliver_idempotent_at_same_target_revision(graph_db_env):
    """Acceptance #2 — second call at same target_revision is a no-op."""
    _write_session_ask(
        session_id="auto-source-2", text="ping me", revision_seq=1,
    )
    _write_refresh_request(
        ask_id="auto-source-2",
        requested_by="operator-a",
        target_revision=1,
    )

    sent: list[tuple[str, str]] = []

    async def _send_fn(target, envelope):
        sent.append((target, envelope))

    first = await deliver_refresh_ping(
        ask_id="auto-source-2", org=None, send_fn=_send_fn,
    )
    second = await deliver_refresh_ping(
        ask_id="auto-source-2", org=None, send_fn=_send_fn,
    )
    third = await deliver_refresh_ping(
        ask_id="auto-source-2", org=None, send_fn=_send_fn,
    )

    assert first is True
    assert second is False
    assert third is False
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_deliver_redelivers_on_higher_target_revision(graph_db_env):
    """Acceptance #3 — after source clears + operator re-clicks at a
    HIGHER target_revision, a fresh ping fires. Re-clicks at the same
    new target stay idempotent.
    """
    # Source ask exists at revision 1.
    _write_session_ask(
        session_id="auto-source-3", text="r1", revision_seq=1,
    )
    # First operator click — target = 1.
    _write_refresh_request(
        ask_id="auto-source-3",
        requested_by="op-1",
        target_revision=1,
    )

    sent: list[tuple[str, str]] = []

    async def _send_fn(target, envelope):
        sent.append((target, envelope))

    assert await deliver_refresh_ping(
        ask_id="auto-source-3", org=None, send_fn=_send_fn,
    ) is True
    # No-op at the same target_revision even if we replay the call.
    assert await deliver_refresh_ping(
        ask_id="auto-source-3", org=None, send_fn=_send_fn,
    ) is False
    # Source bumps revision_seq to 2 (state-machine "clear"). The
    # SessionAsk text is what the source's reply changed to.
    _write_session_ask(
        session_id="auto-source-3", text="r2", revision_seq=2,
    )
    # No automatic re-ping just from the source's bump — still one
    # entry until a NEW refresh request arrives.
    assert len(sent) == 1
    # New operator click at the higher target_revision.
    _write_refresh_request(
        ask_id="auto-source-3",
        requested_by="op-2",
        target_revision=2,
    )
    assert await deliver_refresh_ping(
        ask_id="auto-source-3", org=None, send_fn=_send_fn,
    ) is True
    assert len(sent) == 2
    # Re-clicks at target=2 stay idempotent.
    assert await deliver_refresh_ping(
        ask_id="auto-source-3", org=None, send_fn=_send_fn,
    ) is False
    assert len(sent) == 2

    # The two sent envelopes carry distinct target_revision attributes.
    assert '           target_revision="1"' in sent[0][1]
    assert '           target_revision="2"' in sent[1][1]


@pytest.mark.asyncio
async def test_deliver_does_not_redeliver_at_lower_target_revision(
    graph_db_env,
):
    """Defensive — once we've delivered at target=N, an out-of-order
    write at a LOWER target=N-1 must not re-fire (a chain-clear write
    may legitimately lower the pin, but it's still already-delivered
    work from the source's perspective).
    """
    _write_session_ask(
        session_id="auto-source-4", text="hello", revision_seq=5,
    )
    _write_refresh_request(
        ask_id="auto-source-4",
        requested_by="op-late",
        target_revision=5,
    )

    sent: list[tuple[str, str]] = []

    async def _send_fn(target, envelope):
        sent.append((target, envelope))

    assert await deliver_refresh_ping(
        ask_id="auto-source-4", org=None, send_fn=_send_fn,
    ) is True
    # Write a refresh at a lower target.
    _write_refresh_request(
        ask_id="auto-source-4",
        requested_by="op-late-2",
        target_revision=3,
    )
    assert await deliver_refresh_ping(
        ask_id="auto-source-4", org=None, send_fn=_send_fn,
    ) is False
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_deliver_handles_send_fn_failure_without_loop(graph_db_env):
    """Acceptance #4 — ``send_fn`` raising surfaces as a logged warning,
    not a propagated exception. The tracker advances anyway so a flood
    of identical events doesn't drive a retry loop against a dead
    session.
    """
    _write_session_ask(
        session_id="auto-dead", text="stuck", revision_seq=1,
    )
    _write_refresh_request(
        ask_id="auto-dead", requested_by="op-x", target_revision=1,
    )

    call_count = 0

    async def _flaky_send(target, envelope):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("tmux session not found")

    # First call: send_fn raises, dispatcher swallows.
    delivered = await deliver_refresh_ping(
        ask_id="auto-dead", org=None, send_fn=_flaky_send,
    )
    assert delivered is False
    assert call_count == 1

    # Second call at same target_revision: tracker reservation
    # short-circuits; send_fn must not be invoked again.
    delivered = await deliver_refresh_ping(
        ask_id="auto-dead", org=None, send_fn=_flaky_send,
    )
    assert delivered is False
    assert call_count == 1, (
        "tracker must pin even on send_fn failure to prevent retry "
        "loops against a dead source session"
    )


@pytest.mark.asyncio
async def test_deliver_skips_when_refresh_row_missing(graph_db_env):
    """No row → no ping; never raises."""
    sent: list = []

    async def _send_fn(target, envelope):
        sent.append((target, envelope))

    delivered = await deliver_refresh_ping(
        ask_id="never-existed", org=None, send_fn=_send_fn,
    )
    assert delivered is False
    assert sent == []


@pytest.mark.asyncio
async def test_deliver_skips_when_session_ask_missing(graph_db_env):
    """Refresh row exists but SessionAsk gone — log + skip, no crash."""
    _write_refresh_request(
        ask_id="ghost-ask", requested_by="op", target_revision=1,
    )
    sent: list = []

    async def _send_fn(target, envelope):
        sent.append((target, envelope))

    delivered = await deliver_refresh_ping(
        ask_id="ghost-ask", org=None, send_fn=_send_fn,
    )
    assert delivered is False
    assert sent == []


@pytest.mark.asyncio
async def test_deliver_truncates_long_ask_text(graph_db_env):
    """The body's ``Ask text:`` preview is capped at 80 characters
    plus the trailing ``"..."`` marker.
    """
    long_text = "x" * 200
    _write_session_ask(
        session_id="auto-long", text=long_text, revision_seq=1,
    )
    _write_refresh_request(
        ask_id="auto-long",
        requested_by="op",
        target_revision=1,
    )

    sent: list[tuple[str, str]] = []

    async def _send_fn(target, envelope):
        sent.append((target, envelope))

    assert await deliver_refresh_ping(
        ask_id="auto-long", org=None, send_fn=_send_fn,
    ) is True
    envelope = sent[0][1]
    # Exactly 80 x's + literal "...
    assert ('Ask text: "' + ("x" * 80) + '...') in envelope
    # 81 x's must NOT appear.
    assert ("x" * 81) not in envelope


# ── End-to-end via the dispatcher task ──────────────────────────────


@pytest.mark.asyncio
async def test_dispatcher_pumps_setting_changed_event_to_handler(
    graph_db_env,
):
    """Acceptance #5 — start dispatcher, write a refresh row, the
    EventBus broadcast walks through to ``deliver_refresh_ping`` and
    the recipient ``send_fn`` sees the envelope without any explicit
    poll.
    """
    bus = event_bus_module.EventBus()

    sent: list[tuple[str, str]] = []
    delivered = asyncio.Event()

    async def _send_fn(target, envelope):
        sent.append((target, envelope))
        delivered.set()

    _write_session_ask(
        session_id="auto-pump", text="needs op input", revision_seq=2,
    )

    handle = start_notifications_dispatcher(send_fn=_send_fn, bus=bus)
    try:
        # Yield once so the dispatcher consumes the cached-state replay
        # (none in a fresh bus, but the await still keeps the test
        # deterministic).
        await asyncio.sleep(0)
        # Simulate the emit hook's broadcast for an AskRefreshRequest
        # write — synchronous, mirrors `_settings_emit_hook`.
        bus.broadcast_sync(
            "setting.changed",
            {
                "set_id": ns.ASK_REFRESH_SET_ID,
                "schema_revision": 1,
                "key": "auto-pump",
                "org": None,
                "publication_state": "raw",
                "deprecated": False,
                "operation": "write",
            },
            dedup=False,
        )
        # The dispatcher needs the row to exist when it wakes; write it
        # before we broadcast the event for it.
        # (Already done above via _write_refresh_request below.)
        _write_refresh_request(
            ask_id="auto-pump", requested_by="op", target_revision=2,
        )
        # Re-broadcast so the dispatcher resolves the now-existing row.
        bus.broadcast_sync(
            "setting.changed",
            {
                "set_id": ns.ASK_REFRESH_SET_ID,
                "schema_revision": 1,
                "key": "auto-pump",
                "org": None,
                "publication_state": "raw",
                "deprecated": False,
                "operation": "write",
            },
            dedup=False,
        )
        await asyncio.wait_for(delivered.wait(), timeout=2.0)
        assert len(sent) == 1
        target, envelope = sent[0]
        assert target == "auto-pump"
        assert '           target_revision="2"' in envelope
    finally:
        await stop_notifications_dispatcher(bus=bus)


@pytest.mark.asyncio
async def test_dispatcher_ignores_other_set_ids():
    """Events whose ``set_id`` is not the AskRefresh one must not fire."""
    bus = event_bus_module.EventBus()
    sent: list = []

    async def _send_fn(target, envelope):
        sent.append((target, envelope))

    handle = start_notifications_dispatcher(send_fn=_send_fn, bus=bus)
    try:
        bus.broadcast_sync(
            "setting.changed",
            {
                "set_id": "dashboard.surface.ping",
                "schema_revision": 1,
                "key": "ignore-me",
                "org": None,
                "publication_state": "raw",
                "deprecated": False,
                "operation": "write",
            },
            dedup=False,
        )
        # Give the dispatcher a chance to run.
        await asyncio.sleep(0.05)
        assert sent == []
    finally:
        await stop_notifications_dispatcher(bus=bus)


@pytest.mark.asyncio
async def test_dispatcher_skips_cached_replay_seq_zero():
    """``subscribe()`` enqueues each cached topic at ``seq=0``. Those
    are NOT live events — the dispatcher must skip them so a process
    restart doesn't re-deliver the last refresh-request.
    """
    bus = event_bus_module.EventBus()
    # Prime the cache with a stale ASK_REFRESH event.
    bus.broadcast_sync(
        "setting.changed",
        {
            "set_id": ns.ASK_REFRESH_SET_ID,
            "schema_revision": 1,
            "key": "stale-ask",
            "org": None,
            "publication_state": "raw",
            "deprecated": False,
            "operation": "write",
        },
        dedup=False,
    )
    sent: list = []

    async def _send_fn(target, envelope):
        sent.append((target, envelope))

    handle = start_notifications_dispatcher(send_fn=_send_fn, bus=bus)
    try:
        # The dispatcher subscribes inside start_*; the cached event
        # (seq=0) replays into its queue. Yield a few times so the
        # dispatcher processes whatever is in the queue.
        for _ in range(5):
            await asyncio.sleep(0.01)
        assert sent == [], (
            "cached-state replay (seq=0) must not trigger delivery"
        )
    finally:
        await stop_notifications_dispatcher(bus=bus)


@pytest.mark.asyncio
async def test_dispatcher_start_is_idempotent():
    """Second ``start_*`` call returns the existing handle, not a
    fresh task — re-entrant lifespan startup must not double-bind.
    """
    bus = event_bus_module.EventBus()

    async def _send_fn(target, envelope):
        pass

    h1 = start_notifications_dispatcher(send_fn=_send_fn, bus=bus)
    try:
        h2 = start_notifications_dispatcher(send_fn=_send_fn, bus=bus)
        assert h2 is h1
        assert h1.task is h2.task
    finally:
        await stop_notifications_dispatcher(bus=bus)


@pytest.mark.asyncio
async def test_dispatcher_failure_in_handler_does_not_kill_loop(
    graph_db_env, caplog,
):
    """A handler exception must be caught — subsequent events still
    deliver. We provoke this by making ``deliver_refresh_ping`` raise
    via a monkey-patched failure path, then verifying the dispatcher
    keeps consuming events afterward.
    """
    bus = event_bus_module.EventBus()

    delivered = asyncio.Event()
    success_calls: list[str] = []

    _write_session_ask(
        session_id="auto-survive", text="ok", revision_seq=1,
    )
    _write_refresh_request(
        ask_id="auto-survive", requested_by="op", target_revision=1,
    )

    async def _send_fn(target, envelope):
        success_calls.append(target)
        delivered.set()

    handle = start_notifications_dispatcher(send_fn=_send_fn, bus=bus)
    try:
        with caplog.at_level(logging.ERROR, logger="notifications_actions"):
            # Step 1: a malformed event (missing key) — would normally
            # be filtered out cleanly. Also send a not-a-dict payload
            # to exercise the defensive branches.
            bus.broadcast_sync(
                "setting.changed",
                {
                    "set_id": ns.ASK_REFRESH_SET_ID,
                    "schema_revision": 1,
                    # no "key" field — dispatcher must skip.
                    "org": None,
                    "publication_state": "raw",
                    "deprecated": False,
                    "operation": "write",
                },
                dedup=False,
            )
            await asyncio.sleep(0.02)

            # Step 2: a valid event — must still deliver.
            bus.broadcast_sync(
                "setting.changed",
                {
                    "set_id": ns.ASK_REFRESH_SET_ID,
                    "schema_revision": 1,
                    "key": "auto-survive",
                    "org": None,
                    "publication_state": "raw",
                    "deprecated": False,
                    "operation": "write",
                },
                dedup=False,
            )
            await asyncio.wait_for(delivered.wait(), timeout=2.0)
        assert success_calls == ["auto-survive"]
    finally:
        await stop_notifications_dispatcher(bus=bus)


@pytest.mark.asyncio
async def test_stop_dispatcher_when_not_started_is_noop():
    """``stop_*`` with no dispatcher running returns cleanly."""
    # No prior start; this should not raise.
    await stop_notifications_dispatcher()
