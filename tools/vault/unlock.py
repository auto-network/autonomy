"""Sign-on handoff: turn a persona's material into the generation keys the
vault key holder reads (``auto-pw9bs.1``).

At unlock the browser derives each organization's persona storage
**encapsulation** private key, clears the root, then submits the scoped keys.
The dashboard opens persisted ``CapabilityGrant``s and merges recovered keys
into :class:`tools.vault.key_holder.VaultKeyCache`. The personal root seed never
leaves; the encapsulation key is memory-only and cannot sign, author, or act
(crib §12; sign-in wire graph://b437ecfb-e23).

This module is the key-opening core of that handoff — the part that is pure
cryptography over already-held inputs, with no transport and no browser. It
produces exactly the ``{state_id: generation_key}`` mapping the holder consumes,
so the sign-on wire and the read path cannot disagree about the shape.
"""

from __future__ import annotations

from typing import Dict, Iterable, Mapping

from tools.network.idkit.sealing import derive_encapsulation_keypair
from tools.network.storagekit.capability import accept
from tools.network.storagekit.credentials import (
    kem_purpose, domain_member_keys, verify_against_fold, select_current_credential,
)
from tools.network.storagekit.errors import StorageError


def current_recovery_credentials(frontier, key_control, ancestry) -> tuple:
    """Read current recipients, including admitted claims not projected by a write.

    Unlike sealing's recipient collection, recovery registers nothing. The
    same validation/selection applies to both fold and persisted credentials.
    """
    result = []
    for persona in sorted(domain_member_keys(frontier)):
        candidates = list(key_control.credentials_for_persona(persona))
        candidates.extend(member.kem_credential for member in frontier.members.values()
                          if member.current_key == persona and member.kem_credential)
        valid = []
        for candidate in candidates:
            try:
                valid.append(verify_against_fold(candidate, frontier))
            except StorageError:
                continue
        if valid:
            result.append(select_current_credential(valid, ancestry))
    return tuple(result)


def recover_organization_generations(genesis_id, kem_keys, key_control, cache) -> int:
    """Merge openable, unheld states into the existing cache; no record writes.

    Shared by sign-in and subsequent holder/restore integration. Failed opens
    are not cached, so another key or a later synchronized grant may succeed.
    ``kem_keys`` maps credential IDs to private keys; try only addressed grants.
    """
    descriptors = {sid: state for sid, state in key_control.states.items()
                   if state.genesis_id == genesis_id}
    grants = key_control.accepted_grants()
    held = set(cache.secrets)
    recovered = 0
    for key_id, private in kem_keys.items():
        for grant in grants:
            sid = grant.storage_state_id
            if (sid in held or sid not in descriptors
                    or grant.recipient_kem_key_id != key_id):
                continue
            for state_id, secret in open_generation_keys(private, (grant,), descriptors).items():
                cache.add(state_id, secret)
                held.add(state_id)
                recovered += 1
    return recovered


def open_generation_keys(
    kem_private_key: str,
    grants: Iterable,
    descriptors: Mapping[str, object],
) -> Dict[str, bytes]:
    """Recover ``{state_id: generation_key}`` from the grants an unlock holds.

    For each grant naming a descriptor we hold, ``accept`` verifies the grant's
    signature, suite, descriptor context, and the double commitment before
    returning the secret — a grant that does not open against a present
    descriptor, or fails any check, contributes nothing rather than raising, so
    one unusable grant never denies the rest. A grant naming a state we do not
    have a descriptor for is skipped: its generation is not one this reader can
    currently open, and eager re-provisioning (crib §15) repairs that, not this.
    """
    opened: Dict[str, bytes] = {}
    for grant in grants:
        descriptor = descriptors.get(getattr(grant, "storage_state_id", None))
        if descriptor is None:
            continue
        try:
            opened[descriptor.state_id] = accept(grant, kem_private_key, descriptor)
        except Exception:
            # A grant that will not open is not a reason to fail the unlock;
            # the generations that DO open still load. A caller that needs to
            # know a specific grant failed asks accept() directly.
            continue
    return opened


def open_generation_keys_for_persona(
    persona_kem_seed: bytes,
    genesis_id: str,
    grants: Iterable,
    descriptors: Mapping[str, object],
) -> Dict[str, bytes]:
    """Derive the persona's org-bound encapsulation key from its seed and open
    every grant with it.

    ``persona_kem_seed`` comes from ``credentials.derive_kem_seed(personal_root,
    counter)``, never the personal root itself. The seed is org-independent;
    the encapsulation derivation supplies the organization binding. The key is
    bound to ``kem_purpose(genesis_id)`` — the one fixed label a
    ``PersonaKemCredential`` is published under — so it opens grants sealed to
    this persona in this org and nothing else.
    """
    kem_private, _kem_public = derive_encapsulation_keypair(
        persona_kem_seed, kem_purpose(genesis_id)
    )
    return open_generation_keys(kem_private, grants, descriptors)
