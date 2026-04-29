"""Tests for ``dashboard.agent-actions#1`` schema + autonomy seed migration
(auto-pqgrl).

Covers:

* ``dashboard.agent-actions#1`` is registered and rejects unknown payloads
  via the standard :class:`SchemaValidationError` path.
* The seed migration inserts the universal Send-To member and three note
  actions for autonomy.
* The migration is idempotent: re-running does not duplicate members.
* Missing prompt-template files fail the migration loudly with a runtime
  error that names the offending key + path (no silent NULLs).
* Re-running with a newer prompt file refreshes the live payload (auto-17oir).
* ``--no-update`` pins existing rows even if the file is newer (auto-17oir).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from tools.graph import org_ops, schemas  # noqa: F401 — registers contracts
from tools.graph.db import GraphDB
from tools.graph.migrations import seed_agent_actions
from tools.graph.schemas.agent_actions import (
    AGENT_ACTIONS_REVISION,
    AGENT_ACTIONS_SET_ID,
    AgentActionV1,
)
from tools.graph.schemas.registry import SchemaValidationError


# ── Fixtures ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _evict_pool():
    GraphDB.close_all_pooled()
    try:
        yield
    finally:
        GraphDB.close_all_pooled()


@pytest.fixture
def orgs_dir(tmp_path) -> Path:
    """Per-org DB root with autonomy already bootstrapped."""
    d = tmp_path / "orgs"
    d.mkdir()
    org_ops.create_org(
        "autonomy",
        type_="shared",
        identity_payload={"name": "Autonomy"},
        root=d,
    )
    return d


@pytest.fixture
def prompts_dir(tmp_path) -> Path:
    d = tmp_path / "actions"
    d.mkdir()
    for key in (
        "note.update-summary",
        "note.consolidate-comments",
        "note.review-accuracy",
    ):
        (d / f"{key}.md").write_text(
            f"# {key}\n\nrender against {{asset_id}}.\n"
        )
    return d


def _settings_rows(org_db: Path) -> list[dict]:
    db = GraphDB(org_db)
    try:
        rows = db.conn.execute(
            "SELECT id, set_id, schema_revision, key, payload, "
            "publication_state FROM settings WHERE set_id = ?",
            (AGENT_ACTIONS_SET_ID,),
        ).fetchall()
    finally:
        db.close()
    out: list[dict] = []
    for r in rows:
        out.append({
            "id": r["id"],
            "set_id": r["set_id"],
            "schema_revision": int(r["schema_revision"]),
            "key": r["key"],
            "payload": json.loads(r["payload"]),
            "publication_state": r["publication_state"],
        })
    return out


# ── Schema registration ────────────────────────────────────


def test_schema_is_registered():
    schema = schemas.get_schema(AGENT_ACTIONS_SET_ID, AGENT_ACTIONS_REVISION)
    assert schema is AgentActionV1


def test_schema_rejects_unknown_field():
    with pytest.raises(SchemaValidationError):
        schemas.validate_payload(
            AGENT_ACTIONS_SET_ID, AGENT_ACTIONS_REVISION,
            {
                "asset_type": "note",
                "label": "x",
                "model": "claude-haiku-4-5-20251001",
                "prompt_template": "...",
                "rogue_field": True,
            },
        )


def test_schema_requires_model_for_non_universal():
    with pytest.raises(SchemaValidationError):
        schemas.validate_payload(
            AGENT_ACTIONS_SET_ID, AGENT_ACTIONS_REVISION,
            {"asset_type": "note", "label": "x"},
        )


def test_schema_universal_skips_model_requirement():
    # Universal action — no model/prompt_template required.
    schemas.validate_payload(
        AGENT_ACTIONS_SET_ID, AGENT_ACTIONS_REVISION,
        {"asset_type": "*", "label": "Send To…", "universal": True},
    )


def test_schema_rejects_unknown_asset_type():
    with pytest.raises(SchemaValidationError):
        schemas.validate_payload(
            AGENT_ACTIONS_SET_ID, AGENT_ACTIONS_REVISION,
            {
                "asset_type": "spaceship",
                "label": "x",
                "model": "y",
                "prompt_template": "z",
            },
        )


# ── Seed migration ─────────────────────────────────────────


def test_seed_inserts_universal_send_to(orgs_dir, prompts_dir):
    seed_agent_actions.run(
        org="autonomy", orgs_dir=orgs_dir, prompts_dir=prompts_dir, log=lambda *_: None,
    )
    rows = _settings_rows(orgs_dir / "autonomy.db")
    keys = {r["key"]: r for r in rows}
    assert "session.send-to" in keys
    payload = keys["session.send-to"]["payload"]
    assert payload["universal"] is True
    assert payload["asset_type"] == "*"


def test_seed_inserts_three_note_actions(orgs_dir, prompts_dir):
    seed_agent_actions.run(
        org="autonomy", orgs_dir=orgs_dir, prompts_dir=prompts_dir, log=lambda *_: None,
    )
    rows = _settings_rows(orgs_dir / "autonomy.db")
    note_rows = [r for r in rows if r["payload"]["asset_type"] == "note"]
    assert {r["key"] for r in note_rows} == {
        "note.update-summary",
        "note.consolidate-comments",
        "note.review-accuracy",
    }


def test_seed_inlines_prompt_templates(orgs_dir, prompts_dir):
    seed_agent_actions.run(
        org="autonomy", orgs_dir=orgs_dir, prompts_dir=prompts_dir, log=lambda *_: None,
    )
    rows = _settings_rows(orgs_dir / "autonomy.db")
    note_rows = [r for r in rows if r["payload"]["asset_type"] == "note"]
    for r in note_rows:
        prompt = r["payload"].get("prompt_template")
        assert isinstance(prompt, str) and prompt.strip()
    # Universal entries do not get a prompt_template.
    universal = [r for r in rows if r["payload"].get("universal")]
    assert universal and "prompt_template" not in universal[0]["payload"]


def test_seed_idempotent(orgs_dir, prompts_dir):
    seed_agent_actions.run(
        org="autonomy", orgs_dir=orgs_dir, prompts_dir=prompts_dir, log=lambda *_: None,
    )
    first = _settings_rows(orgs_dir / "autonomy.db")
    seed_agent_actions.run(
        org="autonomy", orgs_dir=orgs_dir, prompts_dir=prompts_dir, log=lambda *_: None,
    )
    second = _settings_rows(orgs_dir / "autonomy.db")
    assert {r["id"] for r in first} == {r["id"] for r in second}
    assert len(first) == len(second) == 4


def test_seed_payloads_validate_against_schema(orgs_dir, prompts_dir):
    seed_agent_actions.run(
        org="autonomy", orgs_dir=orgs_dir, prompts_dir=prompts_dir, log=lambda *_: None,
    )
    for r in _settings_rows(orgs_dir / "autonomy.db"):
        schemas.validate_payload(r["set_id"], r["schema_revision"], r["payload"])


def test_seed_missing_prompt_aborts_migration(orgs_dir, prompts_dir):
    """Per task §3.5C: a missing template fails the migration loudly."""
    target = prompts_dir / "note.review-accuracy.md"
    target.unlink()
    with pytest.raises(RuntimeError) as ei:
        seed_agent_actions.run(
            org="autonomy", orgs_dir=orgs_dir, prompts_dir=prompts_dir, log=lambda *_: None,
        )
    assert "note.review-accuracy" in str(ei.value)
    # Path of the missing file is named in the error so the operator can
    # find and create it without further searching.
    assert str(target) in str(ei.value)


def test_seed_empty_prompt_aborts_migration(orgs_dir, prompts_dir):
    target = prompts_dir / "note.update-summary.md"
    target.write_text("\n  \n")
    with pytest.raises(RuntimeError) as ei:
        seed_agent_actions.run(
            org="autonomy", orgs_dir=orgs_dir, prompts_dir=prompts_dir, log=lambda *_: None,
        )
    assert "note.update-summary" in str(ei.value)


def test_seed_rejects_missing_org_db(tmp_path, prompts_dir):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(RuntimeError):
        seed_agent_actions.run(
            org="autonomy", orgs_dir=empty, prompts_dir=prompts_dir, log=lambda *_: None,
        )


# ── Re-run mtime auto-update (auto-17oir) ───────────────────


def _fetch_payload(org_db: Path, key: str) -> dict:
    db = GraphDB(org_db)
    try:
        row = db.conn.execute(
            "SELECT payload FROM settings WHERE set_id = ? AND key = ?",
            (AGENT_ACTIONS_SET_ID, key),
        ).fetchone()
    finally:
        db.close()
    assert row is not None, f"missing seeded row for {key!r}"
    return json.loads(row["payload"])


def _fetch_id(org_db: Path, key: str) -> str:
    db = GraphDB(org_db)
    try:
        row = db.conn.execute(
            "SELECT id FROM settings WHERE set_id = ? AND key = ?",
            (AGENT_ACTIONS_SET_ID, key),
        ).fetchone()
    finally:
        db.close()
    assert row is not None
    return row["id"]


def test_rerun_with_updated_file_refreshes_payload(orgs_dir, prompts_dir):
    """Run seed once. Modify the prompt template on disk + bump mtime.
    Run seed again. Live Setting payload reflects the new file content.
    Regression for the Round 7c → 7g flailing-agent incident.
    """
    target = prompts_dir / "note.update-summary.md"
    target.write_text("OLD MARKER: original prompt template body.\n")
    seed_agent_actions.run(
        org="autonomy", orgs_dir=orgs_dir, prompts_dir=prompts_dir,
        log=lambda *_: None,
    )
    initial = _fetch_payload(orgs_dir / "autonomy.db", "note.update-summary")
    assert "OLD MARKER" in initial["prompt_template"]
    initial_id = _fetch_id(orgs_dir / "autonomy.db", "note.update-summary")

    # Touch the file with new content + bump mtime well past the row's
    # second-resolution updated_at timestamp.
    target.write_text("NEW MARKER: refreshed prompt template body.\n")
    future = time.time() + 60
    os.utime(target, (future, future))

    seed_agent_actions.run(
        org="autonomy", orgs_dir=orgs_dir, prompts_dir=prompts_dir,
        log=lambda *_: None,
    )
    updated = _fetch_payload(orgs_dir / "autonomy.db", "note.update-summary")
    assert "NEW MARKER" in updated["prompt_template"]
    assert "OLD MARKER" not in updated["prompt_template"]
    # Same row — UPDATE in place, not a duplicate INSERT.
    assert _fetch_id(orgs_dir / "autonomy.db", "note.update-summary") == initial_id


def test_rerun_no_update_flag_pins_payload(orgs_dir, prompts_dir):
    """With ``no_update=True`` the migration must NOT touch existing rows
    even when the prompt file is newer than the stored row."""
    target = prompts_dir / "note.update-summary.md"
    target.write_text("OLD MARKER: original prompt template body.\n")
    seed_agent_actions.run(
        org="autonomy", orgs_dir=orgs_dir, prompts_dir=prompts_dir,
        log=lambda *_: None,
    )
    before = _fetch_payload(orgs_dir / "autonomy.db", "note.update-summary")

    target.write_text("NEW MARKER: should not be applied.\n")
    future = time.time() + 60
    os.utime(target, (future, future))

    seed_agent_actions.run(
        org="autonomy", orgs_dir=orgs_dir, prompts_dir=prompts_dir,
        no_update=True, log=lambda *_: None,
    )
    after = _fetch_payload(orgs_dir / "autonomy.db", "note.update-summary")
    assert after == before, "no_update must leave the existing payload intact"
    assert "OLD MARKER" in after["prompt_template"]


def test_rerun_with_unchanged_file_skips(orgs_dir, prompts_dir):
    """If the file mtime is older than the row's ``updated_at``, skip —
    preserves intentional operator overrides (e.g. via direct SQL or
    ``graph set override``)."""
    target = prompts_dir / "note.update-summary.md"
    # Make the file old enough that the post-seed row will look newer.
    past = time.time() - 60
    os.utime(target, (past, past))

    seed_agent_actions.run(
        org="autonomy", orgs_dir=orgs_dir, prompts_dir=prompts_dir,
        log=lambda *_: None,
    )

    # Simulate operator override: rewrite the row's payload directly.
    db = GraphDB(orgs_dir / "autonomy.db")
    try:
        db.conn.execute(
            "UPDATE settings SET payload = ? "
            "WHERE set_id = ? AND key = ?",
            (
                json.dumps({
                    "asset_type": "note",
                    "label": "Update Title & Summary",
                    "icon": "✏",
                    "model": "claude-haiku-4-5-20251001",
                    "estimated_seconds": 10,
                    "writes": ["source.title", "source.short_description"],
                    "prompt_template": "OPERATOR OVERRIDE",
                }),
                AGENT_ACTIONS_SET_ID, "note.update-summary",
            ),
        )
        db.conn.commit()
    finally:
        db.close()

    seed_agent_actions.run(
        org="autonomy", orgs_dir=orgs_dir, prompts_dir=prompts_dir,
        log=lambda *_: None,
    )
    after = _fetch_payload(orgs_dir / "autonomy.db", "note.update-summary")
    assert after["prompt_template"] == "OPERATOR OVERRIDE", (
        "older file mtime must not clobber an operator override"
    )
