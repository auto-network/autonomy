"""Object create/read: round-trips, backward recovery, fail-closed paths."""

from __future__ import annotations

from tools.network.dag_tag import AUTHORITY, tag_dag

import dataclasses
import os

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger.projections import organization_content_domain_id
from tools.network.ledger.tests.conftest import Sim
from tools.network.storagekit import bridge as bridge_mod
from tools.network.storagekit import object_header, objects, state
from tools.network.storagekit.acceptance import (
    AuthorityError,
    DomainError,
    loss_projection_digest,
)
from tools.network.storagekit.errors import CommitmentError
from tools.network.storagekit.lifecycle import StateAdvanceRequired
from tools.network.storagekit.object_header import ObjectHeaderError
from tools.network.storagekit.objects import (
    BodyHashError,
    StateUnreachableError,
    count_object,
    create_object,
    read_object,
)
from tools.network.storagekit.suites import BODY_SUITE_DEFAULT, BODY_SUITE_LARGE

PLAINTEXT = b"the content body, encrypted exactly once"


class World:
    """Org + a three-state chain S0 <- S1 <- S2 with bridges."""

    def __init__(self):
        self.sim = Sim()
        s = self.sim
        s.role_define(s.root, "member", scope_set=["link:publish"])
        self.member, invite_key = KeyPair.generate(), KeyPair.generate()
        iid = s.invite(s.root, "member", invite_key=invite_key)
        s.claim(iid, invite_key, self.member)
        self.gen = s.genesis_id
        self.dom = organization_content_domain_id(self.gen)

        self.f0 = s.fold()  # pre-contraction frontier
        self.s0, self.sec0 = self.mint(self.f0)
        # One contraction, then two chained advanced states covering it.
        d = s.delegate(s.root, KeyPair.generate(), ["link:publish"])
        s.revoke_event(s.root, d)
        self.f1 = s.fold()
        self.s1, self.sec1 = self.mint(self.f1, parents=[self.s0.state_id])
        self.s2, self.sec2 = self.mint(self.f1, parents=[self.s1.state_id])
        self.descriptors = {x.state_id: x for x in (self.s0, self.s1, self.s2)}
        self.bridges = [
            self.bridge(self.s1, self.sec1, self.s0, self.sec0),
            self.bridge(self.s2, self.sec2, self.s1, self.sec1),
        ]

    def mint(self, frontier, parents=()):
        return state.generate(
            self.member, self.gen, self.dom, parents, list(frontier.heads),
            sorted(frontier.loss_heads), loss_projection_digest(frontier),
        )

    def bridge(self, child, child_secret, parent, parent_secret):
        return bridge_mod.create(
            self.member,
            genesis_id=self.gen,
            domain_id=self.dom,
            child_state_id=child.state_id,
            parent_state_id=parent.state_id,
            child_state_secret=child_secret,
            parent_state_secret=parent_secret,
            authority_heads=list(child.authority_heads),
        )

    @tag_dag(AUTHORITY)
    def ancestry(self, ids):
        return self.sim.ledger.ancestry(ids)

    def create(self, frontier=None, available=None, held=None, author=None, **kw):
        kw.setdefault("object_id", os.urandom(32).hex())
        kw.setdefault("revision_id", os.urandom(32).hex())
        kw.setdefault("body_suite_id", BODY_SUITE_DEFAULT)
        kw.setdefault("bridges", self.bridges)
        kw.setdefault("descriptors", self.descriptors)
        return create_object(
            author or self.member,
            self.dom,
            PLAINTEXT,
            frontier or self.f1,
            held if held is not None else {self.s1.state_id: self.sec1},
            available if available is not None else {self.s1.state_id: self.s1},
            ancestry=self.ancestry,
            **kw,
        )


@pytest.fixture(scope="module")
def w() -> World:
    return World()


# -- round trips -----------------------------------------------------------------------


@pytest.mark.parametrize("suite", [BODY_SUITE_DEFAULT, BODY_SUITE_LARGE])
def test_roundtrip_under_each_body_suite(w, suite):
    header, blob = w.create(body_suite_id=suite)
    assert header.storage_state_id == w.s1.state_id
    assert header.writer_authority_heads == tuple(sorted(w.f1.heads))
    plaintext = read_object(
        header, blob, {w.s1.state_id: w.sec1}, w.bridges, w.descriptors
    )
    assert plaintext == PLAINTEXT


def test_descendant_reader_reaches_ancestor_state(w):
    # Object under S0; reader holds only S2, two bridge hops up.
    header, blob = w.create(
        frontier=w.f0, available={w.s0.state_id: w.s0}, held={w.s0.state_id: w.sec0}
    )
    plaintext = read_object(
        header, blob, {w.s2.state_id: w.sec2}, w.bridges, w.descriptors
    )
    assert plaintext == PLAINTEXT


