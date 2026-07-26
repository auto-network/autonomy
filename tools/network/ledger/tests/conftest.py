"""Shared harness: a Sim builder over a live ledger, plus a seeded random
DAG generator for the L1 determinism property tests.

All timestamps are fixed integers derived from a monotone per-Sim counter,
so every test is deterministic. Concurrency is constructed by passing
explicit ``parents`` instead of the current heads.
"""

from __future__ import annotations

import hashlib
import random

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger import (
    HLC,
    Event,
    Ledger,
    fold,
    make_event,
    sign_approval,
    sign_rotate_continuity,
)

ORG = "33333333-3333-4333-8333-333333333333"
T0 = 1_800_000_000_000  # unix ms
FAR = T0 + 10**9


def key(k) -> str:
    return k.public_hex if isinstance(k, KeyPair) else k


class Sim:
    """A ledger under construction, with an org root and event helpers.

    Every helper returns the new event id. ``parents=None`` means "the
    current heads"; pass explicit parents to build concurrent branches.
    """

    def __init__(self, org: str = ORG):
        self.root = KeyPair.generate()
        self.ledger = Ledger()
        self._ts = T0
        self.genesis_id = self.emit(
            self.root,
            {"type": "genesis", "org": org, "root_pub": self.root.public_hex},
            parents=[],
        )

    def next_ts(self) -> int:
        self._ts += 1_000
        return self._ts

    def emit(self, author: KeyPair, payload: dict, parents=None, ts=None) -> str:
        if parents is None:
            parents = self.ledger.heads()
        hlc = HLC(ts if ts is not None else self.next_ts())
        return self.ledger.add(make_event(author, payload, parents, hlc))

    def fold(self, heads=None, now=None):
        return fold(self.ledger, heads=heads, now=now)

    # -- event helpers -----------------------------------------------------------

    def delegate(self, author, child, scope, redelegate=False, parents=None, ttl=None, ts=None):
        payload = {
            "type": "delegate",
            "child_pub": key(child),
            "scope": sorted(set(scope)),
            "can_redelegate": redelegate,
        }
        if ttl is not None:
            payload["ttl"] = ttl
        return self.emit(author, payload, parents=parents, ts=ts)

    def revoke_event(self, author, target_id, parents=None, reason=None):
        payload = {"type": "revoke", "target_event": target_id}
        if reason is not None:
            payload["reason"] = reason
        return self.emit(author, payload, parents=parents)

    def revoke_key(self, author, target, parents=None):
        return self.emit(
            author, {"type": "revoke", "target_key": key(target)}, parents=parents
        )

    def role_define(
        self, author, name, scope_set=(), requires="self", version=1, parents=None,
        approver_threshold=None,
    ):
        payload = {
            "type": "role.define",
            "name": name,
            "scope_set": sorted(set(scope_set)),
            "claim_requires": requires,
            "version": version,
        }
        if approver_threshold is not None:
            payload["approver_threshold"] = {
                "kind": "static", "count": approver_threshold,
            }
        return self.emit(author, payload, parents=parents)

    def role_grant(self, author, persona, role, parents=None):
        return self.emit(
            author,
            {"type": "role.grant", "persona": key(persona), "role": role},
            parents=parents,
        )

    def role_revoke(self, author, persona, role, parents=None):
        return self.emit(
            author,
            {"type": "role.revoke", "persona": key(persona), "role": role},
            parents=parents,
        )

    def invite(self, author, role, invite_key=None, token_hash=None, expiry=FAR, parents=None):
        payload = {
            "type": "invite",
            "granted_role": role,
            "expiry": expiry,
            "sponsor": author.public_hex,
        }
        if token_hash is not None:
            payload["token_hash"] = token_hash
        else:
            payload["invite_pub"] = key(invite_key)
        return self.emit(author, payload, parents=parents)

    def claim(
        self,
        invite_id,
        signer,
        persona,
        approvers=(),
        token=None,
        profile=None,
        parents=None,
        ts=None,
    ):
        payload = {
            "type": "member.claim",
            "invite_ref": invite_id,
            "persona_pub": key(persona),
            "profile": profile if profile is not None else {},
            "approvals": [],
        }
        if token is not None:
            payload["token"] = token
        approvals = [sign_approval(a, "member.claim", payload) for a in approvers]
        payload["approvals"] = sorted(approvals, key=lambda e: e["key"])
        return self.emit(signer, payload, parents=parents, ts=ts)

    def rekey(self, signer, persona, old, new, approvers=(), parents=None):
        payload = {
            "type": "member.rekey",
            "persona": key(persona),
            "old_pub": key(old),
            "new_pub": key(new),
            "approvals": [],
        }
        approvals = [sign_approval(a, "member.rekey", payload) for a in approvers]
        payload["approvals"] = sorted(approvals, key=lambda e: e["key"])
        return self.emit(signer, payload, parents=parents)

    def rotate(self, signer, new_key, continuity=None, parents=None):
        payload = {
            "type": "key.rotate",
            "old_pub": signer.public_hex,
            "new_pub": new_key.public_hex,
            "continuity": continuity
            if continuity is not None
            else sign_rotate_continuity(new_key, signer.public_hex),
        }
        return self.emit(signer, payload, parents=parents)

    def checkpoint(self, author, state_hash=None, parents=None):
        return self.emit(
            author,
            {
                "type": "checkpoint",
                "state_hash": state_hash or "ab" * 32,
                "signers": [author.public_hex],
            },
            parents=parents,
        )


