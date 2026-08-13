"""CLI tests for ``graph org`` subcommand group.

Spec: graph://d970d946-f95.

Covers parser dispatch + functional create/list/show/rename/remove flows
against a real on-disk org root (redirected via ``AUTONOMY_ORGS_DIR``).
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout, redirect_stderr

import pytest

from tools.graph import cli, org_ops, schemas
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
    """Register a permissive ``autonomy.org#1`` stub for CLI tests.

    Auto-registration via ``__init_subclass__`` collides with the
    production class; drop it first. The autouse ``_isolate_registry``
    fixture restores production after the test.
    """
    from tools.graph.schemas.registry import unregister_schema
    unregister_schema("autonomy.org", 1)

    class OrgV1(schemas.SettingSchema):
        set_id = "autonomy.org"
        schema_revision = 1

    return OrgV1


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    root = tmp_path / "data" / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_API", raising=False)
    # Creation IS the founding ceremony (auto-nixfv): enroll a throwaway
    # personal identity and supply its password through the env source so
    # every `org create` below founds its ledger transparently.
    # No GRAPH_DB pin: explicit-org settings resolution must route to the
    # per-org tree — a pin silently swallows those writes (73bad14e) and,
    # under the fail-loud resolver, conflicts. delenv guards ambient leaks.
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setenv("AUTONOMY_PERSONAL_PASSWORD", "cli-test-password")
    from tools.graph import settings_ops
    from tools.graph.schemas.personal_identity import PERSONAL_IDENTITY_SET_ID
    from tools.network.idkit import KeyPair
    from tools.network.idkit.armor import encrypt_root_key

    owner = KeyPair.generate()
    with settings_ops.identity_write_context():
        settings_ops.upsert_by_key(
            PERSONAL_IDENTITY_SET_ID, 1, "default",
            {
                "armored_private_key": encrypt_root_key(
                    owner, "cli-test-password", iterations=10_000
                ),
                "root_pub": owner.public_hex,
                "display_name": "CLI Test",
                "created_at": "2026-07-26T00:00:00Z",
            },
            org=None,
        )
    return root


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    rc = 0
    saved_argv = sys.argv
    sys.argv = ["graph"] + argv
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as e:
                rc = int(e.code) if e.code is not None else 0
    finally:
        sys.argv = saved_argv
    return rc, out.getvalue(), err.getvalue()


# ── parser sanity ──────────────────────────────────────────


def test_org_list_fresh_tree_shows_only_personal(orgs_root):
    """A fresh orgs tree is never listably empty: caller-scope settings
    resolution materializes personal.db on first touch, so the honest
    baseline is exactly one row — personal — and no shared orgs."""
    rc, out, _ = _run_cli(["org", "list"])
    assert rc == 0
    lines = [l for l in out.splitlines() if l.strip() and not set(l) <= set("─ ")]
    body = [l for l in lines if not l.startswith("SLUG")]
    assert len(body) == 1 and body[0].startswith("personal"), out


def test_org_show_missing_slug_errors(orgs_root):
    rc, _, _ = _run_cli(["org", "show"])
    assert rc != 0


def test_org_create_requires_slug(orgs_root):
    rc, _, _ = _run_cli(["org", "create"])
    assert rc != 0


# ── functional create / list / show ────────────────────────


def test_create_then_list(orgs_root, stub_org_schema):
    rc, out, err = _run_cli(["org", "create", "anchore"])
    assert rc == 0, err
    assert (orgs_root / "anchore.db").exists()
    rc, out, _ = _run_cli(["org", "list"])
    assert rc == 0
    assert "anchore" in out


def test_create_then_show(orgs_root, stub_org_schema):
    _run_cli(["org", "create", "anchore"])
    rc, out, err = _run_cli(["org", "show", "anchore"])
    assert rc == 0, err
    detail = json.loads(out)
    assert detail["org"]["slug"] == "anchore"
    assert detail["org"]["type"] == "shared"
    # No identity authored yet — bare create skips seed (no --identity flag).
    assert detail["identity"] is None


def test_create_with_identity_seeds_setting(orgs_root, stub_org_schema, tmp_path):
    payload_file = tmp_path / "id.json"
    payload_file.write_text(json.dumps({
        "name": "Anchore",
        "byline": "Security platform",
        "color": "#2D7DD2",
        "favicon": "/static/orgs/anchore.png",
        "type": "shared",
    }))
    rc, _, err = _run_cli([
        "org", "create", "anchore", "--identity", str(payload_file),
    ])
    assert rc == 0, err
    rc, out, err = _run_cli(["org", "show", "anchore"])
    assert rc == 0, err
    detail = json.loads(out)
    assert detail["identity"] is not None
    assert detail["identity"]["payload"]["name"] == "Anchore"
    assert detail["identity"]["publication_state"] == "canonical"


def test_create_personal_type(orgs_root, stub_org_schema):
    rc, _, err = _run_cli(["org", "create", "scratch", "--type", "personal"])
    assert rc == 0, err
    ref = org_ops.get_org("scratch")
    assert ref is not None
    assert ref.type == "personal"


def test_create_existing_slug_errors(orgs_root, stub_org_schema):
    _run_cli(["org", "create", "anchore"])
    rc, _, err = _run_cli(["org", "create", "anchore"])
    assert rc != 0
    assert "exists" in err.lower()


def test_create_invalid_slug_errors(orgs_root):
    rc, _, err = _run_cli(["org", "create", "bad/slug"])
    assert rc != 0


# ── remove ────────────────────────────────────────────────


def test_remove_unreferenced(orgs_root, stub_org_schema):
    _run_cli(["org", "create", "anchore"])
    rc, out, err = _run_cli(["org", "remove", "anchore"])
    assert rc == 0, err
    assert not (orgs_root / "anchore.db").exists()


def test_remove_missing_errors(orgs_root):
    rc, _, err = _run_cli(["org", "remove", "ghost"])
    assert rc != 0
    assert "not found" in err.lower()


def test_remove_refuses_when_referenced(orgs_root, stub_org_schema):
    """Settings keyed by the org's slug in a peer DB block removal."""
    _run_cli(["org", "create", "anchore"])
    _run_cli(["org", "create", "personal", "--type", "personal"])
    # Insert a Setting in personal.db keyed by 'anchore' to simulate
    # an operator override referencing the anchore org.
    import sqlite3
    conn = sqlite3.connect(str(orgs_root / "personal.db"))
    conn.execute(
        "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
        "publication_state, created_at, updated_at) "
        "VALUES('s1','autonomy.org.identity-override',1,'anchore',"
        "'{\"name\": \"My Anchore\"}','canonical','t','t')"
    )
    conn.commit()
    conn.close()

    rc, _, err = _run_cli(["org", "remove", "anchore"])
    assert rc != 0
    assert "reference" in err.lower()
    assert (orgs_root / "anchore.db").exists()


