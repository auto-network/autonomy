"""Sign-on handoff: turn a persona's material into the generation keys the
vault key holder reads (``auto-pw9bs.1``).

At unlock the browser (or its headless client-driver, crib ``1e005d5c-c11`` §21)
holds the personal root seed just long enough to derive one thing per
organization — the persona storage **encapsulation** private key — and to open
the ``CapabilityGrant``s addressed to it, recovering each current generation's
key. It transmits the opened generation keys to the dashboard, which loads them
into :class:`tools.vault.key_holder.VaultKeyCache`. The personal root seed never
leaves; the encapsulation key is decrypt-only and cannot sign, author, or act
(crib §12).

This module is the key-opening core of that handoff — the part that is pure
cryptography over already-held inputs, with no transport and no browser. It
produces exactly the ``{state_id: generation_key}`` mapping the holder consumes,
so the sign-on wire and the read path cannot disagree about the shape.
"""

from __future__ import annotations

from typing import Dict, Iterable, Mapping

from tools.network.idkit.sealing import derive_encapsulation_keypair
from tools.network.storagekit.capability import accept
from tools.network.storagekit.credentials import kem_purpose


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

    ``persona_kem_seed`` is the per-organization persona seed (``HKDF(personal
    root, genesis_id)``), never the personal root itself. The derived key is
    bound to ``kem_purpose(genesis_id)`` — the one fixed label a
    ``PersonaKemCredential`` is published under — so it opens grants sealed to
    this persona in this org and nothing else.
    """
    kem_private, _kem_public = derive_encapsulation_keypair(
        persona_kem_seed, kem_purpose(genesis_id)
    )
    return open_generation_keys(kem_private, grants, descriptors)
