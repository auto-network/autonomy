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
    """Tests control first-org naming explicitly; shield from session env.

    These tests are hermetic on a per-test ``tmp_path`` deployment root, but
    ``resolve_store`` precedence is store-env → ambient ``AUTONOMY_DATA_ROOT``
    → the ``root`` argument, so any leaked store/ambient env silently outranks
    the root and sends org/TLS/store writes elsewhere. A dashboard test sharing
    this xdist worker sets ``AUTONOMY_ORGS_DIR`` (and the other store envs)
    process-globally at conftest import time; clear the ambient root and every
    store env here so this deployment's ``tmp_path`` root is authoritative.
    """
    monkeypatch.delenv("AUTONOMY_FIRST_ORG", raising=False)
    monkeypatch.delenv("AUTONOMY_FIRST_ORG_NAME", raising=False)
    from tools.data_paths import DATA_ROOT_ENV, STORE_MANIFEST
    monkeypatch.delenv(DATA_ROOT_ENV, raising=False)
    for store in STORE_MANIFEST:
        if store.env:
            monkeypatch.delenv(store.env, raising=False)


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

    # The obsolete single-DB store is never provisioned.
    assert not (data / "graph.db").exists()

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

    # Everything except TLS (disabled) actually happened, the org-follow seed
    # included: the committed allowlist carries Autonomy's published follow
    # link (2026-09-25, §10.1), and acme is not Autonomy, so the row is seeded.
    actions = {s.name: s.action for s in report.steps}
    assert actions["tls"] == SKIPPED
    assert actions["setting:org-follow"] == CREATED
    skipped_by_design = {"tls", "setting:org-follow"}
    assert all(
        a == CREATED for n, a in actions.items() if n not in skipped_by_design
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
    conn = sqlite3.connect(str(tmp_path / "data" / "personal.db"))
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

    conn = sqlite3.connect(str(tmp_path / "data" / "personal.db"))
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


def test_startup_bootstrap_respects_operator_named_org(tmp_path):
    """Dashboard startup (no arg, no env) must not manufacture a default
    'autonomy' org next to one the operator already created via init."""
    initialize(tmp_path, first_org="acme", tls=False)
    from tools.graph import org_ops

    orgs = org_ops.ensure_bootstrap_orgs(root=tmp_path / "data" / "orgs")
    assert {o.slug for o in orgs} == {"acme", "personal"}
    assert not (tmp_path / "data" / "orgs" / "autonomy.db").exists()


def test_no_first_org_named_creates_no_shared_org(tmp_path):
    # No AUTONOMY_FIRST_ORG: first run creates no shared organization. The
    # operator creates or joins one in onboarding, or has none
    # (graph://5f2f5a49-00d D7).
    initialize(tmp_path, tls=False)
    assert not list((tmp_path / "data" / "orgs").glob("*.db"))
    assert (tmp_path / "data" / "personal.db").exists()


def test_invalid_first_org_slug_rejected(tmp_path):
    from tools.graph.org_ops import OrgError

    with pytest.raises(OrgError):
        initialize(tmp_path, first_org="../evil", tls=False)


# ── Follow-defaults seed (bead auto-krbtk, graph://5f2f5a49-00d §10.5) ──

from tools.init import first_run  # noqa: E402

_AUTONOMY_UUID = "2d4b90cb-1e89-452b-82cb-68ca44fd8e52"
_FAKE_RENDEZVOUS = "https://relay.example/l/abc123token"
_FAKE_LINK_PUB = "a" * 64
_ORG_FOLLOW_SET_ID = "autonomy.org.follow"


def _write_allowlist(path, *, org="autonomy", follow=None):
    """Write a minimal-but-valid bootstrap allowlist YAML, with an optional
    ``follow:`` block, so both allowlist and follow seeds have something to
    read."""
    lines = [
        f"org: {org}",
        "version: 1",
        "canonical:",
        "  - e2c81892-0fb  # a canonical prefix",
        "published:",
        "  - 497cdc20-d43  # a published prefix",
    ]
    if follow is not None:
        lines.append("follow:")
        for key, value in follow.items():
            lines.append(f'  {key}: "{value}"')
    path.write_text("\n".join(lines) + "\n")
    return path


def _follow_rows(root):
    conn = sqlite3.connect(str(root / "data" / "personal.db"))
    try:
        rows = conn.execute(
            "SELECT key, payload FROM settings WHERE set_id = ?",
            (_ORG_FOLLOW_SET_ID,),
        ).fetchall()
    finally:
        conn.close()
    return [(key, json.loads(payload)) for key, payload in rows]


@pytest.fixture
def full_follow_allowlist(tmp_path, monkeypatch):
    """Point first-run at an allowlist whose follow: block is fully published
    (rendezvous + link_pub present, org_uuid = Autonomy's)."""
    path = _write_allowlist(
        tmp_path / "allowlist_with_follow.yaml",
        follow={
            "org_uuid": _AUTONOMY_UUID,
            "rendezvous": _FAKE_RENDEZVOUS,
            "link_pub": _FAKE_LINK_PUB,
        },
    )
    monkeypatch.setattr(first_run, "ALLOWLIST_YAML", path)
    return path


def test_follow_defaults_seeded_no_env(tmp_path, full_follow_allowlist):
    # No AUTONOMY_FIRST_ORG: a personal-only node seeds the Autonomy follow
    # row from the published follow: block; no shared org is needed for it.
    initialize(tmp_path, tls=False)
    assert not list((tmp_path / "data" / "orgs").glob("*.db"))

    rows = _follow_rows(tmp_path)
    assert len(rows) == 1
    key, payload = rows[0]
    assert key == "autonomy"
    assert payload["org_uuid"] == _AUTONOMY_UUID
    assert payload["rendezvous"] == _FAKE_RENDEZVOUS
    assert payload["link_pub"] == _FAKE_LINK_PUB
    assert payload["enabled"] is True
    assert payload["added_at"]

    # Seeded payload validates against the follow schema.
    from tools.graph import schemas
    schemas.validate_payload(_ORG_FOLLOW_SET_ID, 1, payload)


def test_follow_defaults_idempotent(tmp_path, full_follow_allowlist):
    initialize(tmp_path, tls=False)
    second = initialize(tmp_path, tls=False)

    assert not second.changed
    for step in second.steps:
        assert step.action in (EXISTS, SKIPPED), (step.name, step.action)
    # Exactly one follow row after two runs.
    assert len(_follow_rows(tmp_path)) == 1
    assert {s.action for s in second.steps if s.name == "setting:org-follow"} == {
        EXISTS
    }


def test_follow_defaults_skipped_on_self_node(tmp_path, monkeypatch):
    # First run founds the named org acme against an unpublished follow block
    # (nothing seeded). Capture acme's id.
    unpublished = _write_allowlist(
        tmp_path / "unpublished.yaml", follow={"org_uuid": _AUTONOMY_UUID},
    )
    monkeypatch.setattr(first_run, "ALLOWLIST_YAML", unpublished)
    initialize(tmp_path, first_org="acme", tls=False)
    assert _follow_rows(tmp_path) == []
    conn = sqlite3.connect(str(tmp_path / "data" / "orgs" / "acme.db"))
    try:
        (own_org_id,) = conn.execute("SELECT id FROM orgs LIMIT 1").fetchone()
    finally:
        conn.close()

    # Point the allowlist at a published follow: block whose org_uuid IS this
    # node's own org — a node never follows itself.
    path = _write_allowlist(
        tmp_path / "self_follow.yaml",
        follow={
            "org_uuid": own_org_id,
            "rendezvous": _FAKE_RENDEZVOUS,
            "link_pub": _FAKE_LINK_PUB,
        },
    )
    monkeypatch.setattr(first_run, "ALLOWLIST_YAML", path)

    report = initialize(tmp_path, tls=False)
    assert _follow_rows(tmp_path) == []
    step = next(s for s in report.steps if s.name == "setting:org-follow")
    assert step.action == SKIPPED
    assert "self-follow" in step.detail


def test_follow_defaults_skipped_when_link_unpublished(tmp_path, monkeypatch):
    # A follow: block with org_uuid but no rendezvous/link_pub (the shape the
    # committed allowlist had before the org:follow link was published on
    # 2026-09-25): first run seeds no follow row and reports why, never
    # erroring.
    path = _write_allowlist(
        tmp_path / "unpublished.yaml", follow={"org_uuid": _AUTONOMY_UUID},
    )
    monkeypatch.setattr(first_run, "ALLOWLIST_YAML", path)
    report = initialize(tmp_path, tls=False)
    assert _follow_rows(tmp_path) == []
    step = next(s for s in report.steps if s.name == "setting:org-follow")
    assert step.action == SKIPPED
    assert "not yet published" in step.detail


def test_follow_defaults_seed_from_the_committed_allowlist(tmp_path):
    # The real committed allowlist: the org:follow link published 2026-09-25.
    # A fresh personal-only node seeds the Autonomy follow row from it, with
    # the URL's base64url fragment normalized to the row's 64-hex link_pub.
    from tools.graph.curation.allowlist import load
    from tools.graph.schemas.org_follow import normalize_link_pub

    committed = load(first_run.ALLOWLIST_YAML).follow
    report = initialize(tmp_path, tls=False)
    step = next(s for s in report.steps if s.name == "setting:org-follow")
    assert step.action == CREATED, step.detail
    rows = _follow_rows(tmp_path)
    assert len(rows) == 1
    key, payload = rows[0]
    assert key == "autonomy"
    assert payload["org_uuid"] == committed["org_uuid"] == _AUTONOMY_UUID
    assert payload["rendezvous"] == committed["rendezvous"]
    assert payload["rendezvous"].startswith("https://") and "/l/" in payload["rendezvous"]
    assert payload["link_pub"] == normalize_link_pub(committed["link_pub"])
    assert len(payload["link_pub"]) == 64
    assert payload["registry_url"] == committed["registry_url"]


def test_follow_defaults_accept_base64url_fragment(tmp_path, monkeypatch):
    # The operator pastes the fragment as published (unpadded base64url);
    # the seeded row carries the 64-hex form the handshake uses.
    import base64

    raw = bytes(range(32))
    fragment = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    path = _write_allowlist(
        tmp_path / "b64.yaml",
        follow={
            "org_uuid": _AUTONOMY_UUID,
            "rendezvous": _FAKE_RENDEZVOUS,
            "link_pub": fragment,
        },
    )
    monkeypatch.setattr(first_run, "ALLOWLIST_YAML", path)
    initialize(tmp_path, tls=False)
    (_key, payload), = _follow_rows(tmp_path)
    assert payload["link_pub"] == raw.hex()


def test_follow_defaults_skip_a_wrong_length_key(tmp_path, monkeypatch):
    path = _write_allowlist(
        tmp_path / "short.yaml",
        follow={
            "org_uuid": _AUTONOMY_UUID,
            "rendezvous": _FAKE_RENDEZVOUS,
            "link_pub": "AAAA",
        },
    )
    monkeypatch.setattr(first_run, "ALLOWLIST_YAML", path)
    report = initialize(tmp_path, tls=False)
    assert _follow_rows(tmp_path) == []
    step = next(s for s in report.steps if s.name == "setting:org-follow")
    assert step.action == SKIPPED
    assert "not a channel key" in step.detail


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
