"""auto-mldvv (design graph://c2baad48-0a3 §4): an organization's machines
keep their own reachability as rows of the org-homed set
autonomy.org.fleet-reachability#1 -- self-certified, written only on an
address change, read back by co-members from the replicated rows."""

from __future__ import annotations

import asyncio
import copy
import time
from pathlib import Path

from tools.graph.db import GraphDB
from tools.network import fleet_org_reachability as reach
from tools.network.fleet_sync.tests.test_org_channel_routing import (
    ORG, OTHER_ORG, Member, _cert, _has, _insert, _wait,
)
from tools.network.idkit import KeyPair


def test_row_roundtrip_and_every_refusal(tmp_path: Path) -> None:
    pa, pb, px = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
    a = Member(tmp_path, "a", pa, [pa.public_hex, pb.public_hex])
    now = int(time.time())
    row = reach.build_row(a.machine, a.persona_cert, ["ws://10.0.0.1:9", "ws://10.0.0.1:9"], now=now)
    assert row["addresses"] == ["ws://10.0.0.1:9"]
    key = a.machine.public_hex
    members = lambda p: p in {pa.public_hex, pb.public_hex}
    assert reach.verify_row(key, row, org=ORG, now=now, is_member=members) == (
        pa.public_hex, ["ws://10.0.0.1:9"]
    )
    # Membership unknown: the row stands as a hint.
    assert reach.verify_row(key, row, org=ORG, now=now) is not None
    # Tampered addresses: the machine signature no longer covers them.
    tampered = copy.deepcopy(row); tampered["addresses"] = ["ws://6.6.6.6:1"]
    assert reach.verify_row(key, tampered, org=ORG, now=now) is None
    # Key names another machine.
    assert reach.verify_row("00" * 32, row, org=ORG, now=now) is None
    # A row for another organization presented in this one.
    assert reach.verify_row(key, row, org=OTHER_ORG, now=now) is None
    # Persona outside the adopted member set.
    assert reach.verify_row(key, row, org=ORG, now=now, is_member=lambda p: False) is None
    # A certificate from a persona that is not the row's persona.
    forged = copy.deepcopy(row)
    forged["persona_cert"] = _cert(px, a.machine).to_dict()
    assert reach.verify_row(key, forged, org=ORG, now=now) is None
    # Not even a dict.
    assert reach.verify_row(key, "junk", org=ORG, now=now) is None


def test_publish_only_when_the_address_set_changes(tmp_path: Path, monkeypatch) -> None:
    pa, pb = KeyPair.generate(), KeyPair.generate()
    a = Member(tmp_path, "a", pa, [pa.public_hex, pb.public_hex])
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(a.orgs_dir))
    # Nothing to say yet: no row.
    assert reach.publish_if_changed("alpha", a.machine, a.persona_cert, []) is False
    assert reach.read_rows(a.alpha) == {}
    assert reach.publish_if_changed("alpha", a.machine, a.persona_cert, ["ws://10.0.0.1:9"]) is True
    # Same set, any number of times: no write (no heartbeat).
    for _ in range(3):
        assert reach.publish_if_changed("alpha", a.machine, a.persona_cert, ["ws://10.0.0.1:9"]) is False
    rows = reach.read_rows(a.alpha)
    assert list(rows) == [a.machine.public_hex]
    assert rows[a.machine.public_hex]["addresses"] == ["ws://10.0.0.1:9"]
    # The set changed: one write, and the row now says so.
    assert reach.publish_if_changed(
        "alpha", a.machine, a.persona_cert, ["ws://10.0.0.2:9", "ws://10.0.0.1:9"],
    ) is True
    assert reach.read_rows(a.alpha)[a.machine.public_hex]["addresses"] == [
        "ws://10.0.0.2:9", "ws://10.0.0.1:9",
    ]
    # Readers: another machine sees A; A does not see itself.
    other = KeyPair.generate().public_hex
    assert reach.co_member_addresses(a.alpha, org=ORG, own_machine_pub=other) == {
        a.machine.public_hex: ["ws://10.0.0.2:9", "ws://10.0.0.1:9"],
    }
    assert reach.co_member_addresses(a.alpha, org=ORG, own_machine_pub=a.machine.public_hex) == {}
    # Stopped listening: the row is replaced by an empty one, once.
    assert reach.publish_if_changed("alpha", a.machine, a.persona_cert, []) is True
    assert reach.publish_if_changed("alpha", a.machine, a.persona_cert, []) is False
    assert reach.co_member_addresses(a.alpha, org=ORG, own_machine_pub=other) == {}


