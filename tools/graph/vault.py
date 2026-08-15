"""Vault settings — a setting whose payload lives encrypted, not in the row.

An ordinary setting stores its payload as plain JSON in the organization's
database. That is correct for almost everything and wrong for a credential:
anything that can read the file can read the secret, and the file is readable
by design -- a host process can open it directly, and the repository is mounted
into containers read-only on purpose. Access control cannot fix that surface,
so the answer is that the bytes in the row are not the secret.

A vault setting therefore stores a REFERENCE. The payload is encrypted once as
a storage object under the organization's current key generation, and the row
keeps only what is needed to find it again: the object it belongs to, the
revision this row is, and the policy class that says which factors may open it.
Reading resolves the reference and unwraps; a reader without the keys gets an
access error, never plaintext.

Settings are a natural fit because they are already append-only: a change
writes a new row that supersedes the old one rather than editing it, which is
exactly what the storage contract requires of an object revision. So a setting
maps onto one object with many revisions, and the mapping is derived rather
than stored -- two nodes computing it from the same setting must agree without
coordinating.

Only settings explicitly marked as vault secrets take this path. Everything
else is untouched.
"""

from __future__ import annotations

import hashlib

from tools.network.idkit.canonical import canonical_json

#: The reference a vault setting stores in place of its payload.
VAULT_MARKER = "autonomy.vault.v1"

#: Domain separation for the derived identifiers below. Distinct strings, so an
#: object id can never collide with a revision id derived from the same setting.
_OBJECT_ID_DOMAIN = "autonomy/vault/object-id/v1"
_REVISION_ID_DOMAIN = "autonomy/vault/revision-id/v1"

#: Phase one implements the password class only. The passkey classes need
#: WebAuthn PRF, which carries its own review and is deliberately deferred --
#: naming them here would invite storing a secret under a class nothing can
#: currently open.
POLICY_CLASSES = frozenset({"password"})


class VaultError(Exception):
    """A vault setting could not be formed, referenced, or resolved."""


def object_id_for(genesis_id: str, set_id: str, key: str) -> str:
    """The object a setting's revisions all belong to.

    DERIVED, not stored: every revision of one setting must land on the same
    object, and two nodes must agree on which without asking each other. It is
    a function of the organization and the setting's identity, so it is stable
    across revisions and distinct across settings and across organizations.
    """
    return hashlib.sha256(
        canonical_json(
            {
                "domain": _OBJECT_ID_DOMAIN,
                "genesis_id": genesis_id,
                "set_id": set_id,
                "key": key,
            }
        )
    ).hexdigest()


def revision_id_for(genesis_id: str, setting_id: str) -> str:
    """The identity of ONE revision.

    Derived from the settings row's own id, which is already unique per row --
    so writing the same setting twice yields two revisions of one object, which
    is precisely what an append-only history should look like.
    """
    return hashlib.sha256(
        canonical_json(
            {
                "domain": _REVISION_ID_DOMAIN,
                "genesis_id": genesis_id,
                "setting_id": setting_id,
            }
        )
    ).hexdigest()


def build_reference(
    *,
    genesis_id: str,
    domain_id: str,
    object_id: str,
    revision_id: str,
    storage_state_id: str,
    policy_class: str,
) -> dict:
    """What the settings row holds instead of the payload.

    Everything here is a locator or a label. There is no key material and no
    ciphertext: the body lives in the content store, and a row that carried the
    ciphertext would put the secret back in the file this exists to keep it out
    of.
    """
    if policy_class not in POLICY_CLASSES:
        raise VaultError(
            f"unknown policy class {policy_class!r}; this build implements "
            f"{sorted(POLICY_CLASSES)}"
        )
    for name, value in (
        ("genesis_id", genesis_id),
        ("domain_id", domain_id),
        ("object_id", object_id),
        ("revision_id", revision_id),
        ("storage_state_id", storage_state_id),
    ):
        if not isinstance(value, str) or not value:
            raise VaultError(f"vault reference {name} must be a non-empty string")
    return {
        "vault": VAULT_MARKER,
        "genesis_id": genesis_id,
        "domain_id": domain_id,
        "object_id": object_id,
        "revision_id": revision_id,
        "storage_state_id": storage_state_id,
        "policy_class": policy_class,
    }


