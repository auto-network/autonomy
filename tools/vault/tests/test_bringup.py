"""Bringing the vault up at unlock: one cache, one sealer, routed by home."""
from __future__ import annotations

import pytest

from tools.graph import settings_ops
from tools.vault.bringup import (
    PERSONAL_ORG_ID,
    _home_routed_ledger_provider,
    found_personal_ledger_if_absent,
    register_vault_for_unlock,
)
from tools.vault.key_holder import VaultKeyCache
import tools.graph.schemas  # noqa: F401 — registers the sets


@pytest.fixture(autouse=True)
def _clean():
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)
    yield
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)


# ── founding happens once, ever ───────────────────────────────


def test_founding_an_empty_store_produces_a_genesis():
    from tools.network.idkit import KeyPair
    from tools.network.ledger import LedgerStore

    store = LedgerStore()
    res = found_personal_ledger_if_absent(
        store, bytes(range(32)), KeyPair.generate(), now=1_800_000_000_000)

    assert res is not None and res.genesis_id
    assert len(store) == 4          # genesis, role, invite, founder claim


def test_founding_carries_a_kem_credential():
    """THE ONE THAT SILENTLY BREAKS EVERYTHING. found_org_ledger builds the
    PersonaKemCredential only when a kem_seed is supplied, and seal_revision
    seals the content key TO that credential. Found without it and the fold
    looks fine until the first write."""
    from tools.network.idkit import KeyPair
    from tools.network.ledger import LedgerStore

    store = LedgerStore()
    res = found_personal_ledger_if_absent(
        store, bytes(range(32)), KeyPair.generate(), now=1_800_000_000_000)

    assert res.kem_credential is not None, "no KEM credential to seal to"


def test_a_second_call_refuses_rather_than_re_founding():
    """Re-founding is not idempotent: a new genesis at a different `now`
    addresses a different domain, so every secret sealed under the first
    becomes unreachable while still sitting on disk."""
    from tools.network.idkit import KeyPair
    from tools.network.ledger import LedgerStore

    store = LedgerStore()
    root, seed = KeyPair.generate(), bytes(range(32))
    first = found_personal_ledger_if_absent(store, seed, root, now=1_800_000_000_000)

    again = found_personal_ledger_if_absent(store, seed, root, now=1_900_000_000_000)

    assert again is None, "founded twice"
    assert len(store) == 4, "a second founding appended events"
    assert first.genesis_id


def test_the_org_id_is_constant_but_the_genesis_is_not():
    """A constant org id is correct: the genesis EVENT id hashes the event,
    which carries the operator's own root, so two operators passing the same
    string still get different genesis ids. Uniqueness comes from the root."""
    from tools.network.idkit import KeyPair
    from tools.network.ledger import LedgerStore

    a = found_personal_ledger_if_absent(
        LedgerStore(), bytes(range(32)), KeyPair.generate(), now=1_800_000_000_000)
    b = found_personal_ledger_if_absent(
        LedgerStore(), bytes(range(1, 33)), KeyPair.generate(), now=1_800_000_000_000)

    assert PERSONAL_ORG_ID == "personal"
    assert a.genesis_id != b.genesis_id


# ── one sealer, routed by the set's declared home ─────────────


def test_a_personal_homed_set_routes_to_the_personal_fold():
    """THE ROUTING. autonomy.vault.audited is @home('personal'), so it seals
    against the operator's own fold — whatever org the caller is acting as."""
    calls = []
    provider = _home_routed_ledger_provider(
        lambda org: calls.append(("org", org)),
        lambda org: calls.append(("personal", org)),
    )

    provider("autonomy.vault.audited", "anchore")

    assert calls == [("personal", "anchore")], (
        "a personal-homed set sealed against the caller's ORG fold — the row's "
        "home and the acting org are different axes")


def test_an_org_homed_set_routes_to_the_org_fold():
    calls = []
    provider = _home_routed_ledger_provider(
        lambda org: calls.append(("org", org)),
        lambda org: calls.append(("personal", org)),
    )

    provider("autonomy.network.org-key", "anchore")

    assert calls == [("org", "anchore")]


# ── what bringup actually installs ────────────────────────────


def test_bringup_installs_both_seams(tmp_path):
    """Before: neither a read nor a write can happen. After: both are wired."""
    assert settings_ops._vault_sealer is None
    assert settings_ops._vault_key_holder is None

    cache = register_vault_for_unlock(
        generation_keys={"state-1": b"k" * 32},
        author_provider=lambda: object(),
        org_ledger_provider=lambda org: None,
        personal_ledger_provider=lambda org: None,
        keycontrol_path=tmp_path / "kc.db",
        content_path=tmp_path / "content",
    )

    assert settings_ops._vault_sealer is not None, "no write path installed"
    assert settings_ops._vault_key_holder is not None, "no read path installed"
    assert cache.secrets == {"state-1": b"k" * 32}


def test_a_second_unlock_adds_to_the_same_cache(tmp_path):
    """One cache serves reads, writes and both scopes. A second would mean a
    write minting a generation the read side cannot see."""
    cache = register_vault_for_unlock(
        generation_keys={"state-1": b"k" * 32},
        author_provider=lambda: object(),
        org_ledger_provider=lambda org: None,
        personal_ledger_provider=lambda org: None,
        keycontrol_path=tmp_path / "kc.db", content_path=tmp_path / "c",
    )
    again = register_vault_for_unlock(
        generation_keys={"state-2": b"j" * 32},
        author_provider=lambda: object(),
        org_ledger_provider=lambda org: None,
        personal_ledger_provider=lambda org: None,
        keycontrol_path=tmp_path / "kc.db", content_path=tmp_path / "c",
        cache=cache,
    )

    assert again is cache
    assert set(cache.secrets) == {"state-1", "state-2"}
