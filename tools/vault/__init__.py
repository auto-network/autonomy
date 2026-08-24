"""Vault: per-policy key-encryption classes for settings-at-rest.

One key-encryption key per access policy (crib §18, bead auto-39d26): a
setting's data key wraps under its *policy class*, and the class carries the
per-factor wraps. Enrolling a factor adds one wrap per class, never touching a
single secret. Phase one ships the ``password`` policy; ``prf`` / ``both``
depend on the WebAuthn PRF library (out of epic) but are constructible here for
the mandated cross-model attack.

See ``tools/vault/TOOL.md``.
"""

from __future__ import annotations

from .errors import (
    ClassOpenError,
    FactorError,
    PolicyClassError,
    PolicyMismatchError,
    VaultError,
)
from .factors import (
    PASSKEY,
    PASSWORD,
    PublishedFactor,
    create_passkey_factor,
    create_password_factor,
    open_password_seed,
    random_seed,
)
from .policy_class import (
    BOTH_POLICY,
    PASSWORD_POLICY,
    POLICIES,
    PRF_POLICY,
    Generation,
    PolicyClassRecord,
    Wrap,
    create_class,
    create_root_reachable_class,
    enable_public_sealing,
    extend_class,
    open_cek,
    open_class,
    revoke_factor,
    seal_cek,
)
from .root_anchor import (
    ROOT_ANCHOR_WRAP_PURPOSE,
    RootAnchorRecord,
    create_root_anchor,
    open_root_anchor,
)
from .recipients import (
    ORGANIZATION_PERSONA_RECIPIENT,
    PERSONAL_ROOT_RECIPIENT,
    PublishedRecipient,
    recipient_keypair_from_seed,
)

__all__ = [
    "VaultError",
    "PolicyClassError",
    "PolicyMismatchError",
    "ClassOpenError",
    "FactorError",
    "PublishedFactor",
    "PASSWORD",
    "PASSKEY",
    "create_password_factor",
    "open_password_seed",
    "create_passkey_factor",
    "random_seed",
    "PolicyClassRecord",
    "Generation",
    "Wrap",
    "POLICIES",
    "PASSWORD_POLICY",
    "PRF_POLICY",
    "BOTH_POLICY",
    "create_class",
    "create_root_reachable_class",
    "enable_public_sealing",
    "open_class",
    "extend_class",
    "revoke_factor",
    "seal_cek",
    "open_cek",
    "RootAnchorRecord",
    "ROOT_ANCHOR_WRAP_PURPOSE",
    "create_root_anchor",
    "open_root_anchor",
    "PublishedRecipient",
    "PERSONAL_ROOT_RECIPIENT",
    "ORGANIZATION_PERSONA_RECIPIENT",
    "recipient_keypair_from_seed",
]
