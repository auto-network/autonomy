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
    publication_band,
    SettingSchema,
    field,
    keyed_per_entity,
)


SECURE_SETTING_SET_ID = "autonomy.secure.setting"
SECURE_SETTING_REVISION = 1
#: Revision 2 — workspace-bound records: one ciphertext per allowed
#: workspace, purpose label reconstructed by the consumer, never stored.
SECURE_SETTING_V2_REVISION = 2


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


#: Never leaves the database that owns it: sealed secret material. Publication state
#: is the only control over a cross-organization read, so the band is
#: what makes 'promote this' unable to become a disclosure.
@publication_band(max="raw")
@keyed_per_entity(key_strategy="secret_name")
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


#: Never leaves the database that owns it: sealed secret material. Publication state
#: is the only control over a cross-organization read, so the band is
#: what makes 'promote this' unable to become a disclosure.
@publication_band(max="raw")
@keyed_per_entity(key_strategy="secret_name")
class SecureSettingV2(SettingSchema):
    """One sealed secret payload, bound to a workspace allowlist.

    Key: the requester-chosen ``target_key`` — one evolving row per key
    via ``upsert_by_key`` at this revision. Unlike revision 1, the HPKE
    purpose label is deliberately NOT stored: each allowed workspace has
    its own ciphertext sealed under
    ``autonomy.secure-setting.v2|<org>|<target_key>|<nonce>|workspace=<ws>``
    and the consumer (``tools/connectors/repl_login.py``) reconstructs
    that label from its OWN host-derived view of the caller's workspace.
    Editing ``workspaces``/``ciphertexts_hex`` cannot widen access — a
    record moved under a different workspace name simply fails to
    decrypt. Widening an allowlist therefore always means re-sealing the
    plaintext through a fresh operator approval ceremony.
    """

    set_id = SECURE_SETTING_SET_ID
    schema_revision = SECURE_SETTING_V2_REVISION

    ciphertexts_hex: dict = field(
        required=True,
        description=(
            "One HPKE wire record (suite byte || enc || ciphertext, "
            "lowercase hex) per allowed workspace, keyed by workspace id. "
            "Each opens only with the host recipient private key under the "
            "reconstructed v2 purpose label naming that same workspace."
        ),
    )
    workspaces: list = field(
        required=True,
        element=str,
        description=(
            "The workspace allowlist as shown to the operator at approval "
            "time — display/audit copy of the ciphertext map's keys. The "
            "consumer never trusts this field; the binding is the seal."
        ),
    )
    nonce: str = field(
        required=True,
        description=(
            "The single-use 64-hex approval nonce, needed to reconstruct "
            "the purpose label. Public once provisioned; it was already "
            "part of the stored purpose string in revision 1."
        ),
    )
    key_id: str = field(
        required=True,
        description=(
            "Fingerprint of the recipient public key the records were "
            "sealed to (first 16 hex of SHA-256 over the raw public key "
            "bytes)."
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
