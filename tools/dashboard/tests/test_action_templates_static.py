"""Every prompt_template stored in the ``dashboard.agent-actions`` Setting
must render cleanly through ``_render_agent_action_prompt``.

The test resolves members directly from a freshly-seeded org DB and
iterates the live ``payload.prompt_template`` strings. The on-disk
``agents/actions/*.md`` files are an intermediate detail — what
ultimately matters at dispatch time is what the Setting carries, so
that's what we render.

Closes the regression class where a broken template (unbalanced JSON
braces, undefined placeholder, etc.) silently 500s on the first
dispatch — see auto-pme37 for the original incident.
"""

from __future__ import annotations

import pytest

from tools.dashboard.server import _render_agent_action_prompt
from tools.graph import ops as graph_ops
from tools.graph.db import GraphDB
from tools.graph.migrations import seed_agent_actions
from tools.graph.schemas.agent_actions import AGENT_ACTIONS_SET_ID


@pytest.fixture
def seeded_autonomy(tmp_path, monkeypatch):
    """Seed the canonical agent-actions members into a temp autonomy.db
    and route ``ops.read_set`` to that DB for the duration of the test.

    Each test that depends on this fixture iterates whatever members
    the live seed migration produces, so adding a new action in
    ``seed_agent_actions.SEEDS`` automatically extends test coverage.
    """
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path))
    GraphDB.create_org_db("autonomy").close()
    seed_agent_actions.run(org="autonomy", orgs_dir=tmp_path, log=lambda *a, **k: None)
    yield
    GraphDB.close_all_pooled()


def _seeded_members():
    """Resolve seeded members from the temp DB. Helper for parametrize.

    Importantly, this runs *inside* test functions rather than at module
    import time so the fixture's ``monkeypatch`` env var is in effect.
    """
    members = graph_ops.read_set(AGENT_ACTIONS_SET_ID, org="autonomy")
    return [m for m in members.members if m.payload.get("prompt_template")]


def test_every_seeded_template_renders_cleanly(seeded_autonomy):
    """Each Setting member that carries a ``prompt_template`` field
    renders without ``ValueError``.

    Catches:
      - Unescaped braces in the template (``{`` / ``}`` without ``{{``).
      - References to placeholders not in
        ``_AGENT_ACTION_PLACEHOLDERS``.
      - Any other ``str.format`` syntax error.
    """
    members = _seeded_members()
    assert members, (
        "no Setting members carry a prompt_template after seeding; the "
        "test would silently pass with zero cases. Either the seed list "
        "is empty or the seed migration regressed."
    )

    for member in members:
        template = member.payload["prompt_template"]
        rendered = _render_agent_action_prompt(
            template,
            asset_id="test-asset-id",
            page_context={
                "asset_title": "T",
                "asset_short_description": "D",
                "asset_url": "/u",
                "asset_type": "note",
                "asset_org": "test",
                "tag_list": "tag1, tag2",
            },
            dispatched_by_session="test-sess",
            member_key=member.key,
        )
        assert rendered, (
            f"prompt_template for {member.key!r} rendered empty"
        )


def test_seeded_members_include_known_keys(seeded_autonomy):
    """Sanity: the seed migration produced the well-known members so the
    parametrize-on-glob style of dynamic discovery in
    :func:`test_every_seeded_template_renders_cleanly` is actually
    exercising the actions we care about.
    """
    keys = {m.key for m in graph_ops.read_set(AGENT_ACTIONS_SET_ID, org="autonomy").members}
    expected = {"note.update-summary", "note.consolidate-comments", "note.review-accuracy"}
    missing = expected - keys
    assert not missing, (
        f"seed_agent_actions did not produce expected members: missing {missing}"
    )
