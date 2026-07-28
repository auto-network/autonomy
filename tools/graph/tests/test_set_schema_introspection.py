"""Schema introspection tests for ``graph set schema/example/find``.

Bead: auto-82xyq. Covers:
- ``register_schema()`` + first DB op flushes schema + synopsis meta-Settings
- ``set schema`` pretty-prints required/optional fields with descriptions,
  enum choices, defaults, and list-element shapes
- ``set example`` produces a stub that round-trips through ``set add``
- ``set find`` ranks the workspace schema first for noun-layer queries
  (rename workspace, dispatch labels, harness codex)
- Field-level validation errors name the failing field + expected shape
- Bootstrap-flush idempotency: a second connection doesn't duplicate rows
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout, redirect_stderr

import pytest

from tools.graph import ops, schemas, cli
from tools.graph.schemas.registry import (
    SCHEMAS,
    UPCONVERTERS,
    SCHEMA_META_SET_ID,
    SYNOPSIS_META_SET_ID,
    flush_schema_meta,
)


# ── shared fixtures ─────────────────────────────────────────


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin GRAPH_DB to an empty per-test sqlite file. The first DB op
    triggers the lazy schema-meta flush against this fresh DB."""
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


@pytest.fixture(autouse=True)
def _isolate_schema_registry():
    """Snapshot SCHEMAS/UPCONVERTERS so per-test registrations don't leak.

    The flush walks the live SCHEMAS dict, so anything registered during a
    test will be flushed on the next DB op. Restoring after teardown keeps
    one test's stub schemas from polluting the next test's expectations.
    """
    schemas_snap = dict(SCHEMAS)
    upcon_snap = dict(UPCONVERTERS)
    try:
        yield
    finally:
        SCHEMAS.clear()
        SCHEMAS.update(schemas_snap)
        UPCONVERTERS.clear()
        UPCONVERTERS.update(upcon_snap)


