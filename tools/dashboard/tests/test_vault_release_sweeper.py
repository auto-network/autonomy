"""Durable secret-release records, deadline destruction, restart reconciliation.

Acceptance for auto-pw9bs.5 (design ``graph://0c206bd8-1c6`` §4.2). The
sweeper never enters a container — it unlinks host paths — so these tests
drive it against a temporary "delivery root" standing in for the host
ramfs, with ``session_exists`` injected. The bead warns that a fake clock
or process stub can satisfy the literal criteria, so the restart case runs
reconciliation in a REAL fresh python subprocess whose only state is the
on-disk record store, and the container-gone case removes the file's
directory entirely rather than mocking a docker call.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tools.dashboard import vault_release_sweeper as sweeper
from tools.dashboard.dao import vault_releases


@pytest.fixture
def store(tmp_path):
    return tmp_path / "vault_releases.db"


@pytest.fixture
def delivery_root(tmp_path):
    root = tmp_path / "run" / "autonomy-secrets"
    root.mkdir(parents=True)
    return root


def _deliver(root: Path, store, *, id, session, expires_at, now, body=b"s3cr3t"):
    """Model the delivery layer's contract: RECORD FIRST, then materialise
    the file. Returns the host path written."""
    session_dir = root / session
    session_dir.mkdir(parents=True, exist_ok=True)
    host_path = session_dir / f"{id}.secret"
    vault_releases.record_release(
        id=id, session=session, setting_name="dashboard.github.token",
        release_mode="delivered", expires_at=expires_at,
        container_path=f"/run/secrets/{id}.secret", host_path=str(host_path),
        path=store, now=now,
    )
    host_path.write_bytes(body)  # delivery materialises only after the record
    return host_path


# ── The store's ordering invariant ──────────────────────────────────────

def test_only_delivered_mode_is_recorded(store):
    for mode in ("mediated", "client_operation", "open"):
        with pytest.raises(ValueError, match="delivered"):
            vault_releases.record_release(
                id="x", session="s", setting_name="n", release_mode=mode,
                expires_at=1, container_path="/c", host_path="/h", path=store,
            )


def test_a_duplicate_release_id_is_refused(store):
    vault_releases.record_release(
        id="dup", session="s", setting_name="n", release_mode="delivered",
        expires_at=1, container_path="/c", host_path="/h", path=store, now=0,
    )
    with pytest.raises(vault_releases.VaultReleaseStoreError, match="already"):
        vault_releases.record_release(
            id="dup", session="s2", setting_name="n2",
            release_mode="delivered", expires_at=2, container_path="/c2",
            host_path="/h2", path=store, now=0,
        )


def test_shred_is_idempotent_and_keeps_the_first_reason(store):
    vault_releases.record_release(
        id="r", session="s", setting_name="n", release_mode="delivered",
        expires_at=1, container_path="/c", host_path="/h", path=store, now=0,
    )
    assert vault_releases.mark_shredded("r", reason="expired", path=store, now=10)
    # A re-sweep must not rewrite when or why the secret died.
    assert not vault_releases.mark_shredded("r", reason="orphaned", path=store,
                                            now=99)
    rec = vault_releases.get("r", path=store)
    assert rec["shredded_at"] == 10 and rec["shred_reason"] == "expired"


# ── The five ordering points: a crash never leaves a file with no record ──

@pytest.mark.parametrize(
    "point",
    ["before_record", "after_record_before_file", "after_file", "mid_unlink"],
)
def test_a_crash_at_any_owned_point_leaves_no_undestroyed_secret(
    point, store, delivery_root
):
    """The store and the sweeper own the record commit and the unlink; the
    sealed-response/notice/decision points belong to the delivery bead. At
    each point this side owns, the outcome is either no artifact or a
    recorded artifact the sweep destroys — never a file with no record."""
    live = lambda s: True
    sid, session = "rel1", "sess-A"
    host_path = delivery_root / session / f"{sid}.secret"

    if point == "before_record":
        # Crash before anything: no record, no file. Nothing to leak.
        assert vault_releases.get(sid, path=store) is None
        assert not host_path.exists()
        return

    if point == "after_record_before_file":
        # The invariant's whole point: a record with NO file. The sweep
        # marks it shredded (a missing file is success), leaking nothing.
        (delivery_root / session).mkdir(parents=True)
        vault_releases.record_release(
            id=sid, session=session, setting_name="n",
            release_mode="delivered", expires_at=100,
            container_path="/c", host_path=str(host_path), path=store, now=0,
        )
        assert not host_path.exists()
        sweeper.sweep(session_exists=live, delivery_root=delivery_root,
                      store_path=store, now=200)  # past deadline
        assert vault_releases.get(sid, path=store)["shred_reason"] == "expired"
        return

    # after_file / mid_unlink both start from a fully delivered release.
    _deliver(delivery_root, store, id=sid, session=session, expires_at=100,
             now=0)
    assert host_path.exists()
    if point == "mid_unlink":
        # A crash mid-unlink is indistinguishable from "already unlinked":
        # unlink is missing_ok, so a re-sweep completes the transition.
        host_path.unlink()
    sweeper.sweep(session_exists=live, delivery_root=delivery_root,
                  store_path=store, now=200)
    assert not host_path.exists()
    assert vault_releases.get(sid, path=store)["shredded_at"] is not None


# ── Deadline, session end, orphan, container-gone ───────────────────────

def test_a_release_is_destroyed_at_its_deadline(store, delivery_root):
    host = _deliver(delivery_root, store, id="r", session="s",
                    expires_at=1000, now=0)
    live = lambda s: True
    # Before the deadline: left in place, the session is still entitled.
    sweeper.sweep(session_exists=live, delivery_root=delivery_root,
                  store_path=store, now=500)
    assert host.exists()
    assert vault_releases.get("r", path=store)["shredded_at"] is None
    # At/after the deadline: destroyed.
    sweeper.sweep(session_exists=live, delivery_root=delivery_root,
                  store_path=store, now=1000)
    assert not host.exists()
    assert vault_releases.get("r", path=store)["shred_reason"] == "expired"


def test_session_end_reclaims_the_whole_subdirectory(store, delivery_root):
    """At session end the session no longer exists, so its entire subdir —
    every release under it, still-valid or not — is reclaimed at once."""
    _deliver(delivery_root, store, id="a", session="ended", expires_at=9999,
             now=0)
    _deliver(delivery_root, store, id="b", session="ended", expires_at=9999,
             now=0)
    session_dir = delivery_root / "ended"
    assert session_dir.is_dir()
    sweeper.sweep(session_exists=lambda s: False, delivery_root=delivery_root,
                  store_path=store, now=1)
    assert not session_dir.exists()
    for rid in ("a", "b"):
        assert vault_releases.get(rid, path=store)["shred_reason"] == "orphaned"


def test_a_release_whose_container_has_exited_is_still_destroyed(
    store, delivery_root
):
    """The container is gone entirely — its directory removed out from under
    the sweeper — and the release is still marked destroyed. The sweeper
    never depended on entering the container."""
    _deliver(delivery_root, store, id="r", session="dead", expires_at=1,
             now=0)
    import shutil
    shutil.rmtree(delivery_root / "dead")  # container/dir removed
    sweeper.sweep(session_exists=lambda s: False, delivery_root=delivery_root,
                  store_path=store, now=100)
    assert vault_releases.get("r", path=store)["shred_reason"] == "orphaned"


def test_crash_residue_directory_with_no_record_is_reclaimed(
    store, delivery_root
):
    """A file with no record should never occur under the record-first
    contract; if it does (or a whole dir does, from a crash), the
    session-dir GC still reclaims it once the session is gone — the safety
    net under the invariant."""
    stray = delivery_root / "ghost-session"
    stray.mkdir()
    (stray / "orphan.secret").write_bytes(b"leaked")
    sweeper.sweep(session_exists=lambda s: False, delivery_root=delivery_root,
                  store_path=store, now=1)
    assert not stray.exists()


def test_a_live_session_within_deadline_is_left_untouched(store, delivery_root):
    host = _deliver(delivery_root, store, id="r", session="live",
                    expires_at=10_000, now=0)
    sweeper.sweep(session_exists=lambda s: True, delivery_root=delivery_root,
                  store_path=store, now=1)
    assert host.exists()
    assert (delivery_root / "live").is_dir()
    assert vault_releases.get("r", path=store)["shredded_at"] is None


# ── The real restart: reconciliation from a cold process ────────────────

def test_reconciliation_destroys_an_overdue_release_from_a_fresh_process(
    store, delivery_root
):
    """The durable record is the ONLY state that survives a restart. Prove
    it by running reconciliation in a brand-new python process whose only
    input is the on-disk store and the leftover ramfs file — no live
    dashboard, no shared memory. A release past its deadline is destroyed
    and recorded as reconciled; a still-valid live one is left."""
    overdue = _deliver(delivery_root, store, id="old", session="s1",
                       expires_at=100, now=0)
    valid = _deliver(delivery_root, store, id="new", session="s2",
                     expires_at=10_000_000, now=0)
    assert overdue.exists() and valid.exists()

    script = textwrap.dedent(f"""
        from tools.dashboard import vault_release_sweeper as sw
        # s1 has ended (gone); s2 is still live. Only the durable store and
        # the files on disk are available to this cold process.
        live = {{"s2"}}
        out = sw.reconcile_on_startup(
            session_exists=lambda s: s in live,
            delivery_root={str(delivery_root)!r},
            store_path={str(store)!r},
            now=5000,
        )
        print(out["shredded"])
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[3]),
    )
    assert result.returncode == 0, result.stderr
    # s1's overdue release destroyed (gone session -> orphaned, and overdue);
    # its file and s2's are both handled correctly.
    assert not overdue.exists(), "overdue release survived the restart"
    rec_old = vault_releases.get("old", path=store)
    assert rec_old["shredded_at"] is not None
    # s2 is live and within deadline: left for the resumed periodic sweep.
    assert valid.exists(), "a still-valid live release was destroyed on restart"
    assert vault_releases.get("new", path=store)["shredded_at"] is None