_REFERENCE_FIELDS = frozenset({
    "vault", "genesis_id", "domain_id", "object_id", "revision_id",
    "storage_state_id", "policy_class",
})


def is_vault_reference(payload) -> bool:
    """Whether a settings payload is a vault reference rather than a value."""
    return isinstance(payload, dict) and payload.get("vault") == VAULT_MARKER


def parse_reference(payload) -> dict:
    """Strictly read a vault reference back.

    Closed to an exact field set, like the armor body: a tolerated extra field
    would be somewhere to smuggle something into a row that resolution trusts.
    """
    if not is_vault_reference(payload):
        raise VaultError("this settings payload is not a vault reference")
    if set(payload) != _REFERENCE_FIELDS:
        raise VaultError(
            f"a vault reference must carry exactly {sorted(_REFERENCE_FIELDS)} — "
            f"got {sorted(payload)}"
        )
    if payload["policy_class"] not in POLICY_CLASSES:
        raise VaultError(
            f"unknown policy class {payload['policy_class']!r}; refusing to "
            "resolve a secret whose opening rule this build does not implement"
        )
    for name in ("genesis_id", "domain_id", "object_id", "revision_id",
                 "storage_state_id"):
        if not isinstance(payload[name], str) or not payload[name]:
            raise VaultError(f"vault reference {name} must be a non-empty string")
    return dict(payload)


# ── The round trip: a setting's payload, encrypted and back again ──────────


def seal_setting(
    *,
    author,
    frontier,
    setting_id: str,
    set_id: str,
    key: str,
    payload,
    held_secrets,
    available_states,
    ancestry,
    store,
    bridges=(),
    descriptors=None,
    policy_class: str = "password",
) -> dict:
    """Encrypt a setting's payload and return the reference the row keeps.

    The payload is encrypted ONCE, under a content key wrapped by the
    organization's current key generation, and persisted in the content store.
    What comes back carries no key material and no ciphertext, so the settings
    row never holds the secret in any form.
    """
    from tools.network.ledger.projections import organization_content_domain_id
    from tools.network.storagekit.objects import create_object
    from tools.network.storagekit.suites import BODY_SUITE_DEFAULT

    genesis_id = frontier.genesis_id
    domain_id = organization_content_domain_id(genesis_id)
    object_id = object_id_for(genesis_id, set_id, key)
    revision_id = revision_id_for(genesis_id, setting_id)

    header, body = create_object(
        author,
        domain_id,
        canonical_json(payload),
        frontier,
        held_secrets,
        available_states,
        ancestry=ancestry,
        bridges=bridges,
        descriptors=descriptors,
        object_id=object_id,
        revision_id=revision_id,
        body_suite_id=BODY_SUITE_DEFAULT,
    )
    store.put_object(header, body)
    return build_reference(
        genesis_id=genesis_id,
        domain_id=domain_id,
        object_id=object_id,
        revision_id=revision_id,
        storage_state_id=header.storage_state_id,
        policy_class=policy_class,
    )


def open_setting(reference, *, held_secrets, bridges, descriptors, store):
    """Resolve a vault reference back to the setting's payload.

    Raises :class:`VaultError` when the reference itself is unusable, and lets
    the storage layer's own refusals through untouched -- a reader who cannot
    reach the key must see that, not an empty value that could be mistaken for
    a setting that was never written.
    """
    import json

    ref = parse_reference(reference)
    from tools.network.storagekit.objects import read_object

    header, body = store.get_object(ref["object_id"], ref["revision_id"])
    plaintext = read_object(header, body, held_secrets, bridges, descriptors)
    return json.loads(plaintext)
