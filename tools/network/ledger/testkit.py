"""Test scaffolding for ledger sync suites — NOT production code.

One org (root key + genesis + monotone HLC counter) that can mint
replica stores and simple valid events. Shared by
``tools/network/ledger/tests/test_sync.py`` and
``tools/network/registry/tests/test_broker_sync.py`` so the two F3
acceptance suites cannot drift onto different event shapes.
"""

from __future__ import annotations

from tools.network.idkit import KeyPair

from . import HLC, LedgerStore, make_event

T0 = 1_800_000_000_000  # unix ms, matches tests/conftest.py


class OrgSim:
    """A root key + genesis, and replica stores that share them."""

    def __init__(self, org: str):
        self.root = KeyPair.generate()
        self.genesis = make_event(
            self.root,
            {"type": "genesis", "org": org, "root_pub": self.root.public_hex},
            [],
            HLC(T0),
        )
        self._ts = T0

    def next_ts(self) -> int:
        self._ts += 1_000
        return self._ts

    def store(self) -> LedgerStore:
        s = LedgerStore()
        s.append(self.genesis)
        return s

    def emit(self, store: LedgerStore, author: KeyPair, payload: dict,
             parents=None) -> str:
        if parents is None:
            parents = list(store.heads())
        return store.append(make_event(author, payload, parents, HLC(self.next_ts())))

    def delegate(
        self,
        store: LedgerStore,
        child: KeyPair = None,
        scope=("link:publish",),
        parents=None,
    ) -> str:
        return self.emit(
            store,
            self.root,
            {
                "type": "delegate",
                "child_pub": (child or KeyPair.generate()).public_hex,
                "scope": sorted(set(scope)),
                "can_redelegate": False,
            },
            parents=parents,
        )
