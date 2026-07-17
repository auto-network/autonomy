"""First-run initialization tests (bead auto-q1fsp, H3).

All against throwaway tmp roots — nothing here reads or writes this
checkout's real ``data/`` tree, and nothing assumes pre-existing
content (the empty deployment IS the fixture).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess

import pytest

from tools.init.first_run import CREATED, EXISTS, SKIPPED, initialize


@pytest.fixture(autouse=True)
def _clean_first_org_env(monkeypatch):
    """Tests control first-org naming explicitly; shield from session env."""
    monkeypatch.delenv("AUTONOMY_FIRST_ORG", raising=False)
    monkeypatch.delenv("AUTONOMY_FIRST_ORG_NAME", raising=False)


def _tables(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()


def test_initialize_creates_empty_deployment(tmp_path):
    report = initialize(
        tmp_path, first_org="acme", first_org_name="Acme Corp", tls=False,
    )

    data = tmp_path / "data"
    for rel in ("", "orgs", "agent-runs", "session-traces"):
        assert (data / rel).is_dir()

    # graph.db carries the canonical schema.
    graph_tables = _tables(data / "graph.db")
    assert {"sources", "thoughts", "settings", "orgs"} <= graph_tables

    # Org DBs: operator-named first org + personal, each with its
    # bootstrap row and identity Setting.
    from tools.graph import org_ops

    orgs = {
        o.slug: o for o in org_ops.list_orgs(root=data / "orgs")
    }
    assert set(orgs) == {"acme", "personal"}
    assert orgs["acme"].type == "shared"
    assert orgs["personal"].type == "personal"

    conn = sqlite3.connect(str(data / "orgs" / "acme.db"))
    try:
        payload = json.loads(
            conn.execute(
                "SELECT payload FROM settings WHERE set_id='autonomy.org'"
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert payload["name"] == "Acme Corp"

    # Operational DBs exist and are schema'd.
    for filename in (
        "dashboard.db", "auth.db", "dispatch.db",
        "approval_requests.db", "commit_workflow.db",
    ):
        assert (data / filename).exists(), filename
        assert _tables(data / filename), f"{filename} has no tables"

    # Everything except TLS (disabled) actually happened.
    actions = {s.name: s.action for s in report.steps}
    assert actions["tls"] == SKIPPED
    assert all(
        a == CREATED for n, a in actions.items() if n != "tls"
    ), actions
    assert report.changed


def test_second_run_is_noop(tmp_path):
    initialize(tmp_path, first_org="acme", tls=False)
    second = initialize(tmp_path, first_org="acme", tls=False)

    assert not second.changed
    for step in second.steps:
        assert step.action in (EXISTS, SKIPPED), (step.name, step.action)

    # Idempotency in the data too: exactly one orgs row, no duplicate
    # allowlist Setting.
    conn = sqlite3.connect(str(tmp_path / "data" / "orgs" / "personal.db"))
    try:
        (n_orgs,) = conn.execute("SELECT COUNT(*) FROM orgs").fetchone()
        (n_allow,) = conn.execute(
            "SELECT COUNT(*) FROM settings "
            "WHERE set_id='autonomy.org.bootstrap-allowlist'"
        ).fetchone()
    finally:
        conn.close()
    assert n_orgs == 1
    assert n_allow == 1


def test_zero_content_search_is_empty_not_error(tmp_path):
    initialize(tmp_path, first_org="acme", tls=False)
    from tools.graph.db import GraphDB

    for db_path in (
        tmp_path / "data" / "graph.db",
        tmp_path / "data" / "orgs" / "acme.db",
    ):
        db = GraphDB(db_path)
        try:
            assert db.search("anything") == []
        finally:
            db.close()


def test_bootstrap_allowlist_setting_seeded_and_valid(tmp_path):
    initialize(tmp_path, first_org="acme", tls=False)

    conn = sqlite3.connect(str(tmp_path / "data" / "orgs" / "personal.db"))
    try:
        row = conn.execute(
            "SELECT key, schema_revision, payload FROM settings "
            "WHERE set_id='autonomy.org.bootstrap-allowlist'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "bootstrap allowlist Setting missing"
    key, revision, payload_str = row
    assert key == "autonomy"
    payload = json.loads(payload_str)

    from tools.graph import schemas

    schemas.validate_payload(
        "autonomy.org.bootstrap-allowlist", revision, payload,
    )
    # Mirrors the committed YAML, content-free (id prefixes only).
    assert payload["version"] >= 1
    assert payload["canonical"] and payload["published"]


def test_first_org_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_FIRST_ORG", "widgets")
    initialize(tmp_path, tls=False)
    assert (tmp_path / "data" / "orgs" / "widgets.db").exists()
    assert not (tmp_path / "data" / "orgs" / "autonomy.db").exists()


def test_default_first_org_is_autonomy(tmp_path):
    initialize(tmp_path, tls=False)
    assert (tmp_path / "data" / "orgs" / "autonomy.db").exists()


def test_invalid_first_org_slug_rejected(tmp_path):
    from tools.graph.org_ops import OrgError

    with pytest.raises(OrgError):
        initialize(tmp_path, first_org="../evil", tls=False)


@pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl not installed"
)
def test_tls_selfsigned_generation(tmp_path):
    report = initialize(tmp_path, first_org="acme", tls_domain="unit.test")
    actions = {s.name: s.action for s in report.steps}
    assert actions["tls"] == CREATED

    crt = tmp_path / "data" / "tls.crt"
    key = tmp_path / "data" / "tls.key"
    assert crt.exists() and key.exists()
    assert (key.stat().st_mode & 0o777) == 0o600

    subject = subprocess.run(
        ["openssl", "x509", "-in", str(crt), "-noout", "-subject"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "unit.test" in subject

    # Second run leaves the pair untouched.
    before = crt.read_bytes()
    second = initialize(tmp_path, first_org="acme", tls_domain="unit.test")
    assert {s.action for s in second.steps if s.name == "tls"} == {EXISTS}
    assert crt.read_bytes() == before


@pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl not installed"
)
def test_tls_half_pair_left_alone(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "tls.crt").write_text("not really a cert")
    report = initialize(tmp_path, first_org="acme")
    actions = {s.name: s.action for s in report.steps}
    assert actions["tls"] == SKIPPED
    assert not (tmp_path / "data" / "tls.key").exists()
    assert (tmp_path / "data" / "tls.crt").read_text() == "not really a cert"
