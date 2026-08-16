"""The boundary this bead is judged at: write here, read THERE, as SOMEONE ELSE.

A local round trip proves the cryptography composes. It does not prove the
thing settings-as-objects exists for — that a value written by one member on
one machine is readable by a different member on a different store, holding no
plaintext and no shared file, with the ciphertext having crossed between them
verbatim.

So there are two content stores on disk, and nothing is shared between them
except bytes that are safe to replicate: the object header and body, the
public key-control records, and one capability grant sealed to the reader's
own published key-encapsulation credential.
"""

from __future__ import annotations

import pytest

from tools.network.idkit.errors import SealingError
from tools.network.storagekit import capability, distribution
from tools.network.storagekit.errors import StorageError
from tools.network.storagekit.store import ContentStore
from tools.network.storagekit.tests.conftest import World
from tools.vault.storage_object import (
    Holdings,
    open_revision,
    parse_locator,
    seal_revision,
)


@pytest.fixture
def world() -> World:
    return World(member_count=3)


@pytest.fixture
def stores(tmp_path):
    """Two independent content stores — separate SQLite files, separate blobs."""
    with ContentStore(tmp_path / "writer") as writer:
        with ContentStore(tmp_path / "reader") as reader:
            yield writer, reader


def _replicate(sealed, source: ContentStore, destination: ContentStore) -> None:
    """Copy the object between stores the way a replica would: the committed
    header and body, read back out of the source, verbatim."""
    reference = parse_locator(sealed.locator)
    header, body = source.get_object(reference["object_id"], reference["revision_id"])
    destination.put_object(header, body)


def _reader_holdings(world: World, reader, grant) -> Holdings:
    """What the reader has after accepting one grant and nothing else.

    Deliberately built from an empty secret set: the reader starts holding no
    state secret at all, and everything it can open comes from the grant it
    just accepted plus the public records anyone may have.
    """
    entry = world.principals[reader.public_hex]
    descriptor = world.stores.kc.states[grant.storage_state_id]
    secret = capability.accept(grant, entry["kem_private"], descriptor)
    return Holdings(
        secrets={grant.storage_state_id: secret},
        descriptors=dict(world.stores.kc.states),
        bridges=list(world.stores.kc.bridges),
    )


def test_written_here_read_there_by_someone_else(world, stores):
    """The whole path: write → replicate → grant → read, by another principal."""
    writer_store, reader_store = stores
    writer, reader = world.member(0), world.member(1)
    head, head_secret = world.mint_initial_state(writer)
    payload = {"access_token": "sk-live-crosses-the-wire", "refresh_token": "rt"}

    sealed = seal_revision(
        author=writer,
        frontier=world.fold(),
        set_id="dashboard.claude.credentials",
        key="default",
        setting_id="row-1",
        payload=payload,
        holdings=Holdings(
            secrets=world.held(writer),
            descriptors=world.stores.kc.states,
            bridges=world.stores.kc.bridges,
        ),
        ancestry=world.ancestry,
        content_store=writer_store,
    )

    # Nothing but the object crosses. The reader's store had never heard of it.
    reference = parse_locator(sealed.locator)
    with pytest.raises(StorageError):
        reader_store.get_object(reference["object_id"], reference["revision_id"])
    _replicate(sealed, writer_store, reader_store)

    # The grant is minted for the reader's own published credential and is the
    # only key material that moves — sealed, and to nobody else.
    grant = distribution.grant_current_head(
        writer,
        world.dom,
        world.principals[reader.public_hex]["credential"],
        head,
        head_secret,
        world.frontier(),
    )
    assert grant.recipient_kem_key_id == (
        world.principals[reader.public_hex]["credential"].kem_key_id
    )

    opened = open_revision(
        sealed.locator,
        holdings=_reader_holdings(world, reader, grant),
        content_store=reader_store,
    )
    assert opened == payload


