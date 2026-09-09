"""Drive the real _pull_scope with a controlled channel.

Every earlier control for this work tested a store method or a compiled name.
None ran the caller, so none could see that the receive hunk passed an
undefined `peer_pub`, or that a computed `accept_checkpoint` never reached
`encode_pull_request`. Both defects survived a green suite. These controls
execute the actual client path and read what it puts on the wire.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.network import fleet_sync_scheduler as fss
from tools.network.fleet_roster import enroll
from tools.network.fleet_sync_channel import FleetAuthenticator
from tools.network.fleet_sync.catalog import MutationCatalog
from tools.network.fleet_sync.sweep_receive import (
    SWEEP_BEGIN_KIND,
    SWEEP_PROTOCOL_VERSION,
    begin_bootstrap,
    read_bootstrap,
)
from tools.network.idkit import KeyPair, Subject, issue_cert

PEER = "e5" * 32


class _Captured(Exception):
    """Stop the pull once the wire bytes are in hand."""


class _FakeChannel:
    """Records what the client sent, then feeds it control records."""

    def __init__(self, script):
        self.sent: list[bytes] = []
        self._script = list(script)

    async def send_message(self, payload: bytes) -> None:
        self.sent.append(payload)

    def recv_message_stream(self):
        """Replay the script, then a well-formed terminal summary.

        The receiver requires the stream to END with a `done` record, and that
        record's epoch must be the one the CLIENT asked with -- so it is built
        here from the captured request rather than guessed. sweep.begin is a
        control record outside the digest, exactly as the serve path emits it,
        so the summary counts zero frames.
        """
        import hashlib

        script = list(self._script)
        epoch = fss.decode_pull_request(self.sent[0])[0] if self.sent else ""
        done = fss.encode_done(
            epoch=epoch, count=0, digest=hashlib.sha256().hexdigest(),
        )
        frames = script + [done]

        async def stream():
            for index, frame in enumerate(frames):
                yield frame, index == len(frames) - 1

        return stream()

    async def close(self) -> None:
        return None


def _scheduler(tmp_path: Path, *, settled: bool = False):
    """``settled``: give the store real sync state.

    Without this the fixture builds an EMPTY store, which is a store that
    NEEDS a bootstrap -- not a settled one. Two controls asserted "settled"
    behaviour against it and only noticed when fresh-joiner initiation
    changed what an empty store asks for.
    """
    root = KeyPair.generate()
    persona = KeyPair.generate()
    machine = KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(
        persona, machine.public_hex, scope=("fleet:sync",), org="genesis-" + "ab" * 28,
        subject=Subject("persona", persona.public_hex),
        not_before=now - 300, not_after=now + 86_400,
    )
    personal = tmp_path / "personal.db"
    db = GraphDB(personal)
    catalog = MutationCatalog(db.conn, machine.public_hex)
    catalog.install()
    if settled:
        with catalog.transaction(10, "tx-settled"):
            db.conn.execute(
                "INSERT INTO sources(id,type,title,metadata,created_at,"
                "ingested_at) VALUES(?,?,?,?,?,?)",
                ("s-settled", "note", "t", "{}", "2026-08-19T00:00:00Z",
                 "2026-08-19T00:00:00Z"),
            )
    db.close()

    # Both this machine and the peer must be roster members, or the pull
    # is refused before a request is ever built.
    entries = (
        enroll(root, machine_pub=machine.public_hex),
        enroll(root, machine_pub=PEER, seq=1),
    )
    config = fss.FleetSyncRuntimeConfig(
        machine_key=machine,
        personal_root_pub=root.public_hex,
        roster_entries=lambda: entries,
        peer_addresses=lambda: {PEER: ["127.0.0.1:1"]},
        personal_db_path=personal,
        poll_interval=60.0,
        connect_timeout=1.0,
        min_backoff=0.01,
        max_backoff=0.02,
    )
    scheduler = fss.FleetSyncScheduler(config)
    # The authenticator reads a SNAPSHOT the scheduler refreshes on its
    # own loop, not config.roster_entries directly. Without priming it the
    # roster is empty and every peer is refused before a request is built.
    scheduler._roster_snapshot = entries
    return scheduler, personal


def _drive(monkeypatch, scheduler, channel, *, expect=None):
    """Run the real pull. Exceptions are NOT swallowed.

    A broad catch here hid post-anchor failures: the stream now terminates
    with a valid summary, so a success path that raises is a defect, not
    scaffolding noise. ``expect`` names the refusal a control requires, and
    anything else propagates.
    """
    async def fake_connect(*args, **kwargs):
        return channel

    monkeypatch.setattr(fss, "fleet_direct_connect", fake_connect)
    coro = scheduler._pull_scope(PEER, ["127.0.0.1:1"], "personal")
    if expect is None:
        asyncio.run(coro)
        return None
    with pytest.raises(expect) as caught:
        asyncio.run(coro)
    return caught.value


def test_a_bootstrapping_store_asks_v5_and_refuses_a_checkpoint(
    tmp_path: Path, monkeypatch
) -> None:
    """The refusal must reach the WIRE, not merely a local variable.

    The first version computed accept_checkpoint into a local that
    encode_pull_request never read, so the request still offered to accept a
    checkpoint while the code claimed it refused.
    """
    scheduler, personal = _scheduler(tmp_path)

    conn = sqlite3.connect(personal)
    try:
        begin_bootstrap(conn, {PEER: 10})
    finally:
        conn.close()

    channel = _FakeChannel([])
    _drive(monkeypatch, scheduler, channel)

    assert channel.sent, "the client never sent a request"
    decoded = fss.decode_pull_request(channel.sent[0])
    version = decoded[5]
    assert version >= SWEEP_PROTOCOL_VERSION, (
        f"a resuming sweep must not negotiate below v{SWEEP_PROTOCOL_VERSION}, "
        f"asked v{version}"
    )
    # There is no accept_checkpoint to assert: sync checkpoints are deleted,
    # so a resuming sweep has nothing to refuse and the field is off the wire.
    assert b"accept_checkpoint" not in channel.sent[0]


def test_a_delivered_begin_anchors_the_store_through_the_real_receiver(
    tmp_path: Path, monkeypatch
) -> None:
    """Deliver sweep.begin down the real receive path and read durable state.

    This is the control that catches a wrong variable at the call site. The
    receive hunk originally passed `peer_pub`, which does not exist in
    _pull_scope -- a NameError on the first begin. No store-method or
    compiled-name control could see it, because none of them ran the call.
    """
    scheduler, personal = _scheduler(tmp_path)
    # A client only asks v5 when it is already resuming a sweep, so anchor
    # first. Delivering the SAME frontier is idempotent, which is what makes
    # this a legitimate v5 exchange rather than an unsolicited one.
    conn = sqlite3.connect(personal)
    try:
        begin_bootstrap(conn, {PEER: 42})
    finally:
        conn.close()
    begin = json.dumps({
        "v": SWEEP_PROTOCOL_VERSION,
        "kind": SWEEP_BEGIN_KIND,
        "scope": "personal",
        "source_machine_pub": PEER,
        "frontier": {PEER: 42},
    }).encode()

    channel = _FakeChannel([begin])
    _drive(monkeypatch, scheduler, channel)

    conn = sqlite3.connect(personal)
    try:
        state = read_bootstrap(conn)
        assert state is not None, (
            "the delivered sweep.begin never reached the shared handler"
        )
        assert state.frontier == {PEER: 42}
    finally:
        conn.close()


def test_a_settled_store_is_unaffected(tmp_path: Path, monkeypatch) -> None:
    """No bootstrap AND real state: ordinary behaviour must not change.

    The store must be genuinely settled. An empty store is one that NEEDS a
    bootstrap, and asking v5 there is correct -- so testing "unaffected"
    against an empty fixture proved nothing.
    """
    scheduler, personal = _scheduler(tmp_path, settled=True)
    assert read_bootstrap(sqlite3.connect(personal)) is None
    channel = _FakeChannel([])
    _drive(monkeypatch, scheduler, channel)

    assert channel.sent
    decoded = fss.decode_pull_request(channel.sent[0])
    assert decoded[5] == fss.FLEET_SYNC_PROTOCOL_VERSION


def test_an_unsolicited_begin_on_a_v4_pull_anchors_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    """The sweep is an OPT-IN, so a peer must not anchor one unilaterally.

    A SETTLED store asks the default version -- it needs no bootstrap at all.
    Accepting a v5 sweep.begin there would anchor a bootstrap this client
    never requested, and having not asked for v5 it has no sweep receive path
    to finish with, so it would sit anchored and never complete.
    """
    scheduler, personal = _scheduler(tmp_path, settled=True)
    begin = json.dumps({
        "v": SWEEP_PROTOCOL_VERSION,
        "kind": SWEEP_BEGIN_KIND,
        "scope": "personal",
        "source_machine_pub": PEER,
        "frontier": {PEER: 42},
    }).encode()

    channel = _FakeChannel([begin])
    error = _drive(
        monkeypatch, scheduler, channel, expect=fss.FleetSyncProtocolError,
    )
    assert "sweep.begin" in str(error)

    conn = sqlite3.connect(personal)
    try:
        assert read_bootstrap(conn) is None, (
            "an unsolicited sweep.begin anchored a store that never asked"
        )
    finally:
        conn.close()


def test_a_fresh_joiner_asks_for_the_sweep_without_pre_seeding(
    tmp_path: Path, monkeypatch
) -> None:
    """Bootstrap must be startable from an EMPTY store.

    The receiver enforces the opt-in on the requested version, so a store with
    no bootstrap row would ask v4, be refused a sweep.begin, and have no way to
    anchor one. The version pin would then only ever protect bootstraps that
    somehow already existed -- and nothing could create the first.

    Asking is not committing: a fresh joiner still accepts a checkpoint, so
    meeting a v4 server it takes whatever that server can serve.
    """
    scheduler, personal = _scheduler(tmp_path)
    assert read_bootstrap(sqlite3.connect(personal)) is None, (
        "this control is worthless if the fixture pre-seeds a bootstrap"
    )

    channel = _FakeChannel([])
    _drive(monkeypatch, scheduler, channel)

    decoded = fss.decode_pull_request(channel.sent[0])
    assert decoded[5] >= SWEEP_PROTOCOL_VERSION, (
        f"a fresh joiner asked v{decoded[5]}, so it can never be served a "
        f"sweep and bootstrap cannot start"
    )
    assert b"accept_checkpoint" not in channel.sent[0], (
        "the request still carries a checkpoint concept"
    )


def test_a_fresh_joiner_can_anchor_from_a_delivered_begin(
    tmp_path: Path, monkeypatch
) -> None:
    """End to end on the initiation path: empty store in, anchored store out,
    with nothing pre-seeded."""
    scheduler, personal = _scheduler(tmp_path)
    begin = json.dumps({
        "v": SWEEP_PROTOCOL_VERSION,
        "kind": SWEEP_BEGIN_KIND,
        "scope": "personal",
        "source_machine_pub": PEER,
        "frontier": {PEER: 77},
    }).encode()

    _drive(monkeypatch, scheduler, _FakeChannel([begin]))

    conn = sqlite3.connect(personal)
    try:
        state = read_bootstrap(conn)
        assert state is not None, "a fresh joiner could not anchor a bootstrap"
        assert state.frontier == {PEER: 77}
    finally:
        conn.close()


# ── auto-5j6o0: the > F half and durable completion ──────────────────────

def _sweep_stream(frontier, *, records=(), end=True):
    """A server stream: begin, optional pages, optional end."""
    frames = [json.dumps({
        "v": SWEEP_PROTOCOL_VERSION, "kind": SWEEP_BEGIN_KIND,
        "scope": "personal", "source_machine_pub": PEER,
        "frontier": frontier,
    }).encode()]
    for header, ops in records:
        frames.append(header)
        frames.extend(ops)
    if end:
        frames.append(json.dumps({
            "v": SWEEP_PROTOCOL_VERSION, "kind": "sweep.end",
            "records": sum(len(o) for _, o in records),
        }).encode())
    return frames


def test_completion_requires_the_whole_stream_not_a_timestamp(
    tmp_path: Path, monkeypatch
) -> None:
    """A pull that never reaches its summary must NOT complete a bootstrap.

    Completion is recorded only after the receiver has verified the summary's
    count and digest against what it decoded itself. Anything short of that --
    including a stream that simply stops -- leaves the phase where it was and
    the frontier suppressed.
    """
    from tools.network.fleet_sync.sweep_receive import (
        Phase, may_advertise_frontier,
    )

    scheduler, personal = _scheduler(tmp_path)

    class _Truncated(_FakeChannel):
        def recv_message_stream(self):
            frames = list(self._script)

            async def stream():
                for index, frame in enumerate(frames):
                    yield frame, index == len(frames) - 1
            return stream()   # NO summary frame: the stream just ends

    channel = _Truncated(_sweep_stream({PEER: 42}))
    _drive(monkeypatch, scheduler, channel, expect=fss.FleetSyncProtocolError)

    conn = sqlite3.connect(personal)
    try:
        state = read_bootstrap(conn)
        assert state is not None, "the begin should still have anchored"
        assert state.phase is not Phase.COMPLETE, (
            "a truncated stream completed a bootstrap"
        )
        assert may_advertise_frontier(conn) is False, (
            "an incomplete bootstrap is advertising its frontier"
        )
    finally:
        conn.close()


def test_interruption_leaves_the_frontier_suppressed_then_resume_completes(
    tmp_path: Path, monkeypatch
) -> None:
    """The reviewer's sequence: interrupt before the final apply, confirm the
    bootstrap is incomplete and silent, then resume and complete correctly --
    against the SAME F, which must not move."""
    from tools.network.fleet_sync.sweep_receive import (
        Phase, may_advertise_frontier,
    )

    scheduler, personal = _scheduler(tmp_path)

    # 1. Interrupted: begin arrives, stream dies before the summary.
    class _Truncated(_FakeChannel):
        def recv_message_stream(self):
            frames = list(self._script)

            async def stream():
                for index, frame in enumerate(frames):
                    yield frame, index == len(frames) - 1
            return stream()

    # begin THEN sweep.end, and the stream stops before its summary. The
    # begin must not be the final frame: the receiver rejects any stream whose
    # last frame is not the summary BEFORE dispatching control records, so a
    # lone begin correctly anchors nothing. That is a malformed stream, not an
    # interruption -- the case under test is a stream that got somewhere and
    # then died.
    _drive(monkeypatch, scheduler,
           _Truncated(_sweep_stream({PEER: 42})),
           expect=fss.FleetSyncProtocolError)

    conn = sqlite3.connect(personal)
    try:
        interrupted = read_bootstrap(conn)
        assert interrupted.phase is Phase.SWEEPING
        assert interrupted.frontier == {PEER: 42}
        assert may_advertise_frontier(conn) is False
    finally:
        conn.close()

    # 2. Resume: a complete stream, terminating in a valid summary.
    _drive(monkeypatch, scheduler, _FakeChannel(_sweep_stream({PEER: 42})))

    conn = sqlite3.connect(personal)
    try:
        done = read_bootstrap(conn)
        assert done.frontier == {PEER: 42}, (
            "F moved across the resume; every key between the old and new "
            "frontier would be stranded"
        )
        assert done.phase is Phase.COMPLETE, "the resume did not complete"
        assert may_advertise_frontier(conn) is True, (
            "a completed bootstrap is still suppressing its frontier"
        )
    finally:
        conn.close()


def test_a_settled_store_completion_path_is_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    """A store with no bootstrap must not gain one from a successful pull."""
    scheduler, personal = _scheduler(tmp_path, settled=True)
    _drive(monkeypatch, scheduler, _FakeChannel([]))
    conn = sqlite3.connect(personal)
    try:
        assert read_bootstrap(conn) is None
    finally:
        conn.close()
