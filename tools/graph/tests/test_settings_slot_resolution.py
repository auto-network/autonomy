"""Every signer a slot; every store the same winner (auto-y2ubq).

Acceptance for the slot storage model and the six-step resolution ordering
(design of record graph://21a0da9e-1c2, drivers D3/D4/D5). The battery is
built around the design's ABSENCE claims — no per-machine term inside one
organization, no store-local resolution input — which cannot be proven by
one green path: each is exercised as a difference test (permute delivery,
mutate the store-local value, flip the fetch order) whose assertion is that
NOTHING changes. What would make these stale: a new resolution input added
to `_rank_candidates` without a permutation/mutation case here covering it.

Stores are simulated as independently-built org databases under separate
orgs roots — the same organization, its ledger appended identically, its
settings rows delivered in different orders.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
import uuid

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.network.idkit import KeyPair, derive_persona
from tools.network.ledger import HLC, Ledger, LedgerStore, make_event

SET_ID = "autonomy.org.member-directory"
KEY = "shared-value"
ORG_UUID = "44444444-4444-4444-8444-444444444444"
T0_MS = 1_800_000_000_000
NOW_MS = T0_MS + 10_000_000
MIN_MS = 60_000

ROOT_SEED = bytes(range(32))
MEMBER_SEEDS = [bytes(range(i, i + 32)) for i in (1, 2, 3)]


class OrgEvents:
    """The one organization every simulated store holds a copy of."""

    def __init__(self, n_members=2):
        self.root = KeyPair.from_private_hex(ROOT_SEED.hex())
        self.events = []
        self._ledger = Ledger()
        self._ts = T0_MS
        self.genesis_id = self._emit(
            self.root,
            {"type": "genesis", "org": ORG_UUID, "root_pub": self.root.public_hex},
            parents=[],
        )
        self.personas = [
            derive_persona(seed, self.genesis_id) for seed in MEMBER_SEEDS[:n_members]
        ]
        self._emit(self.root, {
            "type": "role.define", "name": "member", "scope_set": [],
            "claim_requires": "self", "version": 1,
        })
        self.claim_ids = []
        for persona in self.personas:
            invite_id = self._emit(self.root, {
                "type": "invite", "granted_role": "member",
                "expiry": T0_MS + 10**9, "sponsor": self.root.public_hex,
                "invite_pub": persona.public_hex,
            })
            self.claim_ids.append(self._emit(persona, {
                "type": "member.claim", "invite_ref": invite_id,
                "persona_pub": persona.public_hex, "profile": {},
                "approvals": [],
            }))

    def _emit(self, author, payload, parents=None):
        if parents is None:
            parents = self._ledger.heads()
        self._ts += 1_000
        event = make_event(author, payload, parents, HLC(self._ts))
        self._ledger.add(event)
        self.events.append(event)
        return event.event_id

    def revoke_claim(self, index):
        """A member's removal: their claim revoked by the org root."""
        return self._emit(self.root, {
            "type": "revoke", "target_event": self.claim_ids[index],
        })


@pytest.fixture(scope="module")
def org():
    return OrgEvents()


@pytest.fixture
def orgs_env(tmp_path, monkeypatch):
    """use(name) -> a fresh orgs root selected as THE machine's data dir."""
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)

    def use(name):
        root = tmp_path / name
        root.mkdir(exist_ok=True)
        monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
        GraphDB.close_all_pooled()
        return root

    yield use
    GraphDB.close_all_pooled()


def make_store(root, org, slug="signedorg", events=None):
    """One machine's copy of the organization: org DB + its ledger."""
    path = root / f"{slug}.db"
    GraphDB.create_org_db(slug, path=path).close()
    store = LedgerStore(path)
    try:
        for event in events if events is not None else org.events:
            store.append(event)
    finally:
        store.close()
    return path


