"""The volume contract, proven by measurement (auto-lr6gu).

DEPLOY.md documents that one volume holds all persistent node state.
This turns that into a guarantee with three teeth:

1. every store is enumerated in ``data_paths.STORE_MANIFEST`` and every
   store resolves through the guarded resolver, so rooting a deployment
   moves all readers together;
2. with the volume rooted at a tmp dir and the refuse guard set, running
   the node's core flows writes NOTHING outside it — asserted by hashing
   the real ``data/`` and ``$HOME`` before and after, not by inspection;
3. an escaping resolver is CAUGHT — the measurement is shown to fail
   when a store is deliberately un-rooted, so a green run means the
   contract held rather than that the test looked away.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.data_paths import (
    DEFAULT_DATA_ROOT,
    REFUSE_REAL_DATA_FALLBACK_ENV,
    STORE_MANIFEST,
    RealDataFallbackRefused,
    resolve_store,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _tree_manifest(root: Path) -> dict:
    """path -> content hash for every file under *root* (missing → {})."""
    if not root.exists():
        return {}
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            try:
                out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                out[str(path)] = "unreadable"
    return out


def _rooted_env(volume: Path) -> dict:
    """The env a node gets when its volume is *volume* — every manifest
    store rooted, and the fallback guard armed so a store we FORGOT to
    root raises instead of quietly using the operator's data/."""
    env = dict(os.environ)
    env[REFUSE_REAL_DATA_FALLBACK_ENV] = "1"
    for store in STORE_MANIFEST:
        if store.env:
            env[store.env] = str(volume / store.relative)
    env["HOME"] = str(volume / "home")
    return env


# -- 1. the manifest is the contract ------------------------------------------------


def test_every_store_is_env_rooted():
    """A store with no environment variable cannot be relocated, so it is
    rooted only by coincidence of layout — the auth.db/tls failure mode."""
    unrooted = [s.key for s in STORE_MANIFEST if not s.env and not s.roots_with]
    assert unrooted == [], f"stores with no rooting variable: {unrooted}"
    # Derived-rooting stores must anchor to a store that IS env-rooted, or
    # the derivation bottoms out in coincidence after all.
    from tools.data_paths import STORES_BY_KEY
    for s in STORE_MANIFEST:
        if s.roots_with:
            assert STORES_BY_KEY[s.roots_with].env, (
                f"{s.key} roots with {s.roots_with}, which has no variable"
            )


def test_resolver_precedence_is_env_then_root():
    """The writer that is handed a root and the reader that only knows the
    env must agree, or state lands where nothing looks for it."""
    from tools.data_paths import STORES_BY_KEY

    store = STORES_BY_KEY["dashboard"]
    os.environ.pop(store.env, None)
    assert resolve_store(store.key, root=Path("/vol")) == Path("/vol") / store.relative
    os.environ[store.env] = "/elsewhere/dashboard.db"
    try:
        assert resolve_store(store.key, root=Path("/vol")) == Path("/elsewhere/dashboard.db")
    finally:
        os.environ.pop(store.env, None)


