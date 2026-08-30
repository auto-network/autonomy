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


def _record(id, session, *, host_path, expires_at=None, now=0):
    vault_releases.record_release(
        id=id, session=session, setting_name="cred",
        release_mode="delivered", expires_at=expires_at,
        container_path="/run/secrets/cred", host_path=host_path, now=now,
    )


def test_gone_session_lease_closes_as_orphaned():
    _record("r", "dead", host_path="container-ns:dead:/run/secrets/cred")
    out = sweeper.sweep(session_exists=lambda s: False, now=1)
    assert out["closed"] == 1
    assert vault_releases.get("r")["shred_reason"] == "orphaned"


def test_live_session_lease_is_left_outstanding():
    _record("r", "live", host_path="container-ns:live:/run/secrets/cred")
    out = sweeper.sweep(session_exists=lambda s: True, now=1)
    assert out["closed"] == 0
    assert vault_releases.get("r")["shredded_at"] is None


def test_ambiguous_liveness_closes_nothing():
    """A failed probe (None) must never drive a transition — the row waits."""
    _record("r", "s", host_path="container-ns:s:/run/secrets/cred")
    out = sweeper.sweep(session_exists=None, now=1)
    assert out["closed"] == 0
    assert vault_releases.get("r")["shredded_at"] is None


def test_session_end_closes_the_session_leases():
    _record("r", "ending", host_path="container-ns:ending:/run/secrets/cred")
    out = sweeper.on_session_end("ending", now=5)
    assert out["closed"] == 1
    assert vault_releases.get("r")["shred_reason"] == "session_end"


def test_legacy_shared_root_file_is_unlinked_on_close(monkeypatch):
    """A lease whose host_path is under the retired shared root gets a
    best-effort unlink when it closes."""
    unlinked = []
    real_unlink = Path.unlink

    def spy_unlink(self, *a, **k):
        unlinked.append(str(self))

    monkeypatch.setattr(sweeper.Path, "unlink", spy_unlink)
    _record("r", "oldsess",
            host_path="/run/autonomy-secrets/oldsess/cred")
    sweeper.sweep(session_exists=lambda s: False, now=1)
    assert unlinked == ["/run/autonomy-secrets/oldsess/cred"]
    assert vault_releases.get("r")["shred_reason"] == "orphaned"


def test_container_ns_locator_is_never_touched_as_a_path(monkeypatch):
    """A current-design locator ('container-ns:...') is not a path; the
    sweeper must not try to unlink it."""
    unlinked = []
    monkeypatch.setattr(
        sweeper.Path, "unlink",
        lambda self, *a, **k: unlinked.append(str(self)),
    )
    _record("r", "dead", host_path="container-ns:dead:/run/secrets/cred")
    sweeper.sweep(session_exists=lambda s: False, now=1)
    assert unlinked == []
