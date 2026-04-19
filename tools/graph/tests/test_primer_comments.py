"""L1 test for primer comment rendering.

Regression guard for auto-7l35: `_get_bead` previously shelled out to
`bd show --json`, which drops comments. The primer now reads via the
dashboard DAO, so every bead comment must reach the agent.
"""

from __future__ import annotations

import pytest

from tools.graph.primer import generate_primer


def test_primer_includes_comments():
    """generate_primer(bead_with_comments) must contain every comment body."""
    primer = generate_primer("auto-1v0o")
    assert "Coordination channel" in primer, \
        "post-description comments missing from primer"
    assert "Test Coverage Audit" in primer, \
        "test-audit comment missing from primer"
    assert "auto-0416-223035" in primer, \
        "coordination session id missing from primer"