def slot_row(persona, signed_at, *, key=KEY, state="published", rev=1,
             payload=None, created_at="2026-08-19T02:00:00Z"):
    return {
        "id": str(uuid.uuid4()),
        "key": key,
        "state": state,
        "rev": rev,
        "payload": json.dumps(payload or {"who": persona.public_hex[:8]}),
        "persona": persona.public_hex,
        "signed_at": signed_at,
        "created_at": created_at,
    }


def deliver(path, row):
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO settings (id, set_id, schema_revision, key,"
                " payload, publication_state, created_at, signed_at,"
                " signing_key, signature, witness, terminal_persona)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (row["id"], SET_ID, row["rev"], row["key"], row["payload"],
                 row["state"], row["created_at"], row["signed_at"],
                 row["persona"], "ab" * 64, row["persona"]),
            )
    finally:
        conn.close()


def resolve(slug="signedorg", now=NOW_MS, **kwargs):
    members = settings_ops.read_set(SET_ID, org=slug, now=now, **kwargs)
    return {m.key: m for m in members.members}


# ── storage: one slot per signer ─────────────────────────────

def test_two_members_rows_coexist_and_a_second_row_per_signer_cannot(
    org, orgs_env,
):
    root = orgs_env("store-a")
    path = make_store(root, org)
    a, b = org.personas
    deliver(path, slot_row(a, NOW_MS - MIN_MS))
    deliver(path, slot_row(b, NOW_MS - 2 * MIN_MS))

    conn = sqlite3.connect(path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM settings WHERE set_id = ?", (SET_ID,)
        ).fetchone()[0] == 2
    finally:
        conn.close()

    # The same signer's second live row at the same address is refused by
    # the slot index — a write replaces the signer's own slot, it does not
    # accumulate. Fifty writes leave one row because each is an in-place
    # replacement of this row, which the storage shape makes the only shape
    # a write can take.
    with pytest.raises(sqlite3.IntegrityError):
        deliver(path, slot_row(a, NOW_MS))


def test_an_unsigned_base_still_takes_the_one_base_rule(org, orgs_env):
    root = orgs_env("store-a")
    path = make_store(root, org)
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO settings (id, set_id, schema_revision, key, payload)"
                " VALUES ('u1', ?, 1, ?, '{}')", (SET_ID, KEY),
            )
        with pytest.raises(sqlite3.IntegrityError), conn:
            conn.execute(
                "INSERT INTO settings (id, set_id, schema_revision, key, payload)"
                " VALUES ('u2', ?, 1, ?, '{}')", (SET_ID, KEY),
            )
    finally:
        conn.close()


# ── convergence: same statements, any order, same winner ─────

def test_every_delivery_permutation_resolves_the_same_winner(org, orgs_env):
    a, b = org.personas
    rows = [
        slot_row(a, NOW_MS - MIN_MS),
        slot_row(b, NOW_MS - 3 * MIN_MS),
        slot_row(b, NOW_MS - 2 * MIN_MS, state="raw"),
    ]
    winners, retained = set(), set()
    for i, perm in enumerate(itertools.permutations(rows)):
        root = orgs_env(f"perm-{i}")
        path = make_store(root, org)
        for row in perm:
            # created_at differs per delivery order, as it does in life.
            deliver(path, dict(row, id=str(uuid.uuid4()),
                               created_at=f"2026-08-19T02:0{perm.index(row)}:00Z"))
        resolved = resolve()
        winners.add(json.dumps(resolved[KEY].payload, sort_keys=True))
        conn = sqlite3.connect(path)
        try:
            retained.add(conn.execute(
                "SELECT COUNT(*) FROM settings WHERE set_id = ?", (SET_ID,)
            ).fetchone()[0])
        finally:
            conn.close()
    assert len(winners) == 1, "delivery order reached the answer"
    assert retained == {3}, "losing slots are retained, not displaced"
    assert json.loads(winners.pop())["who"] == a.public_hex[:8]