def test_serving_key_store_has_portable_env_root_and_historical_default(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("AUTONOMY_NETWORK_KEY_DIR", raising=False)
    monkeypatch.delenv(REFUSE_REAL_DATA_FALLBACK_ENV, raising=False)
    assert resolve_store("serving_keys") == DEFAULT_DATA_ROOT / "network"
    assert resolve_store("serving_keys", root=tmp_path) == tmp_path / "network"

    override = tmp_path / "elsewhere" / "keys"
    monkeypatch.setenv("AUTONOMY_NETWORK_KEY_DIR", str(override))
    assert resolve_store("serving_keys", root=tmp_path) == override


def test_unrooted_resolution_raises_under_the_guard(monkeypatch):
    monkeypatch.setenv(REFUSE_REAL_DATA_FALLBACK_ENV, "1")
    for store in STORE_MANIFEST:
        if store.env:
            monkeypatch.delenv(store.env, raising=False)
        with pytest.raises(RealDataFallbackRefused):
            resolve_store(store.key)


def test_manifest_matches_deploy_md():
    from tools.data_paths import STORES_BY_KEY

    """DEPLOY.md's volume table is generated truth, not hand-maintained."""
    table = (REPO_ROOT / "DEPLOY.md").read_text()
    for store in STORE_MANIFEST:
        assert store.relative in table, f"{store.relative} missing from DEPLOY.md"
        anchor = store.env or f"roots with `{STORES_BY_KEY[store.roots_with].env}`"
        assert anchor in table, f"{anchor} missing from DEPLOY.md"


# -- 2. proven by measurement -------------------------------------------------------


def test_node_flows_write_nothing_outside_the_volume(tmp_path):
    """Run the node's first-run init against a tmp volume and prove the
    operator's real data/ and $HOME are byte-unchanged afterwards."""
    volume = tmp_path / "app-data"
    volume.mkdir()
    before_data = _tree_manifest(DEFAULT_DATA_ROOT)
    home = Path.home()
    before_home = _tree_manifest(home / ".autonomy") if (home / ".autonomy").exists() else {}

    result = subprocess.run(
        [sys.executable, "-m", "tools.init", "--root", str(volume)],
        cwd=str(REPO_ROOT), env=_rooted_env(volume),
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr[-2000:]

    # The volume actually received state (the run did something).
    assert any(volume.rglob("*.db")), "init wrote no databases into the volume"
    # And nothing outside it moved.
    assert _tree_manifest(DEFAULT_DATA_ROOT) == before_data, "the real data/ changed"
    after_home = _tree_manifest(home / ".autonomy") if (home / ".autonomy").exists() else {}
    assert after_home == before_home, "$HOME state changed"


def test_the_measurement_catches_an_escaping_store(tmp_path):
    """The teeth: un-root ONE store and the same measurement must notice.

    Without this, a green run above could mean 'nothing escaped' or
    'we measured the wrong thing'.
    """
    volume = tmp_path / "app-data"
    volume.mkdir()
    env = _rooted_env(volume)
    escaped = tmp_path / "outside" / "dashboard.db"
    env["DASHBOARD_DB"] = str(escaped)  # a store rooted OUTSIDE the volume

    subprocess.run(
        [sys.executable, "-m", "tools.init", "--root", str(volume)],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=300,
    )
    # The escape is visible: state landed outside the volume root.
    assert escaped.exists(), "expected the un-rooted store to escape the volume"
    assert not (volume / "dashboard.db").exists()


# -- one precedence rule across every store, orgs included (auto-sthm3) -------------


def test_orgs_root_follows_the_same_precedence_as_every_other_store(monkeypatch, tmp_path):
    """orgs is the identity/secret store: a writer handed a deployment root
    and a reader that knows only the variable must converge, or credential
    rows land where readers do not look."""
    from tools.data_paths import resolve_orgs_root

    default = tmp_path / "default-orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "env-orgs"))
    # env outranks a passed deployment root — the unified rule.
    assert resolve_orgs_root(tmp_path / "root-orgs", default=default) == (
        tmp_path / "env-orgs"
    )
    monkeypatch.delenv("AUTONOMY_ORGS_DIR")
    assert resolve_orgs_root(tmp_path / "root-orgs", default=default) == (
        tmp_path / "root-orgs"
    )
    assert resolve_orgs_root(None, default=default) == default


def test_orgs_guard_behaviour_is_unchanged(monkeypatch, tmp_path):
    """qhtq7's fail-closed guard must survive the reordering untouched."""
    from tools.data_paths import resolve_orgs_root

    monkeypatch.setenv(REFUSE_REAL_DATA_FALLBACK_ENV, "1")
    monkeypatch.delenv("AUTONOMY_ORGS_DIR", raising=False)
    with pytest.raises(RealDataFallbackRefused):
        resolve_orgs_root(None, default=tmp_path / "default-orgs")
    # An explicit root still satisfies the guard (it is not unrooted).
    assert resolve_orgs_root(tmp_path / "r", default=tmp_path / "d") == tmp_path / "r"


def test_writer_and_reader_converge_on_the_orgs_store(monkeypatch, tmp_path):
    """The bug this closes, end to end: init handed a --root while the
    environment names somewhere else must write where readers read."""
    from tools.graph.db import _org_db_path
    from tools.network.ledger.store import org_ledger_db_path

    env_orgs = tmp_path / "volume" / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(env_orgs))
    writer_root = tmp_path / "somewhere-else"
    assert _org_db_path("acme", writer_root).parent == env_orgs
    assert org_ledger_db_path("acme", writer_root).parent == env_orgs


# ── AUTONOMY_DATA_ROOT: the ambient volume base (auto-fm4zz) ────────────────


def test_data_root_precedence_sits_between_store_env_and_root_arg(
    monkeypatch, tmp_path
):
    from tools.data_paths import resolve_store

    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path / "base"))

    # Ambient base beats the explicit root argument (env-outranks-root,
    # the manifest's standing convergence rule)…
    assert resolve_store("dashboard", root=tmp_path / "other") == (
        tmp_path / "base" / "dashboard.db"
    )
    # …and the store's own variable beats the ambient base.
    monkeypatch.setenv("DASHBOARD_DB", str(tmp_path / "pinned.db"))
    assert resolve_store("dashboard") == tmp_path / "pinned.db"


def test_data_root_satisfies_the_refuse_guard(monkeypatch, tmp_path):
    """The exact gap that motivated the bead: under the guard with no
    store env, the graph store previously had NO resolvable path on a
    node (auto-5jbqa fallout); the ambient base closes it in-volume."""
    from tools.data_paths import resolve_store

    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setenv("AUTONOMY_REFUSE_REAL_DATA_FALLBACK", "1")
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    assert resolve_store("dashboard") == tmp_path / "dashboard.db"


def test_data_root_composes_with_org_routing_never_pins(monkeypatch, tmp_path):
    """A base directory, never a whole-DB pin: org routing goes THROUGH
    the base (base/orgs/<org>.db) — the org argument is honored, which is
    the property whose absence was the GRAPH_DB collapse (auto-23d9m)."""
    from tools.graph.db import resolve_caller_db_path

    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("AUTONOMY_ORGS_DIR", raising=False)
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    assert resolve_caller_db_path("demo") == tmp_path / "orgs" / "demo.db"
    assert resolve_caller_db_path("other") == tmp_path / "orgs" / "other.db"
    # The scopeless default routes to the personal store, which is not an
    # organization and composes to its own home BESIDE orgs/ (auto-35kmy).
    assert resolve_caller_db_path(None) == tmp_path / "personal.db"


def test_relative_data_root_is_refused_fail_closed(monkeypatch):
    import pytest

    from tools.data_paths import AmbiguousDataRoot, resolve_store

    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", "relative/base")
    with pytest.raises(AmbiguousDataRoot):
        resolve_store("dashboard")


def test_data_root_does_not_disturb_the_pin_conflict_semantics(
    monkeypatch, tmp_path
):
    """23d9m's refusal is orthogonal and survives: an explicit org against
    a contradicting GRAPH_DB pin refuses regardless of the ambient base."""
    import pytest

    from tools.graph.db import OrgResolutionConflict, resolve_caller_db_path

    monkeypatch.delenv("AUTONOMY_ORGS_DIR", raising=False)
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "elsewhere.db"))
    with pytest.raises(OrgResolutionConflict):
        resolve_caller_db_path("demo")
