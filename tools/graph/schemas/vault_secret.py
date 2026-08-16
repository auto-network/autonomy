"""``autonomy.vault.secret#1`` — a settings-at-rest secret that NAMES its class.

The vault secret schema and its reference to its policy class (bead auto-39d26,
MODIFIES). A vault secret does not wrap its data key to factors; it seals its
content-encryption key (CEK) under a *policy class* (``autonomy.vault.policy-
class#1``) and stores the reference. Opening the data key means opening the
named class — which means satisfying that class's policy. Enrolling a factor
changes the class, not this row, so this ciphertext is stable across enrollment.

``policy_class_id`` is the reference; ``required_policy`` is the policy this
setting demands, checked equal to the class's policy at seal and open time so a
setting cannot be wrapped under a class with a weaker factor set than it
requires. ``genesis_id`` and the setting name (the row key) are bound into the
seal's associated data, so a ``sealed_cek`` lifted to another setting or genesis
does not verify.

Personal-homed, ``raw`` — it is secret material (crib rubric: all secrets live
in the operator's own store and never read through). This differs from
``autonomy.secure.setting`` (an HPKE ciphertext sealed straight to the host
recipient key): a vault secret's data key is class-mediated, which is what lets
factor enrollment be O(classes), not O(secrets).
"""

from __future__ import annotations

from .registry import (
    SettingSchema,
    field,
    home,
    keyed_per_entity,
)

VAULT_SECRET_SET_ID = "autonomy.vault.secret"
VAULT_SECRET_REVISION = 1


SYNOPSIS = {
    "summary": (
        "A settings-at-rest secret whose data key is sealed under a policy "
        "class rather than wrapped to factors directly. Stores the reference "
        "to its class (policy_class_id) and the policy it requires, plus the "
        "sealed CEK. Enrolling a factor never rewrites this row."
    ),
    "nouns": [
        "vault secret", "settings-at-rest", "sealed cek", "data key",
        "policy class reference", "secured setting",
    ],
    "related_set_ids": [
        "autonomy.vault.policy-class#1",
    ],
}


@home("personal")
@keyed_per_entity(key_strategy="setting_name")
class VaultSecretV1(SettingSchema):
    """One vault secret: its sealed data key and its class reference.

    Key: the ``setting_name`` (bound into the seal AAD). The row names its
    class and the policy it requires; the ``sealed_cek`` opens only through
    that class at that policy.
    """

    set_id = VAULT_SECRET_SET_ID
    schema_revision = VAULT_SECRET_REVISION

    genesis_id: str = field(
        required=True,
        description=(
            "The genesis this secret belongs to, bound into the sealed_cek "
            "AAD. In the organization domain it is the object's genesis; in "
            "the personal domain a stable per-store identifier."
        ),
    )
    policy_class_id: str = field(
        required=True,
        references="autonomy.vault.policy-class",
        description=(
            "The class whose class_key seals this secret's CEK. The data key "
            "opens ONLY through this class — satisfying the class's policy."
        ),
    )
    required_policy: str = field(
        required=True,
        enum=["password", "prf", "both"],
        description=(
            "The policy this setting demands. Checked equal to the named "
            "class's policy at seal and open time, so a setting cannot be "
            "sealed under a class with a weaker factor set than it requires."
        ),
    )
    sealed_cek: dict = field(
        required=True,
        description=(
            "The content-encryption key sealed under the class_key: an AEAD "
            "record {suite_id, nonce (hex), ciphertext (hex)} whose AAD binds "
            "genesis_id, class_id, setting_name and policy."
        ),
    )
