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


def _scheduler(tmp_path: Path):
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
    MutationCatalog(db.conn, machine.public_hex).install()
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
    version, accept_checkpoint = decoded[5], decoded[6]
    assert version >= SWEEP_PROTOCOL_VERSION, (
        f"a resuming sweep must not negotiate below v{SWEEP_PROTOCOL_VERSION}, "
        f"asked v{version}"
    )
    assert accept_checkpoint is False, (
        "a store part-way through a sweep offered to accept a checkpoint"
    )


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
    """No bootstrap, no pin: ordinary behaviour must not change."""
    scheduler, _ = _scheduler(tmp_path)
    channel = _FakeChannel([])
    _drive(monkeypatch, scheduler, channel)

    assert channel.sent
    decoded = fss.decode_pull_request(channel.sent[0])
    assert decoded[5] == fss.FLEET_SYNC_PROTOCOL_VERSION
    assert decoded[6] is True, "an ordinary store still accepts checkpoints"


def test_an_unsolicited_begin_on_a_v4_pull_anchors_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    """The sweep is an OPT-IN, so a peer must not anchor one unilaterally.

    A store with no bootstrap asks the default version. Accepting a v5
    sweep.begin there would anchor a bootstrap this client never requested --
    and having not asked for v5 it has no sweep receive path to finish with,
    so it would sit anchored and never complete.
    """
    scheduler, personal = _scheduler(tmp_path)
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
