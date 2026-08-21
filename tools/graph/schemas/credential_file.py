"""Schema: ``autonomy.credential-file#1``.

Where, on THIS machine, the file holding an organization's credential for a
service host sits. The path only; the credential itself is never in a Setting.

Lives in the machine store because a filesystem location is true on one
computer and false on the next. Carried to a second machine of the same
operator it names a file that is not there and is believed rather than
probed, so the step that would have said "provision this" is skipped and the
failure surfaces somewhere else entirely.

It is also why this is not org configuration. The path was previously carried
on the organization's capability-install row, which replicates to every
member: one operator's home directory layout, published to everyone, where it
is wrong for all of them. What remains on that row -- which service instance
the organization uses, and as whom -- is genuinely the organization's and
stays there.

Composite key ``<org>:<host>``: the machine store holds several
organizations, and two of them can hold different credentials for the same
host. The key matches the sealed credential's
(``autonomy.secure.setting``), so the same address names the same
credential in either, and a value can move between them without being
renamed.

The two are the same value at different stages. This row locates plaintext on
one machine and cannot leave it; the sealed row carries ciphertext and
travels with the operator. This is the form that works before a credential is
sealed, and the form a host-side caller can read without an approval round.

Spec: graph://0d3f750f-f9c (Setting Primitive).
"""

from __future__ import annotations

from .registry import (
    RemediationRef,
    publication_band,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
)


SET_ID = "autonomy.credential-file"
SCHEMA_REVISION = 1


SYNOPSIS = {
    "summary": (
        "Operator-local path to the file holding one organization's "
        "credential for one service host. Path only, never the secret."
    ),
    "nouns": [
        "credential file", "token file", "host credential",
        "operator-local secret path",
    ],
    "related_set_ids": [
        "autonomy.secure.setting#1",
        "autonomy.org.capability.install#1",
    ],
}


@home("machine")
#: Never leaves the database that owns it: the location of a credential on this machine. Publication state
#: is the only control over a cross-organization read, so the band is
#: what makes 'promote this' unable to become a disclosure.
@publication_band(max="raw")
@keyed_per_entity(
    key_strategy="org_slug:host",
    key_references={"org_slug": "autonomy.org"},
)
class CredentialFileV1(SettingSchema):
    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    path: str = field(
        required=True,
        exists="file",
        exists_frame="platform-host",
        remediation=RemediationRef("workspace.declared-path.v1"),
        description=(
            "Absolute path on this machine to the file holding the "
            "credential. Read at the moment it is used and never retained; "
            "the organization and host are the Setting key."
        ),
    )
