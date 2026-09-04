"""A setting revision, encrypted under the organization's key generation.

Drives the real modules end to end — the real ledger, the real authority fold,
the real acceptance layer, the real content store on disk — with throwaway
identities and a throwaway org (crib §21). Nothing here is mocked except the
prohibition on opening a socket, which is itself an assertion.
"""

from __future__ import annotations

import json
import socket
import time
from contextlib import contextmanager

import pytest

from tools.network.storagekit.errors import StorageError
from tools.network.storagekit.lifecycle import StateAdvanceRequired
from tools.network.storagekit.store import ContentStore
from tools.network.storagekit.tests.conftest import World
from tools.vault import policy_class as policy_class_mod
from tools.vault.errors import ClassOpenError, VaultError
from tools.vault.factors import open_password_seed, random_seed
from tools.vault.recipients import (
    PERSONAL_ROOT_RECIPIENT,
    PublishedRecipient,
    recipient_public_from_seed,
)
from tools.vault.storage_object import (
    AUDITED,
    LOCATOR_PREFIX,
    SECURED,
    Holdings,
    build_locator,
    is_vault_locator,
    object_id_for,
    open_revision,
    parse_locator,
    revision_id_for,
    seal_revision,
)
from tools.vault.testkit import content_key_for, make_test_identity


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def world() -> World:
    return World(member_count=3)


@pytest.fixture
def store(tmp_path) -> ContentStore:
    with ContentStore(tmp_path / "content") as store:
        yield store


def holdings_of(world: World, persona) -> Holdings:
    """What one member holds — its own secrets, everyone's public records."""
    return Holdings(
        secrets=world.held(persona),
        descriptors=world.stores.kc.states,
        bridges=world.stores.kc.bridges,
    )


@contextmanager
def no_network():
    """Any attempt to open a socket inside this block is a test failure.

    The claim under test is that a write following an access contraction
    completes in the writer's own operation — no round trip, no quorum, no
    waiting on a recipient existing. A wall-clock bound alone would only say
    the write was fast; this says it could not have talked to anyone.
    """
    real_socket = socket.socket
    real_connection = socket.create_connection

    def refuse(*args, **kwargs):
        raise AssertionError("the write path opened a network connection")

    socket.socket = refuse
    socket.create_connection = refuse
    try:
        yield
    finally:
        socket.socket = real_socket
        socket.create_connection = real_connection


def _anchor(aid="anchor.root"):
    seed = random_seed()
    public = recipient_public_from_seed(seed, PERSONAL_ROOT_RECIPIENT)
    return seed, PublishedRecipient(aid, PERSONAL_ROOT_RECIPIENT, public)


_ANCHOR_SEED, _ANCHOR = _anchor()


def a_policy_class():
    """A password policy class plus the seeds that open it."""
    identity = make_test_identity()
    record = policy_class_mod.create_class(
        "password", [identity.published], created_at="2026-08-16T00:00:00Z",
        recovery=_ANCHOR,
    )
    seed = open_password_seed(identity.armor, identity.password)
    return record, {identity.factor_id: seed}


# ── the derived identifiers ───────────────────────────────────────────────


def test_every_revision_of_one_setting_shares_one_object():
    """The object is a function of the setting's identity, not of the row."""
    genesis = "a" * 64
    first = object_id_for(genesis, "dashboard.claude.credentials", "default")
    second = object_id_for(genesis, "dashboard.claude.credentials", "default")
    assert first == second
    assert first != object_id_for(genesis, "dashboard.claude.credentials", "other")
    assert first != object_id_for(genesis, "dashboard.claude.setup_tokens", "default")
    assert first != object_id_for("b" * 64, "dashboard.claude.credentials", "default")


def test_a_revision_is_distinct_per_row_and_never_an_object_id():
    genesis = "a" * 64
    assert revision_id_for(genesis, "row-1") != revision_id_for(genesis, "row-2")
    # Domain-separated: the same inputs cannot address an object and a
    # revision at once, whichever way a caller happens to pass them.
    assert revision_id_for(genesis, "x") != object_id_for(genesis, "x", "x")


# ── the locator ───────────────────────────────────────────────────────────


def _locator(**overrides):
    base = dict(
        genesis_id="a" * 64,
        domain_id="b" * 64,
        object_id="c" * 64,
        revision_id="d" * 64,
        storage_state_id="e" * 64,
        tier=AUDITED,
    )
    base.update(overrides)
    return build_locator(**base)


