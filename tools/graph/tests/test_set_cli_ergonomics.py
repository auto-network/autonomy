"""CLI ergonomics tests for ``graph set``: dual-positional addressing,
partial-id resolution, ``set read`` (resolved + chain), inline/stdin
payload input, and honest error messages.

Bead: auto-xhimi. Spec: graph://0d3f750f-f9c.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout, redirect_stderr

import pytest

from tools.graph import ops, schemas, cli
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS


# ── shared fixtures ─────────────────────────────────────────


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


@pytest.fixture
def workspace_schema():
    """A schema with a couple of fields so we can exercise merge-patch overrides."""
    class V1(schemas.SettingSchema):
        set_id = "autonomy.test.workspace"
        schema_revision = 1
    schemas.register_schema("autonomy.test.workspace", 1, V1)
    return V1


def _run_cli(argv: list[str], *, stdin: str | None = None) -> tuple[int, str, str]:
    """Drive ``graph`` through ``cli.main``. Returns (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    rc = 0
    saved_argv = sys.argv
    saved_stdin = sys.stdin
    sys.argv = ["graph"] + argv
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
        # io.StringIO is reported as a TTY-by-default-False; helpers check isatty().
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as e:
                rc = int(e.code) if e.code is not None else 0
    finally:
        sys.argv = saved_argv
        sys.stdin = saved_stdin
    return rc, out.getvalue(), err.getvalue()


# ── 1. partial-id resolution ────────────────────────────────


def test_show_accepts_full_uuid(graph_db_env, example_schema):
    sid = ops.add_setting("autonomy.test.example", 1, "k", {"v": 1}, org=ops.CALLER_ORG)
    rc, out, err = _run_cli(["set", "show", sid])
    assert rc == 0, err
    parsed = json.loads(out)
    assert parsed["id"] == sid


def test_show_accepts_12char_prefix(graph_db_env, example_schema):
    sid = ops.add_setting("autonomy.test.example", 1, "k", {"v": 1}, org=ops.CALLER_ORG)
    prefix = sid[:12]
    rc, out, err = _run_cli(["set", "show", prefix])
    assert rc == 0, err
    parsed = json.loads(out)
    assert parsed["id"] == sid


def test_show_accepts_8char_prefix(graph_db_env, example_schema):
    sid = ops.add_setting("autonomy.test.example", 1, "k", {"v": 1}, org=ops.CALLER_ORG)
    rc, out, err = _run_cli(["set", "show", sid[:8]])
    assert rc == 0, err
    parsed = json.loads(out)
    assert parsed["id"] == sid


def test_show_ambiguous_prefix_lists_candidates(graph_db_env, example_schema, monkeypatch):
    """Force two ids to share a common prefix and verify ambiguity surface."""
    import tools.graph.settings_ops as so
    forced_ids = ["abcd1111-aaaa-aaaa-aaaa-000000000001",
                  "abcd1111-bbbb-bbbb-bbbb-000000000002"]
    counter = iter(forced_ids)
    monkeypatch.setattr(so, "uuid4", lambda: next(counter))
    s1 = ops.add_setting("autonomy.test.example", 1, "k1", {"v": 1}, org=ops.CALLER_ORG)
    s2 = ops.add_setting("autonomy.test.example", 1, "k2", {"v": 2}, org=ops.CALLER_ORG)
    assert s1.startswith("abcd1111") and s2.startswith("abcd1111")

    rc, _, err = _run_cli(["set", "show", "abcd1111"])
    assert rc != 0
    assert "ambiguous" in err.lower()
    assert s1 in err and s2 in err


def test_show_no_match_says_so(graph_db_env, example_schema):
    rc, _, err = _run_cli(["set", "show", "deadbeef-no-such-prefix"])
    assert rc != 0
    assert "no Setting" in err


# ── 2. (set_id, key) addressing ─────────────────────────────


def test_show_by_set_id_and_key(graph_db_env, example_schema):
    sid = ops.add_setting("autonomy.test.example", 1, "alice", {"name": "A"}, org=ops.CALLER_ORG)
    rc, out, err = _run_cli(["set", "show", "autonomy.test.example", "alice"])
    assert rc == 0, err
    parsed = json.loads(out)
    assert parsed["id"] == sid
    assert parsed["key"] == "alice"


def test_show_by_set_id_and_key_matches_full_uuid(graph_db_env, example_schema):
    sid = ops.add_setting("autonomy.test.example", 1, "k", {"v": 1}, org=ops.CALLER_ORG)
    _, by_uuid_out, _ = _run_cli(["set", "show", sid])
    _, by_pair_out, _ = _run_cli(["set", "show", "autonomy.test.example", "k"])
    assert json.loads(by_uuid_out) == json.loads(by_pair_out)


def test_show_by_set_id_and_key_no_member(graph_db_env, example_schema):
    rc, _, err = _run_cli(["set", "show", "autonomy.test.example", "ghost"])
    assert rc != 0
    assert "no Setting" in err
    assert "ghost" in err


def test_promote_by_set_id_and_key(graph_db_env, example_schema):
    sid = ops.add_setting("autonomy.test.example", 1, "k", {"v": 1}, org=ops.CALLER_ORG)
    rc, _, err = _run_cli([
        "set", "promote", "autonomy.test.example", "k", "--to", "canonical",
    ])
    assert rc == 0, err
    assert ops.get_setting(sid, org=ops.CALLER_ORG).state == "canonical"


def test_deprecate_by_set_id_and_key(graph_db_env, example_schema):
    sid = ops.add_setting("autonomy.test.example", 1, "k", {"v": 1}, org=ops.CALLER_ORG)
    rc, _, err = _run_cli(["set", "deprecate", "autonomy.test.example", "k"])
    assert rc == 0, err
    assert ops.get_setting(sid, org=ops.CALLER_ORG).deprecated is True


def test_remove_by_set_id_and_key(graph_db_env, example_schema):
    sid = ops.add_setting("autonomy.test.example", 1, "k", {"v": 1}, org=ops.CALLER_ORG)
    rc, _, err = _run_cli(["set", "remove", "autonomy.test.example", "k"])
    assert rc == 0, err
    assert ops.get_setting(sid, org=ops.CALLER_ORG) is None


def test_override_by_set_id_and_key(graph_db_env, example_schema, tmp_path):
    sid = ops.add_setting("autonomy.test.example", 1, "k", {"a": 1, "b": 2}, org=ops.CALLER_ORG)
    p = tmp_path / "ov.json"
    p.write_text(json.dumps({"b": 99}))
    rc, _, err = _run_cli([
        "set", "override", "autonomy.test.example", "k", "--from", str(p),
    ])
    assert rc == 0, err
    members = ops.read_set("autonomy.test.example", org=ops.CALLER_ORG).members
    assert len(members) == 1
    assert members[0].payload == {"a": 1, "b": 99}


def test_exclude_by_set_id_and_key(graph_db_env, example_schema, tmp_path):
    sid = ops.add_setting(
        "autonomy.test.example", 1, "doomed", {"v": 1}, state="canonical",
     org=ops.CALLER_ORG)
    rc, _, err = _run_cli(["set", "exclude", "autonomy.test.example", "doomed"])
    assert rc == 0, err
    assert ops.read_set("autonomy.test.example", org=ops.CALLER_ORG).members == []


# ── 3. graph set read — resolved payload ───────────────────


def test_read_returns_post_merge_payload(graph_db_env, workspace_schema):
    base = ops.add_setting(
        "autonomy.test.workspace", 1, "ws",
        {"name": "Base name", "image": "img:base", "tags": ["t1"]},
        state="canonical",
     org=ops.CALLER_ORG)
    ops.override_setting(base, {"name": "Override name"}, org=ops.CALLER_ORG)
    rc, out, err = _run_cli(["set", "read", "autonomy.test.workspace", "ws"])
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["name"] == "Override name"
    assert payload["image"] == "img:base"
    assert payload["tags"] == ["t1"]


def test_read_by_id_prefix(graph_db_env, workspace_schema):
    base = ops.add_setting(
        "autonomy.test.workspace", 1, "ws",
        {"name": "Base", "image": "i"},
        state="canonical",
     org=ops.CALLER_ORG)
    ops.override_setting(base, {"name": "Over"}, org=ops.CALLER_ORG)
    rc, out, err = _run_cli(["set", "read", base[:8]])
    assert rc == 0, err
    payload = json.loads(out)
    assert payload["name"] == "Over"
    assert payload["image"] == "i"


def test_read_no_member_errors(graph_db_env, workspace_schema):
    rc, _, err = _run_cli(["set", "read", "autonomy.test.workspace", "ghost"])
    assert rc != 0
    assert "no member" in err.lower() or "no setting" in err.lower()


# ── 4. graph set read --chain ──────────────────────────────


def test_read_chain_shows_layers(graph_db_env, workspace_schema):
    base = ops.add_setting(
        "autonomy.test.workspace", 1, "ws",
        {"name": "Base", "image": "img:base"},
        state="canonical",
     org=ops.CALLER_ORG)
    ov_id = ops.override_setting(base, {"name": "Override"}, org=ops.CALLER_ORG)
    rc, out, err = _run_cli([
        "set", "read", "autonomy.test.workspace", "ws", "--chain",
    ])
    assert rc == 0, err
    chain = json.loads(out)
    assert chain["set_id"] == "autonomy.test.workspace"
    assert chain["key"] == "ws"
    assert len(chain["layers"]) == 2
    assert chain["layers"][0]["kind"] == "base"
    assert chain["layers"][0]["id"] == base
    assert chain["layers"][0]["patch"] == {"name": "Base", "image": "img:base"}
    assert chain["layers"][1]["kind"] == "override"
    assert chain["layers"][1]["id"] == ov_id
    assert chain["layers"][1]["patch"] == {"name": "Override"}
    assert chain["layers"][1]["result"] == {"name": "Override", "image": "img:base"}
    assert chain["final"] == {"name": "Override", "image": "img:base"}


def test_read_chain_no_overrides(graph_db_env, workspace_schema):
    base = ops.add_setting(
        "autonomy.test.workspace", 1, "ws", {"name": "Only"}, state="canonical",
     org=ops.CALLER_ORG)
    rc, out, err = _run_cli([
        "set", "read", "autonomy.test.workspace", "ws", "--chain",
    ])
    assert rc == 0, err
    chain = json.loads(out)
    assert len(chain["layers"]) == 1
    assert chain["layers"][0]["kind"] == "base"
    assert chain["final"] == {"name": "Only"}


def test_read_chain_multiple_overrides(graph_db_env, workspace_schema):
    base = ops.add_setting(
        "autonomy.test.workspace", 1, "ws",
        {"a": 1, "b": 2, "c": 3},
        state="canonical",
     org=ops.CALLER_ORG)
    ops.override_setting(base, {"b": 20}, org=ops.CALLER_ORG)
    ops.override_setting(base, {"c": 30}, org=ops.CALLER_ORG)
    rc, out, err = _run_cli([
        "set", "read", "autonomy.test.workspace", "ws", "--chain",
    ])
    assert rc == 0, err
    chain = json.loads(out)
    # 1 base + 2 overrides
    assert len(chain["layers"]) == 3
    # final reflects both overrides
    assert chain["final"]["a"] == 1
    assert chain["final"]["b"] == 20
    assert chain["final"]["c"] == 30


# ── 5. inline / stdin payload input ────────────────────────


def test_add_inline_payload(graph_db_env, example_schema):
    rc, out, err = _run_cli([
        "set", "add", "autonomy.test.example#1",
        "--key", "x", "--inline", '{"name":"X","val":7}',
    ])
    assert rc == 0, err
    members = ops.read_set("autonomy.test.example", org=ops.CALLER_ORG).members
    assert len(members) == 1
    assert members[0].payload == {"name": "X", "val": 7}


def test_override_from_dash_stdin(graph_db_env, example_schema, tmp_path):
    base_p = tmp_path / "p.json"
    base_p.write_text(json.dumps({"a": 1, "b": 2}))
    _run_cli([
        "set", "add", "autonomy.test.example#1",
        "--key", "k", "--from", str(base_p),
    ])
    base_id = ops.read_set("autonomy.test.example", org=ops.CALLER_ORG).members[0].id
    rc, _, err = _run_cli(
        ["set", "override", base_id, "--from", "-"],
        stdin='{"b":99}',
    )
    assert rc == 0, err
    members = ops.read_set("autonomy.test.example", org=ops.CALLER_ORG).members
    assert members[0].payload == {"a": 1, "b": 99}


def test_add_implicit_stdin_when_neither_given(graph_db_env, example_schema):
    rc, _, err = _run_cli(
        ["set", "add", "autonomy.test.example#1", "--key", "k"],
        stdin='{"name": "from-stdin"}',
    )
    assert rc == 0, err
    members = ops.read_set("autonomy.test.example", org=ops.CALLER_ORG).members
    assert members[0].payload == {"name": "from-stdin"}


def test_add_inline_and_from_mutually_exclusive(graph_db_env, example_schema, tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"v": 1}))
    rc, _, err = _run_cli([
        "set", "add", "autonomy.test.example#1",
        "--key", "k", "--inline", '{"v":1}', "--from", str(p),
    ])
    assert rc != 0
    assert "mutually exclusive" in err.lower()


