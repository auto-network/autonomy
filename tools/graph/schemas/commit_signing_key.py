"""``autonomy.commit.signing-key#1`` — the operator's passphrase-encrypted signing key.

Holds the armored, passphrase-encrypted OpenPGP private key the operator signs
commits with. The server only ever stores and serves the ENCRYPTED blob; the
passphrase is entered in the operator's browser, where the key is decrypted and
used to sign. Nothing here is usable without the passphrase.

Authority: PERSONAL (auto-bsbaf). This is the operator's own key — decrypted only
with the operator's passphrase, used to sign the operator's commits, and never
shared outside the operator's own fleet. Per the settings scope/publication-state
rubric (graph://4d88c2ad-625, authority axis: "whose fact is this?"), a personal
secret lives in ``personal.db`` and is read/written pinned to ``personal`` (like
``dashboard.claude.credentials``), NOT in an org DB — where it would sit on that
org's cross-org read-through surface. The ``autonomy.commit.*`` set_id is a legacy
name; the home is personal. Being a personal secret it must never reach a
read-through publication_state — pin to ``raw`` once the schema publication-band
enforcement lands (tracked). Re-vaulting under standard vault secrecy, dropping
the bespoke PGP armor, is the successor: bead auto-wu2al.
"""

from .registry import (
    singleton,
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


@singleton(key="default")
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
