"""auto-coea3 step 2 (design graph://c2baad48-0a3 §1, §3): an org scope's
pull and serve route through the ORG hello when the peer is a co-member's
machine outside the personal roster; the personal path is untouched; an
org-admitted connection is confined to its organization's scope; within a
round the machine's own fleet is pulled before co-members."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.graph.models import Source
from tools.network import fleet_sync_scheduler as fss
from tools.network.fleet_org_channel import (
    OrgFleetAuthenticator, org_epoch, org_state_key,
)
from tools.network.fleet_roster import enroll
from tools.network.fleet_sync_channel import FleetAuthenticator, fleet_direct_connect
from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.ledger import membership_commitment as mc
from tools.network.relaykit.direct import new_session_id

ORG = "genesis-" + "ab" * 28
OTHER_ORG = "genesis-" + "cd" * 28


def _cert(persona: KeyPair, machine: KeyPair, org: str = ORG):
    now = int(time.time())
    return issue_cert(
        persona, machine.public_hex, scope=("fleet:sync",), org=org,
        subject=Subject("persona", persona.public_hex),
        not_before=now - 300, not_after=now + 86_400,
    )


def _prepare(path: Path, machine: KeyPair) -> None:
    db = GraphDB(path)
    try:
        db.activate_fleet_sync_writers(machine.public_hex)
    finally:
        db.close()


def _insert(path: Path, source_id: str, title: str) -> None:
    db = GraphDB(path)
    try:
        db.insert_source(Source(id=source_id, type="note", title=title))
    finally:
        db.close()


def _delete(path: Path, source_id: str) -> None:
    db = GraphDB(path)
    try:
        db.conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
        db.conn.commit()
    finally:
        db.close()


def _has(path: Path, source_id: str) -> bool:
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT 1 FROM sources WHERE id=?", (source_id,)
        ).fetchone() is not None


class Member:
    """One operator: own personal root and roster (one machine), a persona
    in the organization, a personal database and the org's database."""

    def __init__(self, tmp: Path, name: str, persona: KeyPair, members: list[str],
                 *, org: str = ORG) -> None:
        self.name = name
        self.root = KeyPair.generate()
        self.machine = KeyPair.generate()
        self.persona = persona
        self.org = org
        self.members = list(members)
        self.entries = (enroll(self.root, machine_pub=self.machine.public_hex),)
        # Layout mirrors a machine's data root: the org database lives in
        # an orgs/ directory, so AUTONOMY_ORGS_DIR can name it for the
        # settings write path (reachability rows) when a test needs to.
        self.personal = tmp / name / "personal.db"
        self.orgs_dir = tmp / name / "orgs"
        self.orgs_dir.mkdir(parents=True, exist_ok=True)
        self.alpha = self.orgs_dir / "alpha.db"
        _prepare(self.personal, self.machine)
        _prepare(self.alpha, self.machine)
        # Authored-then-deleted local state keeps the org scope on the delta
        # path (checkpoint bootstrap has its own harness coverage).
        _insert(self.alpha, f"{name}-seed", "delta path")
        _delete(self.alpha, f"{name}-seed")
        self.seq = 0
        self.adopted = {0: {"seq": 0, "members_root": mc.compute_root(self.members)}}
        self.members_at = {0: list(self.members)}
        self.persona_cert = _cert(persona, self.machine, org)
        #: Addresses this machine introduces in its org hello and publishes
        #: to the reachability set (filled by scheduler(advertised=...)).
        self.advertised: list[str] = []
        self.channel = self._channel()

    def _channel(self) -> OrgFleetAuthenticator:
        return OrgFleetAuthenticator(
            self.machine, org=self.org, persona_cert=self.persona_cert,
            membership_proof_for=self._rider,
            adopted_checkpoint_for=lambda s: self.adopted.get(int(s)),
            newest_adopted_seq=lambda: max(self.adopted),
            adopted_members_for=lambda s: self.members_at.get(int(s)),
            advertised_addresses=lambda: list(self.advertised),
        )

    def _rider(self) -> dict:
        try:
            index, path = mc.inclusion_proof(self.members, self.persona.public_hex)
        except mc.MembershipCommitmentError:
            index, path = 0, []
        return {"v": 1, "checkpoint_seq": self.seq, "index": index, "path": path}

    def adopt(self, seq: int, members: list[str], *, deadline_s: float = 5.0) -> None:
        """This machine adopts membership checkpoint *seq*; its own hello
        proves under it from now on. ``deadline_s`` is the re-prove grace
        for peers admitted under an older seq (5 s in production)."""
        self.members = list(members)
        self.adopted[seq] = {"seq": seq, "members_root": mc.compute_root(members)}
        self.members_at[seq] = list(members)
        self.seq = seq
        self.channel.note_adoption(seq, deadline_s=deadline_s)

    def rekey(self, persona: KeyPair) -> None:
        """The member's persona is rekeyed: this machine gets a certificate
        from the new persona and proves under the new leaf from now on."""
        self.persona = persona
        self.persona_cert = _cert(persona, self.machine, self.org)
        self.channel = self._channel()

    def personal_authenticator(self) -> FleetAuthenticator:
        return FleetAuthenticator(
            self.machine, root_pub=self.root.public_hex,
            roster_entries=lambda: self.entries,
        )

    def scheduler(self, *, peer_addresses=None, org_peers=None,
                  with_channel: bool = True, poll: float = 0.2,
                  advertised=None, recorder=None) -> fss.FleetSyncScheduler:
        """``advertised``: a callable returning this machine's dialable
        addresses; it feeds both the scheduler (reachability rows) and the
        org hello (introduction)."""
        peers = dict(peer_addresses or {})
        if advertised is not None:
            provider = advertised

            def advertised():  # noqa: F811 - wraps the caller's
                self.advertised = list(provider())
                return list(self.advertised)
        config = fss.FleetSyncRuntimeConfig(
            machine_key=self.machine,
            personal_root_pub=self.root.public_hex,
            roster_entries=lambda: self.entries,
            peer_addresses=lambda: peers,
            personal_db_path=self.personal,
            sync_scopes=lambda: {"alpha": self.alpha},
            org_channels=(lambda: {"alpha": self.channel}) if with_channel else None,
            org_peer_addresses=org_peers,
            advertised_addresses=advertised,
            telemetry_recorder=recorder,
            poll_interval=poll,
            connect_timeout=3.0,
            min_backoff=0.02,
            max_backoff=0.1,
        )
        return fss.FleetSyncScheduler(config)