def test_co_member_learns_addresses_from_the_replicated_rows(tmp_path: Path, monkeypatch) -> None:
    pa, pb = KeyPair.generate(), KeyPair.generate()
    members = [pa.public_hex, pb.public_hex]
    a = Member(tmp_path, "a", pa, members)
    b = Member(tmp_path, "b", pb, members)
    # A's settings write path resolves alpha to A's own org database.
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(a.orgs_dir))
    advertised: list[str] = []
    first_contact: dict = {}
    pulls: list[tuple[str, str | None, str]] = []
    b_port: list[int] = []

    def record(peer, **values):
        # B also SERVES A once A has pulled back; only B's own pulls carry
        # the candidate address this test follows.
        if values.get("direction") != "pull":
            return
        pulls.append((values.get("scope"), values.get("address"), values.get("outcome")))

    async def run() -> None:
        server = a.scheduler(advertised=lambda: list(advertised))
        await server.start()
        real = f"ws://127.0.0.1:{server.port}"
        advertised.append(real)
        # First contact for B is the hook (another machine of B's member,
        # or the join rendezvous); it is withdrawn after B's first success
        # so everything after rides the replicated rows alone.
        first_contact["alpha"] = {a.machine.public_hex: [real]}
        puller = b.scheduler(
            org_peers=lambda: dict(first_contact), recorder=record,
            advertised=lambda: [f"ws://127.0.0.1:{p}" for p in b_port],
        )
        try:
            _insert(a.alpha, "row-1", "first org row")
            await puller.start()
            b_port.append(puller.port)
            await _wait(lambda: _has(b.alpha, "row-1"), timeout=30.0, label="first contact pull")
            first_contact.clear()
            # Sync is pull-only, so A learns where B is from B's own hello
            # (signed by B's machine key) and pulls B back: an org row
            # written on B's machine reaches A with no hook on A's side.
            _insert(b.alpha, "b-row-1", "written on the joiner")
            await _wait(lambda: _has(a.alpha, "b-row-1"), timeout=30.0,
                        label="the first-dialled side pulls back through the hello introduction")
            assert server._org_peer_candidates(server._org_channels())["alpha"] == {
                b.machine.public_hex: (f"ws://127.0.0.1:{puller.port}",),
            }
            await _wait(
                lambda: reach.co_member_addresses(
                    b.alpha, org=ORG, own_machine_pub=b.machine.public_hex,
                    is_member=b.channel.is_member,
                ) == {a.machine.public_hex: [real]},
                timeout=30.0, label="A's reachability row on B",
            )
            # B's own row is not published: B's org database is not the
            # settings home for "alpha" in this process (that is A's), so
            # the scheduler skips the write rather than misplace the row.
            assert reach.co_member_addresses(
                a.alpha, org=ORG, own_machine_pub=a.machine.public_hex,
            ) == {}
            _insert(a.alpha, "row-2", "found through the row, not the hook")
            await _wait(lambda: _has(b.alpha, "row-2"), timeout=30.0, label="row-based pull")
            # A's address set changes (a dead address ahead of the live
            # one): one new row, replicated; B follows it and still
            # reaches A through the live candidate.
            advertised.insert(0, "ws://127.0.0.1:1")
            await _wait(
                lambda: reach.co_member_addresses(
                    b.alpha, org=ORG, own_machine_pub=b.machine.public_hex,
                ) == {a.machine.public_hex: ["ws://127.0.0.1:1", real]},
                timeout=30.0, label="A's changed row on B",
            )
            before = len([p for p in pulls if p[2] == "success"])
            _insert(a.alpha, "row-3", "after the address change")
            await _wait(lambda: _has(b.alpha, "row-3"), timeout=30.0, label="pull after change")
            await _wait(
                lambda: len([p for p in pulls if p[2] == "success"]) > before,
                timeout=30.0, label="a recorded success after the change",
            )
            assert all(p[1] == real for p in pulls if p[2] == "success")
        finally:
            await puller.stop()
            await server.stop()
        # Exactly one row was written per distinct address set: two sets.
        rows = reach.read_rows(a.alpha)
        assert list(rows) == [a.machine.public_hex]
        # Membership filter: B adopts a checkpoint without A's persona and
        # A's row drops out of B's peer list without any row changing.
        b.adopt(1, [pb.public_hex])
        assert reach.co_member_addresses(
            b.alpha, org=ORG, own_machine_pub=b.machine.public_hex,
            is_member=b.channel.is_member,
        ) == {}

    asyncio.run(run())