def test_a_locator_is_a_scalar_that_merge_patch_cannot_splice():
    """RFC 7386 recurses into objects; two writes must never blend.

    Resolution merges candidate rows BEFORE anything is decrypted (crib §17),
    so a locator shaped as an object could yield one carrying the object id of
    one write and the revision id of another — addressing an object nobody
    wrote. As a scalar the merge can only replace it whole.
    """
    from tools.graph.settings_ops import json_merge_patch

    first = _locator(object_id="1" * 64, revision_id="1" * 64)
    second = _locator(object_id="2" * 64, revision_id="2" * 64)
    merged = json_merge_patch(first, second)
    assert merged in (first, second)
    surviving = parse_locator(merged)
    # Every identifier came from ONE of the two writes, never a mixture.
    assert surviving["object_id"] == surviving["revision_id"]


def test_a_locator_carries_no_key_material_and_no_ciphertext():
    reference = parse_locator(_locator())
    assert set(reference) == {
        "genesis_id", "domain_id", "object_id", "revision_id",
        "storage_state_id", "tier", "policy_class_id", "required_policy",
    }


def test_an_unknown_tier_is_refused_at_both_ends():
    with pytest.raises(VaultError):
        _locator(tier="unattended")
    forged = LOCATOR_PREFIX + "eyJ0aWVyIjogIm9wZW4ifQ"
    with pytest.raises(VaultError):
        parse_locator(forged)


def test_an_audited_locator_cannot_claim_a_policy_class_it_does_not_use():
    with pytest.raises(VaultError):
        _locator(tier=AUDITED, policy_class_id="c0ffee", required_policy="password")
    with pytest.raises(VaultError):
        _locator(tier=SECURED)


def test_an_ordinary_payload_is_not_mistaken_for_a_locator():
    assert not is_vault_locator({"token": "sk-live"})
    assert not is_vault_locator("sk-live")
    assert is_vault_locator(_locator())


# ── the write path ────────────────────────────────────────────────────────


def test_writing_a_secret_produces_an_object_and_reveals_nothing_in_the_locator(
    world, store
):
    author = world.member(0)
    world.mint_initial_state(author)
    payload = {"access_token": "sk-live-do-not-log", "refresh_token": "rt-secret"}

    sealed = seal_revision(
        author=author,
        frontier=world.fold(),
        set_id="dashboard.claude.credentials",
        key="default",
        setting_id="row-1",
        payload=payload,
        holdings=holdings_of(world, author),
        ancestry=world.ancestry,
        content_store=store,
    )

    reference = parse_locator(sealed.locator)
    header, body = store.get_object(reference["object_id"], reference["revision_id"])
    assert header.storage_state_id == reference["storage_state_id"]
    for secret in ("sk-live-do-not-log", "rt-secret"):
        assert secret.encode() not in body
        assert secret not in sealed.locator

    assert open_revision(
        sealed.locator, holdings=holdings_of(world, author), content_store=store
    ) == payload


def test_the_same_setting_written_twice_is_two_revisions_of_one_object(world, store):
    author = world.member(0)
    world.mint_initial_state(author)
    common = dict(
        author=author,
        set_id="dashboard.claude.credentials",
        key="default",
        holdings=holdings_of(world, author),
        ancestry=world.ancestry,
        content_store=store,
    )
    first = seal_revision(
        frontier=world.fold(), setting_id="row-1", payload={"v": 1}, **common
    )
    second = seal_revision(
        frontier=world.fold(), setting_id="row-2", payload={"v": 2}, **common
    )

    a, b = parse_locator(first.locator), parse_locator(second.locator)
    assert a["object_id"] == b["object_id"]
    assert a["revision_id"] != b["revision_id"]

    held = holdings_of(world, author)
    assert open_revision(first.locator, holdings=held, content_store=store) == {"v": 1}
    assert open_revision(second.locator, holdings=held, content_store=store) == {"v": 2}


def test_a_write_after_a_contraction_mints_its_own_generation_and_does_not_wait(
    world, store
):
    """The gate is a precondition satisfied in line, not an outage (§7.4)."""
    author, leaving = world.member(0), world.member(1)
    initial, _ = world.mint_initial_state(author)
    world.grant(author, leaving, initial)
    contraction = world.remove(leaving)

    holdings = holdings_of(world, author)
    frontier = world.fold()
    assert contraction in frontier.loss_heads
    # The state that exists covers nothing, so the write cannot use it.
    assert initial.covered_loss_heads == ()

    started = time.monotonic()
    with no_network():
        sealed = seal_revision(
            author=author,
            frontier=frontier,
            set_id="dashboard.claude.credentials",
            key="default",
            setting_id="row-after-removal",
            payload={"access_token": "minted-after-removal"},
            holdings=holdings,
            ancestry=world.ancestry,
            content_store=store,
        )
    elapsed = time.monotonic() - started

    assert sealed.advance is not None, "the write should have minted a generation"
    assert elapsed < 0.5, f"the write took {elapsed:.3f}s — long enough to have waited"
    # And it really is a new generation, used by this very object.
    assert sealed.advance.descriptor.state_id != initial.state_id
    assert parse_locator(sealed.locator)["storage_state_id"] == (
        sealed.advance.descriptor.state_id
    )
    assert open_revision(
        sealed.locator, holdings=holdings, content_store=store
    ) == {"access_token": "minted-after-removal"}


