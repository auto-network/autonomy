"""Transport-independent verification harness (contract §16).

Drives the whole storagekit subsystem as pure state-machine logic over
an in-memory two-stream feed delivered in arbitrary order. The harness
REUSES the real module functions and the real authority fold — it
reimplements no acceptance, recovery, wrap, or commit-status logic; its
own code is stores, dependency-deferral, and bookkeeping only.

Streams: the authority stream carries ledger events; the content-key
stream carries storagekit records (state descriptors, parent bridges,
capability grants, receipts) and content bodies. ``drive`` applies a
feed in a shuffled order into a fresh replica, retrying items whose
dependencies (parents, cited frontiers, referenced descriptors) have
not arrived — so any delivery order converges — and records every
acceptance rejection instead of admitting the record anywhere.

Per-principal held-secret stores model exactly what a principal holds:
mint seeds plus accepted grants addressed to its credential. A snapshot
taken at removal models the §3 adversary — a removed principal
retaining every pre-removal secret, all public records, and all
ciphertext.
"""

from __future__ import annotations

import random

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger import Ledger, fold as ledger_fold
from tools.network.ledger.errors import LedgerError
from tools.network.ledger.projections import organization_content_domain_id
from tools.network.ledger.tests.conftest import Sim
from tools.network.storagekit import (
    acceptance,
    bridge as bridge_mod,
    capability,
    credentials,
    distribution,
    lifecycle,
    objects,
    state as state_mod,
)
from tools.network.storagekit.errors import StorageError

HLC0 = (1_800_000_000_000, 0)


class KeyControlStore:
    """Accepted key-control records + the ``state_ancestry`` interface the
    storage-topic domination check consumes (register pin 4): the
    inclusive closure over accepted descriptors' ``parent_state_ids``
    edges. Object headers contribute nothing here."""

    def __init__(self):
        self.states: dict = {}  # state_id -> descriptor
        self.history_complete: dict = {}
        self.bridges: list = []
        self.grants: list = []

    def state_ancestry(self, ids) -> frozenset:
        seen: set = set()
        stack = list(ids)
        while stack:
            state_id = stack.pop()
            if state_id in seen:
                continue
            seen.add(state_id)
            descriptor = self.states.get(state_id)
            if descriptor is not None:
                stack.extend(descriptor.parent_state_ids)
        return frozenset(seen)


class ReceiptStore:
    """Raw receipts + the per-member receipt-frontier index: a member's
    receipt for a state subsumes its receipts for that state's ancestors
    (CONTINUITY-SURFACE INDEX REQUIREMENTS)."""

    def __init__(self):
        self.receipts: list = []

    def frontier_index(self, state_ancestry) -> dict:
        by_member: dict = {}
        for receipt in self.receipts:
            by_member.setdefault(receipt.receiver_persona, set()).add(
                receipt.storage_state_id
            )
        return {
            member: frozenset(
                s
                for s in states
                if not any(o != s and s in state_ancestry([o]) for o in states)
            )
            for member, states in by_member.items()
        }

    def compacted(self, state_ancestry) -> list:
        """Receipts with everything strictly behind each member's receipt
        frontier deleted."""
        index = self.frontier_index(state_ancestry)
        return [
            r
            for r in self.receipts
            if r.storage_state_id in index.get(r.receiver_persona, frozenset())
        ]


