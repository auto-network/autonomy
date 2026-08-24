"""``autonomy.vault.audited`` / ``autonomy.vault.secured`` — where a secret lives.

One destination for every secret the operator owns, split ONCE, by the only
distinction a reader must know before touching it: whether a human has to
participate to open it.

## Why two sets and not one, and not seven

``@vaulted`` declares the release rule on the SET -- "a value has one release
rule" -- so a set that mixed tiers could not exist. Two sets is therefore the
minimum, and the tier is the NAME rather than a field, because the question a
caller asks first is "will this ask the operator for something?" and that must
be answerable without opening anything.

Two is also the MAXIMUM. Before this, every credential brought its own schema:
Claude's OAuth tokens, Claude's setup tokens, Codex's tokens, the commit
signing key, and GitHub's tokens hiding inside ``autonomy.workspace``. Five
schemas, one job -- each added when its provider was, none of them differing
in home, band, or release rule. They are ROWS. A sixth schema for GitHub would
have been the fifth repetition of one decision.

## The payload is validated, THEN sealed

The schema describes the PLAINTEXT, which is not a contradiction with the row
holding ciphertext: ``upsert_by_key`` validates against the schema before
``_seal_vault_payload`` runs. That ordering is the point -- it is the last
moment anything can look at the value, so a malformed credential is refused
while it can still be read, rather than becoming ciphertext nobody can
inspect and nobody can explain.

## What a row is

The key names the credential: ``github.token``, ``claude.oauth``. That string
is the whole contract with everything that consumes it -- a workspace declares
``credential:<key>`` and never holds a value, so the declaration and the row
are the same string and neither can drift.

The payload IS the secret, because ``@vaulted`` encrypts the whole payload and
leaves the row holding an opaque scalar. Whatever can read the database file
learns the credential's row identity and nothing about what its value says.

## Why personal, and why raw

Personal because they are the operator's credentials and not any
organization's -- the failure this replaces was the operator's GitHub tokens
sitting in plaintext in Anchore's shared org store, readable by every session
that org could reach.

Banded ``raw`` so that is structural rather than habitual. A band is enforced
at write, at promote, AND at the federated read, each failing independently;
a call site remembering to pass ``state="raw"`` is a convention, and a
convention is one forgetful writer away from being false.

## What this replaces

``autonomy.vault.secret`` stored ``genesis_id`` + ``policy_class_id`` +
``required_policy`` + ``sealed_cek`` as explicit fields -- which is exactly
what a SECURED locator already carries. It was a hand-built version of the
decorator, so ``vault.secured`` is not a rename of it but the same idea
expressed once.

``autonomy.credential-file`` (a machine-scoped path to a plaintext file, which
cannot travel by construction) and ``autonomy.secure.setting`` (HPKE sealed to
the HOST key, so bound to a machine rather than to a person) were the stages
before a vault existed. The three-stage lifecycle was always a migration path,
not three permanent homes.
"""

from __future__ import annotations

from .registry import (
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    org_writeback_namespace,
    publication_band,
    vaulted,
)

VAULT_AUDITED_SET_ID = "autonomy.vault.audited"
VAULT_SECURED_SET_ID = "autonomy.vault.secured"
VAULT_CREDENTIAL_REVISION = 1


SYNOPSIS = {
    "summary": (
        "The operator's secrets, stored encrypted in the personal store and "
        "addressed by a stable credential name. Two sets, "
        "split only by whether opening one requires the human: audited "
        "releases to an authorized session unattended, secured additionally "
        "requires a factor."
    ),
    "nouns": [
        "credential", "secret", "token", "vault", "audited", "secured",
        "unattended release", "credential name",
    ],
    "related_set_ids": [
        "autonomy.vault.policy-class#1",
        "autonomy.workspace#2",
    ],
}


@home("personal")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="setting_name")
@vaulted("audited")
class VaultAuditedCredentialV1(SettingSchema):
    """A secret that releases to an authorized session with NO human present.

    This is what lets a workspace start unattended on a machine the operator
    is not sitting at. The audit trail is the control: nothing is hidden from
    the record, but nothing waits on a person either.

    """

    set_id = VAULT_AUDITED_SET_ID
    schema_revision = VAULT_CREDENTIAL_REVISION

    value: str = field(
        required=True,
        description=(
            "The secret itself. A single string, always: every consumer of a "
            "credential wants one value to put somewhere, and a credential "
            "with several parts is several rows under compound keys "
            "(claude.oauth.access, claude.oauth.refresh) rather than a "
            "structure each consumer has to learn to take apart."
        ),
    )


@home("personal")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="setting_name")
@org_writeback_namespace(suffix="credential_name")
@vaulted("secured")
class VaultSecuredCredentialV1(SettingSchema):
    """A secret whose release REQUIRES the operator's factor.

    Same storage, same addressing, one difference: opening it means satisfying
    a policy class, so no unattended process can. Use it for a secret whose
    every use should be a deliberate act -- and NOT for one an agent needs at
    launch, which would turn every session start into a prompt.
    """

    set_id = VAULT_SECURED_SET_ID
    schema_revision = VAULT_CREDENTIAL_REVISION

    value: str = field(
        required=True,
        description="The secret itself, as in the audited set.",
    )
