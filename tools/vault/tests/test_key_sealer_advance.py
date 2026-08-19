"""A first vault write mints a generation, and the write is readable after.

The acceptance for ``auto-ehyoh``: a credential written into the vault has to
come back out. That is not the same claim as "the write succeeded" — a sealer
that drops the ``StateAdvance`` writes an object under a generation whose
descriptor is in no store and whose secret is in no cache, and every part of
that write reports success.

Everything here is REAL: a real ``Ledger`` (so a real, tagged authority
ancestry), a real ``KeyControlStore``, a real ``ContentStore``, a real
provisioned agent delegate as the author, the production sealer and the
production key holder. The one thing deliberately not used is the permissive
storagekit ``World`` double, because it is documented as tolerant of
identifiers it has never seen and would answer the safety question the same
way whichever DAG it was handed — which is exactly how the wrong ancestry
stayed green through three reviews.

The FIRST write is the one worth testing. There is no generation yet, so it
takes the advance path; a second write finds what the first left behind and
would pass even if nothing had been persisted.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SK_TESTS = Path(__file__).resolve().parents[2] / "network" / "storagekit" / "tests"
sys.path.insert(0, str(_SK_TESTS))

from tools.network.storagekit.keycontrol import KeyControlStore
from tools.vault.key_holder import VaultKeyCache, build_key_holder
from tools.vault.key_sealer import build_vault_sealer
from tools.vault.storage_object import open_revision

TTL_MS = 3_600_000


@pytest.fixture
def world(tmp_path):
    """A founded org on a REAL Ledger, with a delegate authorized to seal.

    Reuses the delegate suite's World, which is built on ``Sim`` and therefore
    on a real ``Ledger`` — so ``sim.ledger.ancestry`` is the genuine, tagged
    authority ancestry. The storagekit conftest's own permissive ancestry is
    deliberately not used anywhere here.
    """
    from test_delegate import HLC0, World
    from tools.network.storagekit import delegate as delegate_mod

    w = World()
    agent = delegate_mod.provision(
        w.sim.ledger, w.issuer, w.issuer, w.gen,
        hlc=w._hlc(), ttl_ms=TTL_MS,
    )

    def ledger_provider(_set_id, _org):
        return (
            w.sim.fold(),
            lambda heads: w.sim.fold(heads=list(heads)),
            w.sim.ledger.ancestry,      # the REAL, tagged authority ancestry
        )

    return {
        # seal_revision signs with this directly (creator.sign_hex), so the
        # author is the delegate's KEY, not the StorageDelegate wrapper. Its
        # public half is what the fold resolves up to the member persona.
        "agent": agent.signing_key,
        "ledger_provider": ledger_provider,
        "kc": tmp_path / "keycontrol.db",
        "content": tmp_path / "content",
    }


def test_a_first_write_is_readable_after_it_mints(world):
    """THE ONE THAT MATTERS. Write once into an empty vault, read it back.

    Between the two, the sealer must have accepted the minted descriptor into
    the key-control store and fed the new generation key to the cache — the
    holder builds its Holdings from exactly those two, so if either is missing
    the read cannot open what the write just sealed.
    """
    cache = VaultKeyCache()
    seal = build_vault_sealer(
        cache, world["kc"], world["content"],
        lambda: world["agent"], world["ledger_provider"],
    )
    hold = build_key_holder(cache, world["kc"], world["content"])

    secret = {"value": "ghp_" + "a" * 36}
    locator = seal(
        set_id="autonomy.vault.audited", schema_revision=1,
        key="github.token", setting_id="s-1", payload=secret,
        tier="audited", org="acme",
    )

    control = hold(set_id="autonomy.vault.audited", org="acme")
    opened = open_revision(
        locator, holdings=control.holdings,
        content_store=control.content_store,
    )
    assert opened == secret


def test_the_minted_descriptor_is_durable(world):
    """The descriptor must reach DISK, not just the caller's holdings.

    seal_revision mutates the Holdings it was handed, which makes an
    in-process read look correct while nothing was persisted. Re-opening the
    store is what separates the two.
    """
    cache = VaultKeyCache()
    seal = build_vault_sealer(
        cache, world["kc"], world["content"],
        lambda: world["agent"], world["ledger_provider"],
    )

    seal(set_id="autonomy.vault.audited", schema_revision=1, key="github.token",
         setting_id="s-1", payload={"value": "x" * 40}, tier="audited", org="acme")

    with KeyControlStore(world["kc"]) as reopened:
        assert reopened.states, "the generation this write minted is not on disk"


def test_the_new_generation_key_reaches_the_cache(world):
    """VaultKeyCache.secrets hands out a COPY, so the mutation seal_revision
    makes to the holdings it was passed lands on a dict that dies with the
    call. Without an explicit add, the cache never learns the key."""
    cache = VaultKeyCache()
    seal = build_vault_sealer(
        cache, world["kc"], world["content"],
        lambda: world["agent"], world["ledger_provider"],
    )
    assert not cache, "precondition: the cache starts empty"

    seal(set_id="autonomy.vault.audited", schema_revision=1, key="github.token",
         setting_id="s-1", payload={"value": "x" * 40}, tier="audited", org="acme")

    assert cache, "the generation the write minted never reached the cache"


def test_the_storage_ancestry_is_refused_on_this_path(world):
    """The guard is live through the real sealer, not only in its own unit
    test. This is the mistake that was made twice; here it stops the write
    instead of quietly answering the safety question against the wrong graph.
    """
    cache = VaultKeyCache()

    def wrong_ledger(_set_id, _org):
        frontier, fold_at, _authority = world["ledger_provider"](_set_id, _org)
        with KeyControlStore(world["kc"]) as kc:
            return frontier, fold_at, kc.ancestry     # the STORAGE DAG

    seal = build_vault_sealer(
        cache, world["kc"], world["content"],
        lambda: world["agent"], wrong_ledger,
    )

    with pytest.raises(TypeError, match="authority"):
        seal(set_id="autonomy.vault.audited", schema_revision=1,
             key="github.token", setting_id="s-1", payload={"value": "x" * 40},
             tier="audited", org="acme")


def test_no_delegate_means_no_write(world):
    """Cold until a human unlocks: with no delegate provisioned there is no
    author, and the write refuses rather than falling back to anything."""
    from tools.vault.key_sealer import VaultSealerNotReady

    seal = build_vault_sealer(
        VaultKeyCache(), world["kc"], world["content"],
        lambda: None, world["ledger_provider"],
    )

    with pytest.raises(VaultSealerNotReady, match="unlock"):
        seal(set_id="autonomy.vault.audited", schema_revision=1,
             key="github.token", setting_id="s-1", payload={"value": "x" * 40},
             tier="audited", org="acme")
