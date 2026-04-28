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
"""

from __future__ import annotations

import json
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