def test_remove_force_overrides_references(orgs_root, stub_org_schema):
    _run_cli(["org", "create", "anchore"])
    _run_cli(["org", "create", "personal", "--type", "personal"])
    import sqlite3
    conn = sqlite3.connect(str(orgs_root / "personal.db"))
    conn.execute(
        "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
        "publication_state, created_at, updated_at) "
        "VALUES('s1','autonomy.org.identity-override',1,'anchore',"
        "'{}','canonical','t','t')"
    )
    conn.commit()
    conn.close()
    rc, out, err = _run_cli(["org", "remove", "anchore", "--force"])
    assert rc == 0, err
    assert not (orgs_root / "anchore.db").exists()


# ── rename ────────────────────────────────────────────────


def test_rename_moves_file_and_preserves_uuid(orgs_root, stub_org_schema):
    _run_cli(["org", "create", "anchore"])
    before = org_ops.get_org("anchore")
    rc, _, err = _run_cli(["org", "rename", "anchore", "anchore-prod"])
    assert rc == 0, err
    assert not (orgs_root / "anchore.db").exists()
    assert (orgs_root / "anchore-prod.db").exists()
    after = org_ops.get_org("anchore-prod")
    assert after is not None
    assert after.id == before.id


def test_rename_rewrites_identity_setting_key(orgs_root, stub_org_schema, tmp_path):
    payload_file = tmp_path / "id.json"
    payload_file.write_text(json.dumps({"name": "Anchore", "type": "shared"}))
    _run_cli([
        "org", "create", "anchore", "--identity", str(payload_file),
    ])
    rc, _, err = _run_cli(["org", "rename", "anchore", "anchore-prod"])
    assert rc == 0, err
    detail = json.loads(_run_cli(["org", "show", "anchore-prod"])[1])
    assert detail["identity"] is not None
    assert detail["identity"]["key"] == "anchore-prod"
    # Payload retains the human-friendly display name; only structural
    # references (key + payload.org) get rewritten.
    assert detail["identity"]["payload"]["name"] == "Anchore"


def test_rename_to_existing_slug_errors(orgs_root, stub_org_schema):
    _run_cli(["org", "create", "anchore"])
    _run_cli(["org", "create", "anchore-prod"])
    rc, _, err = _run_cli(["org", "rename", "anchore", "anchore-prod"])
    assert rc != 0


def test_rename_missing_source_errors(orgs_root):
    rc, _, err = _run_cli(["org", "rename", "ghost", "specter"])
    assert rc != 0
    assert "not found" in err.lower()
