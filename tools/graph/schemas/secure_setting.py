"""``autonomy.secure.setting#1`` — HPKE-sealed secret material, ciphertext only.

Each row holds one secret payload (e.g. a connector's login credentials)
sealed in the operator's browser to the host's X25519 recipient key
(``tools/dashboard/secure_setting_keys.py``). The graph — and every
dashboard code path — stores and serves ONLY the ciphertext; opening it
requires the mode-0600 private key file on the host
(``REPL_LOGIN_KEY_FILE``) plus the row's exact ``purpose`` string, via
:func:`tools.network.idkit.seal_open`.
"""

from .registry import (
    SettingSchema,
    field,
    keyed_per_entity,
)


SECURE_SETTING_SET_ID = "autonomy.secure.setting"
SECURE_SETTING_REVISION = 1


SYNOPSIS = {
    "summary": (
        "Operator-provisioned secrets as HPKE ciphertext sealed in the "
        "browser to the host recipient key. The dashboard never holds the "
        "plaintext; decryption happens host-side with the REPL_LOGIN_KEY_FILE "
        "private key and the row's purpose string."
    ),
    "nouns": [
        "secure setting", "sealed credential", "provisioned secret",
        "connector login", "hpke ciphertext",
    ],
    "related_set_ids": [],
}


@keyed_per_entity
class SecureSettingV1(SettingSchema):
    """One sealed secret payload.

    Key: the requester-chosen ``target_key`` (e.g.
    ``connector.eversource.login``) — one evolving row per key via
    ``upsert_by_key``, so re-provisioning replaces in place.
    """

    set_id = SECURE_SETTING_SET_ID
    schema_revision = SECURE_SETTING_REVISION

    ciphertext_hex: str = field(
        required=True,
        description=(
            "The HPKE wire record (suite byte || enc || ciphertext) as "
            "lowercase hex, sealed in the operator's browser. Opens only "
            "with the host recipient private key and this row's purpose."
        ),
    )
    key_id: str = field(
        required=True,
        description=(
            "Fingerprint of the recipient public key the record was sealed "
            "to (first 16 hex of SHA-256 over the raw public key bytes) — "
            "lets a consumer detect a rotated host key before failing to open."
        ),
    )
    purpose: str = field(
        required=True,
        description=(
            "The exact HPKE purpose label bound into the seal "
            "(autonomy.secure-setting.v1|<org>|<target_key>|<nonce>). "
            "Required verbatim to open the record."
        ),
    )
    origin: str = field(
        required=True,
        description=(
            "What the secret authenticates against (e.g. the service "
            "hostname) — display/audit metadata, not part of the seal."
        ),
    )
    title: str = field(
        default="",
        description="Human title the operator saw when approving.",
    )
    description: str = field(
        default="",
        description="Requester-supplied description shown at approval time.",
    )
    payload_keys: list = field(
        default_factory=list,
        element=str,
        description=(
            "The dict keys present in the sealed JSON payload, in form "
            "order — the payload's shape without its values."
        ),
    )
    provisioned_at: float = field(
        required=True,
        description="Unix time the approval executor stored this row.",
    )
    approval_id: str = field(
        default="",
        description="The approval-rendezvous request id that produced this row.",
    )
    requested_by_session: str = field(
        default="",
        description="The agent session that requested provisioning.",
    )
