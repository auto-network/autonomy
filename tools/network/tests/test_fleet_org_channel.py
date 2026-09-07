"""Org-scope admission (auto-coea3, design graph://c2baad48-0a3): the org
hello admits a member's machine by persona certificate plus the registry's
membership-proof rider, verified against the ADOPTED checkpoint; every
refusal is typed; removal is the re-prove deadline after an adoption."""

from __future__ import annotations

import json
import time

import pytest

from tools.dashboard.tests.membership_sim._harness import Org, RoleSpec
from tools.network.fleet_org_channel import (
    ORG_HELLO_MAX_SKEW_S, OrgFleetAuthenticator,
)
from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.ledger import membership_commitment as mc
from tools.network.relaykit.channel import HandshakeError

ORG = "genesis-" + "ab" * 28


def _cert(persona: KeyPair, machine: KeyPair, org: str = ORG):
    now = int(time.time())
    return issue_cert(
        persona, machine.public_hex, scope=("fleet:sync",), org=org,
        subject=Subject("persona", persona.public_hex),
        not_before=now - 300, not_after=now + 86_400,
    )


class Node:
    """One member machine: its persona, machine key, and its own view of
    the adopted checkpoints (a dict seq -> record)."""

    def __init__(self, persona: KeyPair, members: list[str], *, seq: int = 0,
                 org: str = ORG, now=None, monotonic=None):
        self.persona = persona
        self.machine = KeyPair.generate()
        self.adopted = {seq: {"seq": seq, "members_root": mc.compute_root(members)}}
        self.members_at = {seq: list(members)}
        self.rider_seq = seq
        self.auth = OrgFleetAuthenticator(
            self.machine, org=org, persona_cert=_cert(persona, self.machine, org),
            membership_proof_for=self.rider,
            adopted_checkpoint_for=lambda s: self.adopted.get(int(s)),
            newest_adopted_seq=lambda: max(self.adopted),
            now=now or time.time, monotonic=monotonic or time.monotonic,
        )

    def rider(self) -> dict:
        members = self.members_at[self.rider_seq]
        try:
            index, path = mc.inclusion_proof(members, self.persona.public_hex)
        except mc.MembershipCommitmentError:
            index, path = 0, []  # an outsider fabricates a proof of itself
        return {"v": 1, "checkpoint_seq": self.rider_seq, "index": index, "path": path}

    def adopt(self, seq: int, members: list[str]) -> None:
        self.adopted[seq] = {"seq": seq, "members_root": mc.compute_root(members)}
        self.members_at[seq] = list(members)
        # A node's own hello proves its persona under what it has adopted.
        self.rider_seq = seq
        self.auth.note_adoption(seq)


def _handshake(client: Node, server: Node, session: str = "s1"):
    priv, hello = client.auth.build_client_hello(session)
    peer, _spriv, server_hello, t_server = server.auth.accept_client(hello, session=session)
    assert peer == client.machine.public_hex
    client_eph = json.loads(hello)["eph_pub"]
    _seph, t_client = client.auth.verify_server(
        server_hello, session=session, client_eph=client_eph,
        expected_machine_pub=server.machine.public_hex,
    )
    assert t_server == t_client
    return t_client


@pytest.fixture
def org():
    o = Org.found(org=ORG, roles={"member": RoleSpec(scope_set=("fleet:sync",), requires="self")})
    o.admit("bob", "member")
    return o


def test_two_members_admit_each_other_mutually(org):
    members = list(org.member_pubs())
    alice = Node(org.founder, members)
    bob = Node(org.personas["bob"], members)
    _handshake(alice, bob)
    assert bob.auth.admitted(alice.machine.public_hex).persona_pub == org.founder.public_hex
    assert alice.auth.admitted(bob.machine.public_hex).persona_pub == org.personas["bob"].public_hex
    bob.auth.authorize(alice.machine.public_hex)
    alice.auth.authorize(bob.machine.public_hex)