async def _wait(predicate, *, timeout: float, label: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {label}")
        await asyncio.sleep(0.05)


def test_co_member_machine_pulls_the_org_scope_through_the_org_hello(tmp_path: Path) -> None:
    pa, pb = KeyPair.generate(), KeyPair.generate()
    members = [pa.public_hex, pb.public_hex]
    a = Member(tmp_path, "a", pa, members)
    b = Member(tmp_path, "b", pb, members)

    async def run() -> None:
        server = a.scheduler()
        await server.start()
        b_peers = {"alpha": {a.machine.public_hex: [f"ws://127.0.0.1:{server.port}"]}}
        puller = b.scheduler(org_peers=lambda: b_peers)
        try:
            _insert(a.personal, "p-row", "personal, must not cross")
            _insert(a.alpha, "a-row", "org row crossing between members")
            await puller.start()
            await _wait(lambda: _has(b.alpha, "a-row"), timeout=30.0,
                        label="org row on the co-member's machine")
            # A second write after admission rides the same path.
            _insert(a.alpha, "a-row-2", "second org row")
            await _wait(lambda: _has(b.alpha, "a-row-2"), timeout=30.0,
                        label="second org row")
            await asyncio.sleep(0.5)
            # Isolation: B is not in A's personal roster, so nothing of A's
            # personal scope reaches B by any path, and the org rows do not
            # land in B's personal database.
            assert not _has(b.personal, "p-row")
            assert not _has(b.personal, "a-row")
            # Both members adopt a newer membership checkpoint (a join, say:
            # the member set here is unchanged, the seq advances). The org
            # epoch on the wire changes; the peer-state key does not, and
            # no checkpoint is pulled -- rows keep flowing by delta.
            wire_before = puller._scope_epochs("alpha")
            a.adopt(1, members)
            b.adopt(1, members)
            wire_after = puller._scope_epochs("alpha")
            assert wire_after[0] != wire_before[0]
            assert wire_after[1] == wire_before[1] == org_state_key(ORG)
            _insert(a.alpha, "a-row-3", "after the adoption")
            await _wait(lambda: _has(b.alpha, "a-row-3"), timeout=30.0,
                        label="org row after both members adopted seq 1")
        finally:
            await puller.stop()
            await server.stop()
        # Per-peer state for the org scope is keyed by (machine pair, org):
        # exactly one row for A's machine in B's org store, under the org
        # key, with no checkpoint received across the adoption; nothing in
        # B's personal store.
        with sqlite3.connect(b.alpha) as conn:
            rows = conn.execute(
                "SELECT machine_public_key,roster_epoch,checkpoints_received "
                "FROM fleet_sync_peer_state"
            ).fetchall()
        assert [(r[0], r[1]) for r in rows] == [(a.machine.public_hex, org_state_key(ORG))]
        assert rows[0][2] == 0
        with sqlite3.connect(b.personal) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM fleet_sync_peer_state"
            ).fetchone()[0] == 0
        # A's org store recorded B's served acknowledgement under the same key.
        with sqlite3.connect(a.alpha) as conn:
            served = conn.execute(
                "SELECT machine_public_key,roster_epoch FROM fleet_sync_peer_state"
            ).fetchall()
        assert (b.machine.public_hex, org_state_key(ORG)) in served

    asyncio.run(run())


