"""Discovery contract for built-in repo workflow skills."""

from __future__ import annotations

from pathlib import Path

import yaml

from agents.session_launcher import _skill_frontmatter


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_repo_workflow_skills_are_discoverable_by_both_harnesses():
    expected_terms = {
        "design-studio": {"create", "revise", "pull down", "design studio"},
        "present": {"publish", "revise", "pull down", "present app"},
        "prior-art-analysis": {"existing", "before", "designing", "implementing"},
        "feature-planning": {"before", "non-trivial", "testable", "decision-note"},
    }

    for name, terms in expected_terms.items():
        codex_dir = REPO_ROOT / ".agents" / "skills" / name
        claude_dir = REPO_ROOT / ".claude" / "skills" / name
        skill_path = codex_dir / "SKILL.md"

        assert skill_path.is_file()
        assert claude_dir.is_symlink()
        assert claude_dir.resolve() == codex_dir.resolve()

        metadata = _skill_frontmatter(skill_path.read_text())
        assert metadata["name"] == name
        description = metadata["description"].lower()
        assert all(term in description for term in terms)

        interface = yaml.safe_load((codex_dir / "agents" / "openai.yaml").read_text())["interface"]
        assert 25 <= len(interface["short_description"]) <= 64
        assert f"${name}" in interface["default_prompt"]


def test_workspace_primer_points_to_repo_workflow_skills():
    primer = (REPO_ROOT / "agents" / "primers" / "workspace.md.j2").read_text()

    assert "### Design Studio and Present" in primer
    assert "`design-studio`" in primer
    assert "`present`" in primer
    assert "`prior-art-analysis`" in primer
    assert "`feature-planning`" in primer
    assert "Prior-art gate" in primer
    assert "consume" in primer and "extend" in primer
    assert "--force" in primer
