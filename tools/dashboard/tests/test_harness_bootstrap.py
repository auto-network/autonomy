"""Tests for Layer-0 harness bootstrap (bead auto-n130b).

Covers the four contract surfaces:
- schema validation of ``autonomy.harness.bootstrap#1`` (incl. no-secret shape),
- discovery classification (not-installed / needs-sign-in / ready),
- record → gate round-trip against a real settings store,
- behavioral first-launch gate: no verified harness serves the walkthrough;
  a verified harness lets the session UI through.
"""

from __future__ import annotations

import pytest

from tools.dashboard import harness_bootstrap as hb
from tools.graph.schemas.harness_bootstrap import (
    HarnessBootstrapV1,
    SET_ID,
)
from tools.graph.schemas.registry import SchemaValidationError


# ── Schema ────────────────────────────────────────────────────────────

def test_schema_accepts_discovery_payload():
    HarnessBootstrapV1.validate({
        "harness": "claude",
        "path": "/usr/bin/claude",
        "version": "1.2.3 (Claude Code)",
        "auth": "ok",
        "verified_at": "2026-08-13T00:00:00+00:00",
    })


@pytest.mark.parametrize("bad", [
    {"harness": "gpt4", "path": "/x", "version": "1", "auth": "ok",
     "verified_at": "t"},          # unknown harness
    {"harness": "claude", "path": "/x", "version": "1", "auth": "maybe",
     "verified_at": "t"},          # bad auth enum
    {"harness": "claude", "path": "/x", "version": "1", "auth": "ok"},  # missing field
])
def test_schema_rejects_bad_payloads(bad):
    with pytest.raises(SchemaValidationError):
        HarnessBootstrapV1.validate(bad)


def test_schema_rejects_secret_material():
    """No credential/token fields may ride on this row — unknown keys reject."""
    with pytest.raises(SchemaValidationError):
        HarnessBootstrapV1.validate({
            "harness": "claude",
            "path": "/usr/bin/claude",
            "version": "1.2.3",
            "auth": "ok",
            "verified_at": "t",
            "raw_key": "sk-ant-oat01-secret",
        })


# ── Discovery classification ──────────────────────────────────────────

def _install(monkeypatch, present, version_ok=True, noop_ok=True):
    monkeypatch.setattr(
        hb, "_which", lambda slug: f"/usr/bin/{slug}" if slug in present else None,
    )

    def fake_run(cmd, timeout):
        slug = cmd[0]
        if slug not in present:
            return 127, "not found"
        if cmd[1:2] == ["--version"]:
            return (0, f"{slug} 1.0.0") if version_ok else (1, "boom")
        # the no-op invocation
        return (0, "ok") if noop_ok else (1, "not signed in")

    monkeypatch.setattr(hb, "_run", fake_run)


def test_probe_not_installed(monkeypatch):
    _install(monkeypatch, present=set())
    r = hb.probe_harness("claude")
    assert r["state"] == hb.STATE_NOT_INSTALLED
    assert r["present"] is False and r["auth"] is None


def test_probe_needs_sign_in(monkeypatch):
    _install(monkeypatch, present={"claude"}, noop_ok=False)
    r = hb.probe_harness("claude")
    assert r["state"] == hb.STATE_NEEDS_SIGN_IN
    assert r["present"] is True and r["auth"] == "missing"


def test_probe_ready(monkeypatch):
    _install(monkeypatch, present={"claude"}, noop_ok=True)
    r = hb.probe_harness("claude")
    assert r["state"] == hb.STATE_READY
    assert r["auth"] == "ok" and r["version"] == "claude 1.0.0"


def test_probe_present_but_version_fails_is_not_installed(monkeypatch):
    _install(monkeypatch, present={"codex"}, version_ok=False)
    r = hb.probe_harness("codex")
    assert r["state"] == hb.STATE_NOT_INSTALLED
    assert r["present"] is False


# ── Record → gate round-trip ──────────────────────────────────────────

