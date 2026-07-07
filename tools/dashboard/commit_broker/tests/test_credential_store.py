"""D5-10 tests — 0600 mode, agent-mount isolation, audited reads, wrapped return."""

from __future__ import annotations

import stat

import pytest

from tools.dashboard.commit_broker.credentials import AuthorizedScope, Credential
from tools.dashboard.commit_broker.credential_store import (
    CredentialAuditRecord,
    CredentialStoreLocationError,
    FileCredentialStore,
)

SECRET = "ghp_STORE_SECRET_abcdef123456"


def _scope() -> AuthorizedScope:
    return AuthorizedScope.for_repos("op-7", ["auto-network/autonomy"])


def _store(tmp_path, audit):
    return FileCredentialStore(
        store_dir=tmp_path / "broker-creds",
        agent_mount_roots=[tmp_path / "agent-mounts"],
        audit_sink=audit.append,
        clock=lambda: 1234.0,
    )


def test_store_file_is_mode_0600(tmp_path):
    store = _store(tmp_path, [])
    store.put_secret("github", SECRET)
    path = tmp_path / "broker-creds" / "github.cred"
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_put_tightens_a_preexisting_loose_file(tmp_path):
    store = _store(tmp_path, [])
    path = tmp_path / "broker-creds" / "github.cred"
    store.put_secret("github", SECRET)  # dir now exists
    # widen it, then re-put; perms must come back to 0600
    path.chmod(0o644)
    store.put_secret("github", "ghp_rotated_secret_9999")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_store_inside_agent_mount_is_refused(tmp_path):
    mount = tmp_path / "agent-mounts"
    with pytest.raises(CredentialStoreLocationError):
        FileCredentialStore(
            store_dir=mount / "creds",  # INSIDE the agent mount
            agent_mount_roots=[mount],
            audit_sink=[].append,
        )


def test_each_read_appends_one_audit_record(tmp_path):
    audit: list[CredentialAuditRecord] = []
    store = _store(tmp_path, audit)
    store.put_secret("github", SECRET)
    store.get_real_credential("github", _scope())
    store.get_real_credential("github", _scope())
    assert len(audit) == 2
    rec = audit[0]
    assert rec.operator_id == "op-7"
    assert rec.provider == "github"
    assert rec.at == 1234.0
    assert rec.repos == ("auto-network/autonomy",)


def test_get_returns_a_redaction_wrapped_credential(tmp_path):
    store = _store(tmp_path, [])
    store.put_secret("github", SECRET)
    cred = store.get_real_credential("github", _scope())
    assert isinstance(cred, Credential)
    assert cred.reveal() == SECRET
    assert SECRET not in str(cred)


def test_missing_secret_raises_and_does_not_audit(tmp_path):
    audit: list[CredentialAuditRecord] = []
    store = _store(tmp_path, audit)
    with pytest.raises(KeyError):
        store.get_real_credential("github", _scope())
    assert audit == []  # no read happened, so no audit record


def test_provider_name_cannot_escape_the_store_dir(tmp_path):
    store = _store(tmp_path, [])
    for bad in ("../etc/passwd", "a/b", "", "."):
        with pytest.raises(ValueError):
            store.put_secret(bad, SECRET)
