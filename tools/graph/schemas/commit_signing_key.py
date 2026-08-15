"""``autonomy.commit.signing-key#1`` — the operator's passphrase-encrypted signing key.

Holds the armored, passphrase-encrypted OpenPGP private key the operator signs
commits with. The server only ever stores and serves the ENCRYPTED blob; the
passphrase is entered in the operator's browser, where the key is decrypted and
used to sign. Nothing here is usable without the passphrase.

Lives in the operator's own database, keyed by organization slug: it is the
operator's key, decrypted only with their passphrase and never shared outside
their own fleet, and they hold one per organization they sign for. Naming the
organization is how a reader gets the right one.

Being a personal secret it must never reach a read-through publication state --
pin to ``raw`` once the publication-band enforcement lands. Re-vaulting under
standard vault secrecy, dropping the bespoke PGP armor, is the successor:
bead auto-wu2al.

The ``autonomy.commit.*`` set_id is a legacy name; the home is personal.
"""

from .registry import (
    SettingSchema,
    field,
    home,
    keyed_per_entity,
)


SIGN_KEY_SET_ID = "autonomy.commit.signing-key"
SIGN_KEY_REVISION = 1


SYNOPSIS = {
    "summary": (
        "Org's GPG commit-signing key as an armored, passphrase-encrypted "
        "private key. The dashboard stores and serves only the encrypted blob; "
        "the passphrase and decrypted key never leave the operator's browser."
    ),
    "nouns": [
        "signing key", "gpg key", "commit signing key",
        "encrypted private key", "org signing identity",
    ],
    "related_set_ids": [
        "autonomy.commit.policy#1",
    ],
}


@home("personal")
@keyed_per_entity(key_strategy="org_slug")
class CommitSigningKeyV1(SettingSchema):
    """The operator's commit-signing key for one organization.

    Keyed by the organization's slug, so the key for an organization is
    found by naming it. The alternative — one row under a fixed label —
    cannot hold a second organization's key at all, and leaves a reader
    with nothing to ask for, so it has to identify the row it wants by
    looking INSIDE the stored values until one appears to be a private
    key. That is a scan over secrets standing in for a lookup, and it
    returns whichever row happens to come first.
    """

    set_id = SIGN_KEY_SET_ID
    schema_revision = SIGN_KEY_REVISION

    armored_private_key: str = field(
        required=True,
        description=(
            "The armored, passphrase-encrypted OpenPGP private key. Encrypted "
            "at rest; only ever decrypted in the operator's browser with the "
            "passphrase, which the server never sees."
        ),
    )