@pytest.fixture
def sim() -> Sim:
    return Sim()


# -- random DAG generation (L1 property tests) -------------------------------------

SCOPE_POOL = [
    "link:publish",
    "link:revoke",
    "invite:member",
    "invite:guest",
    "role:define",
    "role:grant:member",
    "role:grant:guest",
    "checkpoint",
    "tunnel:serve",
    "role:grant:*",
]
ROLE_POOL = ["member", "guest"]


def random_events(seed: int, n: int = 40) -> list:
    """A structurally valid random DAG of *n* events (plus genesis and two
    role definitions), authored by a small key pool with random parent
    choices — plenty of concurrency, plenty of semantically invalid events.
    The fold's judgement over the whole mess is what L1 pins down.
    """
    rng = random.Random(seed)
    root = KeyPair.generate()
    ledger = Ledger()
    events = []
    ts_of = {}

    def emit(author, payload, parents, ts):
        ev = make_event(author, payload, sorted(set(parents)), HLC(ts))
        ledger.add(ev)
        events.append(ev)
        ts_of[ev.event_id] = ts
        return ev.event_id

    gid = emit(root, {"type": "genesis", "org": ORG, "root_pub": root.public_hex}, [], T0)

    keys = [root] + [KeyPair.generate() for _ in range(6)]
    pending_invites = []  # (invite_id, invite_key, role)

    def pick_parents():
        ids = list(ts_of)
        count = min(len(ids), rng.choice([1, 1, 1, 2, 2, 3]))
        return rng.sample(ids, count)

    def next_ts(parents):
        return max(ts_of[p] for p in parents) + rng.randint(1, 5)

    for name in ROLE_POOL:
        emit(
            root,
            {
                "type": "role.define",
                "name": name,
                "scope_set": sorted(rng.sample(SCOPE_POOL[:4], 2)),
                "claim_requires": rng.choice(["self", "sponsor"]),
                "version": 1,
            },
            [gid],
            T0 + rng.randint(1, 5),
        )

    for _ in range(n):
        parents = pick_parents()
        ts = next_ts(parents)
        author = rng.choice(keys)
        op = rng.choices(
            ["delegate", "revoke_event", "revoke_key", "role.define", "role.grant",
             "role.revoke", "invite", "claim", "checkpoint"],
            weights=[30, 12, 8, 6, 10, 6, 12, 10, 6],
        )[0]

        if op == "delegate":
            if rng.random() < 0.3:
                keys.append(KeyPair.generate())
            child = rng.choice(keys)
            emit(
                author,
                {
                    "type": "delegate",
                    "child_pub": child.public_hex,
                    "scope": sorted(set(rng.sample(SCOPE_POOL, rng.randint(1, 4)))),
                    "can_redelegate": rng.random() < 0.5,
                },
                parents,
                ts,
            )
        elif op == "revoke_event":
            emit(
                author,
                {"type": "revoke", "target_event": rng.choice(list(ts_of))},
                parents,
                ts,
            )
        elif op == "revoke_key":
            emit(
                author,
                {"type": "revoke", "target_key": rng.choice(keys).public_hex},
                parents,
                ts,
            )
        elif op == "role.define":
            emit(
                author,
                {
                    "type": "role.define",
                    "name": rng.choice(ROLE_POOL),
                    "scope_set": sorted(set(rng.sample(SCOPE_POOL, rng.randint(0, 3)))),
                    "claim_requires": rng.choice(["self", "sponsor", "admin-ack"]),
                    "version": rng.randint(1, 3),
                },
                parents,
                ts,
            )
        elif op == "role.grant":
            emit(
                author,
                {
                    "type": "role.grant",
                    "persona": rng.choice(keys).public_hex,
                    "role": rng.choice(ROLE_POOL),
                },
                parents,
                ts,
            )
        elif op == "role.revoke":
            emit(
                author,
                {
                    "type": "role.revoke",
                    "persona": rng.choice(keys).public_hex,
                    "role": rng.choice(ROLE_POOL),
                },
                parents,
                ts,
            )
        elif op == "invite":
            invite_key = KeyPair.generate()
            role = rng.choice(ROLE_POOL)
            iid = emit(
                author,
                {
                    "type": "invite",
                    "invite_pub": invite_key.public_hex,
                    "granted_role": role,
                    "expiry": FAR,
                    "sponsor": author.public_hex,
                },
                parents,
                ts,
            )
            pending_invites.append((iid, invite_key, role))
        elif op == "claim":
            if not pending_invites:
                continue
            iid, ikey, _role = rng.choice(pending_invites)
            persona = KeyPair.generate()
            if rng.random() < 0.7 and iid in ts_of:
                parents = sorted(set(parents) | {iid})
                ts = next_ts(parents)
            emit(
                ikey,
                {
                    "type": "member.claim",
                    "invite_ref": iid,
                    "persona_pub": persona.public_hex,
                    "profile": {},
                    "approvals": [],
                },
                parents,
                ts,
            )
        elif op == "checkpoint":
            emit(
                author,
                {
                    "type": "checkpoint",
                    "state_hash": hashlib.sha256(str(rng.random()).encode()).hexdigest(),
                    "signers": [author.public_hex],
                },
                parents,
                ts,
            )

    return events


def shuffled_ledger(events, rng) -> Ledger:
    batch = list(events)
    rng.shuffle(batch)
    ledger = Ledger()
    ledger.ingest(batch)
    return ledger
