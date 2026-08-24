"""``autonomy.vault.policy-class#1`` — one key-encryption key per access policy.

The persisted shape of a policy class (bead auto-39d26, crib §18). A class holds
one factor-wrapped secret that derives an X25519 sealing keypair. A setting's
data key is sealed to the published class key, never to factors directly, so
enrolling a factor adds one wrap here and touches no secret. The class secret
and private sealing key are NEVER stored.

Lives in the operator's own database (``@home("personal")``). This is the
human-factor tier: the wraps are sealed to a person's own factors (their
password, their passkey PRF), so they are the operator's fact even when the
class governs organization content — the class serves BOTH domains and is not
scoped to organizations (bead). Being secret material it must never reach a
read-through publication state; pin to ``raw`` once band enforcement lands, as
``autonomy.commit.signing-key`` does.

Keyed by ``class_id`` (a random 128-bit id): classes across every org and the
personal domain have distinct ids, so the id alone addresses one without naming
a domain. See ``tools/vault/policy_class.py`` for the construction. Each
generation publishes an X25519 ``sealing_public_key``. Writers use
that public key without a factor; the factor-wrapped class secret derives its
private half only during open. ``tools/vault/store.py`` is the runtime store;
this schema is the Settings-surface contract of the same record.
"""

from __future__ import annotations

from .registry import (
    publication_band,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
)

VAULT_POLICY_CLASS_SET_ID = "autonomy.vault.policy-class"
VAULT_POLICY_CLASS_REVISION = 1


SYNOPSIS = {
    "summary": (
        "One key-encryption class per access policy. Holds factor wraps of a "
        "private class secret plus its public sealing key. Writes use only the "
        "public key; reads require the factor. Enrolling adds one class wrap "
        "and touches no secret."
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


#: Pinned. The record carries the sealed wraps of a class key: useless without
#: a factor seed, and still nobody else's business. The band is what stops the
#: row reaching a peer-visible state AND what stops peer databases being opened
#: for this set at all, since the two are the same declaration.
@home("personal")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="class_id")
class VaultPolicyClassV1(SettingSchema):
    """One policy class: its policy, its factor wraps, and when it was minted.

    Key: the ``class_id``. The record evolves in place — enrolling a factor
    appends a wrap; revoking one re-mints the class secret sealed to the
    survivors, under the SAME ``class_id`` so settings keep naming it.
    """

    set_id = VAULT_POLICY_CLASS_SET_ID
    schema_revision = VAULT_POLICY_CLASS_REVISION

    policy: str = field(
        required=True,
        enum=["password", "prf", "both"],
        description=(
            "Which human factors the class demands. 'password' and 'prf' wrap "
            "the whole class secret to each factor (any one opens); 'both' is a "
            "2-of-2 XOR split needing one password and one passkey. Phase one "
            "ships 'password'."
        ),
    )
    generations: list = field(
        required=True,
        element={
            "gen_id": str,
            "wraps": list,
            "sealing_public_key": str,
        },
        description=(
            "The key generations, oldest first; the last is current and is "
            "what new writes use. Revoking a factor APPENDS a generation sealed "
            "to the survivors; old generations are retained so survivors keep "
            "reading existing secrets (never a bulk re-wrap). Each generation: "
            "{gen_id, sealing_public_key, wraps[]}. The 64-hex X25519 public "
            "key permits unattended CEK sealing; its private key derives from "
            "the factor-wrapped class secret only during open. Every wrap is "
            "{factor_id, factor_type "
            "(password|passkey), role (single, or a|b for a both split), the "
            "64-hex encapsulation public_key, and 'wrapped' — the hex idkit "
            "HPKE record sealing that generation's class secret (or share) "
            "to the factor}. No class secret is stored."
        ),
    )
    created_at: str = field(
        required=True,
        description="RFC 3339 timestamp the class (or its current generation) was minted.",
    )