def test_org_admitted_connection_is_confined_and_wrong_hellos_are_refused(tmp_path: Path) -> None:
    pa, pb, pc = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
    members = [pa.public_hex, pb.public_hex]
    a = Member(tmp_path, "a", pa, members)
    b = Member(tmp_path, "b", pb, members)
    # C's persona is a member of ANOTHER organization; A has no channel for it.
    c = Member(tmp_path, "c", pc, [pc.public_hex], org=OTHER_ORG)

    async def refused(coro) -> None:
        with pytest.raises(Exception):
            await coro

    async def run() -> None:
        server = a.scheduler()
        await server.start()
        addr = f"ws://127.0.0.1:{server.port}"
        try:
            # Admitted by the org hello; a personal-scope request on that
            # connection is refused (the channel ends without a frame).
            channel = await fleet_direct_connect(
                addr, authenticator=b.channel,
                expected_machine_pub=a.machine.public_hex, session=new_session_id(),
            )
            async with channel:
                epoch = fss.roster_epoch(b.entries, b.root.public_hex)
                store = server._store_for("personal")
                await channel.send_message(fss.encode_pull_request(
                    epoch, compat=store.compatibility_digest(), scope="personal",
                    watermarks={},
                ))
                try:
                    got = await asyncio.wait_for(channel.recv_message(), timeout=5.0)
                except Exception:
                    got = None
                assert got is None, got
            # Blob answers for an org-admitted peer come only from that
            # organization's scope; a personal peer sees every scope.
            assert server._blob_paths(None) == [a.personal, a.alpha]
            assert server._blob_paths(ORG) == [a.alpha]
            assert server._blob_paths(OTHER_ORG) == []
            # An org hello for an organization this machine has no channel
            # for is refused at the hello.
            await refused(fleet_direct_connect(
                addr, authenticator=c.channel,
                expected_machine_pub=a.machine.public_hex, session=new_session_id(),
            ))
            # A co-member's PERSONAL hello is refused as before: its
            # machine is not in A's personal roster.
            await refused(fleet_direct_connect(
                addr, authenticator=b.personal_authenticator(),
                expected_machine_pub=a.machine.public_hex, session=new_session_id(),
            ))
        finally:
            await server.stop()

    asyncio.run(run())