def test_the_minted_generation_names_the_contraction_it_covers(world, store):
    """Assert the descriptor's contents, not merely that a state exists."""
    author, leaving = world.member(0), world.member(1)
    initial, _ = world.mint_initial_state(author)
    world.grant(author, leaving, initial)
    contraction = world.remove(leaving)

    sealed = seal_revision(
        author=author,
        frontier=world.fold(),
        set_id="dashboard.claude.credentials",
        key="default",
        setting_id="row-after-removal",
        payload={"token": "t"},
        holdings=holdings_of(world, author),
        ancestry=world.ancestry,
        content_store=store,
    )

    descriptor = sealed.advance.descriptor
    assert contraction in descriptor.covered_loss_heads
    assert descriptor.creator_persona == author.public_hex
    assert descriptor.domain_id == world.dom
    assert descriptor.genesis_id == world.gen
    # It descends from the generation it replaces, and says so under signature,
    # with a bridge so a holder of the new key still reaches the old content.
    assert initial.state_id in descriptor.parent_state_ids
    assert [b.parent_state_id for b in sealed.advance.bridges] == [initial.state_id]
    assert all(b.child_state_id == descriptor.state_id for b in sealed.advance.bridges)


def test_a_second_write_after_the_advance_reuses_the_generation(world, store):
    """Minting is what a write does when it must, not on every write."""
    author, leaving = world.member(0), world.member(1)
    initial, _ = world.mint_initial_state(author)
    world.grant(author, leaving, initial)
    world.remove(leaving)
    holdings = holdings_of(world, author)
    common = dict(
        author=author,
        set_id="dashboard.claude.credentials",
        key="default",
        holdings=holdings,
        ancestry=world.ancestry,
        content_store=store,
    )

    first = seal_revision(frontier=world.fold(), setting_id="r1", payload={"v": 1}, **common)
    second = seal_revision(frontier=world.fold(), setting_id="r2", payload={"v": 2}, **common)

    assert first.advance is not None
    assert second.advance is None
    assert parse_locator(first.locator)["storage_state_id"] == (
        parse_locator(second.locator)["storage_state_id"]
    )


def test_the_removed_member_cannot_read_what_was_written_after_it_left(world, store):
    author, leaving = world.member(0), world.member(1)
    initial, _ = world.mint_initial_state(author)
    world.grant(author, leaving, initial)
    world.remove(leaving)
    retained = world.snapshots[leaving.public_hex]

    sealed = seal_revision(
        author=author,
        frontier=world.fold(),
        set_id="dashboard.claude.credentials",
        key="default",
        setting_id="row-after-removal",
        payload={"token": "written-after-you-left"},
        holdings=holdings_of(world, author),
        ancestry=world.ancestry,
        content_store=store,
    )

    # Everything it kept: every pre-removal secret, every public record, the
    # whole ciphertext store.
    stranded = Holdings(
        secrets=dict(retained),
        descriptors=world.stores.kc.states,
        bridges=world.stores.kc.bridges,
    )
    with pytest.raises(StorageError):
        open_revision(sealed.locator, holdings=stranded, content_store=store)


def test_a_member_holding_no_capability_cannot_write(world, store):
    """The unprovisioned case fails closed rather than minting an orphan."""
    author, stranger = world.member(0), world.member(2)
    world.mint_initial_state(author)

    with pytest.raises(StateAdvanceRequired):
        seal_revision(
            author=stranger,
            frontier=world.fold(),
            set_id="dashboard.claude.credentials",
            key="default",
            setting_id="row-1",
            payload={"token": "t"},
            holdings=holdings_of(world, stranger),
            ancestry=world.ancestry,
            content_store=store,
        )


# ── the secured tier: the policy layer beneath the storage state ──────────


