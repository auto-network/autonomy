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


def status(config: dict | None = None) -> str:
    """The credential status alone, for the /backup page. Reads the
    vault (decryption, seconds of crypto): call off the event loop."""
    return offsite_env(config)[1]


def offsite_env(config: dict | None = None) -> tuple[dict | None, str]:
    """(environment for the capture/drill subprocess, status).

    The environment is complete (provider + bucket + all three secrets)
    or ``None`` — a partial credential set must not reach restic, where
    it would fail with a misleading provider error.

    The sealed reads decrypt, which costs seconds: never call this on
    the event loop.
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


# ── release for the host's cron run (auto-5gdao) ────────────────────────────
#
# The capture run is a host cron job: a process that cannot open the vault.
# So the dashboard, which can, RELEASES the credentials into the host's
# ramfs key cache -- the same carrier the serving connector's key uses
# (link_serving_supervisor._release_serving_key) -- and
# tools/graph/backup-env.sh reads only those files. The values are never on
# disk, and a reboot (which empties the ramfs) leaves offsite backup skipped
# with `vault-cold` until the vault is unlocked again: "we don't need to be
# running backups if we're not live".

#: Subdirectory of the key cache the cron run reads.
RELEASE_SUBDIR = "backup"
#: vault row key -> released file name (one value per file).
RELEASE_FILES = {
    "backup.restic-password": "restic-password",
    "backup.b2-key-id": "b2-key-id",
    "backup.b2-application-key": "b2-application-key",
}
#: Non-secret configuration, released beside them so backup-env.sh needs no
#: other source: provider and bucket.
RELEASE_CONFIG_FILE = "offsite.env"


def release_dir():
    import os
    from pathlib import Path

    from agents.secret_ramfs import KEYCACHE_MOUNT

    return Path(os.environ.get("AUTONOMY_KEYCACHE_MOUNT") or KEYCACHE_MOUNT) / RELEASE_SUBDIR


def _write_released(directory, name: str, data: bytes) -> None:
    import os

    tmp = directory / f".{name}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)
    os.replace(tmp, directory / name)


def _clear_released(directory) -> None:
    for name in (*RELEASE_FILES.values(), RELEASE_CONFIG_FILE):
        try:
            (directory / name).unlink()
        except FileNotFoundError:
            pass


def release_offsite(config: dict | None = None, *, directory=None,
                    memory_check=None) -> str:
    """Release the offsite credentials for the host cron run; the status.

    ``ok``: every file written (0600, temp-then-rename, on ramfs only).
    ``vault-cold``: nothing to read now -- files already released are kept
    (they are still the right values; a reboot is what empties them).
    ``disabled`` / ``unconfigured`` / ``unsealed``: offsite cannot run with
    what is there, so any released files are removed. Never raises; never
    logs a value. Decrypts: call off the event loop."""
    try:
        env, status = offsite_env(config)
        directory = release_dir() if directory is None else directory
        if status == STATUS_VAULT_COLD:
            return status
        if env is None:
            if directory.exists():
                _clear_released(directory)
            return status
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if memory_check is None:
            from tools.network.storagekit.memory_cache import assert_memory_backed

            memory_check = assert_memory_backed
        memory_check(directory)          # ramfs only: never tmpfs, never disk
        for key, name in RELEASE_FILES.items():
            _write_released(directory, name, env[CREDENTIAL_ENV[key]].encode("utf-8"))
        _write_released(directory, RELEASE_CONFIG_FILE, (
            f"BACKUP_PROVIDER={env['BACKUP_PROVIDER']}\n"
            f"BACKUP_BUCKET={env['BACKUP_BUCKET']}\n").encode("utf-8"))
        logger.info("backup: offsite credentials released for the host run")
        return status
    except Exception:
        logger.exception("backup: offsite credential release failed")
        return "release-failed"


def release_offsite_in_background(config: dict | None = None) -> None:
    """Run :func:`release_offsite` on a daemon thread (it decrypts)."""
    import threading

    threading.Thread(target=release_offsite, args=(config,),
                     name="backup-credential-release", daemon=True).start()
