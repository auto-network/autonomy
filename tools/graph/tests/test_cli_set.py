"""CLI tests for ``graph set`` subcommand group.

Covers parse + dispatch for every subcommand, plus end-to-end add/show/members
against a real GraphDB. Spec: graph://0d3f750f-f9c.
"""

from __future__ import annotations

import io
import json
import os
import sys
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pytest

from tools.graph import ops, schemas, cli, set_cmd
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


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
def example_schema():
    class V1(schemas.SettingSchema):
        set_id = "autonomy.test.example"
        schema_revision = 1
    schemas.register_schema("autonomy.test.example", 1, V1)
    return V1


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Drive ``graph`` through ``cli.main``. Returns (rc, stdout, stderr)."""
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


# ── parse + dispatch coverage ───────────────────────────────


def test_parser_recognises_set_list(graph_db_env):
    rc, out, _ = _run_cli(["set", "list"])
    assert rc == 0
    assert "no Settings yet" in out


def test_parser_set_members_no_set_id_errors(graph_db_env):
    rc, _, err = _run_cli(["set", "members"])
    assert rc != 0


def test_parser_set_show_requires_id(graph_db_env):
    rc, _, _ = _run_cli(["set", "show"])
    assert rc != 0


def test_parser_set_add_requires_args(graph_db_env):
    rc, _, _ = _run_cli(["set", "add", "x#1"])  # missing --key/--from
    assert rc != 0


def test_parser_set_promote_validates_state(graph_db_env, example_schema):
    sid = ops.add_setting("autonomy.test.example", 1, "k", {"v": 1})
    rc, _, err = _run_cli(["set", "promote", sid, "--to", "garbage"])
    assert rc != 0


def test_parser_set_migrate_requires_to_rev(graph_db_env):
    rc, _, _ = _run_cli(["set", "migrate", "autonomy.test.example"])
    assert rc != 0


# ── functional add/show/members/list ────────────────────────


def test_cli_add_then_show(graph_db_env, example_schema, tmp_path):
    payload = tmp_path / "p.json"
    payload.write_text(json.dumps({"name": "Alice"}))
    rc, out, err = _run_cli([
        "set", "add", "autonomy.test.example#1",
        "--key", "alice", "--from", str(payload),
    ])
    assert rc == 0, err
    assert "Setting:" in out

    members = ops.read_set("autonomy.test.example")
    assert len(members.members) == 1
    sid = members.members[0].id

    rc, out, err = _run_cli(["set", "show", sid])
    assert rc == 0, err
    parsed = json.loads(out)
    assert parsed["payload"] == {"name": "Alice"}
    assert parsed["key"] == "alice"


def test_cli_members_lists_rows(graph_db_env, example_schema, tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"name": "x"}))
    _run_cli(["set", "add", "autonomy.test.example#1", "--key", "k1", "--from", str(p)])
    _run_cli(["set", "add", "autonomy.test.example#1", "--key", "k2", "--from", str(p)])
    rc, out, _ = _run_cli(["set", "members", "autonomy.test.example"])
    assert rc == 0
    assert "k1" in out and "k2" in out


def test_cli_list_includes_known_set_ids(graph_db_env, example_schema, tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"v": 1}))
    _run_cli(["set", "add", "autonomy.test.example#1", "--key", "k", "--from", str(p)])
    rc, out, _ = _run_cli(["set", "list"])
    assert rc == 0
    assert "autonomy.test.example" in out


# ── override / exclude / promote / deprecate / remove ──────


def test_cli_override_merges(graph_db_env, example_schema, tmp_path):
    base_payload = tmp_path / "base.json"
    base_payload.write_text(json.dumps({"a": 1, "b": 2}))
    _run_cli(["set", "add", "autonomy.test.example#1", "--key", "k", "--from", str(base_payload)])
    base = ops.read_set("autonomy.test.example").members[0].id

    over_payload = tmp_path / "over.json"
    over_payload.write_text(json.dumps({"b": 99}))
    rc, out, err = _run_cli(["set", "override", base, "--from", str(over_payload)])
    assert rc == 0, err
    members = ops.read_set("autonomy.test.example").members
    assert len(members) == 1
    assert members[0].payload == {"a": 1, "b": 99}


def test_cli_exclude_drops_target(graph_db_env, example_schema, tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"v": 1}))
    _run_cli(["set", "add", "autonomy.test.example#1", "--key", "doomed",
              "--from", str(p), "--state", "canonical"])
    target = ops.read_set("autonomy.test.example").members[0].id
    rc, _, _ = _run_cli(["set", "exclude", target])
    assert rc == 0
    assert ops.read_set("autonomy.test.example").members == []


def test_cli_promote(graph_db_env, example_schema, tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"v": 1}))
    _run_cli(["set", "add", "autonomy.test.example#1", "--key", "k", "--from", str(p)])
    sid = ops.read_set("autonomy.test.example").members[0].id
    rc, _, _ = _run_cli(["set", "promote", sid, "--to", "canonical"])
    assert rc == 0
    assert ops.get_setting(sid).state == "canonical"


def test_cli_deprecate(graph_db_env, example_schema, tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"v": 1}))
    _run_cli(["set", "add", "autonomy.test.example#1", "--key", "k", "--from", str(p)])
    sid = ops.read_set("autonomy.test.example").members[0].id
    rc, _, _ = _run_cli(["set", "deprecate", sid])
    assert rc == 0
    assert ops.get_setting(sid).deprecated is True


def test_cli_remove_raw(graph_db_env, example_schema, tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"v": 1}))
    _run_cli(["set", "add", "autonomy.test.example#1", "--key", "k", "--from", str(p)])
    sid = ops.read_set("autonomy.test.example").members[0].id
    rc, _, _ = _run_cli(["set", "remove", sid])
    assert rc == 0
    assert ops.get_setting(sid) is None


def test_cli_remove_canonical_blocked(graph_db_env, example_schema, tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"v": 1}))
    _run_cli(["set", "add", "autonomy.test.example#1", "--key", "k",
              "--from", str(p), "--state", "canonical"])
    sid = ops.read_set("autonomy.test.example").members[0].id
    rc, _, err = _run_cli(["set", "remove", sid])
    assert rc != 0
    assert "deprecate first" in err.lower() or "raw" in err.lower()


# ── migrate ────────────────────────────────────────────────


def test_cli_migrate_dry_run(graph_db_env, tmp_path):
    class V1(schemas.SettingSchema):
        set_id = "autonomy.test.lin"
        schema_revision = 1
    class V2(schemas.SettingSchema):
        set_id = "autonomy.test.lin"
        schema_revision = 2
    schemas.register_schema("autonomy.test.lin", 1, V1)
    schemas.register_schema("autonomy.test.lin", 2, V2,
                            upconvert_from_prev=lambda p: {**p, "v2": True})

    sid = ops.add_setting("autonomy.test.lin", 1, "k", {"x": 1})
    rc, out, err = _run_cli(["set", "migrate", "autonomy.test.lin",
                              "--to-rev", "2", "--dry-run"])
    assert rc == 0, err
    assert "DRY RUN" in out
    assert ops.get_setting(sid).stored_revision == 1


def test_cli_migrate_writes(graph_db_env):
    class V1(schemas.SettingSchema):
        set_id = "autonomy.test.lin"
        schema_revision = 1
    class V2(schemas.SettingSchema):
        set_id = "autonomy.test.lin"
        schema_revision = 2
    schemas.register_schema("autonomy.test.lin", 1, V1)
    schemas.register_schema("autonomy.test.lin", 2, V2,
                            upconvert_from_prev=lambda p: {**p, "v2": True})

    sid = ops.add_setting("autonomy.test.lin", 1, "k", {"x": 1})
    rc, out, _ = _run_cli(["set", "migrate", "autonomy.test.lin", "--to-rev", "2"])
    assert rc == 0
    got = ops.get_setting(sid)
    assert got.stored_revision == 2
    assert got.payload == {"x": 1, "v2": True}


# ── read flags pass through ────────────────────────────────


def test_cli_members_as_rev_upconverts(graph_db_env):
    class V1(schemas.SettingSchema):
        set_id = "autonomy.test.up"
        schema_revision = 1
    class V2(schemas.SettingSchema):
        set_id = "autonomy.test.up"
        schema_revision = 2
    schemas.register_schema("autonomy.test.up", 1, V1)
    schemas.register_schema("autonomy.test.up", 2, V2,
                            upconvert_from_prev=lambda p: {**p, "v2": True})
    ops.add_setting("autonomy.test.up", 1, "k", {"x": 1})
    rc, out, _ = _run_cli(["set", "members", "autonomy.test.up", "--as-rev", "2"])
    assert rc == 0
    assert "k" in out
    # TARGET column should show 2.
    assert "2" in out


def test_cli_members_min_rev_drops(graph_db_env):
    class V1(schemas.SettingSchema):
        set_id = "autonomy.test.floor"
        schema_revision = 1
    class V2(schemas.SettingSchema):
        set_id = "autonomy.test.floor"
        schema_revision = 2
    schemas.register_schema("autonomy.test.floor", 1, V1)
    schemas.register_schema("autonomy.test.floor", 2, V2)
    ops.add_setting("autonomy.test.floor", 1, "k1", {"x": 1})
    ops.add_setting("autonomy.test.floor", 2, "k2", {"x": 2})
    rc, out, _ = _run_cli(["set", "members", "autonomy.test.floor", "--min-rev", "2"])
    assert rc == 0
    assert "k2" in out
    assert "k1" not in out


def test_cli_members_stored_rev_filter(graph_db_env):
    class V1(schemas.SettingSchema):
        set_id = "autonomy.test.flt"
        schema_revision = 1
    class V2(schemas.SettingSchema):
        set_id = "autonomy.test.flt"
        schema_revision = 2
    schemas.register_schema("autonomy.test.flt", 1, V1)
    schemas.register_schema("autonomy.test.flt", 2, V2)
    ops.add_setting("autonomy.test.flt", 1, "k1", {"x": 1})
    ops.add_setting("autonomy.test.flt", 2, "k2", {"x": 2})
    rc, out, _ = _run_cli(["set", "members", "autonomy.test.flt", "--stored-rev", "1"])
    assert rc == 0
    assert "k1" in out
    assert "k2" not in out