def test_the_replicated_bytes_alone_reveal_nothing(world, stores):
    """Holding the whole second store, with no grant, reads nothing."""
    writer_store, reader_store = stores
    writer = world.member(0)
    world.mint_initial_state(writer)

    sealed = seal_revision(
        author=writer,
        frontier=world.fold(),
        set_id="dashboard.claude.credentials",
        key="default",
        setting_id="row-1",
        payload={"access_token": "sk-live-never-in-the-clear"},
        holdings=Holdings(
            secrets=world.held(writer),
            descriptors=world.stores.kc.states,
            bridges=world.stores.kc.bridges,
        ),
        ancestry=world.ancestry,
        content_store=writer_store,
    )
    _replicate(sealed, writer_store, reader_store)

    reference = parse_locator(sealed.locator)
    _, body = reader_store.get_object(reference["object_id"], reference["revision_id"])
    assert b"sk-live-never-in-the-clear" not in body

    with pytest.raises(StorageError):
        open_revision(
            sealed.locator,
            holdings=Holdings(
                secrets={},
                descriptors=dict(world.stores.kc.states),
                bridges=list(world.stores.kc.bridges),
            ),
            content_store=reader_store,
        )


def test_a_grant_addressed_to_one_member_does_not_open_for_another(world, stores):
    """Intercepting a grant in flight is not a way in."""
    writer_store, reader_store = stores
    writer, intended, interceptor = world.member(0), world.member(1), world.member(2)
    head, head_secret = world.mint_initial_state(writer)

    sealed = seal_revision(
        author=writer,
        frontier=world.fold(),
        set_id="dashboard.claude.credentials",
        key="default",
        setting_id="row-1",
        payload={"access_token": "sk-live"},
        holdings=Holdings(
            secrets=world.held(writer),
            descriptors=world.stores.kc.states,
            bridges=world.stores.kc.bridges,
        ),
        ancestry=world.ancestry,
        content_store=writer_store,
    )
    _replicate(sealed, writer_store, reader_store)

    grant = distribution.grant_current_head(
        writer,
        world.dom,
        world.principals[intended.public_hex]["credential"],
        head,
        head_secret,
        world.frontier(),
    )
    with pytest.raises((StorageError, SealingError)):
        capability.accept(
            grant,
            world.principals[interceptor.public_hex]["kem_private"],
            world.stores.kc.states[grant.storage_state_id],
        )


def test_a_generation_minted_in_line_replicates_and_still_reads(world, stores):
    """A write that had to mint its own generation is not a local artefact.

    The reader is granted the NEW generation only — no bridge walk, no prior
    secret — and reads the object written under it on the other store.
    """
    writer_store, reader_store = stores
    writer, leaving, reader = world.member(0), world.member(1), world.member(2)
    initial, _ = world.mint_initial_state(writer)
    world.grant(writer, leaving, initial)
    contraction = world.remove(leaving)

    sealed = seal_revision(
        author=writer,
        frontier=world.fold(),
        set_id="dashboard.claude.credentials",
        key="default",
        setting_id="row-after-removal",
        payload={"access_token": "sk-live-post-contraction"},
        holdings=Holdings(
            secrets=world.held(writer),
            descriptors=world.stores.kc.states,
            bridges=world.stores.kc.bridges,
        ),
        ancestry=world.ancestry,
        content_store=writer_store,
    )
    advance = sealed.advance
    assert advance is not None and contraction in advance.descriptor.covered_loss_heads

    # The new generation's public records replicate exactly as any other do.
    world.offer(("state", (advance.descriptor, advance.bridges)))
    assert advance.descriptor.state_id in world.stores.kc.states
    _replicate(sealed, writer_store, reader_store)

    grant = distribution.grant_current_head(
        writer,
        world.dom,
        world.principals[reader.public_hex]["credential"],
        advance.descriptor,
        advance.secret,
        world.frontier(),
    )
    assert open_revision(
        sealed.locator,
        holdings=_reader_holdings(world, reader, grant),
        content_store=reader_store,
    ) == {"access_token": "sk-live-post-contraction"}