def test_exact_signed_at_tie_is_broken_by_the_hash_in_both_orders(
    org, orgs_env,
):
    """The one case fetch order could decide — so it is the one case that
    proves step 6 exists. An implementation that stops at signed_at and
    takes SQLite fetch order passes every other test in this file."""
    a, b = org.personas
    tie = NOW_MS - MIN_MS
    expected = min(org.personas, key=lambda p: hashlib.sha256(
        "\x00".join((SET_ID, KEY, p.public_hex)).encode()
    ).hexdigest())
    for order_name, order in (("ab", (a, b)), ("ba", (b, a))):
        root = orgs_env(f"tie-{order_name}")
        path = make_store(root, org)
        for persona in order:
            deliver(path, slot_row(persona, tie))
        resolved = resolve()
        assert resolved[KEY].payload["who"] == expected.public_hex[:8], (
            f"ingest order {order_name} changed the tie-break"
        )


def test_created_at_is_not_a_resolution_input(org, orgs_env):
    """The store-local claim, tested as a mutation: created_at records when
    THIS store received the row, so changing it must change nothing."""
    a, b = org.personas
    root = orgs_env("created-at")
    path = make_store(root, org)
    deliver(path, slot_row(a, NOW_MS - MIN_MS))
    deliver(path, slot_row(b, NOW_MS - 2 * MIN_MS))
    before = resolve()[KEY].payload
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute(
                "UPDATE settings SET created_at = '1999-01-01T00:00:00Z' "
                "WHERE terminal_persona = ?", (a.public_hex,),
            )
            conn.execute(
                "UPDATE settings SET created_at = '2099-01-01T00:00:00Z' "
                "WHERE terminal_persona = ?", (b.public_hex,),
            )
    finally:
        conn.close()
    GraphDB.close_all_pooled()
    assert resolve()[KEY].payload == before


# ── retraction and re-winning ────────────────────────────────

def test_retraction_lets_the_next_slot_resolve_with_nothing_resent(
    org, orgs_env,
):
    a, b = org.personas
    root = orgs_env("retract")
    path = make_store(root, org)
    deliver(path, slot_row(a, NOW_MS - MIN_MS))
    deliver(path, slot_row(b, NOW_MS - 2 * MIN_MS))
    assert resolve()[KEY].payload["who"] == a.public_hex[:8]
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute(
                "UPDATE settings SET deprecated = 1 WHERE terminal_persona = ?",
                (a.public_hex,),
            )
    finally:
        conn.close()
    GraphDB.close_all_pooled()
    assert resolve()[KEY].payload["who"] == b.public_hex[:8]


def test_a_loser_wins_only_by_exceeding_the_current_leader(org, orgs_env):
    """Newer than their own last write is a valid write that still loses;
    the guarantee is that a winning write always EXISTS, not that any write
    wins. Asserting the losing case is what stops the weaker rule."""
    a, b = org.personas
    root = orgs_env("rewin")
    path = make_store(root, org)
    deliver(path, slot_row(a, NOW_MS - 10 * MIN_MS))
    deliver(path, slot_row(b, NOW_MS - 2 * MIN_MS))
    assert resolve()[KEY].payload["who"] == b.public_hex[:8]

    def rewrite_a(signed_at):
        conn = sqlite3.connect(path)
        try:
            with conn:
                conn.execute(
                    "UPDATE settings SET signed_at = ? WHERE terminal_persona = ?",
                    (signed_at, a.public_hex),
                )
        finally:
            conn.close()
        GraphDB.close_all_pooled()

    rewrite_a(NOW_MS - 5 * MIN_MS)  # newer than their own; older than leader
    assert resolve()[KEY].payload["who"] == b.public_hex[:8]
    rewrite_a(NOW_MS - MIN_MS)  # later than the leader
    assert resolve()[KEY].payload["who"] == a.public_hex[:8]


# ── the plausibility window at resolution ────────────────────