def test_reconciliation_marks_an_overdue_live_release_reconciled_not_expired(
    store, delivery_root
):
    """The audit distinguishes a deadline caught on the way back up from one
    caught by the live sweeper."""
    _deliver(delivery_root, store, id="r", session="s", expires_at=100, now=0)
    sweeper.reconcile_on_startup(session_exists=lambda s: True,
                                 delivery_root=delivery_root, store_path=store,
                                 now=500)
    assert vault_releases.get("r", path=store)["shred_reason"] == "reconciled"


# ── Launcher optimisation vs sweeper backstop (crypto ruling 2026-08-19) ──
#
# The design (graph://83c92d72-0ed QA5) makes the SWEEPER the correctness
# guarantee — a kill -9 at the unlink boundary must still destroy the secret
# by its deadline plus one sweep interval — and demotes the launcher's
# per-session unlink to a memory-reclamation optimisation nothing may depend
# on. So the two actors must compose: whichever fires first wins, the second
# is inert, and a file already gone when the sweeper arrives is ordinary
# success, never an error.

def test_on_session_end_marks_session_end_and_reclaims_the_subdir(
    store, delivery_root
):
    _deliver(delivery_root, store, id="a", session="s", expires_at=9999, now=0)
    _deliver(delivery_root, store, id="b", session="s", expires_at=9999, now=0)
    out = sweeper.on_session_end("s", delivery_root=delivery_root,
                                 store_path=store, now=5)
    assert out == {"shredded": 2, "reclaimed_dir": True}
    assert not (delivery_root / "s").exists()
    for rid in ("a", "b"):
        assert vault_releases.get(rid, path=store)["shred_reason"] == "session_end"


