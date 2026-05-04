"""``--force-host`` global flag — auto-lq20j disaster-recovery escape hatch.

The flag flips ``client._FORCE_HOST_DIRECT`` before any subcommand
dispatches, so ``get_client()`` returns the in-process ``ops`` module
instead of an HttpClient that would try to reach an unavailable
dashboard. Direct writes succeed against the local SQLite DB, and
``setting.changed`` events do NOT fire (no emit hook in this process).
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

import pytest

from tools.graph import client as _client_mod
from tools.graph import cli, ops, schemas
from tools.graph.client import GraphHttpError
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS


@pytest.fixture(autouse=True)
def _isolate_schema_registry():
    snap_s = dict(SCHEMAS)
    snap_u = dict(UPCONVERTERS)
    try:
        yield
    finally:
        SCHEMAS.clear()
        SCHEMAS.update(snap_s)
        UPCONVERTERS.clear()
        UPCONVERTERS.update(snap_u)


@pytest.fixture
def example_schema():
    class V1(schemas.SettingSchema):
        set_id = "autonomy.test.example"
        schema_revision = 1
    schemas.register_schema("autonomy.test.example", 1, V1)
    return V1


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    rc = 0
    saved = sys.argv
    sys.argv = ["graph"] + argv
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as e:
                rc = int(e.code) if e.code is not None else 0
    finally:
        sys.argv = saved
    return rc, out.getvalue(), err.getvalue()


# ── Behaviour: the flag bypasses the API entirely ────────────────


def test_cli_succeeds_when_force_host_passed_and_api_unreachable(
    graph_db_env, example_schema, tmp_path, monkeypatch,
):
    """With ``--force-host`` the CLI never instantiates HttpClient — even if
    every HttpClient call would raise, the write still lands in SQLite
    because the dispatcher returns ``ops`` directly.

    The conftest pins ``_FORCE_HOST_DIRECT = True``; we explicitly clear it
    here so ``main()`` is the one that flips it (proving the flag works
    end-to-end, not just the conftest's pin).
    """
    monkeypatch.setattr(_client_mod, "_FORCE_HOST_DIRECT", False)

    # Tripwire: any HttpClient call would raise. With --force-host this
    # construction must never happen — get_client() must short-circuit to ops.
    def _explode(*a, **kw):
        raise AssertionError("HttpClient must not be constructed under --force-host")
    monkeypatch.setattr(_client_mod, "HttpClient", _explode)

    payload = tmp_path / "p.json"
    payload.write_text(json.dumps({"v": 1}))

    rc, out, err = _run_cli([
        "--force-host",
        "set", "add", "autonomy.test.example#1",
        "--key", "k1", "--from", str(payload),
    ])
    assert rc == 0, f"stderr={err!r} stdout={out!r}"

    # Write actually landed in SQLite via direct ops.* path.
    members = ops.read_set("autonomy.test.example", org=ops.CALLER_ORG).members
    assert len(members) == 1
    assert members[0].key == "k1"
    assert members[0].payload == {"v": 1}


def test_cli_without_force_host_attempts_http_and_surfaces_error(
    graph_db_env, example_schema, tmp_path, monkeypatch,
):
    """Without the flag, ``get_client()`` returns HttpClient and the
    connection error propagates rather than the CLI silently writing to
    SQLite.

    This is the inverse of ``--force-host``: the contract relies on the
    user seeing a connection error so they know to add ``--force-host``
    (or fix the dashboard). The current CLI lets the exception surface
    unwrapped — we don't catch it here, just verify that nothing landed in
    the local DB.
    """
    monkeypatch.setattr(_client_mod, "_FORCE_HOST_DIRECT", False)

    real_http = _client_mod.HttpClient

    def _stub_request(self, *a, **kw):
        raise GraphHttpError(
            "Cannot reach graph API at https://localhost:8080: Connection refused",
            0,
        )

    monkeypatch.setattr(real_http, "_request", _stub_request)

    payload = tmp_path / "p.json"
    payload.write_text(json.dumps({"v": 1}))

    with pytest.raises(GraphHttpError, match="Cannot reach graph API"):
        _run_cli([
            "set", "add", "autonomy.test.example#1",
            "--key", "k_unreached", "--from", str(payload),
        ])

    # Nothing landed in the local DB — the CLI did not silently fall back to ops.
    assert ops.read_set("autonomy.test.example", org=ops.CALLER_ORG).members == []


# ── Visibility: the flag is documented globally ─────────────────


def test_force_host_visible_in_top_level_help():
    """``graph --help`` lists ``--force-host`` so users can find the
    disaster-recovery escape when the dashboard is down."""
    rc, out, _ = _run_cli(["--help"])
    assert rc == 0
    assert "--force-host" in out
    # Help text must explain the trade-off so users don't reach for it casually.
    assert "events" in out.lower() or "disaster" in out.lower()


@pytest.mark.parametrize("subargv", [
    ["set", "add", "x#1", "--force-host", "--key", "k", "--from", "/dev/null"],
    ["note", "--force-host", "anything"],
    ["bead", "--force-host", "anything"],
    ["link", "--force-host", "a", "b"],
])
def test_force_host_global_works_before_any_subcommand(
    graph_db_env, example_schema, monkeypatch, subargv,
):
    """The flag is a top-level option, so passing it before the subcommand
    name flips ``_FORCE_HOST_DIRECT`` regardless of which subcommand
    follows. We don't care if the subcommand itself succeeds (some require
    extra args) — only that ``main()`` parses ``--force-host`` and sets the
    module switch."""
    monkeypatch.setattr(_client_mod, "_FORCE_HOST_DIRECT", False)

    seen = {"flag": None}

    real_main = cli.main

    def _spy_main():
        try:
            real_main()
        finally:
            seen["flag"] = _client_mod._FORCE_HOST_DIRECT

    monkeypatch.setattr(cli, "main", _spy_main)

    # Pass --force-host as a top-level flag, before the subcommand name.
    _run_cli(["--force-host"] + [a for a in subargv if a != "--force-host"])
    assert seen["flag"] is True
