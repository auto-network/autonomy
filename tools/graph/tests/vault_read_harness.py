"""A real key control, wired into the settings seams, for driving step six.

Everything here is the production article: a throwaway org founded through the
real ledger, real generation keys, a real ``ContentStore`` on disk and a REAL
persisted ``KeyControlStore`` — descriptors and bridges written to SQLite and
re-verified on every hydrate, not the in-memory double the storagekit harness
uses for its own tests.

The one stand-in is the key HOLDER, which is exactly what this bead defines
and ``auto-a1pub`` later fills with the ramfs-backed cache. Its being a stub
is the point: if a stub satisfies the read path, the seam is the only thing
the read path needs.

No browser, no operator, no network, no human-entered password (crib §21).
"""

from __future__ import annotations

import hashlib
from dataclasses import replace as dataclass_replace
from pathlib import Path

from tools.graph import settings_ops
from tools.network.storagekit.keycontrol import KeyControlStore
from tools.network.storagekit.store import ContentStore
from tools.network.storagekit.tests.conftest import World
from tools.vault import policy_class as policy_class_mod
from tools.vault.factors import open_password_seed
from tools.vault.storage_object import AUDITED, SECURED, Holdings, seal_revision
from tools.vault.testkit import make_test_identity


class TamperingStore:
    """A content store that damages ONE object on its way back out.

    Real ciphertext, damaged in transit: the fault a resolver has to survive
    is not a store that refuses, it is bytes that arrive and do not open. Each
    mode is a different layer failing, which is the whole point of asserting
    the refusals apart.
    """

    def __init__(self, inner, object_id: str, mode: str):
        self.inner = inner
        self.object_id = object_id
        self.mode = mode

    def put_object(self, header, body):
        return self.inner.put_object(header, body)

    def get_object(self, object_id, revision_id):
        header, body = self.inner.get_object(object_id, revision_id)
        if object_id != self.object_id:
            return header, body
        if self.mode == "unknown_suite":
            # A suite this build does not implement. Fails closed at the
            # negotiation rather than being decrypted with something else.
            return dataclass_replace(header, body_suite_id="not-a-suite-v9"), body
        if self.mode == "decryption_failed":
            # Different ciphertext, content address restated so the address
            # check passes and the failure is genuinely the AEAD's.
            junk = b"\x00" * len(body)
            return (
                dataclass_replace(header, ciphertext_hash=hashlib.sha256(junk).hexdigest()),
                junk,
            )
        raise AssertionError(f"unknown tamper mode {self.mode!r}")

    def close(self):
        self.inner.close()


class PerReadStore:
    """A content store opened for one read and closed again.

    A real holder hands over a store its own process already has open. A test
    drives the same read from the pytest thread AND from the server's event
    loop, and a SQLite handle does not cross threads — so this opens where it
    is used. It costs an open per object and buys the HTTP boundary being a
    real one.
    """

    def __init__(self, path: Path, tamper=None):
        self.path = path
        self.tamper = tamper

    def get_object(self, object_id, revision_id):
        with ContentStore(self.path) as store:
            source = (
                TamperingStore(store, *self.tamper) if self.tamper is not None
                else store
            )
            header, body = source.get_object(object_id, revision_id)
            return header, bytes(body)


