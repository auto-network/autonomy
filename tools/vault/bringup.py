"""Register organization storage seams and the shared generation-key cache.


Personal-homed Settings use personal_object directly, with no authority ledger.
Old personal storage-format values remain readable through the key holder.
"""

from __future__ import annotations

from typing import Callable

from tools.vault.key_holder import VaultKeyCache, register_key_holder
from tools.vault.key_sealer import register_vault_sealer

def register_vault_for_unlock(
    *,
    generation_keys: dict,
    author_provider: Callable[[], object],
    org_ledger_provider: Callable[["str | None"], object],
    cache: "VaultKeyCache | None" = None,
) -> VaultKeyCache:
    """Make the vault usable in this process. Call once per unlock.

    Returns the cache, so a caller unlocking a second organization loads more
    keys into the same one rather than building a second.

    ONE CACHE serves reads and writes and both scopes. A storage state id is
    globally unique, so there is nothing to keep apart — and a second cache
    would mean a write minting a generation the read side cannot see, which is
    the failure the sealer's advance handling exists to prevent.

    NO STORE PATHS. Both seams resolve the scoped database per call, so a
    vault set's ciphertext lands in the same file its settings row does. A
    single path here put every scope in one sidecar — a store the design does
    not have, and a leak between organizations.

    This sealer serves organization storage only. Personal Settings encryption
    bypasses it in settings_ops._seal_vault_payload.
    """
    if cache is None:
        # A fresh unlock is AUTHORITATIVE — load() replaces, which is what we
        # want when this is the unlock that opened the process.
        cache = VaultKeyCache()
        cache.load(dict(generation_keys))
    else:
        # A SECOND scope inside the same unlock is ADDITIVE. Calling load()
        # here would silently drop the keys the first scope installed, and the
        # symptom would be an audited read failing for one organization after
        # unlocking another — which reads as a key bug, not a wiring one.
        for state_id, secret in dict(generation_keys).items():
            cache.add(state_id, secret)

    register_key_holder(cache)
    register_vault_sealer(
        cache,
        author_provider,
        lambda set_id, org: org_ledger_provider(org),
    )
    return cache