@pytest.fixture
def graph_db(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPH_DB", str(tmp_path / "graph.db"))
    monkeypatch.delenv("GRAPH_API", raising=False)
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()
    yield
    GraphDB.close_all_pooled()


def test_record_and_gate_roundtrip(graph_db, monkeypatch):
    assert hb.has_verified_harness() is False

    # installed-but-not-authed records a row, but does NOT open the gate.
    _install(monkeypatch, present={"claude"}, noop_ok=False)
    res = hb.verify_and_record("claude")
    assert res["recorded"] is True and res["auth"] == "missing"
    assert hb.has_verified_harness() is False

    rows = hb.recorded_rows()
    assert len(rows) == 1 and rows[0]["harness"] == "claude"
    # discovery results only — never a token/credential field
    assert set(rows[0]) == {"harness", "path", "version", "auth", "verified_at"}

    # signing in flips the same row to ok and opens the gate (idempotent upsert).
    _install(monkeypatch, present={"claude"}, noop_ok=True)
    res = hb.verify_and_record("claude")
    assert res["auth"] == "ok"
    assert hb.has_verified_harness() is True
    assert len(hb.recorded_rows()) == 1  # upsert in place, not a second row


def test_not_installed_records_nothing(graph_db, monkeypatch):
    _install(monkeypatch, present=set())
    res = hb.verify_and_record("codex")
    assert res["recorded"] is False
    assert hb.recorded_rows() == []


# ── Behavioral first-launch gate ──────────────────────────────────────

def test_first_launch_gates_to_bootstrap(test_client, monkeypatch):
    """No verified harness → GET / serves the walkthrough, not the session UI."""
    monkeypatch.setattr(hb, "has_verified_harness", lambda: False)
    r = test_client.get("/", follow_redirects=False)
    assert r.status_code == 200
    assert "Set up your assistant" in r.text


def test_verified_harness_does_not_block(test_client, monkeypatch):
    """A verified harness → GET / falls through to the session UI."""
    monkeypatch.setattr(hb, "has_verified_harness", lambda: True)
    r = test_client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/beads"


def test_bootstrap_page_always_served(test_client):
    r = test_client.get("/bootstrap")
    assert r.status_code == 200
    assert "Set up your assistant" in r.text


def test_screens_speak_interface_not_engineering():
    """User-facing copy states what to do, never how the system works.

    Operator product rule (ruled repeatedly): no engineering narration in
    UI. The screen may instruct ("Run this in a terminal") and report state
    ("Signed in", "Waiting…"); it may never explain mechanism, describe its
    own behavior, or leak internal vocabulary. This is the same guard the
    invitation pages carry, extended to setup.
    """
    from pathlib import Path

    template = (
        Path(__file__).resolve().parents[1] / "templates" / "bootstrap.html"
    ).read_text(encoding="utf-8")
    visible = template[template.index("<body"):template.index("</main>")]
    visible = visible.lower()
    for leaked in ("this page", "own tool", "notices", "no-op", "oauth",
                   "run its command", "probe", "verif", "endpoint",
                   "record", "gate"):
        assert leaked not in visible, leaked


def test_existing_install_passes_through_without_interaction():
    """A machine with a signed-in harness must never be stopped by setup.

    The gate opens once on an established install (the recording set is new
    and empty there). The page's init must handle that case itself: when the
    gate rendered it (pathname "/"), a probe that finds a ready harness gets
    verified — which records the row and closes the gate — and the page
    reloads straight into the session UI. Interaction is only for machines
    where nothing is ready. A deliberate /bootstrap visit never auto-leaves.
    """
    from pathlib import Path

    template = (
        Path(__file__).resolve().parents[1] / "templates" / "bootstrap.html"
    ).read_text(encoding="utf-8")
    assert "screen: 'checking'" in template          # no setup-UI flash
    assert "location.pathname === '/'" in template   # gate-rendered only
    assert "window.location.replace('/')" in template
    # The pass-through path runs before any screen is shown.
    init_body = template[template.index("async init()"):
                         template.index("async refresh()")]
    assert "state === 'ready'" in init_body
    assert "_verify" in init_body


def test_probe_endpoint_returns_harnesses(test_client, monkeypatch):
    _install(monkeypatch, present={"claude"}, noop_ok=True)
    r = test_client.get("/api/bootstrap/probe")
    assert r.status_code == 200
    harnesses = {h["harness"]: h for h in r.json()["harnesses"]}
    assert harnesses["claude"]["state"] == hb.STATE_READY
    assert harnesses["codex"]["state"] == hb.STATE_NOT_INSTALLED


def test_verify_endpoint_records(test_client, graph_db, monkeypatch):
    _install(monkeypatch, present={"claude"}, noop_ok=True)
    r = test_client.post("/api/bootstrap/verify", json={"harness": "claude"})
    assert r.status_code == 200 and r.json()["auth"] == "ok"
    assert hb.has_verified_harness() is True


def test_verify_endpoint_rejects_bad_harness(test_client):
    r = test_client.post("/api/bootstrap/verify", json={"harness": "nope"})
    assert r.status_code == 400
