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


def test_toctou_ancestor_symlinked_into_mount_after_construction_is_caught(tmp_path):
    """Regression for Sonnet's D5-10 finding: isolation was checked only at
    __init__. Swap the store dir's ancestor to a symlink into the agent mount
    AFTER construction and the next put_secret must fail closed, not write the
    secret into the mount."""
    mount = tmp_path / "agent-mounts"
    mount.mkdir()
    real_parent = tmp_path / "real-parent"
    (real_parent / "creds").mkdir(parents=True)
    store = FileCredentialStore(
        store_dir=real_parent / "creds",
        agent_mount_roots=[mount],
        audit_sink=[].append,
    )
    store.put_secret("github", SECRET)  # fine while ancestor is real
    # Now swap the ancestor to a symlink pointing into the agent mount.
    inside_mount = mount / "creds"
    inside_mount.mkdir()
    (real_parent / "creds").rename(tmp_path / "creds-backup")
    (real_parent / "creds").symlink_to(inside_mount, target_is_directory=True)
    # The next write must be refused, and nothing must land in the mount.
    with pytest.raises(CredentialStoreLocationError):
        store.put_secret("github", "ghp_should_never_be_written_2222")
    assert not (inside_mount / "github.cred").exists()


def test_final_cred_file_cannot_be_a_symlink_and_target_is_untouched(tmp_path):
    """O_NOFOLLOW: a pre-planted symlink at the target path is not followed, and
    (Codex's D5-10 finding) the refused path must not chmod or write the symlink
    target either — the old finally: chmod-by-path followed the link."""
    store = _store(tmp_path, [])
    store.put_secret("github", SECRET)  # creates the dir
    victim = tmp_path / "victim.txt"
    victim.write_text("victim-contents")
    victim.chmod(0o644)
    evil = tmp_path / "broker-creds" / "evil.cred"
    evil.symlink_to(victim)
    with pytest.raises(OSError):
        store.put_secret("evil", "ghp_via_symlink_3333")
    # The symlink target's mode and contents must be completely untouched.
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644
    assert victim.read_text() == "victim-contents"