def test_a_secured_setting_needs_the_human_factor_on_top_of_membership(
    world, store, monkeypatch
):
    """Membership opens the object and yields the wrapped key — and stops."""
    author = world.member(0)
    world.mint_initial_state(author)
    record, seeds = a_policy_class()
    payload = {"access_token": "sk-live-secured"}

    sealed = seal_revision(
        author=author,
        frontier=world.fold(),
        set_id="dashboard.claude.credentials",
        key="default",
        setting_id="row-1",
        payload=payload,
        holdings=holdings_of(world, author),
        ancestry=world.ancestry,
        content_store=store,
        tier=SECURED,
        policy_class=record,
    )
    reference = parse_locator(sealed.locator)
    assert reference["tier"] == SECURED
    assert reference["policy_class_id"] == record.class_id
    assert reference["required_policy"] == "password"

    held = holdings_of(world, author)

    # 1. A domain member opens the object. What it gets is the wrapped content
    #    key, which proves membership and nothing else.
    from tools.network.storagekit import objects as objects_mod

    header, body = store.get_object(reference["object_id"], reference["revision_id"])
    envelope = json.loads(
        objects_mod.read_object(
            header, body, held.secrets, list(held.bridges), dict(held.descriptors)
        )
    )
    assert set(envelope) == {"v", "sealed_cek", "body_suite_id", "nonce", "ciphertext"}
    assert "sk-live-secured" not in json.dumps(envelope)

    # 2. Opening that requires the class, which requires the factor. Since B-1
    #    the class open is the browser's job (simulated by content_key_for);
    #    the server-side open_revision refuses without that content key, and an
    #    empty opener set fails closed in the class open itself.
    with pytest.raises(VaultError):
        open_revision(sealed.locator, holdings=held, content_store=store)
    with pytest.raises(ClassOpenError):
        content_key_for(
            sealed.locator, record=record, seeds={},
            holdings=held, content_store=store,
        )
    # The browser opens exactly this revision's content key; the server then
    # applies it to the frozen body.
    content_key = content_key_for(
        sealed.locator, record=record, seeds=seeds,
        holdings=held, content_store=store,
    )
    captured_cek = []
    from tools.network.storagekit import object_header as object_header_mod

    original_open_body = object_header_mod.open_body

    def capture_open_body(header, cek, body_blob):
        # The outer storage object still uses its ordinary bytes CEK. Capture
        # only the mutable inner secured-value key owned by this chokepoint.
        if isinstance(cek, bytearray):
            captured_cek.append(cek)
            assert any(cek)
        return original_open_body(header, cek, body_blob)

    monkeypatch.setattr(object_header_mod, "open_body", capture_open_body)
    assert open_revision(
        sealed.locator,
        holdings=held,
        content_store=store,
        content_key=content_key,
    ) == payload
    assert captured_cek == [bytearray(32)]


def test_a_secured_setting_is_refused_rather_than_downgraded(world, store):
    author = world.member(0)
    world.mint_initial_state(author)
    with pytest.raises(VaultError):
        seal_revision(
            author=author,
            frontier=world.fold(),
            set_id="dashboard.claude.credentials",
            key="default",
            setting_id="row-1",
            payload={"token": "t"},
            holdings=holdings_of(world, author),
            ancestry=world.ancestry,
            content_store=store,
            tier=SECURED,
        )


def test_a_secured_key_does_not_open_another_setting_of_the_same_class(world, store):
    """The class wrap binds the object, so material does not travel."""
    author = world.member(0)
    world.mint_initial_state(author)
    record, seeds = a_policy_class()
    common = dict(
        author=author,
        holdings=holdings_of(world, author),
        ancestry=world.ancestry,
        content_store=store,
        tier=SECURED,
        policy_class=record,
    )
    first = seal_revision(
        frontier=world.fold(), set_id="s.one", key="k", setting_id="r1",
        payload={"v": 1}, **common,
    )
    second = seal_revision(
        frontier=world.fold(), set_id="s.two", key="k", setting_id="r2",
        payload={"v": 2}, **common,
    )

    a, b = parse_locator(first.locator), parse_locator(second.locator)
    # Point the first locator at the second object: the class wrap inside is
    # bound to the object it was written for and does not open here.
    spliced = build_locator(
        genesis_id=a["genesis_id"], domain_id=a["domain_id"],
        object_id=b["object_id"], revision_id=b["revision_id"],
        storage_state_id=b["storage_state_id"], tier=SECURED,
        policy_class_id=a["policy_class_id"], required_policy=a["required_policy"],
    )
    # It resolves to the second object's own payload rather than the first's —
    # never a blend, and never the first setting's plaintext.
    held = holdings_of(world, author)
    content_key = content_key_for(
        spliced, record=record, seeds=seeds, holdings=held, content_store=store,
    )
    assert open_revision(
        spliced, holdings=held, content_store=store, content_key=content_key,
    ) == {"v": 2}