def test_add_inline_invalid_json(graph_db_env, example_schema):
    rc, _, err = _run_cli([
        "set", "add", "autonomy.test.example#1",
        "--key", "k", "--inline", "not json",
    ])
    assert rc != 0
    assert "invalid" in err.lower()


# ── 6. honest error messages ───────────────────────────────


def test_show_not_found_does_not_mention_as_rev_when_unused(graph_db_env, example_schema):
    """Without --as-rev, error should NOT name --as-rev."""
    rc, _, err = _run_cli(["set", "show", "ffffffff-ffff-ffff-ffff-ffffffffffff"])
    assert rc != 0
    assert "--as-rev" not in err


def test_show_not_found_mentions_as_rev_only_when_used(graph_db_env, example_schema):
    """With --as-rev for a real id with no upconvert path, the hint may be relevant."""
    sid = ops.add_setting("autonomy.test.example", 1, "k", {"v": 1}, org=ops.CALLER_ORG)
    # Stored at rev 1, ask for rev 99 with no upconverter — get_setting returns None.
    rc, _, err = _run_cli(["set", "show", sid, "--as-rev", "99"])
    assert rc != 0
    assert "--as-rev" in err


# ── 7. resolve_setting_strict ops-layer behavior ───────────


def test_resolve_setting_strict_unique(graph_db_env, example_schema):
    sid = ops.add_setting("autonomy.test.example", 1, "k", {"v": 1}, org=ops.CALLER_ORG)
    hit = ops.resolve_setting_strict(sid[:8], org=ops.CALLER_ORG)
    assert isinstance(hit, dict)
    assert hit["id"] == sid


