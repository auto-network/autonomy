"""``autonomy.vault.policy-class#1`` — one key-encryption key per access policy.

The persisted shape of a policy class (bead auto-39d26, crib §18). A class holds
one symmetric ``class_key`` and carries the per-factor *wraps* of it; a setting's
data key is sealed under the class, never to factors directly, so enrolling a
factor adds one wrap here and touches no secret. The ``class_key`` itself is
NEVER stored — only its wraps, each a sealed HPKE record openable by one factor.

Lives in the operator's own database (``@home("personal")``). This is the
human-factor tier: the wraps are sealed to a person's own factors (their
password, their passkey PRF), so they are the operator's fact even when the
class governs organization content — the class serves BOTH domains and is not
scoped to organizations (bead). Being secret material it must never reach a
read-through publication state; pin to ``raw`` once band enforcement lands, as
``autonomy.commit.signing-key`` does.

Keyed by ``class_id`` (a random 128-bit id): classes across every org and the
personal domain have distinct ids, so the id alone addresses one without naming
a domain. See ``tools/vault/policy_class.py`` for the construction and
``tools/vault/store.py`` for the runtime store; this schema is the settings-
surface contract of the same record.
"""

from __future__ import annotations

from .registry import (
    SettingSchema,
    field,
    home,
    keyed_per_entity,
)

VAULT_POLICY_CLASS_SET_ID = "autonomy.vault.policy-class"
VAULT_POLICY_CLASS_REVISION = 1


SYNOPSIS = {
    "summary": (
        "One key-encryption key per access policy. Holds the per-factor wraps "
        "of a symmetric class_key (never the key itself); a setting's data key "
        "seals under the class, so enrolling a factor adds one wrap and touches "
        "no secret. Personal-homed human-factor tier; serves both the personal "
        "and organization domains."
    ),
    "nouns": [
        "policy class", "key-encryption key", "kek", "factor wrap",
        "class key", "vault policy", "access policy", "password class",
    ],
    "related_set_ids": [
        "autonomy.vault.secured#1",
        "autonomy.secure.setting#1",
    ],
}


@home("personal")
@keyed_per_entity(key_strategy="class_id")
class VaultPolicyClassV1(SettingSchema):
    """One policy class: its policy, its factor wraps, and when it was minted.

    Key: the ``class_id``. The record evolves in place — enrolling a factor
    appends a wrap; revoking one re-mints the ``class_key`` sealed to the
    survivors, under the SAME ``class_id`` so settings keep naming it.
    """

    set_id = VAULT_POLICY_CLASS_SET_ID
    schema_revision = VAULT_POLICY_CLASS_REVISION

    policy: str = field(
        required=True,
        enum=["password", "prf", "both"],
        description=(
            "Which human factors the class demands. 'password' and 'prf' wrap "
            "the whole class_key to each factor (any one opens); 'both' is a "
            "2-of-2 XOR split needing one password and one passkey. Phase one "
            "ships 'password'."
        ),
    )
    generations: list = field(
        required=True,
        element={
            "gen_id": str,
            "wraps": list,
        },
        description=(
            "The key generations, oldest first; the last is current and is "
            "what new writes use. Revoking a factor APPENDS a generation sealed "
            "to the survivors; old generations are retained so survivors keep "
            "reading existing secrets (never a bulk re-wrap). Each generation: "
            "{gen_id, wraps[]}, where every wrap is {factor_id, factor_type "
            "(password|passkey), role (single, or a|b for a both split), the "
            "64-hex encapsulation public_key, and 'wrapped' — the hex idkit "
            "HPKE record sealing that generation's class_key (or share) to the "
            "factor}. No class_key is stored."
        ),
    )
    created_at: str = field(
        required=True,
        description="RFC 3339 timestamp the class (or its current generation) was minted.",
    )