def test_a_future_dated_row_is_stored_ignored_then_resolves(org, orgs_env):
    a, b = org.personas
    root = orgs_env("window")
    path = make_store(root, org)
    deliver(path, slot_row(a, NOW_MS - MIN_MS))
    future = NOW_MS + 31 * MIN_MS
    deliver(path, slot_row(b, future))

    resolved = resolve(now=NOW_MS)
    assert resolved[KEY].payload["who"] == a.public_hex[:8]

    # Stored, not refused: the row is physically present and untouched.
    conn = sqlite3.connect(path)
    try:
        held = conn.execute(
            "SELECT signed_at FROM settings WHERE terminal_persona = ?",
            (b.public_hex,),
        ).fetchone()
    finally:
        conn.close()
    assert held is not None and held[0] == future

    # The clock advancing is the only change: no re-ingest, no rewrite.
    assert resolve(now=future)[KEY].payload["who"] == b.public_hex[:8]


# ── eligibility: the roster is the ledger's answer ───────────

def test_a_departed_members_slot_stops_resolving(orgs_env):
    org = OrgEvents()
    a, b = org.personas
    root = orgs_env("departed")
    path = make_store(root, org)
    deliver(path, slot_row(a, NOW_MS - 2 * MIN_MS))
    deliver(path, slot_row(b, NOW_MS - MIN_MS))
    assert resolve()[KEY].payload["who"] == b.public_hex[:8]

    org.revoke_claim(1)  # b removed
    store = LedgerStore(path)
    try:
        store.append(org.events[-1])
    finally:
        store.close()
    GraphDB.close_all_pooled()

    resolved = settings_ops.read_set(SET_ID, org="signedorg", now=NOW_MS)
    by_key = {m.key: m for m in resolved.members}
    assert by_key[KEY].payload["who"] == a.public_hex[:8]
    assert resolved.dropped.ineligible_signer == 1
    # Nothing swept: the row is retained and re-admission needs no rule.
    conn = sqlite3.connect(path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM settings WHERE terminal_persona = ?",
            (b.public_hex,),
        ).fetchone()[0] == 1
    finally:
        conn.close()

    # A store that has NOT folded the revocation still resolves b —
    # governed by what it knows, which is the convergence property working.
    stale_root = orgs_env("departed-stale")
    stale_path = make_store(stale_root, org, events=org.events[:-1])
    deliver(stale_path, slot_row(a, NOW_MS - 2 * MIN_MS))
    deliver(stale_path, slot_row(b, NOW_MS - MIN_MS))
    assert resolve()[KEY].payload["who"] == b.public_hex[:8]


# ── contention is visible ────────────────────────────────────

def test_contested_keys_reports_the_winning_rung_and_store_only(
    org, orgs_env,
):
    a, b = org.personas
    root = orgs_env("contested")
    path = make_store(root, org)
    deliver(path, slot_row(a, NOW_MS - MIN_MS))
    deliver(path, slot_row(b, NOW_MS - 2 * MIN_MS))
    deliver(path, slot_row(b, NOW_MS, key="uncontested"))

    contested = settings_ops.contested_keys(SET_ID, org="signedorg", now=NOW_MS)
    assert [c["key"] for c in contested] == [KEY]
    slots = contested[0]["slots"]
    assert {s["terminal_persona"] for s in slots} == {
        a.public_hex, b.public_hex,
    }
    assert [s["resolves"] for s in slots].count(True) == 1
    winner = next(s for s in slots if s["resolves"])
    assert winner["terminal_persona"] == a.public_hex

    # Retraction ends the contention.
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute(
                "UPDATE settings SET deprecated = 1 "
                "WHERE terminal_persona = ? AND key = ?", (a.public_hex, KEY),
            )
    finally:
        conn.close()
    GraphDB.close_all_pooled()
    assert settings_ops.contested_keys(SET_ID, org="signedorg", now=NOW_MS) == []


# ── the fold is built once per ledger advancement ────────────