def _run_cli(argv: list[str], *, stdin: str | None = None) -> tuple[int, str, str]:
    """Drive ``graph`` through ``cli.main``. Returns (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    rc = 0
    saved_argv = sys.argv
    saved_stdin = sys.stdin
    sys.argv = ["graph"] + argv
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
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


def _trigger_flush(graph_db_env):
    """Force at least one writable connection so ``flush_schema_meta`` runs."""
    ops.list_set_ids(org=ops.CALLER_ORG)


# ── 1. registration upserts on import + first DB op ─────────


def test_registered_schemas_appear_as_meta_settings(graph_db_env):
    _trigger_flush(graph_db_env)
    members = ops.read_set(SCHEMA_META_SET_ID, org=ops.CALLER_ORG).members
    keys = {m.key for m in members}
    # All real autonomy schemas should be flushed in.
    for expected in (
        "autonomy.workspace#1",
        "autonomy.org#1",
        "autonomy.workspace.mount#1",
        "autonomy.workspace.artifact#1",
        "autonomy.capability.contract#1",
    ):
        assert expected in keys, f"missing schema meta-Setting: {expected}"


def test_workspace_schema_payload_shape(graph_db_env):
    _trigger_flush(graph_db_env)
    members = ops.read_set(SCHEMA_META_SET_ID, org=ops.CALLER_ORG).members
    ws = next(m for m in members if m.key == "autonomy.workspace#1")
    payload = ws.payload
    assert payload["set_id"] == "autonomy.workspace"
    assert payload["schema_revision"] == 1
    assert payload["type"] == "object"
    assert "name" in payload["required"]
    assert "image" in payload["required"]
    properties = payload["properties"]
    # Description is non-derivable from validator code — comes from
    # _field_metadata. Its presence is the contract.
    assert properties["name"]["description"]
    assert properties["image"]["description"]
    # Enum + default for harness.
    assert properties["harness"]["enum"] == ["claude", "codex"]
    assert properties["harness"]["default"] == "claude"
    # Element shape for repos.
    repos = properties["repos"]
    assert repos["type"] == "array"
    assert "url" in repos["element"]
    assert "mount" in repos["element"]


def test_synopses_appear_for_every_autonomy_schema(graph_db_env):
    _trigger_flush(graph_db_env)
    syn_members = ops.read_set(SYNOPSIS_META_SET_ID, org=ops.CALLER_ORG).members
    syn_keys = {m.key for m in syn_members}
    autonomy_keys = {
        sk for sk, _ in SCHEMAS.items() if sk.split(".", 1)[0] == "autonomy"
    }
    missing = autonomy_keys - syn_keys
    assert not missing, f"missing synopses for: {sorted(missing)}"
    # And dashboard.agent-actions ships a synopsis too.
    assert "dashboard.agent-actions#1" in syn_keys


# ── 2. set schema CLI ───────────────────────────────────────


def test_set_schema_prints_required_optional_descriptions(graph_db_env):
    _trigger_flush(graph_db_env)
    rc, out, err = _run_cli(["set", "schema", "autonomy.workspace"])
    assert rc == 0, err
    assert "Required fields:" in out
    assert "Optional fields:" in out
    # Required: name + image
    assert "name (string)" in out
    assert "image (string)" in out
    # Optional: harness with enum + default
    assert "harness (string)" in out
    assert "[enum: claude, codex]" in out
    assert "[default: \"claude\"]" in out
    # Element shape for repos
    assert "Element shape:" in out
    assert "url (string)  [required]" in out
    assert "mount (string)  [required]" in out
    # Descriptions come from _field_metadata, not the validator code
    assert "Workspace identifier" in out
    assert "Container image" in out


def test_set_schema_explicit_revision(graph_db_env):
    _trigger_flush(graph_db_env)
    rc, out, err = _run_cli(["set", "schema", "autonomy.workspace#1"])
    assert rc == 0, err
    assert "autonomy.workspace#1" in out


def test_set_schema_unknown_set_id(graph_db_env):
    _trigger_flush(graph_db_env)
    rc, _, err = _run_cli(["set", "schema", "autonomy.nonexistent"])
    assert rc != 0
    assert "no schema registered" in err


def test_set_schema_unknown_revision(graph_db_env):
    _trigger_flush(graph_db_env)
    rc, _, err = _run_cli(["set", "schema", "autonomy.workspace#99"])
    assert rc != 0
    assert "no schema for autonomy.workspace#99" in err


# ── 3. set example CLI ──────────────────────────────────────


def test_set_example_emits_required_fields(graph_db_env):
    _trigger_flush(graph_db_env)
    rc, out, err = _run_cli(["set", "example", "autonomy.workspace"])
    assert rc == 0, err
    stub = json.loads(out)
    assert "name" in stub
    assert "image" in stub
    # Optional fields are omitted from the stub.
    assert "harness" not in stub


def test_set_example_round_trips_through_set_add(graph_db_env, tmp_path):
    _trigger_flush(graph_db_env)
    rc, out, err = _run_cli(["set", "example", "autonomy.workspace"])
    assert rc == 0, err
    p = tmp_path / "ws.json"
    p.write_text(out)
    rc2, out2, err2 = _run_cli([
        "set", "add", "autonomy.workspace#1", "--key", "from-example",
        "--from", str(p),
    ])
    assert rc2 == 0, err2
    members = ops.read_set("autonomy.workspace", org=ops.CALLER_ORG).members
    assert any(m.key == "from-example" for m in members)


# ── 4. set find CLI (noun-layer discoverability) ────────────


def test_set_find_rename_workspace_returns_workspace_first(graph_db_env):
    _trigger_flush(graph_db_env)
    rc, out, err = _run_cli(["set", "find", "rename", "workspace"])
    assert rc == 0, err
    first_line = out.strip().splitlines()[0]
    assert first_line.startswith("autonomy.workspace#1")


def test_set_find_dispatch_labels_returns_workspace(graph_db_env):
    _trigger_flush(graph_db_env)
    rc, out, err = _run_cli(["set", "find", "dispatch", "labels"])
    assert rc == 0, err
    assert "autonomy.workspace#1" in out


def test_set_find_harness_codex_finds_workspace_via_schema_text(graph_db_env):
    """`codex` only appears as an enum value in the workspace schema —
    not in any synopsis — so this also exercises the schema-text fallback
    in ``_haystack_for_synopsis``.
    """
    _trigger_flush(graph_db_env)
    rc, out, err = _run_cli(["set", "find", "harness", "codex"])
    assert rc == 0, err
    assert "autonomy.workspace#1" in out


def test_set_find_no_match(graph_db_env):
    _trigger_flush(graph_db_env)
    rc, out, err = _run_cli(["set", "find", "zzzz-nonexistent-token"])
    assert rc == 0, err
    assert "no schema matches" in out


def test_set_find_requires_at_least_one_term(graph_db_env):
    _trigger_flush(graph_db_env)
    rc, _, err = _run_cli(["set", "find"])
    # argparse rejects when nargs="+" gets nothing.
    assert rc != 0


# ── 5. field-level validation errors ────────────────────────


def test_bad_harness_names_field_and_lists_choices(graph_db_env):
    _trigger_flush(graph_db_env)
    bad = json.dumps({"name": "x", "image": "y", "harness": "gemini"})
    rc, _, err = _run_cli(
        [
            "set", "add", "autonomy.workspace#1", "--key", "bad-harness",
            "--from", "-",
        ],
        stdin=bad,
    )
    assert rc != 0
    # The CLI must surface the validator's message verbatim — generic
    # prefixes hide the field-level signal that operators need.
    assert "'harness'" in err
    assert "claude" in err
    assert "codex" in err
    assert "gemini" in err


def test_validation_error_does_not_get_generic_prefix(graph_db_env):
    """The CLI used to wrap with 'Error: schema validation failed:'.
    That prefix is gone — only ``Error: <validator message>`` remains.
    """
    _trigger_flush(graph_db_env)
    bad = json.dumps({"name": "x"})  # missing required 'image'
    rc, _, err = _run_cli(
        [
            "set", "add", "autonomy.workspace#1", "--key", "miss-image",
            "--from", "-",
        ],
        stdin=bad,
    )
    assert rc != 0
    assert "schema validation failed" not in err
    assert "'image'" in err


# ── 6. bootstrap idempotency ────────────────────────────────


def test_second_connection_does_not_duplicate_meta_rows(graph_db_env):
    """Open + close two writable GraphDBs against the same path. The second
    flush should be a payload-equality no-op — ``read_set`` must still
    show exactly one row per (set_id, key).
    """
    from tools.graph.db import GraphDB

    db1 = GraphDB(graph_db_env)
    db1.close()
    db2 = GraphDB(graph_db_env)
    db2.close()

    members = ops.read_set(SCHEMA_META_SET_ID, org=ops.CALLER_ORG).members
    seen: dict[str, int] = {}
    for m in members:
        seen[m.key] = seen.get(m.key, 0) + 1
    duplicates = {k: c for k, c in seen.items() if c > 1}
    assert not duplicates, f"duplicate meta rows: {duplicates}"


def test_explicit_flush_is_idempotent(graph_db_env):
    from tools.graph.db import GraphDB

    db = GraphDB(graph_db_env)
    try:
        # Two extra explicit flushes after the auto one in _init_schema.
        flush_schema_meta(db)
        flush_schema_meta(db)
    finally:
        db.close()
    members = ops.read_set(SCHEMA_META_SET_ID, org=ops.CALLER_ORG).members
    keys = [m.key for m in members]
    # No duplicates.
    assert len(keys) == len(set(keys))


# ── 7. registration of a fresh schema flushes on next DB op ─
#
# ``_DemoSchema`` is built inside the tests so its
# ``__init_subclass__`` auto-registration is scoped to the test that
# uses it; the per-test ``_isolate_schema_registry`` fixture cleans it
# up afterward. A module-level definition would auto-register at import
# time and pollute every other test in the file.


def _build_demo_schema() -> type:
    class _DemoSchema(schemas.SettingSchema):
        set_id = "autonomy.test.demo"
        schema_revision = 1

        _field_metadata = {
            "label": {
                "type": "string",
                "required": True,
                "description": "Demo label field",
            },
            "count": {
                "type": "integer",
                "description": "Demo count field",
                "default": 0,
            },
        }
    return _DemoSchema


def test_newly_registered_schema_flushes_on_first_db_op(graph_db_env):
    _build_demo_schema()  # auto-registers via __init_subclass__

    # Force a writable connection so the lazy flush fires.
    _trigger_flush(graph_db_env)

    rc, out, err = _run_cli(["set", "schema", "autonomy.test.demo"])
    assert rc == 0, err
    assert "label (string)" in out
    assert "count (integer)" in out
    assert "Demo label field" in out


def test_register_schema_synopsis_round_trip(graph_db_env, monkeypatch):
    """A module-level SYNOPSIS dict surfaces in autonomy.schema.synopsis."""
    demo_cls = _build_demo_schema()

    # Stamp a SYNOPSIS onto _DemoSchema's defining module.
    import sys as _sys
    mod = _sys.modules[demo_cls.__module__]
    monkeypatch.setattr(
        mod,
        "SYNOPSIS",
        {
            "summary": "Demo schema for round-trip tests",
            "nouns": ["demo", "round-trip"],
            "related_set_ids": [],
        },
        raising=False,
    )

    _trigger_flush(graph_db_env)

    syn_members = ops.read_set(SYNOPSIS_META_SET_ID, org=ops.CALLER_ORG).members
    demo = next(
        (m for m in syn_members if m.key == "autonomy.test.demo#1"), None,
    )
    assert demo is not None
    assert demo.payload["summary"] == "Demo schema for round-trip tests"

    rc, out, err = _run_cli(["set", "find", "round-trip"])
    assert rc == 0, err
    assert "autonomy.test.demo#1" in out


# ── Schema-meta flush across a _SCHEMA_USER_VERSION bump ─────────────

def test_stale_version_db_picks_up_registered_schemas(tmp_path, monkeypatch):
    """A DB stamped at an older schema version re-flushes on next open.

    ``_init_schema`` short-circuits when ``PRAGMA user_version`` already
    equals ``_SCHEMA_USER_VERSION``, which means a newly registered
    Setting schema never lands as an ``autonomy.schema#1`` row on an
    existing database until that constant is bumped. When it is bumped,
    the next writable open must materialise every registered schema —
    otherwise ``graph set schema <set_id>`` reports schemas as
    unregistered even though their module imported cleanly.
    """
    from tools.graph.db import GraphDB, _SCHEMA_USER_VERSION

    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()

    db = GraphDB.create_org_db("stale-probe")
    db_path = db.db_path
    expected = set(SCHEMAS)
    assert expected, "registry should not be empty"

    # Simulate a database created before the newest schemas existed:
    # drop their meta rows and stamp the previous version.
    db.conn.execute(f"DELETE FROM settings WHERE set_id = '{SCHEMA_META_SET_ID}'")
    db.conn.execute(f"PRAGMA user_version = {_SCHEMA_USER_VERSION - 1}")
    db.conn.commit()
    db.close()
    GraphDB.close_all_pooled()

    reopened = GraphDB(str(db_path), mode="rw")
    try:
        rows = {
            r[0] for r in reopened.conn.execute(
                f"SELECT key FROM settings WHERE set_id = '{SCHEMA_META_SET_ID}'"
            ).fetchall()
        }
        missing = expected - rows
        assert not missing, (
            "registered schemas absent from the meta-Settings flush after a "
            f"version bump: {sorted(missing)}"
        )
        assert reopened.conn.execute(
            "PRAGMA user_version"
        ).fetchone()[0] == _SCHEMA_USER_VERSION
    finally:
        reopened.close()
        GraphDB.close_all_pooled()


def test_primer_overlay_schemas_are_registered():
    """The org/workspace primer overlays must be discoverable via set schema."""
    assert "autonomy.org.primer#1" in SCHEMAS
    assert "autonomy.workspace.primer#1" in SCHEMAS


def test_primer_overlay_example_stub_is_useful():
    """``set example`` must emit a body field, not an empty object.

    Both overlay fields were optional at first, so the stub generator
    produced ``{}`` — an operator copying it would write a row that
    renders nothing. ``markdown`` is required precisely so the stub
    carries the field the operator has to fill in.
    """
    for set_id in ("autonomy.org.primer#1", "autonomy.workspace.primer#1"):
        schema = SCHEMAS[set_id].export_json_schema()
        assert "markdown" in schema.get("required", []), (
            f"{set_id}: 'markdown' must be required so the example stub "
            f"is not empty"
        )


def test_primer_overlay_rejects_bodyless_payload():
    """A row with no markdown is a no-op row; reject it at write time."""
    from tools.graph.schemas.registry import SchemaValidationError

    for set_id in ("autonomy.org.primer#1", "autonomy.workspace.primer#1"):
        cls = SCHEMAS[set_id]
        with pytest.raises(SchemaValidationError, match="required"):
            cls.validate({"enabled": False})
        cls.validate({"markdown": "## Fine", "enabled": False})