def test_refusals_are_typed_and_name_the_check(org):
    members = list(org.member_pubs())
    bob = Node(org.personas["bob"], members)

    # An outsider with a fabricated proof of itself.
    outsider = Node(KeyPair.generate(), members)
    with pytest.raises(HandshakeError, match="not in the adopted member set"):
        _handshake(outsider, bob)

    # A member proving under a checkpoint the server has not adopted.
    alice = Node(org.founder, members)
    alice.rider_seq = 1
    alice.members_at[1] = members
    with pytest.raises(HandshakeError, match="has not adopted"):
        _handshake(alice, bob)

    # A member's cert for ANOTHER organization presented on this one.
    alice_other = Node(org.founder, members, org="genesis-" + "cd" * 28)
    with pytest.raises(HandshakeError, match="another organization"):
        _, hello = alice_other.auth.build_client_hello("s")
        bob.auth.accept_client(hello, session="s")

    # A cert that delegates to a machine other than the one signing.
    alice = Node(org.founder, members)
    stolen = KeyPair.generate()
    alice.auth.persona_cert = _cert(org.founder, stolen)
    with pytest.raises(HandshakeError, match="names another machine"):
        _handshake(alice, bob)

    # A replayed capture: a valid hello whose ts is outside the window.
    stale = Node(org.founder, members, now=lambda: time.time() - ORG_HELLO_MAX_SKEW_S - 60)
    with pytest.raises(HandshakeError, match="freshness"):
        _handshake(stale, bob)

    # A hello without the rider.
    alice = Node(org.founder, members)
    _, hello = alice.auth.build_client_hello("s")
    body = json.loads(hello); body.pop("membership_proof")
    with pytest.raises(HandshakeError, match="must carry exactly"):
        bob.auth.accept_client(json.dumps(body), session="s")

    # A tampered rider (path) under a valid signature attempt: refused at inclusion.
    alice = Node(org.founder, members)
    _, hello = alice.auth.build_client_hello("s")
    body = json.loads(hello)
    body["membership_proof"]["path"] = ["00" * 32] * len(body["membership_proof"]["path"])
    with pytest.raises(HandshakeError):
        bob.auth.accept_client(json.dumps(body), session="s")

    # Nobody was admitted by any of these.
    assert bob.auth.admitted(outsider.machine.public_hex) is None


def test_adopting_a_removing_checkpoint_closes_the_removed_member_after_the_deadline(org):
    members = list(org.member_pubs())
    clock = [1000.0]
    alice = Node(org.founder, members)
    bob = Node(org.personas["bob"], members, monotonic=lambda: clock[0])
    _handshake(alice, bob)
    bob.auth.authorize(alice.machine.public_hex)

    # Bob adopts seq 1: alice removed. Inside the deadline alice is still served.
    without_alice = [m for m in members if m != org.founder.public_hex]
    bob.adopt(1, without_alice)
    bob.auth.authorize(alice.machine.public_hex)
    assert [p.persona_pub for p in bob.auth.stale_peers()] == [org.founder.public_hex]
    # Alice cannot re-prove under the new root.
    alice.members_at[1] = without_alice
    alice.rider_seq = 1
    with pytest.raises(HandshakeError, match="not in the adopted member set"):
        bob.auth.reprove(alice.machine.public_hex, alice.rider())
    # After the deadline, every served message is refused.
    clock[0] += 5.1
    with pytest.raises(HandshakeError, match="re-prove required"):
        bob.auth.authorize(alice.machine.public_hex)
    # And a NEW connection under the old checkpoint is refused too.
    fresh_alice = Node(org.founder, members)
    with pytest.raises(HandshakeError, match="re-prove required"):
        _handshake(fresh_alice, bob)


def test_a_join_or_rekey_checkpoint_keeps_honest_members_who_reprove(org):
    members = list(org.member_pubs())
    clock = [1000.0]
    alice = Node(org.founder, members)
    bob = Node(org.personas["bob"], members, monotonic=lambda: clock[0])
    _handshake(alice, bob)

    # A third member joins; bob adopts seq 1 with the larger set.
    carol = KeyPair.generate()
    larger = sorted(members + [carol.public_hex])
    bob.adopt(1, larger)
    assert bob.auth.stale_peers()
    # Alice re-proves under seq 1 and survives, before and after the deadline.
    alice.members_at[1] = larger
    alice.rider_seq = 1
    bob.auth.reprove(alice.machine.public_hex, alice.rider())
    assert bob.auth.stale_peers() == []
    clock[0] += 60
    bob.auth.authorize(alice.machine.public_hex)

    # A rekey: alice's persona key changes; her new leaf is in the set and
    # she re-proves under it with a cert from the NEW persona.
    alice_new = KeyPair.generate()
    rekeyed = sorted([m for m in larger if m != org.founder.public_hex] + [alice_new.public_hex])
    bob.adopt(2, rekeyed)
    alice2 = Node(alice_new, rekeyed, seq=2)
    _handshake(alice2, bob)
    bob.auth.authorize(alice2.machine.public_hex)