def test_the_fold_builds_once_per_ledger_advancement_not_once_per_call(
    orgs_env,
):
    org = OrgEvents()
    a, b = org.personas
    root = orgs_env("fold-cache")
    path = make_store(root, org)
    deliver(path, slot_row(a, NOW_MS - MIN_MS))
    deliver(path, slot_row(b, NOW_MS - 2 * MIN_MS))

    settings_ops._FOLD_VIEW_CACHE.clear()
    settings_ops._fold_builds = 0
    for _ in range(5):
        resolve()
    assert settings_ops._fold_builds == 1, (
        "five resolutions against an unchanged ledger must build one fold"
    )

    org.revoke_claim(1)
    store = LedgerStore(path)
    try:
        store.append(org.events[-1])
    finally:
        store.close()
    GraphDB.close_all_pooled()
    for _ in range(3):
        resolve()
    assert settings_ops._fold_builds == 2, (
        "a ledger advancement invalidates by heads inequality, exactly once"
    )


# ── the store ladder: most local wins ────────────────────────

def make_plain_store(root, slug):
    GraphDB.create_org_db(slug, path=root / f"{slug}.db").close()
    return root / f"{slug}.db"


def deliver_unsigned(path, *, state, payload, key=KEY, rev=1):
    conn = sqlite3.connect(path)
    try:
        with conn:
            conn.execute(
                "INSERT INTO settings (id, set_id, schema_revision, key,"
                " payload, publication_state) VALUES (?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), SET_ID, rev, key,
                 json.dumps(payload), state),
            )
    finally:
        conn.close()


def test_personal_outranks_the_organization_at_the_same_rung(org, orgs_env):
    """D4: the operator's own store answers for their own machine — a
    federated personal row beats the org's row at the same rung, and the
    org's store is untouched."""
    root = orgs_env("ladder")
    path = make_store(root, org)
    deliver(path, slot_row(org.personas[0], NOW_MS - MIN_MS,
                           payload={"from": "org"}))
    personal = make_plain_store(root, "personal")
    deliver_unsigned(personal, state="published", payload={"from": "personal"})

    assert resolve()[KEY].payload == {"from": "personal"}


def test_the_machine_store_outranks_personal(org, orgs_env):
    root = orgs_env("ladder-machine")
    make_store(root, org)
    personal = make_plain_store(root, "personal")
    machine = make_plain_store(root, "machine")
    deliver_unsigned(personal, state="published", payload={"from": "personal"})
    deliver_unsigned(machine, state="published", payload={"from": "machine"})

    assert resolve()[KEY].payload == {"from": "machine"}


def test_a_personal_read_takes_the_machine_store_as_its_one_peer(
    org, orgs_env,
):
    """v42: no organization is a peer of a personal read — the sovereignty
    ladder read one rung down, not an aggregation point for org content."""
    root = orgs_env("personal-read")
    path = make_store(root, org)
    deliver(path, slot_row(org.personas[0], NOW_MS - MIN_MS,
                           payload={"from": "org"}))
    personal = make_plain_store(root, "personal")
    machine = make_plain_store(root, "machine")
    deliver_unsigned(personal, state="raw", payload={"from": "personal"})
    deliver_unsigned(machine, state="published", payload={"from": "machine"})

    members = settings_ops.read_set(SET_ID, org=None, now=NOW_MS)
    by_key = {m.key: m for m in members.members}
    # The machine's federated row outranks the personal raw one; the org's
    # row participates in no personal read at all.
    assert by_key[KEY].payload == {"from": "machine"}

    conn = sqlite3.connect(machine)
    try:
        with conn:
            conn.execute("DELETE FROM settings")
    finally:
        conn.close()
    GraphDB.close_all_pooled()
    members = settings_ops.read_set(SET_ID, org=None, now=NOW_MS)
    by_key = {m.key: m for m in members.members}
    assert by_key[KEY].payload == {"from": "personal"}, (
        "with the machine store empty, a personal read answers from "
        "personal — never from an organization"
    )