def test_a_round_pulls_the_own_fleet_before_co_members(tmp_path: Path) -> None:
    pa, pb = KeyPair.generate(), KeyPair.generate()
    members = [pa.public_hex, pb.public_hex]
    a = Member(tmp_path, "a", pa, members)
    fleet_mate = KeyPair.generate()
    a.entries = a.entries + (enroll(a.root, machine_pub=fleet_mate.public_hex),)
    co_member_machine = KeyPair.generate().public_hex
    org_peers = {"alpha": {
        co_member_machine: ["ws://127.0.0.1:1"],
        # A machine that is ALSO in the personal roster is pulled on the
        # personal path and never again through the org hello.
        fleet_mate.public_hex: ["ws://127.0.0.1:1"],
    }}
    scheduler = a.scheduler(
        peer_addresses={fleet_mate.public_hex: ["ws://127.0.0.1:1"]},
        org_peers=lambda: org_peers,
    )
    scheduler._roster_snapshot = a.entries
    calls: list[tuple[str, str, bool]] = []

    async def fake_pull(machine_pub, addresses, scope, *, org_channel=None):
        calls.append((scope, machine_pub, org_channel is not None))
        if org_channel is not None:
            scheduler._stopping.set()

    scheduler._pull_scope = fake_pull  # type: ignore[method-assign]
    asyncio.run(scheduler._run())

    assert calls == [
        ("personal", fleet_mate.public_hex, False),
        ("alpha", fleet_mate.public_hex, False),
        ("alpha", co_member_machine, True),
    ]


def test_without_an_org_channel_the_scheduler_is_the_personal_one(tmp_path: Path) -> None:
    pa = KeyPair.generate()
    a = Member(tmp_path, "a", pa, [pa.public_hex])
    scheduler = a.scheduler(with_channel=False, org_peers=lambda: {"alpha": {"ff" * 32: ["ws://x"]}})
    assert scheduler._org_channels() == {}
    assert scheduler._org_channel_for_genesis(ORG) is None
    scheduler._confine_scope("personal", None)
    with pytest.raises(fss.FleetSyncProtocolError):
        scheduler._confine_scope("alpha", ORG)
    calls: list = []

    async def fake_pull(*args, **kwargs):
        calls.append(args)

    scheduler._pull_scope = fake_pull  # type: ignore[method-assign]
    asyncio.run(scheduler._sync_org_peers(set(), 0.0))
    assert calls == []


def test_scope_epochs_are_the_org_epoch_only_with_an_org_channel(tmp_path: Path) -> None:
    pa = KeyPair.generate()
    a = Member(tmp_path, "a", pa, [pa.public_hex])
    with_channel = a.scheduler()
    with_channel._roster_snapshot = a.entries
    personal = fss.roster_epoch(a.entries, a.root.public_hex)
    assert with_channel._scope_epochs("personal") == (personal, personal)
    assert with_channel._scope_epochs("alpha") == (org_epoch(ORG, 0), org_state_key(ORG))
    without = a.scheduler(with_channel=False)
    without._roster_snapshot = a.entries
    assert without._scope_epochs("alpha") == (personal, personal)
    # Shape: every roster_epoch field and validator accepts 64 lowercase hex.
    for value in (org_epoch(ORG, 0), org_epoch(ORG, None), org_state_key(ORG)):
        assert len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)
    assert org_epoch(ORG, 0) != org_epoch(ORG, 1) != org_epoch(OTHER_ORG, 1)
    assert org_state_key(ORG) != org_state_key(OTHER_ORG)
    assert org_state_key(ORG) != org_epoch(ORG, 0)


