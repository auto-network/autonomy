"""Offsite backup credentials — vault-released, never file-plaintext.

Operator ruling (auto-uy896, 2026-09-06): credentials live in the VAULT,
audited tier — released unattended while the vault is warm, and "we
don't need to be running backups if we're not live: what's being
written?" — so a cold vault yields ``vault-cold`` and callers record a
skip, never an alarm.

The rows (personal store, ``autonomy.vault.audited``, one string value
per row — the vault_credential contract):

- ``backup.restic-password``     → RESTIC_PASSWORD
- ``backup.b2-key-id``           → B2_KEY_ID
- ``backup.b2-application-key``  → B2_APPLICATION_KEY

Provider and bucket are configuration, not secrets: they come from
``backup.config`` (offsite_provider / offsite_bucket) and ride the same
environment the deprecated agents/backup.env used, so
tools/graph/backup-env.sh consumes vault-released credentials without
knowing the vault exists.

Seal them once (from wherever the values live today):

    graph vault seal backup.restic-password    --tier audited --prompt
    graph vault seal backup.b2-key-id          --tier audited --prompt
    graph vault seal backup.b2-application-key --tier audited --prompt
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: vault row key -> environment variable backup-env.sh consumes.
CREDENTIAL_ENV = {
    "backup.restic-password": "RESTIC_PASSWORD",
    "backup.b2-key-id": "B2_KEY_ID",
    "backup.b2-application-key": "B2_APPLICATION_KEY",
}

#: offsite_env() statuses. Distinct on purpose: "cold" is the operator's
#: not-live state (record a skip, stay quiet); "unsealed" means the
#: credentials were never put in the vault (surface it — offsite is
#: configured but cannot ever run); "disabled"/"unconfigured" are plain
#: config states.
STATUS_OK = "ok"
STATUS_VAULT_COLD = "vault-cold"
STATUS_UNSEALED = "unsealed"
STATUS_DISABLED = "disabled"
STATUS_UNCONFIGURED = "unconfigured"


def offsite_env(config: dict | None = None) -> tuple[dict | None, str]:
    """(environment for the capture/drill subprocess, status).

    The environment is complete (provider + bucket + all three secrets)
    or ``None`` — a partial credential set must not reach restic, where
    it would fail with a misleading provider error.
    """
    from tools.graph import settings_ops
    from tools.graph.schemas.vault_credential import VAULT_AUDITED_SET_ID

    if config is None:
        from tools.dashboard.plugins.backup.entrypoints.api import _read_config
        config = _read_config()
    if not config.get("offsite_enabled", True):
        return None, STATUS_DISABLED
    provider = (config.get("offsite_provider") or "").strip()
    bucket = (config.get("offsite_bucket") or "").strip()
    if not provider or not bucket:
        return None, STATUS_UNCONFIGURED
    try:
        if not settings_ops.personal_delegate_audited_is_warm():
            return None, STATUS_VAULT_COLD
    except Exception:
        logger.exception("audited-warm probe failed; treating as cold")
        return None, STATUS_VAULT_COLD
    env = {"BACKUP_PROVIDER": provider, "BACKUP_BUCKET": bucket}
    for key, variable in CREDENTIAL_ENV.items():
        try:
            row = settings_ops.read_set_key(
                VAULT_AUDITED_SET_ID, key, org=None, peers=[])
        except Exception:
            logger.exception("vault read failed for %s", key)
            return None, STATUS_VAULT_COLD
        if row is None:
            return None, STATUS_UNSEALED
        if row.get("vault_error") is not None:
            return None, STATUS_VAULT_COLD
        payload = row.get("payload") or {}
        value = payload.get("value")
        if not value:
            return None, STATUS_UNSEALED
        if variable == "RESTIC_PASSWORD":
            # The row was sealed from the legacy password FILE, whose
            # trailing newline restic's --password-file reader strips
            # (first line only) — but the RESTIC_PASSWORD environment
            # variable is used VERBATIM, so releasing the raw bytes
            # would fail every repo unlock. Match restic's own file
            # semantics exactly.
            value = value.splitlines()[0] if value.splitlines() else ""
            if not value:
                return None, STATUS_UNSEALED
        else:
            value = value.rstrip("\r\n")
        env[variable] = value
    return env, STATUS_OK
