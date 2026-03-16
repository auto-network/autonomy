"""Tests for agents.readiness — bead readiness gate lifecycle."""

import pytest
from agents.readiness import (
    READINESS_LEVELS,
    ReadinessCheck,
    check_approved,
    check_readiness,
    check_ready,
    check_spec_complete,
    check_specified,
    format_check,
    get_readiness_level,
    is_dispatch_ready,
)


# ── Fixtures ─────────────────────────────────────────────────────

def _make_bead(**overrides):
    """Create a minimal bead dict with sensible defaults."""
    bead = {
        "id": "auto-test",
        "title": "Implement readiness gate for beads",
        "description": "Add readiness dimension via set-state: idea -> draft -> specified -> approved. "
                       "Dispatcher only picks up readiness=approved beads.",
        "status": "open",
        "priority": 1,
        "issue_type": "feature",
        "labels": ["implementation"],
        "acceptance_criteria": "Dispatcher filters on readiness:approved label.",
        "design": "Use bd set-state readiness=<level> to track lifecycle.",
    }
    bead.update(overrides)
    return bead


# ── get_readiness_level ──────────────────────────────────────────

class TestGetReadinessLevel:
    def test_no_labels(self):
        assert get_readiness_level({"labels": []}) == "idea"

    def test_no_readiness_label(self):
        assert get_readiness_level({"labels": ["implementation"]}) == "idea"

    def test_idea_label(self):
        assert get_readiness_level({"labels": ["readiness:idea"]}) == "idea"

    def test_draft_label(self):
        assert get_readiness_level({"labels": ["readiness:draft"]}) == "draft"

    def test_specified_label(self):
        assert get_readiness_level({"labels": ["readiness:specified"]}) == "specified"

    def test_approved_label(self):
        assert get_readiness_level({"labels": ["readiness:approved"]}) == "approved"

    def test_ignores_unknown_readiness(self):
        assert get_readiness_level({"labels": ["readiness:bogus"]}) == "idea"

    def test_ignores_old_levels(self):
        """Old level names (spec-complete, ready) should not be recognized."""
        assert get_readiness_level({"labels": ["readiness:spec-complete"]}) == "idea"
        assert get_readiness_level({"labels": ["readiness:ready"]}) == "idea"

    def test_none_labels(self):
        assert get_readiness_level({"labels": None}) == "idea"

    def test_missing_labels_key(self):
        assert get_readiness_level({}) == "idea"


# ── check_specified (was check_spec_complete) ────────────────────

class TestCheckSpecified:
    def test_full_bead_passes(self):
        bead = _make_bead()
        result = check_specified(bead)
        assert result.passed is True
        assert result.gaps == []

    def test_backward_compat_alias(self):
        """check_spec_complete still works as alias."""
        bead = _make_bead()
        result = check_spec_complete(bead)
        assert result.passed is True

    def test_short_title_fails(self):
        bead = _make_bead(title="Fix")
        result = check_specified(bead)
        assert result.passed is False
        assert any("Title too short" in g for g in result.gaps)

    def test_missing_description_fails(self):
        bead = _make_bead(description="")
        result = check_specified(bead)
        assert result.passed is False
        assert any("Missing description" in g for g in result.gaps)

    def test_short_description_fails(self):
        bead = _make_bead(description="Too short.")
        result = check_specified(bead)
        assert result.passed is False
        assert any("Description too short" in g for g in result.gaps)

    def test_no_acceptance_or_design_fails(self):
        bead = _make_bead(acceptance_criteria="", design="")
        result = check_specified(bead)
        assert result.passed is False
        assert any("acceptance_criteria" in g and "design" in g for g in result.gaps)

    def test_acceptance_only_passes(self):
        bead = _make_bead(design="")
        result = check_specified(bead)
        assert result.passed is True
        assert any("design" in w.lower() for w in result.warnings)

    def test_design_only_passes(self):
        bead = _make_bead(acceptance_criteria="")
        result = check_specified(bead)
        assert result.passed is True
        assert any("acceptance" in w.lower() for w in result.warnings)