def test_resolve_setting_strict_ambiguous(graph_db_env, example_schema, monkeypatch):
    import tools.graph.settings_ops as so
    forced_ids = ["aaaa1111-aaaa-aaaa-aaaa-000000000001",
                  "aaaa1111-bbbb-bbbb-bbbb-000000000002"]
    counter = iter(forced_ids)
    monkeypatch.setattr(so, "uuid4", lambda: next(counter))
    ops.add_setting("autonomy.test.example", 1, "k1", {"v": 1}, org=ops.CALLER_ORG)
    ops.add_setting("autonomy.test.example", 1, "k2", {"v": 2}, org=ops.CALLER_ORG)
    hit = ops.resolve_setting_strict("aaaa1111", org=ops.CALLER_ORG)
    assert isinstance(hit, list)
    assert len(hit) == 2


def test_resolve_setting_strict_none(graph_db_env, example_schema):
    assert ops.resolve_setting_strict("no-such-id", org=ops.CALLER_ORG) is None


# ── 8. resolve_set_key ops-layer behavior ──────────────────


def test_resolve_set_key_returns_base_after_overrides(graph_db_env, workspace_schema):
    base = ops.add_setting(
        "autonomy.test.workspace", 1, "ws",
        {"name": "Base"}, state="canonical",
     org=ops.CALLER_ORG)
    ops.override_setting(base, {"name": "Override"}, org=ops.CALLER_ORG)
    hit = ops.resolve_set_key("autonomy.test.workspace", "ws", org=ops.CALLER_ORG)
    assert isinstance(hit, dict)
    # Should return the BASE row id (matches read_set's chosen member).
    assert hit["id"] == base


