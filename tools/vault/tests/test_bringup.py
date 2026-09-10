"""Bringing the vault up at unlock: one cache, one sealer, routed by home."""
from __future__ import annotations

import pytest

from tools.graph import settings_ops
from tools.vault.bringup import (
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


# ── what bringup actually installs ────────────────────────────


def test_bringup_installs_both_seams(tmp_path):
    """Before: neither a read nor a write can happen. After: both are wired."""
    assert settings_ops._vault_sealer is None
    assert settings_ops._vault_key_holder is None

    cache = register_vault_for_unlock(
        generation_keys={"state-1": b"k" * 32},
        author_provider=lambda org: object(),
        org_ledger_provider=lambda org: None,
    )

    assert settings_ops._vault_sealer is not None, "no write path installed"
    assert settings_ops._vault_key_holder is not None, "no read path installed"
    assert cache.secrets == {"state-1": b"k" * 32}


def test_a_second_unlock_adds_to_the_same_cache(tmp_path):
    """One cache serves reads, writes and both scopes. A second would mean a
    write minting a generation the read side cannot see."""
    cache = register_vault_for_unlock(
        generation_keys={"state-1": b"k" * 32},
        author_provider=lambda org: object(),
        org_ledger_provider=lambda org: None,
    )
    again = register_vault_for_unlock(
        generation_keys={"state-2": b"j" * 32},
        author_provider=lambda org: object(),
        org_ledger_provider=lambda org: None,
        cache=cache,
    )

    assert again is cache
    assert set(cache.secrets) == {"state-1", "state-2"}