class ContentStores:
    """The full content-key-side store set one receiver maintains, fed
    exclusively through the REAL acceptance layer."""

    def __init__(self, fold_at, ancestry, credentials_by_kem_id):
        self.fold_at = fold_at
        self.ancestry = ancestry
        self.credentials_by_kem_id = credentials_by_kem_id
        self.kc = KeyControlStore()
        self.receipt_store = ReceiptStore()
        self.object_store: dict = {}  # ciphertext_hash -> (header, body)
        self.counters: dict = {}
        self.rejected: list = []  # (item, error)

    def admit(self, item) -> bool:
        """True: admitted or terminally rejected. False: defer (a
        dependency has not arrived yet)."""
        kind, payload = item
        try:
            if kind == "state":
                descriptor, bridges = payload
                kept = []
                for bridge in bridges:
                    try:
                        bridge_mod.verify_signature(bridge)
                        kept.append(bridge)
                    except StorageError as exc:
                        self.rejected.append((("bridge", bridge), exc))
                result = acceptance.accept_state(
                    descriptor, self.fold_at, self.ancestry, bridges=tuple(kept)
                )
                self.kc.states[descriptor.state_id] = descriptor
                self.kc.history_complete[descriptor.state_id] = result.history_complete
                self.kc.bridges.extend(kept)
            elif kind == "bridge":
                bridge_mod.verify_signature(payload)
                self.kc.bridges.append(payload)
            elif kind == "grant":
                grant = payload
                descriptor = self.kc.states.get(grant.storage_state_id)
                if descriptor is None:
                    return False  # defer
                credential = self.credentials_by_kem_id.get(grant.recipient_kem_key_id)
                if credential is None:
                    raise StorageError("grant addressed to no known credential")
                acceptance.accept_grant(
                    grant, self.fold_at, self.ancestry, credential, descriptor
                )
                self.kc.grants.append(grant)
            elif kind == "receipt":
                capability.verify_receipt(payload)
                self.receipt_store.receipts.append(payload)
            elif kind == "object":
                header, body = payload
                descriptor = self.kc.states.get(header.storage_state_id)
                if descriptor is None:
                    return False  # defer
                acceptance.accept_object(header, self.fold_at, self.ancestry, descriptor)
                self.object_store[header.ciphertext_hash] = (header, bytes(body))
                objects.count_object(self.counters, header)
            else:
                raise StorageError(f"unknown stream item kind {kind!r}")
        except StorageError as exc:
            self.rejected.append((item, exc))
        return True


