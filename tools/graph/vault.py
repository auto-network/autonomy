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

import base64
import hashlib
import json

from tools.network.idkit.canonical import canonical_json

#: Version tag opening every locator. The locator is an OPAQUE SCALAR, so this
#: is a prefix on a string rather than a field in an object -- see below for why
#: that distinction is load-bearing rather than stylistic.
LOCATOR_PREFIX = "autonomy.vault.v1."

#: Domain separation for the derived identifiers below. Distinct strings, so an
#: object id can never collide with a revision id derived from the same setting.
_OBJECT_ID_DOMAIN = "autonomy/vault/object-id/v1"
_REVISION_ID_DOMAIN = "autonomy/vault/revision-id/v1"

#: TIER (design of record §4.1) — WHO MUST PARTICIPATE. Distinct from the policy
#: class, which is the human-factor key and a separate axis entirely (§18,
#: auto-39d26). Conflating them is how a setting ends up labelled as
#: password-protected while nothing enforces a password.
#:
#:   audited  — released by the session, authenticated. The boundary is
#:              ACCOUNTABILITY, NOT CONFIDENTIALITY: any authorized session may
#:              have the value, and the system always knows which asked.
#:   secured  — released by a human, cryptographically. NOT IMPLEMENTED HERE.
#:
#: `secured` requires the second wrapping layer: the content key wrapped under a
#: policy class key, that wrapped material forming the object body beneath the
#: storage state (§9.2). The nesting is what makes a cached state secret
#: insufficient BY CONSTRUCTION rather than by a check code could omit. That
#: machinery is auto-39d26 and is not built, so `secured` is REFUSED rather than
#: accepted and silently downgraded to what this actually provides.
TIERS = frozenset({"audited"})


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


_LOCATOR_FIELDS = frozenset({
    "genesis_id", "domain_id", "object_id", "revision_id", "storage_state_id",
    "tier",
})


def build_locator(
    *,
    genesis_id: str,
    domain_id: str,
    object_id: str,
    revision_id: str,
    storage_state_id: str,
    tier: str = "audited",
) -> str:
    """The OPAQUE SCALAR a vault setting stores in place of its payload.

    A scalar, and that is a correctness requirement rather than a preference.
    Settings resolution merges candidate rows with RFC 7386 json_merge_patch
    BEFORE anything is decrypted, and merge-patch RECURSES INTO OBJECTS. A
    locator shaped as an object would therefore be merged field by field, so a
    partial override could yield one carrying the object id from one write and
    the revision id from another -- addressing an object that was never
    written, and deriving a wrap key for it. As a scalar it can only ever be
    replaced whole, which is the behaviour the merge step must have.

    The tier travels INSIDE for the same reason: a sibling field could be
    merged in from another row, and a tier that can be overridden separately
    from the thing it describes is a downgrade waiting to happen.

    Everything in here is a locator or a label. No key material, no ciphertext:
    the body lives in the content store, and a row carrying the ciphertext
    would put the secret back in the file this exists to keep it out of.
    """
    if tier not in TIERS:
        raise VaultError(
            f"unknown or unimplemented tier {tier!r}; this build provides "
            f"{sorted(TIERS)}. `secured` needs the policy-class wrap (auto-39d26), "
            "which is not built -- accepting it here would promise a human "
            "factor that nothing enforces"
        )
    for name, value in (
        ("genesis_id", genesis_id),
        ("domain_id", domain_id),
        ("object_id", object_id),
        ("revision_id", revision_id),
        ("storage_state_id", storage_state_id),
    ):
        if not isinstance(value, str) or not value:
            raise VaultError(f"vault locator {name} must be a non-empty string")
    body = canonical_json(
        {
            "genesis_id": genesis_id,
            "domain_id": domain_id,
            "object_id": object_id,
            "revision_id": revision_id,
            "storage_state_id": storage_state_id,
            "tier": tier,
        }
    )
    return LOCATOR_PREFIX + base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")


def is_vault_locator(payload) -> bool:
    """Whether a settings payload is a vault locator rather than a value."""
    return isinstance(payload, str) and payload.startswith(LOCATOR_PREFIX)


def parse_locator(payload) -> dict:
    """Strictly read a vault locator back.

    Closed to an exact field set, like the armor body: a tolerated extra field
    would be somewhere to smuggle something into a row that resolution trusts.
    """
    if not is_vault_locator(payload):
        raise VaultError("this settings payload is not a vault locator")
    encoded = payload[len(LOCATOR_PREFIX):]
    padding = "=" * (-len(encoded) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(encoded + padding))
    except Exception as exc:
        raise VaultError(f"vault locator does not decode: {exc}") from exc
    if not isinstance(data, dict) or set(data) != _LOCATOR_FIELDS:
        raise VaultError(
            f"a vault locator must carry exactly {sorted(_LOCATOR_FIELDS)} — "
            f"got {sorted(data) if isinstance(data, dict) else type(data).__name__}"
        )
    if data["tier"] not in TIERS:
        raise VaultError(
            f"unknown or unimplemented tier {data['tier']!r}; refusing to "
            "resolve a secret whose release rule this build does not implement"
        )
    for name in ("genesis_id", "domain_id", "object_id", "revision_id",
                 "storage_state_id"):
        if not isinstance(data[name], str) or not data[name]:
            raise VaultError(f"vault locator {name} must be a non-empty string")
    return data


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
    tier: str = "audited",
) -> str:
    """Encrypt a setting's payload and return the OPAQUE LOCATOR the row keeps.

    The payload is encrypted ONCE, under a content key wrapped by the
    organization's current key generation, and persisted in the content store.
    What comes back is a scalar carrying no key material and no ciphertext, so
    the settings row never holds the secret in any form.

    TIER: `audited` only. The value is released to any authorized session, so
    the boundary this provides is accountability rather than confidentiality.
    A `secured` setting -- one a human must open -- needs the policy-class wrap
    that is not built here (auto-39d26), and is refused rather than quietly
    downgraded to this.
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
    return build_locator(
        genesis_id=genesis_id,
        domain_id=domain_id,
        object_id=object_id,
        revision_id=revision_id,
        storage_state_id=header.storage_state_id,
        tier=tier,
    )


def open_setting(locator, *, held_secrets, bridges, descriptors, store):
    """Resolve a vault locator back to the setting's payload.

    Raises :class:`VaultError` when the locator itself is unusable, and lets
    the storage layer's own refusals through untouched -- a reader who cannot
    reach the key must see that, not an empty value that could be mistaken for
    a setting that was never written.
    """
    ref = parse_locator(locator)
    from tools.network.storagekit.objects import read_object

    header, body = store.get_object(ref["object_id"], ref["revision_id"])
    plaintext = read_object(header, body, held_secrets, bridges, descriptors)
    return json.loads(plaintext)
