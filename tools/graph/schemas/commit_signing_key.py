"""``autonomy.commit.signing-key#1`` — the org's passphrase-encrypted signing key.

Holds the armored, passphrase-encrypted OpenPGP private key the operator signs
commits with. The server only ever stores and serves the ENCRYPTED blob; the
passphrase is entered in the operator's browser, where the key is decrypted and
used to sign. Nothing here is usable without the passphrase.
"""

from .registry import (
    SettingSchema,
    field,
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


@keyed_per_entity
class CommitSigningKeyV1(SettingSchema):
    """The org's commit-signing key.

    Key: an operator-chosen label (e.g. ``default``) — one org may hold more
    than one key. Payload: the armored, passphrase-encrypted private key.
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
