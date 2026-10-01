"""Vault-released offsite credentials (auto-uy896).

Pins the operator's rulings: secrets come only from the audited vault
(complete set or nothing — a partial environment must never reach
restic), a cold vault is a quiet skip state distinct from
never-sealed, and configuration (provider/bucket) stays out of the
secret rows.
"""
from __future__ import annotations

import pytest

from tools.dashboard.plugins.backup import credentials as C
from tools.graph.schemas.vault_credential import VAULT_AUDITED_SET_ID

CONFIG = {"offsite_enabled": True, "offsite_provider": "b2",
          "offsite_bucket": "autonomy-backups"}


@pytest.fixture
def vault(monkeypatch):
    state = {"warm": True, "rows": {
        "backup.restic-password": {"payload": {"value": "pw"}},
        "backup.b2-key-id": {"payload": {"value": "kid"}},
        "backup.b2-application-key": {"payload": {"value": "appkey"}},
    }}

    import tools.graph.settings_ops as settings_ops
    monkeypatch.setattr(settings_ops, "personal_delegate_audited_is_warm",
                        lambda: state["warm"])

    def read_set_key(set_id, key, *, org, peers=None):
        assert set_id == VAULT_AUDITED_SET_ID and org is None
        return state["rows"].get(key)

    monkeypatch.setattr(settings_ops, "read_set_key", read_set_key)
    return state


def test_warm_vault_yields_complete_environment(vault):
    env, status = C.offsite_env(CONFIG)
    assert status == C.STATUS_OK
    assert env == {
        "BACKUP_PROVIDER": "b2", "BACKUP_BUCKET": "autonomy-backups",
        "RESTIC_PASSWORD": "pw", "B2_KEY_ID": "kid",
        "B2_APPLICATION_KEY": "appkey",
    }


def test_cold_vault_is_a_quiet_skip(vault):
    vault["warm"] = False
    env, status = C.offsite_env(CONFIG)
    assert env is None and status == C.STATUS_VAULT_COLD


def test_vault_error_row_reads_as_cold(vault):
    vault["rows"]["backup.b2-key-id"] = {
        "payload": None, "vault_error": {"code": "no_key_holder"}}
    env, status = C.offsite_env(CONFIG)
    assert env is None and status == C.STATUS_VAULT_COLD


def test_missing_row_is_unsealed_never_partial(vault):
    del vault["rows"]["backup.b2-application-key"]
    env, status = C.offsite_env(CONFIG)
    assert env is None and status == C.STATUS_UNSEALED


def test_disabled_and_unconfigured_short_circuit(vault):
    env, status = C.offsite_env({**CONFIG, "offsite_enabled": False})
    assert env is None and status == C.STATUS_DISABLED
    env, status = C.offsite_env({**CONFIG, "offsite_bucket": ""})
    assert env is None and status == C.STATUS_UNCONFIGURED
    # Neither state touched the vault warm probe? (Both return before
    # secrets; the fixture would have served them, so assert intent via
    # the cold path still returning the config statuses.)
    vault["warm"] = False
    env, status = C.offsite_env({**CONFIG, "offsite_enabled": False})
    assert status == C.STATUS_DISABLED


def test_restic_password_matches_password_file_semantics(vault):
    """The row was sealed from agents/.restic.pw byte-for-byte, trailing
    newline included; restic's env variable is verbatim while its file
    reader takes the first line — release must match the file reader."""
    vault["rows"]["backup.restic-password"] = {
        "payload": {"value": "the-real-password\n"}}
    vault["rows"]["backup.b2-key-id"] = {"payload": {"value": "kid\n"}}
    env, status = C.offsite_env(CONFIG)
    assert status == C.STATUS_OK
    assert env["RESTIC_PASSWORD"] == "the-real-password"
    assert env["B2_KEY_ID"] == "kid"


