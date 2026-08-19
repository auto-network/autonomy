"""Bring the vault to life at unlock, and found the personal ledger once.

The vault is COLD until a human unlocks. Nothing here survives a restart, and
that is the design (crib ``1e005d5c-c11`` §10): "a reboot requiring a human to
sign in and reactivate the node is the ACCEPTED cost, not a defect to engineer
around. There is NO mode where a machine resumes usable without an unlock."

Every piece of the vault runtime hangs off that one moment, which is why they
are one function here rather than four integrations scattered through the
unlock route:

* the generation keys, opened in the BROWSER (the personal root never reaches
  the server) and handed over, land in the cache,
* the agent delegate is provisioned — MEMORY-class, gone on restart,
* the key holder is registered, so an audited READ can open a secret,
* the sealers are registered, so a WRITE can create one.

Unattended operation on a fresh machine comes from the AGENT performing the
unlock — a throwaway persona whose factor the agents hold — never from any of
this surviving a restart. Do not add a path that provisions without an unlock;
that is the hot-key-across-reboot shape §10 retired.

## Founding is not part of unlock

``found_personal_ledger_if_absent`` runs ONCE, ever. The genesis event embeds a
timestamp and is signed, so founding twice yields a different genesis id and
orphans every secret sealed under the first. It needs an unlocked root, so it
happens under an unlock — but it is a provisioning step that unlock triggers,
not something unlock does. Every unlock after the first only READS the fold.
"""

from __future__ import annotations

from typing import Callable

from tools.vault.key_holder import VaultKeyCache, register_key_holder
from tools.vault.key_sealer import register_vault_sealer

#: The organization id in the personal ledger's genesis payload. A constant is
#: correct: the genesis EVENT id is a hash over the event, which carries the
#: operator's own root, so two operators passing this same string still get
#: different genesis ids. Uniqueness comes from the root, not from this.
PERSONAL_ORG_ID = "personal"


class AlreadyFounded(RuntimeError):
    """The personal ledger exists. Founding again would orphan every secret."""


def found_personal_ledger_if_absent(store, personal_root_seed: bytes, root, *, now: int):
    """Found the operator's own ledger, once, and return the founded result.

    The personal store is a degenerate organization: one member, the operator.
    That is not a workaround — it is the whole storage machinery running at
    N=1, which is what lets a personal secret reuse the same seal path an org
    secret takes rather than needing a second one.

    ``kem_seed`` is NOT optional and is the easiest thing here to get wrong.
    ``found_org_ledger`` builds the ``PersonaKemCredential`` only when one is
    supplied, and ``seal_revision`` seals the content key TO that credential —
    so founding without it produces a fold that looks fine and fails at the
    first write. It is derived, not minted: the same root yields a
    byte-identical seed on every machine, which is why a second machine needs
    no enrolment ceremony to read what this one sealed.

    Returns ``None`` when the ledger is already founded, so a caller can treat
    this as "ensure" rather than branching on existence.
    """
    from tools.network.ledger.found import found_org_ledger
    from tools.network.storagekit import credentials as credentials_mod

    if len(store):
        # Already founded. Re-founding is not idempotent — a fresh genesis at a
        # different `now` addresses a different domain, and every object sealed
        # under the old one becomes unreachable while still sitting on disk.
        return None

    return found_org_ledger(
        store,
        org_id=PERSONAL_ORG_ID,
        org_root=root,
        personal_root_seed=personal_root_seed,
        now=now,
        kem_seed=credentials_mod.derive_kem_seed(personal_root_seed),
    )


def _home_routed_ledger_provider(org_ledger_provider, personal_ledger_provider):
    """One ledger provider that answers for whichever scope the set belongs to.

    ``settings_ops`` holds exactly ONE sealer, so "register an org sealer and a
    personal sealer" is not available and should not be faked with two globals.
    The distinction is not really between two sealers anyway — it is between
    two folds — so it belongs here, in the one parameter that differs.

    Routing is by the set's DECLARED HOME, not by the org argument. The org a
    write is scoped to and the database its row lives in are different axes: a
    personal-homed set's row lands in personal.db whatever org the caller is
    acting as, and it must seal against the operator's own fold either way.
    """
    from tools.graph import schemas

    def provider(set_id: str, org: "str | None"):
        if schemas.declared_home(set_id) == "personal":
            return personal_ledger_provider(org)
        return org_ledger_provider(org)

    return provider


def register_vault_for_unlock(
    *,
    generation_keys: dict,
    author_provider: Callable[[], object],
    org_ledger_provider: Callable[["str | None"], object],
    personal_ledger_provider: Callable[["str | None"], object],
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

    ONE SEALER, routing on the set's declared home. Personal-homed vaulted sets
    seal against the operator's own fold; everything else against its
    organization's. Same function, same signature, one parameter different.
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
        _home_routed_ledger_provider(org_ledger_provider, personal_ledger_provider),
    )
    return cache
