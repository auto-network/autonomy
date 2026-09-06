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


def test_config_schema_carries_provider_and_bucket():
    from tools.graph.schemas.registry import validate_payload
    from tools.dashboard.plugins.backup.entrypoints import schemas as S
    validate_payload(S.CONFIG_SET_ID, S.SCHEMA_REVISION,
                     {"offsite_provider": "b2",
                      "offsite_bucket": "autonomy-backups"})
    with pytest.raises(Exception):
        validate_payload(S.CONFIG_SET_ID, S.SCHEMA_REVISION,
                         {"offsite_provider": "gdrive"})
