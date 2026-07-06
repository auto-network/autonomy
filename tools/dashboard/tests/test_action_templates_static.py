"""Every prompt_template stored in the live ``dashboard.agent-actions``
Setting must render cleanly through ``_render_agent_action_prompt``.

The Setting payload is the source of truth at dispatch time — see the
note on agentic action authoring in CLAUDE.md. This test reads members
directly from the autonomy org's DB and exercises each template against
the renderer's static-check + format pass.

Skipped when no autonomy.db is present (CI environments without a
seeded data dir). The test runs against whatever's in the DB; adding a
new action via ``graph set add`` automatically extends coverage on the
next test run.
"""

from __future__ import annotations

from pathlib import Path
import os

import pytest

from tools.dashboard.server import _render_agent_action_prompt
from tools.graph import ops as graph_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.agent_actions import AGENT_ACTIONS_SET_ID

REPO_ROOT = Path(__file__).resolve().parents[3]
AUTONOMY_DB = REPO_ROOT / "data" / "orgs" / "autonomy.db"


def _live_members():
    """Resolve seeded members from autonomy's live DB.

    Returns members that carry a ``prompt_template`` field. Empty when
    the DB has no agent-action members (fresh org / before bootstrap).
    """
    members = graph_ops.read_set(AGENT_ACTIONS_SET_ID, org="autonomy")
    return [m for m in members.members if m.payload.get("prompt_template")]


@pytest.fixture(autouse=True)
def _require_autonomy_db():
    """Skip if no autonomy.db exists. The test is a production-data
    validation; CI environments without a seeded data dir should pass.
    """
    if not AUTONOMY_DB.exists():
        pytest.skip(f"no autonomy.db at {AUTONOMY_DB} — nothing to validate")
    yield
    GraphDB.close_all_pooled()


def test_every_live_template_renders_cleanly():
    """Each Setting member that carries a ``prompt_template`` field
    renders without ``ValueError``.

    Catches:
      - Unescaped braces in the template (``{`` / ``}`` without ``{{``).
      - References to placeholders not in
        ``_AGENT_ACTION_PLACEHOLDERS``.
      - Any other ``str.format`` syntax error.
    """
    members = _live_members()
    if not members:
        pytest.skip(
            "autonomy.db has no agent-action members with prompt_template; "
            "fresh DB without bootstrap. Test is production-data scoped."
        )

    for member in members:
        template = member.payload["prompt_template"]
        rendered = _render_agent_action_prompt(
            template,
            page_context={
                "asset": {
                    "id": "test-asset-id",
                    "title": "T",
                    "short_description": "D",
                    "url": "/u",
                    "type": "note",
                    "org": "test",
                    "primer": "",
                    "metadata": {},
                },
                "source": {},
                "bead": {},
                "design": {
                    "design_id": "test-design",
                    "status": "pending",
                    "revision_count": 1,
                    "variant_count": 1,
                    "creator_session_id": "test-sess",
                },
                "tags": {"values": ["tag1", "tag2"], "list": "tag1, tag2"},
            },
            dispatched_by_session="test-sess",
            member_key=member.key,
        )
        assert rendered, (
            f"prompt_template for {member.key!r} rendered empty"
        )
