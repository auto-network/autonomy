"""Every shipped prompt template under agents/actions/*.md must pass
_render_agent_action_prompt's static placeholder check.

Without this test, a broken template ships to production and 500s on
the first dispatch (Round 7c → 7j). The synthetic prompt_template
fixtures in test_api_agent_action_dispatch.py and the seed tests
bypass the disk files entirely."""

from pathlib import Path
import pytest

from tools.dashboard.server import _render_agent_action_prompt

REPO_ROOT = Path(__file__).resolve().parents[3]
ACTIONS_DIR = REPO_ROOT / "agents" / "actions"


@pytest.mark.parametrize(
    "template_path",
    sorted(ACTIONS_DIR.glob("*.md")),
    ids=lambda p: p.stem,
)
def test_action_template_passes_static_check(template_path):
    """Real prompt template renders without ValueError. Detects:
    - Unescaped JSON braces ({ → {{)
    - References to placeholders not in _AGENT_ACTION_PLACEHOLDERS
    - Other str.format syntax errors
    """
    template = template_path.read_text()
    rendered = _render_agent_action_prompt(
        template,
        asset_id="test-asset-id",
        page_context={
            "asset_title": "T",
            "asset_short_description": "D",
            "asset_url": "/u",
            "asset_type": "note",
            "asset_org": "test",
            "tag_list": "",
        },
        dispatched_by_session="test-sess",
        member_key=template_path.stem,
    )
    assert rendered, f"rendered empty for {template_path.name}"


def test_actions_dir_not_empty():
    """Guard against the dir being moved without updating this test."""
    assert any(ACTIONS_DIR.glob("*.md")), (
        f"No templates found under {ACTIONS_DIR}; the parametrize "
        f"above would silently pass with zero cases."
    )
