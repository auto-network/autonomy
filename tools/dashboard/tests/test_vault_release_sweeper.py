"""The lease ledger closes honestly, and closes NOTHING it cannot answer for.

Reduced with the sweeper itself (2026-08-30): delivered secrets live in each
container's private mount namespace and die with the container, so there is
no host artifact to destroy and no destruction to test. What remains is
bookkeeping — rows must close when their session ends or disappears, must
never close on an ambiguous liveness answer, and legacy shared-root rows
get a best-effort unlink of the real path they recorded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.dashboard import vault_release_sweeper as sweeper
from tools.dashboard.dao import vault_releases


@pytest.fixture(autouse=True)
def _machine_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    (tmp_path / "orgs").mkdir(parents=True, exist_ok=True)


@pytest.fixture
def destroyed(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "agents.secret_ramfs.destroy_secret_file",
        lambda container, filename, **kw: calls.append((container, filename)),
    )
    return calls


def _record(id, session, *, host_path, expires_at=None, container_path="/run/secrets/cred", now=0):
    vault_releases.record_release(
        id=id, session=session, setting_name="cred",
        release_mode="delivered", expires_at=expires_at,
        container_path=container_path, host_path=host_path, now=now,
    )


def test_past_deadline_lease_destroys_the_exact_file_then_closes(destroyed):
    _record("r", "live", host_path="container-ns:live:/run/secrets/fleet-key",
            container_path="/run/secrets/fleet-key", expires_at=1000)
    out = sweeper.sweep(session_exists=lambda s: True, now=2000)
    assert out == {"destroyed": 1, "closed": 1}
    assert destroyed == [("live", "fleet-key")]   # by exact address, never a scan
    assert vault_releases.get("r")["shred_reason"] == "expired"


def test_gone_session_before_deadline_closes_as_orphaned_no_destroy(destroyed):
    _record("r", "dead", host_path="container-ns:dead:/run/secrets/cred",
            expires_at=10_000)
    out = sweeper.sweep(session_exists=lambda s: False, now=1)
    assert out == {"destroyed": 0, "closed": 1}
    assert destroyed == []   # file already freed with the container's mount
    assert vault_releases.get("r")["shred_reason"] == "orphaned"


def test_live_session_before_deadline_is_left_outstanding(destroyed):
    _record("r", "live", host_path="container-ns:live:/run/secrets/cred",
            expires_at=10_000)
    out = sweeper.sweep(session_exists=lambda s: True, now=1)
    assert out == {"destroyed": 0, "closed": 0}
    assert destroyed == []
    assert vault_releases.get("r")["shredded_at"] is None


def test_ambiguous_liveness_still_destroys_at_deadline_but_not_orphans(destroyed):
    """A failed probe (None) never drives an ORPHAN close, but a past-deadline
    credential is still destroyed — deadline needs no liveness."""
    _record("past", "s", host_path="container-ns:s:/run/secrets/k",
            container_path="/run/secrets/k", expires_at=1000)
    _record("fresh", "s2", host_path="container-ns:s2:/run/secrets/k",
            expires_at=10_000)
    out = sweeper.sweep(session_exists=None, now=2000)
    assert out == {"destroyed": 1, "closed": 1}
    assert destroyed == [("s", "k")]
    assert vault_releases.get("past")["shred_reason"] == "expired"
    assert vault_releases.get("fresh")["shredded_at"] is None


def test_session_end_closes_the_session_leases(destroyed):
    _record("r", "ending", host_path="container-ns:ending:/run/secrets/cred",
            expires_at=10_000)
    out = sweeper.on_session_end("ending", now=5)
    assert out["closed"] == 1
    assert destroyed == []   # file died with the container; ledger-only close
    assert vault_releases.get("r")["shred_reason"] == "session_end"


def test_legacy_shared_root_file_is_unlinked_at_deadline(monkeypatch, destroyed):
    """A past-deadline legacy lease unlinks its real host path (not nsenter)."""
    import pathlib as _pl
    unlinked = []
    monkeypatch.setattr(_pl.Path, "unlink",
                        lambda self, *a, **k: unlinked.append(str(self)))
    _record("r", "oldsess",
            host_path="/run/autonomy-secrets/oldsess/cred", expires_at=1000)
    sweeper.sweep(session_exists=lambda s: True, now=2000)
    assert unlinked == ["/run/autonomy-secrets/oldsess/cred"]
    assert destroyed == []   # legacy path uses unlink, not the container helper
    assert vault_releases.get("r")["shred_reason"] == "expired"