def test_a_removing_checkpoint_closes_the_removed_member_with_4417(tmp_path: Path) -> None:
    pa, pb = KeyPair.generate(), KeyPair.generate()
    members = [pa.public_hex, pb.public_hex]
    a = Member(tmp_path, "a", pa, members)
    b = Member(tmp_path, "b", pb, members)

    async def run() -> None:
        server = a.scheduler()
        await server.start()
        addr = f"ws://127.0.0.1:{server.port}"
        b_peers = {"alpha": {a.machine.public_hex: [addr]}}
        puller = b.scheduler(org_peers=lambda: b_peers)
        try:
            _insert(a.alpha, "row-1", "before removal")
            await puller.start()
            await _wait(lambda: _has(b.alpha, "row-1"), timeout=30.0, label="row before removal")
            # A connection B holds open, admitted under seq 0.
            held = await fleet_direct_connect(
                addr, authenticator=b.channel,
                expected_machine_pub=a.machine.public_hex, session=new_session_id(),
            )
            # A adopts a checkpoint that removes B's persona. Inside the
            # re-prove window the held connection still serves; after it,
            # the next message closes it with 4417.
            a.adopt(1, [pa.public_hex], deadline_s=0.3)
            await asyncio.sleep(0.5)
            async with held:
                epoch = org_epoch(ORG, 0)
                store = server._store_for("alpha")
                await held.send_message(fss.encode_pull_request(
                    epoch, compat=store.compatibility_digest(), scope="alpha",
                    watermarks={},
                ))
                try:
                    await asyncio.wait_for(held.recv_message(), timeout=5.0)
                except Exception:
                    pass
                assert held._ws.close_code == 4417, held._ws.close_code
            # A fresh hello from B proves under seq 0, which A no longer
            # accepts outside the window: refused at the hello, typed.
            with pytest.raises(Exception):
                await fleet_direct_connect(
                    addr, authenticator=b.channel,
                    expected_machine_pub=a.machine.public_hex, session=new_session_id(),
                )
            # And even if B adopts seq 1 itself, its persona is not in that
            # member set: nothing written on A after the removal reaches B.
            b.adopt(1, [pa.public_hex])
            _insert(a.alpha, "row-2", "after removal")
            await asyncio.sleep(2.0)
            assert not _has(b.alpha, "row-2")
        finally:
            await puller.stop()
            await server.stop()

    asyncio.run(run())


def test_a_rekeyed_member_re_proves_under_its_new_leaf_and_survives(tmp_path: Path) -> None:
    pa, pb, pb2 = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
    members = [pa.public_hex, pb.public_hex]
    a = Member(tmp_path, "a", pa, members)
    b = Member(tmp_path, "b", pb, members)

    async def run() -> None:
        server = a.scheduler()
        await server.start()
        b_peers = {"alpha": {a.machine.public_hex: [f"ws://127.0.0.1:{server.port}"]}}
        puller = b.scheduler(org_peers=lambda: b_peers)
        try:
            _insert(a.alpha, "row-1", "before the rekey")
            await puller.start()
            await _wait(lambda: _has(b.alpha, "row-1"), timeout=30.0, label="row before rekey")
            # B's persona is rekeyed; both machines adopt the checkpoint
            # whose member set carries the new leaf. B's machine proves
            # under its new certificate and keeps syncing; no checkpoint
            # is pulled and the peer-state key is unchanged.
            rekeyed = [pa.public_hex, pb2.public_hex]
            b.rekey(pb2)
            a.adopt(1, rekeyed, deadline_s=0.3)
            b.adopt(1, rekeyed, deadline_s=0.3)
            await asyncio.sleep(0.5)
            _insert(a.alpha, "row-2", "after the rekey")
            await _wait(lambda: _has(b.alpha, "row-2"), timeout=30.0, label="row after rekey")
        finally:
            await puller.stop()
            await server.stop()
        with sqlite3.connect(b.alpha) as conn:
            rows = conn.execute(
                "SELECT machine_public_key,roster_epoch,checkpoints_received "
                "FROM fleet_sync_peer_state"
            ).fetchall()
        assert rows == [(a.machine.public_hex, org_state_key(ORG), 0)]

    asyncio.run(run())
