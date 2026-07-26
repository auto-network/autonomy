"""Fold-based authorize(): the five acceptance cases, both founding paths."""

from __future__ import annotations

import dataclasses
import os

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger import (
    HLC,
    LedgerStore,
    SignatureError,
    make_event,
    org_ledger_db_path,
)
from tools.network.ledger.errors import GenesisError
from tools.network.ledger.found import found_org_ledger
from tools.dashboard import org_authority
from tools.dashboard.org_authority import authorize

ORG_ID = "018f6b2a-7c4d-7e11-8a3b-9d5c1e2f4a6b"
T0 = 1_800_000_000_000


@pytest.fixture(autouse=True)
def orgs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path))
    org_authority._fold_cache.clear()
    yield tmp_path
    org_authority._fold_cache.clear()


def _found(slug: str):
    """The production founding path: found_org_ledger claimed founder."""
    store = LedgerStore(org_ledger_db_path(slug))
    root = KeyPair.generate()
    result = found_org_ledger(
        store, org_id=ORG_ID, org_root=root, personal_root_seed=os.urandom(32), now=T0
    )
    return store, root, result


class TestAuthorize:
    def test_owner_holds_every_scope(self):
        store, _, result = _found("acme")
        try:
            founder = result.founder_persona_pub
            for scope in ("role:grant:admin", "link:publish", "anything:at:all"):
                assert authorize("acme", founder, scope) is True
        finally:
            store.close()

    def test_scoped_role_covers_only_its_scopes(self):
        # The bare role.grant path: authority to a key that is no member.
        store, root, _ = _found("acme")
        try:
            publisher = KeyPair.generate()
            define = make_event(
                root,
                {
                    "type": "role.define",
                    "name": "publisher",
                    "scope_set": ["link:publish"],
                    "claim_requires": "self",
                    "version": 1,
                },
                list(store.heads()),
                HLC(T0 + 1_000),
            )
            define_id = store.append(define)
            store.append(
                make_event(
                    root,
                    {
                        "type": "role.grant",
                        "persona": publisher.public_hex,
                        "role": "publisher",
                    },
                    [define_id],
                    HLC(T0 + 2_000),
                )
            )
            assert authorize("acme", publisher.public_hex, "link:publish") is True
            assert authorize("acme", publisher.public_hex, "role:grant:admin") is False
            assert authorize("acme", publisher.public_hex, "link:revoke") is False
        finally:
            store.close()

    def test_unknown_persona_holds_nothing(self):
        store, _, _ = _found("acme")
        try:
            nobody = KeyPair.generate().public_hex
            for scope in ("link:publish", "role:grant:admin", "*"):
                assert authorize("acme", nobody, scope) is False
        finally:
            store.close()

    def test_forged_grant_never_lands_and_grants_nothing(self):
        store, root, _ = _found("acme")
        try:
            mallory = KeyPair.generate()
            grant = make_event(
                root,
                {"type": "role.grant", "persona": mallory.public_hex, "role": "owner"},
                list(store.heads()),
                HLC(T0 + 1_000),
            )
            forged = dataclasses.replace(
                grant, sig=KeyPair.generate().sign_hex(b"unrelated")
            )
            with pytest.raises(SignatureError):
                store.append(forged)
            assert authorize("acme", mallory.public_hex, "link:publish") is False
        finally:
            store.close()

    def test_at_head_pins_and_head_advance_recomputes(self):
        store, root, result = _found("acme")
        try:
            founder = result.founder_persona_pub
            # Before the founding claim entered, the founder held nothing.
            assert (
                authorize("acme", founder, "link:publish", at_head=[result.genesis_id])
                is False
            )
            assert authorize("acme", founder, "link:publish") is True
            # A head advance recomputes rather than serving the cached fold:
            # revoking the founder's role flips the same call to False.
            store.append(
                make_event(
                    root,
                    {"type": "role.revoke", "persona": founder, "role": "owner"},
                    list(store.heads()),
                    HLC(T0 + 1_000),
                )
            )
            assert authorize("acme", founder, "link:publish") is False
            # The pinned historical head still answers as history had it.
            assert (
                authorize("acme", founder, "link:publish", at_head=[result.genesis_id])
                is False
            )
        finally:
            store.close()

    def test_missing_ledger_fails_closed(self):
        with pytest.raises(GenesisError):
            authorize("ghost-org", KeyPair.generate().public_hex, "link:publish")

    def test_cache_holds_one_head_per_org(self):
        store, _, result = _found("acme")
        try:
            authorize("acme", result.founder_persona_pub, "link:publish")
            authorize(
                "acme",
                result.founder_persona_pub,
                "link:publish",
                at_head=[result.genesis_id],
            )
            keys = [k for k in org_authority._fold_cache if k[0] == "acme"]
            assert len(keys) == 1  # miss evicted the older entry
        finally:
            store.close()