class World:
    """A founded org, credentialed principals, stores, and a feed log."""

    def __init__(self, member_count: int = 3):
        self.sim = Sim()
        self.sim.role_define(self.sim.root, "member", scope_set=["link:publish"])
        self.gen = self.sim.genesis_id
        self.dom = organization_content_domain_id(self.gen)
        self.principals: dict = {}
        self.claims: dict = {}
        self.snapshots: dict = {}
        self.feed: list = []  # every content-stream item ever offered
        for i in range(member_count):
            self.admit(seed_index=i)
        self.stores = ContentStores(
            lambda heads: self.fold(heads),
            self.ancestry,
            self._credentials_by_kem_id,
        )

    # -- principals -------------------------------------------------------------

    @property
    def _credentials_by_kem_id(self) -> dict:
        return {
            p["credential"].kem_key_id: p["credential"] for p in self.principals.values()
        }

    def admit(self, seed_index: int = 99) -> KeyPair:
        persona, invite_key = KeyPair.generate(), KeyPair.generate()
        iid = self.sim.invite(self.sim.root, "member", invite_key=invite_key)
        cid = self.sim.claim(iid, invite_key, persona)
        credential, kem_private = credentials.build(
            persona, self.gen, bytes(range(seed_index, seed_index + 32)), [self.gen], HLC0
        )
        self.principals[persona.public_hex] = {
            "keypair": persona,
            "credential": credential,
            "kem_private": kem_private,
            "held": {},
        }
        self.claims[persona.public_hex] = cid
        return persona

    def member(self, index: int) -> KeyPair:
        return list(self.principals.values())[index]["keypair"]

    def held(self, persona: KeyPair) -> dict:
        return self.principals[persona.public_hex]["held"]

    def remove(self, persona: KeyPair, parents=None) -> str:
        """Revoke the claim; snapshot the retained pre-removal state first."""
        self.snapshots[persona.public_hex] = dict(self.held(persona))
        return self.sim.revoke_event(
            self.sim.root, self.claims[persona.public_hex], parents=parents
        )

    # -- seams -------------------------------------------------------------------

    def fold(self, heads=None):
        return self.sim.fold(heads=list(heads) if heads is not None else None)

    def ancestry(self, ids) -> frozenset:
        return self.sim.ledger.ancestry(ids)

    def frontier(self) -> list:
        return sorted(self.sim.ledger.heads())

    # -- content-stream helpers (route through the real modules) -------------------

    def offer(self, item) -> bool:
        self.feed.append(item)
        return self.stores.admit(item)

    def _register(self, creator, descriptor, secret, bridges):
        self.held(creator)[descriptor.state_id] = secret
        self.offer(("state", (descriptor, tuple(bridges))))
        self.offer(
            (
                "receipt",
                capability.issue_receipt(  # creator self-receipt, same act
                    creator, genesis_id=self.gen, domain_id=self.dom,
                    storage_state_id=descriptor.state_id, state_secret=secret,
                ),
            )
        )
        return descriptor, secret

    def mint_initial_state(self, creator: KeyPair, heads=None):
        f = self.fold(heads)
        descriptor, secret = state_mod.generate(
            creator, self.gen, self.dom, (), sorted(f.heads),
            sorted(f.loss_heads), acceptance.loss_projection_digest(f),
        )
        return self._register(creator, descriptor, secret, ())

    def advance(self, creator: KeyPair, parent, heads=None, *, offer_bridges=True):
        f = self.fold(heads)
        descriptor, secret, bridges = lifecycle.advance_state(
            creator, self.dom, self.gen, list(f.loss_heads),
            acceptance.loss_projection_digest(f), [parent],
            {parent.state_id: self.held(creator)[parent.state_id]},
            sorted(f.heads),
        )
        return self._register(
            creator, descriptor, secret, bridges if offer_bridges else ()
        )

    def union(self, creator: KeyPair, parents, heads=None):
        f = self.fold(heads)
        descriptor, secret, bridges = lifecycle.union_state(
            creator, self.dom, self.gen, list(parents),
            {p.state_id: self.held(creator)[p.state_id] for p in parents},
            list(f.loss_heads), acceptance.loss_projection_digest(f), sorted(f.heads),
        )
        return self._register(creator, descriptor, secret, bridges)

    def grant(self, grantor: KeyPair, recipient: KeyPair, descriptor, *, receipt=True):
        """Head grant, offered + accepted + decapsulated + receipted."""
        record = distribution.grant_current_head(
            grantor, self.dom, self.principals[recipient.public_hex]["credential"],
            descriptor, self.held(grantor)[descriptor.state_id], self.frontier(),
        )
        self.offer(("grant", record))
        entry = self.principals[recipient.public_hex]
        secret = capability.accept(record, entry["kem_private"], descriptor)
        entry["held"][descriptor.state_id] = secret
        if receipt:
            self.offer(
                (
                    "receipt",
                    capability.issue_receipt(
                        recipient, genesis_id=self.gen, domain_id=self.dom,
                        storage_state_id=descriptor.state_id, state_secret=secret,
                    ),
                )
            )
        return secret

    def create_object(self, author: KeyPair, plaintext: bytes, **kw):
        import os

        f = self.fold()
        header, body = objects.create_object(
            author, self.dom, plaintext, f, self.held(author),
            dict(self.stores.kc.states), ancestry=self.ancestry,
            bridges=list(self.stores.kc.bridges),
            descriptors=dict(self.stores.kc.states),
            object_id=kw.pop("object_id", None) or os.urandom(32).hex(),
            revision_id=kw.pop("revision_id", None) or os.urandom(32).hex(),
            body_suite_id=kw.pop("body_suite_id", "aes-256-gcm-siv"),
        )
        self.offer(("object", (header, body)))
        return header, body

    def read(self, held: dict, header, stores=None):
        """The plaintext, or the raised fail-closed error CLASS."""
        stores = stores or self.stores
        _, body = stores.object_store[header.ciphertext_hash]
        try:
            return objects.read_object(
                header, body, held, list(stores.kc.bridges), dict(stores.kc.states)
            )
        except StorageError as exc:
            return type(exc)

    def commit_status(self, state_id, *, state_secrets=None, receipts=None):
        return distribution.commit_status(
            state_id,
            receipts if receipts is not None else self.stores.receipt_store.receipts,
            dict(self.stores.kc.states),
            self.fold(),
            state_secrets=state_secrets,
        )

    def full_feed(self) -> list:
        return [("auth", e) for e in self.sim.ledger.events()] + list(self.feed)


def drive(feed, order_seed: int, credentials_by_kem_id):
    """Apply *feed* in a shuffled order into a fresh replica — real
    ledger, real acceptance — deferring not-yet-satisfiable items.
    Returns ``(ledger, stores, undelivered)``."""
    rng = random.Random(order_seed)
    pending = list(feed)
    rng.shuffle(pending)
    ledger = Ledger()
    stores = ContentStores(
        lambda heads: ledger_fold(ledger, heads=list(heads)),
        ledger.ancestry,
        credentials_by_kem_id,
    )
    while pending:
        progressed = False
        deferred = []
        for item in pending:
            kind, payload = item
            if kind == "auth":
                try:
                    ledger.add(payload)
                    progressed = True
                except LedgerError:
                    deferred.append(item)
                continue
            try:
                done = stores.admit(item)
            except LedgerError:
                done = False  # cited frontier not yet delivered
            if done:
                progressed = True
            else:
                deferred.append(item)
        if not progressed:
            break
        pending = deferred
    return ledger, stores, pending


@pytest.fixture
def world() -> World:
    return World()