class VaultWorld:
    """A founded org that can seal settings and hold the keys that open them.

    ``register()`` installs both halves of the settings seam — the sealer this
    process writes through and the key holder it reads through — so a test
    only ever calls ``ops.add_setting`` and ``read_set`` and never mentions
    the storage layer.
    """

    def __init__(self, root: Path, member_count: int = 2, *, scoped: bool = False):
        root = Path(root)
        self.world = World(member_count=member_count)
        self.author = self.world.member(0)
        self.initial, _ = self.world.mint_initial_state(self.author)
        self.genesis_id = self.world.gen
        if scoped:
            # Read through the PRODUCTION holder and it resolves the scoped
            # database, so a world that wrote to a sidecar would be testing a
            # store nothing reads. Opt-in, because most tests here inject
            # their own holder and want the isolated files.
            from tools.vault.db_content_store import DbContentStore, vault_db_path_for

            self._scoped_db = vault_db_path_for(None)
            self.content_store = DbContentStore(self._scoped_db)
        else:
            self._scoped_db = None
            self.content_store = ContentStore(root / "content")
        self.store = self.content_store
        # The real one, on disk. Every open re-verifies content addresses and
        # bridge signatures, so a descriptor this test could not have written
        # cannot appear in the holdings the read path is handed.
        self.key_control = KeyControlStore(self._scoped_db or (root / "keycontrol.db"))
        self.policy_class = None
        self.opener_seeds: dict = {}
        self.holder_calls: list = []
        self._held_override: dict | None = None
        self._bridges_enabled = True
        self._tamper: tuple | None = None
        self._root = root
        self.sync()

    # -- key control ----------------------------------------------------------

    def sync(self) -> None:
        """Persist every descriptor and bridge the world has produced."""
        for descriptor in self.world.stores.kc.states.values():
            bridges = [
                b for b in self.world.stores.kc.bridges
                if b.child_state_id == descriptor.state_id
            ]
            self.key_control.accept_state(
                descriptor,
                self.world.fold,
                self.world.ancestry,
                bridges=tuple(bridges),
            )

    def holdings(self, persona=None) -> Holdings:
        """What the reader holds, read back out of the persisted store.

        Opened here rather than reused, for the reason :class:`PerReadStore`
        gives — and it costs nothing to correctness, because ``states`` and
        ``accepted_bridges`` are already snapshots of verified records.
        """
        secrets = (
            dict(self._held_override) if self._held_override is not None
            else dict(self.world.held(persona or self.author))
        )
        with KeyControlStore(self._scoped_db or (self._root / "keycontrol.db")) as key_control:
            return Holdings(
                secrets=secrets,
                descriptors=key_control.states,
                bridges=(
                    list(key_control.accepted_bridges())
                    if self._bridges_enabled else []
                ),
            )

    def hold_only(self, *state_ids: str) -> None:
        """Narrow the reader to these generations — the rest must come through
        bridges, or not at all."""
        every = dict(self.world.held(self.author))
        self._held_override = {s: every[s] for s in state_ids}

    def hold_nothing(self) -> None:
        self._held_override = {}

    def drop_bridges(self) -> None:
        """Lose every parent bridge, keeping the descriptors that name them."""
        self._bridges_enabled = False

    # -- the two seams --------------------------------------------------------

    def sealer(self, *, set_id, schema_revision, key, setting_id, payload, tier, org):
        secured = tier == SECURED
        if secured and self.policy_class is None:
            self.mint_policy_class()
        sealed = seal_revision(
            author=self.author,
            frontier=self.world.fold(),
            set_id=set_id,
            key=key,
            setting_id=setting_id,
            payload=payload,
            # The world's own live records: a generation minted in line lands
            # back in them, so the next write finds it rather than minting a
            # second one — which is the contract ``seal_revision`` documents.
            holdings=Holdings(
                secrets=self.world.held(self.author),
                descriptors=self.world.stores.kc.states,
                bridges=self.world.stores.kc.bridges,
            ),
            ancestry=self.world.ancestry,
            content_store=self.content_store,
            tier=tier,
            policy_class=self.policy_class if secured else None,
            opener_seeds=self.opener_seeds if secured else None,
        )
        self.sync()
        return sealed.locator

    def key_holder(self, *, set_id, org):
        self.holder_calls.append((set_id, org))
        return settings_ops.VaultKeyControl(
            holdings=self.holdings(),
            content_store=PerReadStore(self._root / "content", self._tamper),
        )

    def register(self) -> "VaultWorld":
        settings_ops.set_vault_sealer(self.sealer)
        settings_ops.set_vault_key_holder(self.key_holder)
        return self

    def tamper(self, object_id: str, mode: str) -> None:
        self._tamper = (object_id, mode)

    # -- the human factor -----------------------------------------------------

    def mint_policy_class(self):
        """A throwaway password class, plus the seed that opens it."""
        identity = make_test_identity()
        self.policy_class = policy_class_mod.create_class(
            "password", [identity.published], created_at="2026-08-17T00:00:00Z",
        )
        self.opener_seeds = {
            identity.factor_id: open_password_seed(identity.armor, identity.password)
        }
        return self.policy_class

    def close(self) -> None:
        self.content_store.close()
        self.key_control.close()


def clear_seams() -> None:
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)


__all__ = [
    "AUDITED",
    "SECURED",
    "TamperingStore",
    "VaultWorld",
    "clear_seams",
]