def test_launcher_optimisation_then_sweeper_is_an_ordinary_no_op(
    store, delivery_root
):
    """Launcher unlinks the subdir first (the optimisation); the sweeper
    later finds the ledger rows already shredded and the files already gone
    — a success path, not a reconciliation failure."""
    _deliver(delivery_root, store, id="r", session="s", expires_at=1, now=0)
    sweeper.on_session_end("s", delivery_root=delivery_root, store_path=store,
                           now=5)
    # Sweeper runs afterward on the same (now gone) session: nothing to do.
    result = sweeper.sweep(session_exists=lambda s: False,
                           delivery_root=delivery_root, store_path=store, now=99)
    assert result == {"shredded": 0, "reclaimed_dirs": 0}
    assert vault_releases.get("r", path=store)["shred_reason"] == "session_end"


def test_sweeper_first_then_on_session_end_is_inert(store, delivery_root):
    """The reverse race: the sweeper reclaims a gone session's directory as
    'orphaned', then a late launcher teardown call is a harmless no-op that
    does not rewrite the reason."""
    _deliver(delivery_root, store, id="r", session="s", expires_at=1, now=0)
    sweeper.sweep(session_exists=lambda s: False, delivery_root=delivery_root,
                  store_path=store, now=99)
    assert vault_releases.get("r", path=store)["shred_reason"] == "orphaned"
    out = sweeper.on_session_end("s", delivery_root=delivery_root,
                                 store_path=store, now=200)
    assert out == {"shredded": 0, "reclaimed_dir": False}
    # The first destruction stands; the reason is not rewritten.
    assert vault_releases.get("r", path=store)["shred_reason"] == "orphaned"
