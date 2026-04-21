"""Bootstrap path tests — first-launch creation of autonomy.db + personal.db.

Spec: graph://d970d946-f95.

The seed identity Setting requires the ``autonomy.org#1`` schema (auto-S1)
to be registered. Tests register a permissive stub schema so the seed
path runs end-to-end; without the stub, ``ensure_bootstrap_orgs`` skips
the seed silently and only the bootstrap row is asserted.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from tools.graph import org_ops, schemas
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS


@pytest.fixture(autouse=True)
def _isolate_schema_registry():
    schemas_snap = dict(SCHEMAS)
    upcon_snap = dict(UPCONVERTERS)
    try:
        yield
    finally:
        SCHEMAS.clear()
        SCHEMAS.update(schemas_snap)
        UPCONVERTERS.clear()
        UPCONVERTERS.update(upcon_snap)


@pytest.fixture
def stub_org_schema():
    """Register a permissive ``autonomy.org#1`` schema so seeds succeed."""

    class OrgV1(schemas.SettingSchema):
        set_id = "autonomy.org"
        schema_revision = 1

    schemas.register_schema("autonomy.org", 1, OrgV1)
    return OrgV1


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    root = tmp_path / "data" / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    return root


def _read_settings(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM settings WHERE set_id = 'autonomy.org'"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def test_ensure_bootstrap_creates_both_dbs(orgs_root, stub_org_schema):
    refs = org_ops.ensure_bootstrap_orgs()
    slugs = sorted(r.slug for r in refs)
    assert slugs == ["autonomy", "personal"]
    assert (orgs_root / "autonomy.db").exists()
    assert (orgs_root / "personal.db").exists()


def test_ensure_bootstrap_seeds_identity(orgs_root, stub_org_schema):
    org_ops.ensure_bootstrap_orgs()
    aut_settings = _read_settings(orgs_root / "autonomy.db")
    assert len(aut_settings) == 1
    payload = json.loads(aut_settings[0]["payload"])
    assert payload["name"] == "Autonomy Network"
    assert payload["color"] == "#6C63FF"
    assert aut_settings[0]["publication_state"] == "canonical"
    assert aut_settings[0]["key"] == "autonomy"

    per_settings = _read_settings(orgs_root / "personal.db")
    assert len(per_settings) == 1
    payload = json.loads(per_settings[0]["payload"])
    assert payload["name"] == "Personal"
    assert payload["color"] == "#A0A0A0"
    assert per_settings[0]["key"] == "personal"


def test_bootstrap_skips_seed_when_schema_unregistered(orgs_root):
    # Explicitly unregister autonomy.org#1 (normally registered at import
    # time by tools.graph.schemas.org) to simulate the pre-S1 world.
    # The autouse _isolate_schema_registry fixture restores it after.
    from tools.graph.schemas.registry import unregister_schema
    unregister_schema("autonomy.org", 1)
    refs = org_ops.ensure_bootstrap_orgs()
    assert sorted(r.slug for r in refs) == ["autonomy", "personal"]
    # DB exists, but no autonomy.org#1 Setting was seeded.
    assert _read_settings(orgs_root / "autonomy.db") == []
    assert _read_settings(orgs_root / "personal.db") == []


def test_bootstrap_is_idempotent(orgs_root, stub_org_schema):
    refs1 = org_ops.ensure_bootstrap_orgs()
    refs2 = org_ops.ensure_bootstrap_orgs()
    refs3 = org_ops.ensure_bootstrap_orgs()
    # UUIDs stable across re-runs.
    by_slug1 = {r.slug: r.id for r in refs1}
    by_slug3 = {r.slug: r.id for r in refs3}
    assert by_slug1 == by_slug3
    # No duplicate Setting rows.
    aut = _read_settings(orgs_root / "autonomy.db")
    assert len(aut) == 1
    per = _read_settings(orgs_root / "personal.db")
    assert len(per) == 1


def test_bootstrap_uses_uuid7(orgs_root, stub_org_schema):
    """Bootstrap row id should be a valid UUID v7 (version nibble == 7)."""
    refs = org_ops.ensure_bootstrap_orgs()
    for r in refs:
        # Canonical 8-4-4-4-12 hex.
        parts = r.id.split("-")
        assert len(parts) == 5
        assert len(parts[0]) == 8 and len(parts[1]) == 4
        assert len(parts[2]) == 4 and len(parts[3]) == 4
        assert len(parts[4]) == 12
        # Version nibble is the leading char of the third group.
        assert parts[2][0] == "7"


def test_bootstrap_orgs_have_correct_types(orgs_root, stub_org_schema):
    refs = org_ops.ensure_bootstrap_orgs()
    by_slug = {r.slug: r for r in refs}
    assert by_slug["autonomy"].type == "shared"
    assert by_slug["personal"].type == "personal"


def test_bootstrap_skips_when_db_already_present(orgs_root, stub_org_schema):
    """Pre-existing DB must not be re-created.

    Models the upgrade path on a system that already had a per-org DB
    laid down manually — bootstrap is idempotent and never clobbers.
    """
    orgs_root.mkdir(parents=True, exist_ok=True)
    pre_existing_path = orgs_root / "autonomy.db"
    # Manually create the org with a known UUID via create_org.
    ref = org_ops.create_org("autonomy", type_="shared")
    pre_existing_id = ref.id
    pre_existing_mtime = pre_existing_path.stat().st_mtime

    org_ops.ensure_bootstrap_orgs()
    after = org_ops.get_org("autonomy")
    assert after is not None
    assert after.id == pre_existing_id
    assert pre_existing_path.stat().st_mtime == pre_existing_mtime
