"""Headless acceptance for the storage agent delegate (auto-pw9bs.2).

Driven entirely via the ledger/acceptance CLI-surface with throwaway test
identities and a test org — no browser, no human password/PRF (§21). Each
class maps to one bead acceptance criterion:

  * :class:`TestValidDelegateAccepted` — a valid delegate signs a
    generation advance and a capability grant unattended; both accepted.
  * :class:`TestOverReachRefusedByFold` — a delegate reaching beyond the
    two storage scopes, carrying ``*``, or chaining outside the roster
    produces records EVERY honest node refuses — refused at fold
    acceptance, never by an in-process check.
  * :class:`TestExpiryRenewRevoke` — an expired delegate is refused;
    renewing before expiry restores acceptance; a revocation refuses it
    immediately.
  * :class:`TestMemoryClass` — the delegate is MEMORY-class: ramfs only,
    never on disk, gone (empty) on a fresh cache until re-provisioned.

The enforcement boundary under test is the fold: acceptance resolves a
record's signer up to a member persona and checks THAT persona's
membership. Nothing here consults a local ``if`` for authorization.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger import HLC, sign_delegate_proof
from tools.network.ledger.projections import organization_content_domain_id
from tools.network.ledger.tests.conftest import Sim
from tools.network.storagekit import (
    RAMFS_MAGIC,
    TMPFS_MAGIC,
    MemoryClassError,
    RamDelegateCache,
    assert_memory_backed,
    credentials,
    delegate as delegate_mod,
    filesystem_magic,
    storage_delegate_scopes,
)
from tools.network.storagekit.acceptance import (
    ScopeError,
    accept_grant,
    accept_state,
    loss_projection_digest,
    resolve_member_key,
    scope_storage_advance,
    scope_storage_grant,
)

HLC0 = (1_800_000_000_000, 0)
KEM_SEED = bytes(range(32))
TTL = 10_000  # ms


class World:
    """A founded test org with a member issuer holding the two storage
    scopes THROUGH ITS ROLE, a recipient member, and a non-member
    outsider. No root-present enabling act exists any more (auto-wrkaq
    deleted authorize_member_storage): a member persona mints its own
    bounded delegates from role-held authority."""

    def __init__(self):
        self.sim = Sim()
        s = self.sim
        self.gen = s.genesis_id
        self.dom = organization_content_domain_id(self.gen)
        s.role_define(
            s.root, "member",
            scope_set=sorted(
                ["link:publish"] + storage_delegate_scopes(self.dom)
            ),
        )
        self.issuer = self._admit()
        self.recipient = self._admit()
        # A non-member granted the two scopes re-delegably by the root
        # (root delegation is unchanged by auto-wrkaq): a chain through it
        # terminates OUTSIDE the roster.
        self.outside = KeyPair.generate()
        s.delegate(
            s.root, self.outside, storage_delegate_scopes(self.dom),
            redelegate=True,
        )

    def _admit(self):
        s = self.sim
        persona, invite_key = KeyPair.generate(), KeyPair.generate()
        iid = s.invite(s.root, "member", invite_key=invite_key)
        s.claim(iid, invite_key, persona)
        return persona

    def _hlc(self, ts=None) -> HLC:
        return HLC(self.sim.next_ts() if ts is None else ts)

    def seams(self, now):
        s = self.sim
        return (
            lambda heads: s.fold(heads=list(heads), now=now),
            lambda ids: s.ledger.ancestry(ids),
        )

    def mint_advance(self, delegate, now):
        """Sign a generation descriptor at the current frontier with the
        delegate, folding for the projection at ``now``."""
        heads = sorted(self.sim.ledger.heads())
        f = self.sim.fold(heads=heads, now=now)
        return delegate_mod.sign_generation_advance(
            delegate,
            authority_heads=heads,
            covered_loss_heads=sorted(f.loss_heads),
            loss_projection_digest=loss_projection_digest(f),
        )

    def credential(self, persona, seed=KEM_SEED):
        return credentials.build(persona, self.gen, seed, [self.gen], HLC0)


@pytest.fixture
def world():
    return World()


def test_scopes_are_exactly_two(world):
    scopes = delegate_mod.storage_delegate_scopes(world.dom)
    assert scopes == sorted(
        [scope_storage_advance(world.dom), scope_storage_grant(world.dom)]
    )
    assert "*" not in scopes
    assert not any(s.startswith("checkpoint") for s in scopes)
    assert not any(
        s.startswith(("role:", "invite", "link:")) for s in scopes
    )


class TestValidDelegateAccepted:
    def test_advance_and_grant_accepted_unattended(self, world):
        now = HLC0[0] + 1_000
        d = delegate_mod.provision(
            world.sim.ledger, world.issuer, world.issuer, world.gen,
            hlc=world._hlc(), ttl_ms=TTL,
        )
        # The delegate carries exactly the two scopes and chains to a member.
        assert d.scopes == tuple(delegate_mod.storage_delegate_scopes(world.dom))
        state_fold = world.sim.fold(now=now)
        assert resolve_member_key(state_fold, d.public_hex) == world.issuer.public_hex

        # A generation advance, signed by the delegate, accepted.
        descriptor, secret = world.mint_advance(d, now)
        fold_at, ancestry = world.seams(now)
        accepted = accept_state(descriptor, fold_at, ancestry)
        assert accepted.creator_member == world.issuer.public_hex

        # A capability grant, signed by the delegate, accepted.
        credential, _ = world.credential(world.recipient)
        grant = delegate_mod.sign_capability_grant(
            d,
            storage_state_id=descriptor.state_id,
            recipient_credential=credential,
            state_secret=secret,
            state_secret_commitment=descriptor.secret_commitment,
            authority_heads=sorted(world.sim.ledger.heads()),
        )
        g = accept_grant(grant, fold_at, ancestry, credential, descriptor)
        assert g.grantor_member == world.issuer.public_hex
        assert g.recipient_persona == world.recipient.public_hex


class TestOverReachRefusedByFold:
    def _agent_signs_advance(self, world, agent_key, now):
        heads = sorted(world.sim.ledger.heads())
        f = world.sim.fold(heads=heads, now=now)
        stub = delegate_mod.StorageDelegate(
            signing_key=agent_key, child_pub=agent_key.public_hex,
            member_persona=world.issuer.public_hex, genesis_id=world.gen,
            domain_id=world.dom, scopes=(), grant_event_id="0" * 64,
            issued_ts=now, not_after=now + TTL,
        )
        return world.mint_advance(stub, now)

    def test_wildcard_scope_refused(self, world):
        now = HLC0[0] + 1_000
        agent = KeyPair.generate()
        # A mint reaching for `*`: the issuer cannot delegate `*`, so the
        # ledger marks the delegate event invalid and no edge enters the
        # fold. The signer resolves to no member; the record is refused.
        world.sim.delegate(world.issuer, agent, ["*"], ttl=TTL)
        descriptor, _ = self._agent_signs_advance(world, agent, now)
        fold_at, ancestry = world.seams(now)
        assert resolve_member_key(world.sim.fold(now=now), agent.public_hex) is None
        with pytest.raises(ScopeError):
            accept_state(descriptor, fold_at, ancestry)

    def test_extra_scope_beyond_the_two_refused(self, world):
        now = HLC0[0] + 1_000
        agent = KeyPair.generate()
        # Exactly-two PLUS an intent scope the issuer does not hold
        # delegably: attenuation fails, the event is invalid, no edge.
        over = sorted(
            delegate_mod.storage_delegate_scopes(world.dom) + ["link:publish"]
        )
        world.sim.delegate(world.issuer, agent, over, ttl=TTL)
        descriptor, _ = self._agent_signs_advance(world, agent, now)
        fold_at, ancestry = world.seams(now)
        with pytest.raises(ScopeError):
            accept_state(descriptor, fold_at, ancestry)

    def test_chain_terminating_outside_roster_refused(self, world):
        now = HLC0[0] + 1_000
        # The outsider holds the two scopes re-delegably but is NOT a
        # member. A delegate under it resolves up to a non-member: void.
        d = delegate_mod.provision(
            world.sim.ledger, world.outside, world.outside, world.gen,
            hlc=world._hlc(), ttl_ms=TTL,
        )
        assert resolve_member_key(world.sim.fold(now=now), d.public_hex) is None
        descriptor, _ = world.mint_advance(d, now)
        fold_at, ancestry = world.seams(now)
        with pytest.raises(ScopeError):
            accept_state(descriptor, fold_at, ancestry)


class TestExpiryRenewRevoke:
    def test_expired_then_renew_then_revoke(self, world):
        t0 = world.sim.next_ts()  # strictly beyond every founded-org head
        d = delegate_mod.provision(
            world.sim.ledger, world.issuer, world.issuer, world.gen,
            hlc=HLC(t0), ttl_ms=TTL,
        )
        assert d.not_after == t0 + TTL

        # Within the TTL: accepted.
        within = t0 + 1_000
        descriptor, _ = world.mint_advance(d, within)
        fold_at, ancestry = world.seams(within)
        assert accept_state(descriptor, fold_at, ancestry).creator_member == (
            world.issuer.public_hex
        )

        # Past the TTL: the fold drops the edge; the SAME record refused.
        beyond = t0 + TTL + 1
        fold_at, ancestry = world.seams(beyond)
        with pytest.raises(ScopeError):
            accept_state(descriptor, fold_at, ancestry)
        # And a fresh record minted past expiry is refused too.
        stale_desc, _ = world.mint_advance(d, beyond)
        with pytest.raises(ScopeError):
            accept_state(stale_desc, fold_at, ancestry)

        # Renew before the ORIGINAL expiry: a new grant, later window.
        renewed = delegate_mod.renew(
            world.sim.ledger, world.issuer, d, hlc=HLC(t0 + 2_000), ttl_ms=TTL,
        )
        assert renewed.signing_key is d.signing_key  # same key, new window
        assert renewed.not_after == t0 + 2_000 + TTL

        # `beyond` is inside the renewed window: acceptance restored.
        assert beyond < renewed.not_after
        fresh_desc, secret = world.mint_advance(renewed, beyond)
        fold_at, ancestry = world.seams(beyond)
        assert accept_state(fresh_desc, fold_at, ancestry).creator_member == (
            world.issuer.public_hex
        )

        # Revoke: the edge is gone immediately; records minted after the
        # revoke (citing the frontier that includes it) are refused.
        delegate_mod.revoke(
            world.sim.ledger, world.issuer, renewed, hlc=HLC(beyond + 1),
        )
        after = beyond + 2
        fold_at, ancestry = world.seams(after)
        assert resolve_member_key(world.sim.fold(now=after), renewed.public_hex) is None
        revoked_desc, _ = world.mint_advance(renewed, after)
        with pytest.raises(ScopeError):
            accept_state(revoked_desc, fold_at, ancestry)


class TestMemoryClass:
    def test_real_disk_path_is_refused(self, tmp_path):
        # tmp_path is on the container's real filesystem (not ramfs). The
        # guard must refuse it — the key never touches disk.
        with pytest.raises(MemoryClassError):
            assert_memory_backed(tmp_path)
        cache = RamDelegateCache(tmp_path)
        with pytest.raises(MemoryClassError):
            cache.store(KeyPair.generate())
        # Nothing was written.
        assert not (tmp_path / RamDelegateCache._FILENAME).exists()

    def test_tmpfs_is_refused_as_swappable(self):
        with pytest.raises(MemoryClassError) as exc:
            assert_memory_backed("/whatever", magic_probe=lambda _p: TMPFS_MAGIC)
        assert "swap" in str(exc.value).lower()

    def test_ramfs_roundtrip_and_reboot(self, tmp_path):
        ramfs = lambda _p: RAMFS_MAGIC  # noqa: E731 — inject the class
        cache = RamDelegateCache(tmp_path, magic_probe=ramfs)

        # Fresh cache (post-reboot state): empty until re-provisioned.
        assert cache.load() is None

        key = KeyPair.generate()
        cache.store(key)
        loaded = cache.load()
        assert loaded is not None
        assert loaded.public_hex == key.public_hex
        assert loaded.private_hex == key.private_hex

        # A brand-new cache object over the SAME dir but simulating a reboot
        # (the ramfs is gone) reads empty — the file no longer exists.
        cache.clear()
        assert cache.load() is None

    def test_provisioned_key_survives_the_ram_cache(self, world, tmp_path):
        cache = RamDelegateCache(tmp_path, magic_probe=lambda _p: RAMFS_MAGIC)
        d = delegate_mod.provision(
            world.sim.ledger, world.issuer, world.issuer, world.gen,
            hlc=world._hlc(), ttl_ms=TTL,
        )
        cache.store(d.signing_key)
        # A later process reloads the exact signing identity and can sign
        # an acceptable record with it.
        reloaded = cache.load()
        assert reloaded.public_hex == d.public_hex