def test_ancestor_only_holder_cannot_read(w):
    # Object under S1; a holder of only the ancestor S0 has no path up.
    header, blob = w.create()
    with pytest.raises(StateUnreachableError):
        read_object(header, blob, {w.s0.state_id: w.sec0}, w.bridges, w.descriptors)
    with pytest.raises(StateUnreachableError):
        read_object(header, blob, {}, w.bridges, w.descriptors)


# -- create paths ----------------------------------------------------------------------


def test_advance_required_when_nothing_covers(w):
    with pytest.raises(StateAdvanceRequired):
        w.create(available={w.s0.state_id: w.s0}, held={w.s0.state_id: w.sec0})


def test_create_recovers_secret_from_held_descendant(w):
    # S1 selected; only S2's secret held; bridge S2->S1 supplies it.
    header, blob = w.create(held={w.s2.state_id: w.sec2})
    assert header.storage_state_id == w.s1.state_id
    assert read_object(
        header, blob, {w.s1.state_id: w.sec1}, w.bridges, w.descriptors
    ) == PLAINTEXT


def test_advance_required_when_no_held_secret_reaches(w):
    with pytest.raises(StateAdvanceRequired):
        w.create(held={w.s0.state_id: w.sec0})  # ancestor cannot reach S1
    with pytest.raises(StateAdvanceRequired):
        w.create(held={})


def test_denied_author_and_domain_mismatch(w):
    for outsider in (KeyPair.generate(), w.sim.root):
        with pytest.raises(AuthorityError):
            w.create(author=outsider)
    with pytest.raises(DomainError):
        create_object(
            w.member, "9c" * 32, PLAINTEXT, w.f1,
            {w.s1.state_id: w.sec1}, {w.s1.state_id: w.s1},
            ancestry=w.ancestry,
            object_id=os.urandom(32).hex(), revision_id=os.urandom(32).hex(),
            body_suite_id=BODY_SUITE_DEFAULT,
        )


def test_create_rejects_wrong_held_secret(w):
    with pytest.raises(CommitmentError):
        w.create(held={w.s1.state_id: os.urandom(32)})


# -- read fail-closed --------------------------------------------------------------------


def test_body_tamper_fails_before_any_unwrap(w, monkeypatch):
    header, blob = w.create()

    def bomb(*args, **kwargs):
        raise AssertionError("unwrap reached despite a bad content address")

    monkeypatch.setattr(objects.object_header, "unwrap_cek", bomb)
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(BodyHashError):
        read_object(header, tampered, {w.s1.state_id: w.sec1}, w.bridges, w.descriptors)


def test_post_sign_header_tamper_fails(w):
    header, blob = w.create()
    for field in ("object_id", "revision_id"):
        crooked = dataclasses.replace(header, **{field: "9c" * 32})
        with pytest.raises(ObjectHeaderError):
            read_object(
                crooked, blob, {w.s1.state_id: w.sec1}, w.bridges, w.descriptors
            )


def test_wrong_secret_fails_commitment(w):
    header, blob = w.create()
    with pytest.raises(CommitmentError):
        read_object(header, blob, {w.s1.state_id: os.urandom(32)}, w.bridges, w.descriptors)


def test_missing_descriptor_is_unreachable(w):
    header, blob = w.create()
    descriptors = {k: v for k, v in w.descriptors.items() if k != w.s1.state_id}
    with pytest.raises(StateUnreachableError):
        read_object(header, blob, {w.s1.state_id: w.sec1}, w.bridges, descriptors)


# -- freshness and counters -----------------------------------------------------------------


def test_two_creates_are_fresh(w):
    h1, b1 = w.create()
    h2, b2 = w.create()
    assert h1.ciphertext_hash != h2.ciphertext_hash
    assert h1.wrapped_cek != h2.wrapped_cek
    assert h1.body_nonce != h2.body_nonce
    assert h1.wrap_nonce != h2.wrap_nonce
    assert b1 != b2


def test_count_object_is_per_state_exact(w):
    headers = [w.create()[0] for _ in range(2)]
    under_s0 = w.create(
        frontier=w.f0, available={w.s0.state_id: w.s0}, held={w.s0.state_id: w.sec0}
    )[0]
    counters = {}
    for header in (*headers, under_s0):
        count_object(counters, header)
    assert counters == {w.s1.state_id: 2, w.s0.state_id: 1}
    assert count_object(counters, headers[0]) == 3
