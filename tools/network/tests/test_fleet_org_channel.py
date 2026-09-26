"""Org-scope admission (auto-coea3, design graph://c2baad48-0a3): the org
hello admits a member's machine by persona certificate plus the registry's
membership-proof rider, verified against a RETAINED adopted checkpoint at or
after the peer's admission (OrgAdmission.tla E-any-adm); the server proves
back under the client's checkpoint (prover-downgrade); every refusal is
typed; removal is judged against the newest adopted member set, per message.
"""

from __future__ import annotations

import json
import time

import pytest

from tools.network import clock

from tools.dashboard.tests.membership_sim._harness import Org, RoleSpec
from tools.network.fleet_org_channel import (
    CLOSE_MEMBERSHIP_STALE, MembershipStaleError,
    OrgFleetAuthenticator,
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
    the retained adopted checkpoints (a dict seq -> record). ``admitted_at``
    optionally maps persona -> the seq of its current admission, for the
    E-any-adm floor (None: the node models no admissions)."""

    def __init__(self, persona: KeyPair, members: list[str], *, seq: int = 0,
                 org: str = ORG, now=None, admitted_at: dict[str, int] | None = None):
        self.persona = persona
        self.machine = KeyPair.generate()
        self.adopted = {seq: {"seq": seq, "members_root": mc.compute_root(members)}}
        self.members_at = {seq: list(members)}
        self.rider_seq = seq
        self.admitted_at = admitted_at
        #: Optional rotation list of seqs this node may prove under, indexed
        #: by the authenticator's attempt counter (a stand-in for the
        #: production prover's candidate roots).
        self.candidates: list[int] = []
        self.auth = OrgFleetAuthenticator(
            self.machine, org=org, persona_cert=_cert(persona, self.machine, org),
            membership_proof_for=self.rider,
            adopted_checkpoint_for=lambda s: self.adopted.get(int(s)),
            newest_adopted_seq=lambda: max(self.adopted),
            retained_checkpoints=lambda: list(self.adopted.values()),
            adopted_members_for=lambda s: self.members_at.get(int(s)),
            admission_ok_for=self.admission_ok,
            now=now or time.time,
        )

    def rider(self, under: int | None = None, root: str | None = None, attempt: int = 0) -> dict:
        seq, label = self.rider_seq, self.rider_seq
        if root is None and attempt and self.candidates:
            seq = label = self.candidates[attempt % len(self.candidates)]
        if root is not None:
            for k, record in self.adopted.items():
                if record["members_root"] == root:
                    seq, label = k, (int(under) if under is not None else k)
        members = self.members_at[seq]
        try:
            index, path = mc.inclusion_proof(members, self.persona.public_hex)
        except mc.MembershipCommitmentError:
            index, path = 0, []  # an outsider fabricates a proof of itself
        return {"v": 1, "checkpoint_seq": label, "index": index, "path": path}

    def admission_ok(self, seq: int, persona: str) -> bool | None:
        if self.admitted_at is None or persona not in self.admitted_at:
            return None
        return int(seq) >= self.admitted_at[persona]

    def adopt(self, seq: int, members: list[str]) -> None:
        """This node adopts and retains checkpoint *seq*; its own hello
        proves under it from now on, and *members* is the newest set."""
        self.adopted[seq] = {"seq": seq, "members_root": mc.compute_root(members)}
        self.members_at[seq] = list(members)
        self.rider_seq = seq


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

    # A member proving under a checkpoint the server has not adopted, whose
    # root the server retains under no seq either (the label alone is never
    # what is checked: the same root under another label admits).
    alice = Node(org.founder, members)
    alice.rider_seq = 1
    alice.members_at[1] = sorted(members + [KeyPair.generate().public_hex])
    with pytest.raises(HandshakeError, match="nor in any other retained one"):
        _handshake(alice, bob)
    alice.members_at[1] = members
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
    stale = Node(org.founder, members, now=lambda: time.time() - clock.MAX_CLOCK_SKEW - 60)
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


def test_adopting_a_removing_checkpoint_refuses_the_removed_member_at_once(org):
    """Removal is judged against the newest adopted member set, per served
    message: no grace window, nothing to re-prove (OrgAdmission.tla)."""
    members = list(org.member_pubs())
    alice = Node(org.founder, members)
    bob = Node(org.personas["bob"], members)
    _handshake(alice, bob)
    bob.auth.authorize(alice.machine.public_hex)

    # Bob adopts seq 1: alice removed. Her next message is refused, typed.
    without_alice = [m for m in members if m != org.founder.public_hex]
    bob.adopt(1, without_alice)
    with pytest.raises(MembershipStaleError, match="removed") as caught:
        bob.auth.authorize(alice.machine.public_hex)
    assert caught.value.close_code == CLOSE_MEMBERSHIP_STALE == 4417
    # Her old proof still verifies under the retained seq 0, but she is not
    # in the newest set: a NEW connection is refused too.
    fresh_alice = Node(org.founder, members)
    with pytest.raises(MembershipStaleError, match="removed"):
        _handshake(fresh_alice, bob)
    # And she cannot prove under seq 1 at all.
    alice.members_at[1] = without_alice
    alice.rider_seq = 1
    with pytest.raises(HandshakeError, match="not in the adopted member set"):
        _handshake(alice, bob)
    # Bob's other peers are unaffected by the adoption.
    bob.auth.authorize  # (bob admits nobody else here; see the join test)


def test_any_retained_checkpoint_admits_and_the_server_proves_back_under_it(org):
    """E-any-adm and prover-downgrade: a peer proving under an OLDER
    retained checkpoint is admitted, keeps being served after this node
    adopts newer ones, and receives a server hello it can verify."""
    members = list(org.member_pubs())
    alice = Node(org.founder, members)
    bob = Node(org.personas["bob"], members)
    _handshake(alice, bob)

    # A third member joins; bob adopts seq 1 with the larger set. Alice,
    # admitted under seq 0, is still a member of the newest set: served.
    carol = KeyPair.generate()
    larger = sorted(members + [carol.public_hex])
    bob.adopt(1, larger)
    bob.auth.authorize(alice.machine.public_hex)
    # A fresh connection from alice still proves under seq 0 (she has not
    # adopted 1): admitted, and bob's server hello proves back under 0,
    # which is what alice can verify.
    fresh_alice = Node(org.founder, members)
    _priv, hello = fresh_alice.auth.build_client_hello("s9")
    _peer, _spriv, server_hello, _t = bob.auth.accept_client(hello, session="s9")
    assert json.loads(server_hello)["membership_proof"]["checkpoint_seq"] == 0
    fresh_alice.auth.verify_server(
        server_hello, session="s9", client_eph=json.loads(hello)["eph_pub"],
        expected_machine_pub=bob.machine.public_hex,
    )
    assert bob.auth.admitted(fresh_alice.machine.public_hex).checkpoint_seq == 0
    # Carol, who adopted seq 1, is admitted under it; bob proves back under 1.
    carol_node = Node(carol, larger, seq=1)
    _priv, hello = carol_node.auth.build_client_hello("s10")
    _peer, _spriv, server_hello, _t = bob.auth.accept_client(hello, session="s10")
    assert json.loads(server_hello)["membership_proof"]["checkpoint_seq"] == 1
    # Bob dialling alice proves under HIS newest (1), which alice has not
    # retained: alice refuses, typed; the pair still syncs in the other
    # direction, which is what the model's liveness rests on.
    with pytest.raises(HandshakeError, match="nor in any other retained one"):
        _handshake(bob, fresh_alice)

    # A rekey: alice's persona key changes; her new leaf is in the set and
    # she proves under it with a cert from the NEW persona; the old persona
    # is no longer in the newest set.
    alice_new = KeyPair.generate()
    rekeyed = sorted([m for m in larger if m != org.founder.public_hex] + [alice_new.public_hex])
    bob.adopt(2, rekeyed)
    alice2 = Node(alice_new, rekeyed, seq=2)
    _handshake(alice2, bob)
    bob.auth.authorize(alice2.machine.public_hex)
    with pytest.raises(MembershipStaleError, match="removed"):
        bob.auth.authorize(fresh_alice.machine.public_hex)


def test_a_checkpoint_before_the_peers_admission_does_not_admit_it(org):
    """E-any-adm's floor: a retained checkpoint older than the peer's current
    admission never admits it, even when a proof verifies under it (a
    re-admitted persona proving under a record from its first membership)."""
    members = list(org.member_pubs())
    alice = Node(org.founder, members)
    # Bob retains seq 0 and 1 with alice in both, but records her CURRENT
    # admission at seq 1 (removed and re-admitted in between).
    bob = Node(org.personas["bob"], members, admitted_at={org.founder.public_hex: 1})
    bob.adopt(1, members)
    with pytest.raises(HandshakeError, match="predates this persona's current admission"):
        _handshake(alice, bob)
    alice.adopt(1, members)
    _handshake(alice, bob)
    bob.auth.authorize(alice.machine.public_hex)


def test_a_replayed_client_hello_is_refused_and_removal_is_typed() -> None:
    pa, pb = KeyPair.generate(), KeyPair.generate()
    members = [pa.public_hex, pb.public_hex]
    a, b = Node(pa, members), Node(pb, members)
    _priv, hello = b.auth.build_client_hello("s1")
    a.auth.accept_client(hello, session="s1")
    # The same capture again, on the same or a new session id: refused
    # before any other check -- its ephemeral key was seen already.
    for session in ("s1", "s2"):
        with pytest.raises(HandshakeError, match="replayed"):
            a.auth.accept_client(hello, session=session)
    # A fresh hello from the same machine is fine.
    _priv, fresh = b.auth.build_client_hello("s3")
    a.auth.accept_client(fresh, session="s3")
    # A adopting a newer checkpoint that keeps b changes nothing for b.
    a.adopt(1, members)
    a.auth.authorize(b.machine.public_hex)
    # A adopting one that drops b: the typed close, code 4417, on the next
    # message and on a new hello alike.
    a.adopt(2, [pa.public_hex])
    with pytest.raises(MembershipStaleError) as caught:
        a.auth.authorize(b.machine.public_hex)
    assert caught.value.close_code == CLOSE_MEMBERSHIP_STALE == 4417
    with pytest.raises(MembershipStaleError):
        _priv, gone = b.auth.build_client_hello("s4")  # b still proves under seq 0
        a.auth.accept_client(gone, session="s4")


def test_a_genuine_root_under_the_wrong_label_still_admits_and_is_answered_in_kind(org):
    """OrgAdmissionBundleBound.tla BundleBoundPlanted: a sponsor bundled the
    seq-2 root labelled seq 1. The joiner's proof recomputes that root; the
    founder retains it (at seq 2) and admits; the founder proves back under
    that root labelled 1, which the joiner verifies against its own record.
    The pair syncs, so the joiner can pull the events that heal its cache."""
    members = list(org.member_pubs())
    carol = KeyPair.generate()
    larger = sorted(members + [carol.public_hex])
    founder = Node(org.founder, members)          # seq 0: {F, bob}
    founder.adopt(1, members)                     # seq 1: {F, bob} again
    founder.adopt(2, larger)                      # seq 2: {F, bob, carol}
    planted = Node(carol, larger, seq=1)          # carol's record 1 carries seq 2's root
    _priv, hello = planted.auth.build_client_hello("p1")
    _peer, _spriv, server_hello, _t = founder.auth.accept_client(hello, session="p1")
    reply = json.loads(server_hello)["membership_proof"]
    assert reply["checkpoint_seq"] == 1
    mc.verify_inclusion(mc.compute_root(larger), org.founder.public_hex, reply["index"], reply["path"])
    planted.auth.verify_server(
        server_hello, session="p1", client_eph=json.loads(hello)["eph_pub"],
        expected_machine_pub=founder.machine.public_hex,
    )
    founder.auth.authorize(planted.machine.public_hex)
    # A root nobody retains is still refused, naming both failures.
    outsider_root = Node(carol, sorted(members + [carol.public_hex, KeyPair.generate().public_hex]), seq=1)
    with pytest.raises(HandshakeError, match="nor in any other retained one"):
        _handshake(outsider_root, founder)


def test_refusals_rotate_the_candidate_and_a_completed_hello_resets(org):
    """OrgAdmissionBundleBound.tla Rotate (master ac114f59): an up-to-date
    client dialling a lagging server proves under its newest root, is
    refused, and on the next attempt proves under an older candidate the
    server retains; the completed hello resets the counter. hello_state
    reports the stuck state in between."""
    members = list(org.member_pubs())
    carol = KeyPair.generate()
    larger = sorted(members + [carol.public_hex])
    lagging = Node(org.personas["bob"], members)          # retains seq 0 only
    client = Node(org.founder, members)
    client.adopt(1, larger)                                # newest: seq 1
    client.candidates = [1, 0]
    assert client.auth.hello_state()["stuck"] is False
    with pytest.raises(HandshakeError, match="nor in any other retained one"):
        _handshake(client, lagging)
    client.auth.note_refusal(lagging.machine.public_hex, "refused")
    state = client.auth.hello_state()
    assert state["stuck"] is True and state["attempts_since_completed"] == 1
    assert state["last_refused"]["peer"] == lagging.machine.public_hex
    _handshake(client, lagging)                            # attempt 1 -> seq 0
    state = client.auth.hello_state()
    assert state["stuck"] is False and state["attempts_since_completed"] == 0
    assert state["last_completed"]["checkpoint_seq"] == 0
    assert state["last_completed"]["members_root"] == mc.compute_root(members)


def test_an_idle_connection_is_refused_after_removal_and_re_admission(org):
    """OrgAdmissionLeaves.tla ReAdmitAfterRemoval at connection level: a
    connection admitted under a record from before a removal does not
    survive the removal plus a re-admission adopted with no message served
    in between; the floor is re-checked per message."""
    members = list(org.member_pubs())
    alice = Node(org.founder, members)
    bob = Node(org.personas["bob"], members, admitted_at={org.founder.public_hex: 0})
    _handshake(alice, bob)
    bob.auth.authorize(alice.machine.public_hex)
    without_alice = [m for m in members if m != org.founder.public_hex]
    bob.adopt(1, without_alice)                 # removal
    bob.adopt(2, members)                       # re-admission: a new claim
    bob.admitted_at[org.founder.public_hex] = 2
    with pytest.raises(MembershipStaleError, match="must hello again"):
        bob.auth.authorize(alice.machine.public_hex)
    alice.adopt(2, members)
    _handshake(alice, bob)
    bob.auth.authorize(alice.machine.public_hex)


def test_a_rekeyed_old_key_is_refused_even_under_a_retained_pre_rekey_root(org):
    """OrgAdmissionLeaves.tla RekeyedOldKeyExcluded: the old leaf is in a
    retained root, but not in the newest set."""
    members = list(org.member_pubs())
    bob = Node(org.personas["bob"], members)
    alice_new = KeyPair.generate()
    rekeyed = sorted([m for m in members if m != org.founder.public_hex] + [alice_new.public_hex])
    bob.adopt(1, rekeyed)
    old_alice = Node(org.founder, members)      # proves under seq 0, old leaf
    with pytest.raises(MembershipStaleError, match="removed"):
        _handshake(old_alice, bob)


def test_a_root_that_fell_out_of_retention_is_refused(org):
    """The retention cap is an assumption of the proof (lag <= 64 records,
    or an own-fold root still retained): a proof under an evicted root is
    refused."""
    members = list(org.member_pubs())
    bob = Node(org.personas["bob"], members)
    del bob.adopted[0]; del bob.members_at[0]
    bob.adopt(70, members + [KeyPair.generate().public_hex])
    alice = Node(org.founder, members)
    with pytest.raises(HandshakeError, match="nor in any other retained one"):
        _handshake(alice, bob)