def test_newline_only_password_is_unsealed(vault):
    vault["rows"]["backup.restic-password"] = {"payload": {"value": "\n"}}
    env, status = C.offsite_env(CONFIG)
    assert env is None and status == C.STATUS_UNSEALED


def test_status_reads_the_vault_each_time(vault):
    assert C.status(CONFIG) == C.STATUS_OK
    vault["warm"] = False
    assert C.status(CONFIG) == C.STATUS_VAULT_COLD


def test_config_schema_carries_provider_and_bucket():
    from tools.graph.schemas.registry import validate_payload
    from tools.dashboard.plugins.backup.entrypoints import schemas as S
    validate_payload(S.CONFIG_SET_ID, S.SCHEMA_REVISION,
                     {"offsite_provider": "b2",
                      "offsite_bucket": "autonomy-backups"})
    with pytest.raises(Exception):
        validate_payload(S.CONFIG_SET_ID, S.SCHEMA_REVISION,
                         {"offsite_provider": "gdrive"})


# ── release for the host cron run (auto-5gdao) ─────────────────────────────

ENV = {"BACKUP_PROVIDER": "b2", "BACKUP_BUCKET": "bkt",
       "RESTIC_PASSWORD": "pw", "B2_KEY_ID": "kid", "B2_APPLICATION_KEY": "akey"}


def _release(monkeypatch, tmp_path, result, checked=None):
    from tools.dashboard.plugins.backup import credentials as c

    monkeypatch.setattr(c, "offsite_env", lambda config=None: result)
    return c.release_offsite(directory=tmp_path / "backup",
                             memory_check=(checked.append if checked is not None
                                           else (lambda d: None)))


def test_release_writes_each_value_0600_for_the_cron_run(monkeypatch, tmp_path):
    import stat

    checked = []
    assert _release(monkeypatch, tmp_path, (ENV, "ok"), checked) == "ok"
    d = tmp_path / "backup"
    assert checked == [d]                       # ramfs check before any write
    assert (d / "restic-password").read_text() == "pw"
    assert (d / "b2-key-id").read_text() == "kid"
    assert (d / "b2-application-key").read_text() == "akey"
    assert (d / "offsite.env").read_text() == "BACKUP_PROVIDER=b2\nBACKUP_BUCKET=bkt\n"
    for f in d.iterdir():
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert not list(d.glob(".*.tmp"))


def test_a_cold_vault_keeps_what_was_released(monkeypatch, tmp_path):
    _release(monkeypatch, tmp_path, (ENV, "ok"))
    assert _release(monkeypatch, tmp_path, (None, "vault-cold")) == "vault-cold"
    assert (tmp_path / "backup" / "restic-password").exists()


@pytest.mark.parametrize("status", ["disabled", "unconfigured", "unsealed"])
def test_credentials_that_cannot_run_remove_the_released_files(monkeypatch, tmp_path, status):
    _release(monkeypatch, tmp_path, (ENV, "ok"))
    assert _release(monkeypatch, tmp_path, (None, status)) == status
    assert sorted(p.name for p in (tmp_path / "backup").iterdir()) == []


def test_a_release_that_cannot_reach_ramfs_writes_nothing(monkeypatch, tmp_path):
    from tools.dashboard.plugins.backup import credentials as c

    monkeypatch.setattr(c, "offsite_env", lambda config=None: (ENV, "ok"))

    def not_ramfs(directory):
        raise RuntimeError("tmpfs swaps to disk")

    assert c.release_offsite(directory=tmp_path / "backup",
                             memory_check=not_ramfs) == "release-failed"
    assert not (tmp_path / "backup" / "restic-password").exists()


def test_unlock_schedules_the_release(monkeypatch):
    from tools.dashboard import unlock_routes
    from tools.dashboard.plugins.backup import credentials as c

    scheduled = []
    monkeypatch.setattr(c, "release_offsite_in_background",
                        lambda config=None: scheduled.append(config))
    unlock_routes._schedule_vault_releases()
    assert scheduled == [None]
