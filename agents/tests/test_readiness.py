"""Tests for agents.readiness — bead readiness gate lifecycle."""

import pytest
from agents.readiness import (
    READINESS_LEVELS,
    ReadinessCheck,
    check_readiness,
    check_ready,
    check_spec_complete,
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
        "description": "Add readiness dimension via set-state: draft -> spec-complete -> ready. "
                       "Dispatcher only picks up readiness=ready beads.",
        "status": "open",
        "priority": 1,
        "issue_type": "feature",
        "labels": ["implementation", "approved"],
        "acceptance_criteria": "Dispatcher filters on readiness:ready label.",
        "design": "Use bd set-state readiness=<level> to track lifecycle.",
    }
    bead.update(overrides)
    return bead


# ── get_readiness_level ──────────────────────────────────────────

class TestGetReadinessLevel:
    def test_no_labels(self):
        assert get_readiness_level({"labels": []}) == "draft"

    def test_no_readiness_label(self):
        assert get_readiness_level({"labels": ["implementation", "approved"]}) == "draft"

    def test_draft_label(self):
        assert get_readiness_level({"labels": ["readiness:draft"]}) == "draft"

    def test_spec_complete_label(self):
        assert get_readiness_level({"labels": ["readiness:spec-complete"]}) == "spec-complete"

    def test_ready_label(self):
        assert get_readiness_level({"labels": ["readiness:ready"]}) == "ready"

    def test_ignores_unknown_readiness(self):
        assert get_readiness_level({"labels": ["readiness:bogus"]}) == "draft"

    def test_none_labels(self):
        assert get_readiness_level({"labels": None}) == "draft"

    def test_missing_labels_key(self):
        assert get_readiness_level({}) == "draft"


# ── check_spec_complete ──────────────────────────────────────────

class TestCheckSpecComplete:
    def test_full_bead_passes(self):
        bead = _make_bead()
        result = check_spec_complete(bead)
        assert result.passed is True
        assert result.gaps == []

    def test_short_title_fails(self):
        bead = _make_bead(title="Fix")
        result = check_spec_complete(bead)
        assert result.passed is False
        assert any("Title too short" in g for g in result.gaps)

    def test_missing_description_fails(self):
        bead = _make_bead(description="")
        result = check_spec_complete(bead)
        assert result.passed is False
        assert any("Missing description" in g for g in result.gaps)

    def test_short_description_fails(self):
        bead = _make_bead(description="Too short.")
        result = check_spec_complete(bead)
        assert result.passed is False
        assert any("Description too short" in g for g in result.gaps)

    def test_no_acceptance_or_design_fails(self):
        bead = _make_bead(acceptance_criteria="", design="")
        result = check_spec_complete(bead)
        assert result.passed is False
        assert any("acceptance_criteria" in g and "design" in g for g in result.gaps)

    def test_acceptance_only_passes(self):
        bead = _make_bead(design="")
        result = check_spec_complete(bead)
        assert result.passed is True
        assert any("design" in w.lower() for w in result.warnings)

    def test_design_only_passes(self):
        bead = _make_bead(acceptance_criteria="")
        result = check_spec_complete(bead)
        assert result.passed is True
        assert any("acceptance" in w.lower() for w in result.warnings)


# ── check_ready ──────────────────────────────────────────────────

class TestCheckReady:
    def test_full_bead_passes(self):
        bead = _make_bead()
        result = check_ready(bead)
        assert result.passed is True

    def test_missing_approved_fails(self):
        bead = _make_bead(labels=["implementation"])
        result = check_ready(bead)
        assert result.passed is False
        assert any("approved" in g for g in result.gaps)

    def test_missing_implementation_fails(self):
        bead = _make_bead(labels=["approved"])
        result = check_ready(bead)
        assert result.passed is False
        assert any("implementation" in g for g in result.gaps)

    def test_missing_priority_fails(self):
        bead = _make_bead(priority=None)
        result = check_ready(bead)
        assert result.passed is False
        assert any("Priority" in g for g in result.gaps)

    def test_spec_incomplete_fails(self):
        bead = _make_bead(description="")
        result = check_ready(bead)
        assert result.passed is False
        assert any("spec-complete" in g for g in result.gaps)

    def test_empty_labels_fails(self):
        bead = _make_bead(labels=[])
        result = check_ready(bead)
        assert result.passed is False


# ── check_readiness (router) ─────────────────────────────────────

class TestCheckReadiness:
    def test_draft_always_passes(self):
        result = check_readiness({}, "draft")
        assert result.passed is True

    def test_unknown_level_fails(self):
        result = check_readiness({}, "bogus")
        assert result.passed is False
        assert any("Unknown" in g for g in result.gaps)

    def test_routes_to_spec_complete(self):
        bead = _make_bead()
        result = check_readiness(bead, "spec-complete")
        assert result.target_level == "spec-complete"
        assert result.passed is True

    def test_routes_to_ready(self):
        bead = _make_bead()
        result = check_readiness(bead, "ready")
        assert result.target_level == "ready"
        assert result.passed is True


# ── is_dispatch_ready ────────────────────────────────────────────

class TestIsDispatchReady:
    def test_ready_label(self):
        bead = _make_bead(labels=["readiness:ready", "implementation"])
        assert is_dispatch_ready(bead) is True

    def test_no_readiness_label(self):
        bead = _make_bead()
        assert is_dispatch_ready(bead) is False

    def test_draft_label(self):
        bead = _make_bead(labels=["readiness:draft"])
        assert is_dispatch_ready(bead) is False

    def test_spec_complete_label(self):
        bead = _make_bead(labels=["readiness:spec-complete"])
        assert is_dispatch_ready(bead) is False


# ── format_check ─────────────────────────────────────────────────

class TestFormatCheck:
    def test_pass_format(self):
        check = ReadinessCheck("auto-x", "draft", "spec-complete", True)
        output = format_check(check)
        assert "PASS" in output
        assert "auto-x" in output

    def test_fail_format_with_gaps(self):
        check = ReadinessCheck("auto-x", "draft", "ready", False,
                               gaps=["Missing description"])
        output = format_check(check)
        assert "FAIL" in output
        assert "Missing description" in output

    def test_warnings_shown(self):
        check = ReadinessCheck("auto-x", "draft", "ready", True,
                               warnings=["No design notes"])
        output = format_check(check)
        assert "No design notes" in output


# ── READINESS_LEVELS constant ────────────────────────────────────

class TestConstants:
    def test_levels_ordered(self):
        assert READINESS_LEVELS == ("draft", "spec-complete", "ready")

    def test_levels_are_tuple(self):
        """Immutable — can't accidentally mutate."""
        assert isinstance(READINESS_LEVELS, tuple)
