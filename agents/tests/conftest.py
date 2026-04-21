"""Shared fixtures for agents/tests.

Provides a ``shipped_workspaces`` fixture that populates per-org Settings
from the shipped yaml fixture into a tmp ``data/orgs/`` tree so tests
that previously exercised the yaml loader can now exercise the
Settings-backed :mod:`agents.workspace_settings` module with identical
data.

The fixture yaml lives under ``agents/tests/fixtures/`` because
``agents/projects.yaml`` itself was retired in auto-gko4e (all content
now lives in per-org Setting DBs). Tests keep a frozen copy of the
final yaml shape as ground truth for the migration round-trip.

Tests that construct :class:`agents.workspace_settings.WorkspaceV1`
directly do not need this fixture — they bypass the read path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.graph.migrations.migrate_artifacts_yaml import (
    apply_migration as apply_artifacts_migration,
    build_plan as build_artifacts_plan,
)
from tools.graph.migrations.migrate_workspaces_yaml import (
    apply_migration as apply_workspaces_migration,
    build_plan as build_workspaces_plan,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_PROJECTS_YAML = (
    REPO_ROOT / "agents" / "tests" / "fixtures" / "shipped_projects.yaml"
)


def populate_workspaces_from_yaml(
    yaml_path: Path,
    orgs_dir: Path,
    *,
    org_slugs: tuple[str, ...] = ("autonomy", "anchore", "personal"),
) -> None:
    """Bootstrap per-org DBs and migrate workspace + artifact Settings from *yaml_path*.

    Mirrors the production bootstrap chain (auto-9iq2s + auto-raycq + auto-hhi23)
    for test use. Idempotent — re-invoking is a no-op.
    """
    orgs_dir.mkdir(parents=True, exist_ok=True)
    for slug in org_slugs:
        db_path = orgs_dir / f"{slug}.db"
        if not db_path.exists():
            db = GraphDB.create_org_db(slug, type_="shared", path=db_path)
            db.close()

    plan = build_workspaces_plan(yaml_path, orgs_dir)
    apply_workspaces_migration(plan, log=lambda *_a, **_kw: None)

    artifact_plan = build_artifacts_plan(yaml_path, orgs_root=orgs_dir)
    apply_artifacts_migration(artifact_plan)


@pytest.fixture
def shipped_workspaces(tmp_path, monkeypatch):
    """Populate Settings from the shipped projects.yaml into a tmp orgs tree.

    Yields the ``data/orgs`` path so tests can introspect if they need to.
    The :envvar:`AUTONOMY_ORGS_DIR` env var is redirected for the test's
    duration so :mod:`tools.graph.ops` reads from this tree.
    """
    orgs_dir = tmp_path / "orgs"
    GraphDB.close_all_pooled()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    populate_workspaces_from_yaml(SHIPPED_PROJECTS_YAML, orgs_dir)
    try:
        yield orgs_dir
    finally:
        GraphDB.close_all_pooled()


@pytest.fixture
def isolated_settings_db(tmp_path, monkeypatch):
    """Point ``GRAPH_DB`` at a fresh empty tmp DB for tests that insert
    Settings directly via :func:`tools.graph.ops.add_setting`.
    """
    db_path = tmp_path / "graph.db"
    GraphDB.close_all_pooled()
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("AUTONOMY_ORGS_DIR", raising=False)
    # Open once to materialise the schema.
    db = GraphDB(db_path)
    db.close()
    try:
        yield db_path
    finally:
        GraphDB.close_all_pooled()