# ── check_approved (was check_ready) ─────────────────────────────

class TestCheckApproved:
    def test_full_bead_passes(self):
        bead = _make_bead()
        result = check_approved(bead)
        assert result.passed is True

    def test_backward_compat_alias(self):
        """check_ready still works as alias."""
        bead = _make_bead()
        result = check_ready(bead)
        assert result.passed is True

    def test_no_approved_label_not_required(self):
        """Approval is the readiness transition itself, not a label prerequisite."""
        bead = _make_bead(labels=["implementation"])
        result = check_approved(bead)
        assert result.passed is True

    def test_missing_implementation_fails(self):
        bead = _make_bead(labels=[])
        result = check_approved(bead)
        assert result.passed is False
        assert any("implementation" in g for g in result.gaps)

    def test_missing_priority_fails(self):
        bead = _make_bead(priority=None)
        result = check_approved(bead)
        assert result.passed is False
        assert any("Priority" in g for g in result.gaps)

    def test_spec_incomplete_fails(self):
        bead = _make_bead(description="")
        result = check_approved(bead)
        assert result.passed is False
        assert any("specified" in g for g in result.gaps)

    def test_empty_labels_fails(self):
        bead = _make_bead(labels=[])
        result = check_approved(bead)
        assert result.passed is False


# ── check_readiness (router) ─────────────────────────────────────

class TestCheckReadiness:
    def test_idea_always_passes(self):
        result = check_readiness({}, "idea")
        assert result.passed is True

    def test_draft_always_passes(self):
        result = check_readiness({}, "draft")
        assert result.passed is True

    def test_unknown_level_fails(self):
        result = check_readiness({}, "bogus")
        assert result.passed is False
        assert any("Unknown" in g for g in result.gaps)

    def test_routes_to_specified(self):
        bead = _make_bead()
        result = check_readiness(bead, "specified")
        assert result.target_level == "specified"
        assert result.passed is True

    def test_routes_to_approved(self):
        bead = _make_bead()
        result = check_readiness(bead, "approved")
        assert result.target_level == "approved"
        assert result.passed is True


# ── is_dispatch_ready ────────────────────────────────────────────

class TestIsDispatchReady:
    def test_approved_label(self):
        bead = _make_bead(labels=["readiness:approved", "implementation"])
        assert is_dispatch_ready(bead) is True

    def test_no_readiness_label(self):
        bead = _make_bead()
        assert is_dispatch_ready(bead) is False

    def test_idea_label(self):
        bead = _make_bead(labels=["readiness:idea"])
        assert is_dispatch_ready(bead) is False

    def test_draft_label(self):
        bead = _make_bead(labels=["readiness:draft"])
        assert is_dispatch_ready(bead) is False

    def test_specified_label(self):
        bead = _make_bead(labels=["readiness:specified"])
        assert is_dispatch_ready(bead) is False

    def test_old_ready_label_not_recognized(self):
        """Old readiness:ready label should not trigger dispatch."""
        bead = _make_bead(labels=["readiness:ready"])
        assert is_dispatch_ready(bead) is False


# ── format_check ─────────────────────────────────────────────────

class TestFormatCheck:
    def test_pass_format(self):
        check = ReadinessCheck("auto-x", "draft", "specified", True)
        output = format_check(check)
        assert "PASS" in output
        assert "auto-x" in output

    def test_fail_format_with_gaps(self):
        check = ReadinessCheck("auto-x", "idea", "approved", False,
                               gaps=["Missing description"])
        output = format_check(check)
        assert "FAIL" in output
        assert "Missing description" in output

    def test_warnings_shown(self):
        check = ReadinessCheck("auto-x", "draft", "approved", True,
                               warnings=["No design notes"])
        output = format_check(check)
        assert "No design notes" in output


# ── READINESS_LEVELS constant ────────────────────────────────────

class TestConstants:
    def test_levels_ordered(self):
        assert READINESS_LEVELS == ("idea", "draft", "specified", "approved")

    def test_levels_are_tuple(self):
        """Immutable — can't accidentally mutate."""
        assert isinstance(READINESS_LEVELS, tuple)