def test_resolve_set_key_none(graph_db_env, workspace_schema):
    assert ops.resolve_set_key("autonomy.test.workspace", "ghost", org=ops.CALLER_ORG) is None


# ── 9. chain_setting ops-layer behavior ───────────────────


def test_chain_setting_no_overrides(graph_db_env, workspace_schema):
    ops.add_setting(
        "autonomy.test.workspace", 1, "ws",
        {"name": "Only"}, state="canonical",
     org=ops.CALLER_ORG)
    chain = ops.chain_setting("autonomy.test.workspace", "ws", org=ops.CALLER_ORG)
    assert chain is not None
    assert len(chain["layers"]) == 1
    assert chain["layers"][0]["kind"] == "base"


def test_chain_setting_with_override(graph_db_env, workspace_schema):
    base = ops.add_setting(
        "autonomy.test.workspace", 1, "ws",
        {"name": "Base", "image": "img"}, state="canonical",
     org=ops.CALLER_ORG)
    ov = ops.override_setting(base, {"name": "Over"}, org=ops.CALLER_ORG)
    chain = ops.chain_setting("autonomy.test.workspace", "ws", org=ops.CALLER_ORG)
    assert chain is not None
    assert len(chain["layers"]) == 2
    assert chain["layers"][0]["id"] == base
    assert chain["layers"][1]["id"] == ov
    assert chain["final"] == {"name": "Over", "image": "img"}


def test_chain_setting_none_for_missing(graph_db_env, workspace_schema):
    assert ops.chain_setting("autonomy.test.workspace", "ghost", org=ops.CALLER_ORG) is None
