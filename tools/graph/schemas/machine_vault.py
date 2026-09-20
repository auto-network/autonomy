"""``autonomy.machine.vault.audited#1`` — audited-tier secrets that are true on
ONE machine and never replicate.

Design of record: graph://67d0aa5f-885 (approved by the operator 2026-09-20).

The two vault tiers that existed before this set are personal-homed, so a
row written there reaches every machine in the operator's fleet. Some
secrets are about one computer only: the fleet runtime credential the
browser hands THIS node at sign-on, and THIS machine's serving delegate key
for each organization it serves. Replicating them is the wrong home; keeping
them in a hand-carried ramfs file or a mode-0600 disk file is the wrong
carrier. This set is the right one on both counts.

What vaulting buys, in the operator's words: nothing about warmth. The ramfs
carries the vault's own warmth across a hot reload, so everything in the
audited tier is warm across a hot reload by the same carrier, and after a
reboot both are cold until a sign-on or the headless warm client. Vaulting
replaces a per-credential hand-carried file with the one carrier that
already exists. Standardization, nothing more.

Sealing: exactly the personal AUDITED path. The per-revision content key is
sealed COLD to the operator's published audited delegate recipient (the
write needs no warm vault), and the delegate's private half, warm after
unlock, opens it unattended on read. The ciphertext lives inline in the
machine store. The organization sealer and its key generations are never
consulted (``settings_ops._seal_vault_payload``).

One value per row, as in ``autonomy.vault.audited``: a credential with
several parts is one canonical JSON string here when it must be replayed
atomically (the runtime credential), or several rows under compound keys
when its parts are consumed separately.
"""
from __future__ import annotations

from .registry import (
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    publication_band,
    vaulted,
)

MACHINE_VAULT_AUDITED_SET_ID = "autonomy.machine.vault.audited"
MACHINE_VAULT_AUDITED_REVISION = 1

#: Row names this set is known to carry (the design of record, section 4).
RUNTIME_CREDENTIAL_KEY = "fleet-runtime"
SERVING_KEY_PREFIX = "serving-key."

SYNOPSIS = {
    "summary": (
        "Audited-tier secrets true on this machine only: the fleet runtime "
        "credential and the per-organization serving delegate keys. Sealed "
        "cold to the operator's audited delegate recipient, opened "
        "unattended once the vault is warm, never replicated."
    ),
    "nouns": [
        "machine vault", "runtime credential", "serving key",
        "serving delegate key", "machine-homed secret", "audited",
    ],
    "related_set_ids": [
        "autonomy.vault.audited#1",
        "autonomy.vault.policy-class#1",
    ],
}


@home("machine")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="setting_name")
@vaulted("audited")
class MachineVaultAuditedSecretV1(SettingSchema):
    """One secret that exists on this machine only and releases to this
    machine's own processes with no human present once the vault is warm."""

    set_id = MACHINE_VAULT_AUDITED_SET_ID
    schema_revision = MACHINE_VAULT_AUDITED_REVISION

    value: str = field(
        required=True,
        description=(
            "The secret itself, one string. A structured credential that must "
            "be replayed whole (the fleet runtime credential) is one canonical "
            "JSON string; parts consumed separately are separate rows."
        ),
    )
