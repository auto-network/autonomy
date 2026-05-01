"""Tests for the capability skill / primer Markdown projection.

Verifies the rendering functions produce stable, structured output
from a contract Setting payload — no DB touchpoint, no network. End-
to-end smoke against the canonical ``source_control@1`` shape pins
the prefix-grouping renderer for the nested-namespace case the
github provider relies on today.

Bead: auto-3.5 in the codegen migration sprint.
"""

from __future__ import annotations

import pytest

from tools.graph.capability_skilltext import (
    group_ops_by_prefix,
    list_op_names,
    render_primer_md,
    render_skill_md,
)


# ── Fixtures ──────────────────────────────────────────────────


def _source_control_payload() -> dict:
    return {
        "name": "source_control",
        "version": 1,
        "summary": (
            "Branch and commit state, PR review, and merge-gate "
            "state across providers."
        ),
        "ops": [
            {
                "name": "review_read",
                "summary": "Read the PR review state(s) for a branch.",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
            {
                "name": "review_refresh",
                "summary": "Re-fetch the PR review state(s) for a branch.",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
            {
                "name": "gates_watch_set",
                "summary": "Set the merge-gate watch mode for a branch.",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
        ],
    }


def _flat_payload() -> dict:
    return {
        "name": "issue_tracker",
        "version": 1,
        "summary": "Read and update tickets across providers.",
        "ops": [
            {
                "name": "read",
                "summary": "Fetch a single ticket by id.",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
            {
                "name": "create",
                "summary": "Create a new ticket.",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
            },
        ],
    }


def _single_prefix_payload() -> dict:
    return {
        "name": "thing",
        "version": 1,
        "summary": "Single prefix has no nested-namespace signal.",
        "ops": [
            {"name": "alpha_one", "summary": "First.", "input_schema": {}, "output_schema": {}},
            {"name": "alpha_two", "summary": "Second.", "input_schema": {}, "output_schema": {}},
        ],
    }


# ── group_ops_by_prefix ──────────────────────────────────────


def test_group_ops_by_prefix_nested_namespace_signal():
    contract = _source_control_payload()
    grouped, ungrouped = group_ops_by_prefix(contract["ops"])
    assert ungrouped == []
    assert [p for p, _ in grouped] == ["review", "gates"]
    review_ops = dict(grouped)["review"]
    gates_ops = dict(grouped)["gates"]
    assert [o["name"] for o in review_ops] == ["review_read", "review_refresh"]
    assert [o["name"] for o in gates_ops] == ["gates_watch_set"]


def test_group_ops_by_prefix_flat_falls_back_to_ungrouped():
    contract = _flat_payload()
    grouped, ungrouped = group_ops_by_prefix(contract["ops"])
    assert grouped == []
    assert [o["name"] for o in ungrouped] == ["read", "create"]


def test_group_ops_by_prefix_single_prefix_flattens():
    """Only one prefix → no nested-namespace signal → flatten."""
    contract = _single_prefix_payload()
    grouped, ungrouped = group_ops_by_prefix(contract["ops"])
    assert grouped == []
    assert [o["name"] for o in ungrouped] == ["alpha_one", "alpha_two"]


def test_group_ops_by_prefix_empty():
    grouped, ungrouped = group_ops_by_prefix([])
    assert grouped == []
    assert ungrouped == []


# ── list_op_names ────────────────────────────────────────────


def test_list_op_names_renders_dotted_paths_for_nested_namespaces():
    names = list_op_names(_source_control_payload())
    assert names == [
        "source_control.review.read",
        "source_control.review.refresh",
        "source_control.gates.watch_set",
    ]


def test_list_op_names_uses_flat_form_when_no_grouping_signal():
    names = list_op_names(_flat_payload())
    assert names == ["issue_tracker.read", "issue_tracker.create"]


# ── render_skill_md ──────────────────────────────────────────


def test_render_skill_md_emits_frontmatter_and_title():
    out = render_skill_md(_source_control_payload())
    assert out.startswith("---\n")
    assert "name: source_control@1" in out
    assert "description: Branch and commit state" in out
    assert "# source_control@1" in out


def test_render_skill_md_with_provider_replaces_title():
    provider = {
        "name": "autonomy/github",
        "notes": "Implementation lives in agents/capabilities/github/service.py.",
    }
    out = render_skill_md(_source_control_payload(), provider=provider)
    assert "name: autonomy/github" in out
    assert "# autonomy/github" in out
    assert "Implementation lives in agents/capabilities/github/service.py." in out


def test_render_skill_md_groups_under_subheadings():
    """Nested-namespace signal → render `<contract>.<prefix>.*` headings."""
    out = render_skill_md(_source_control_payload())
    assert "### `source_control.review.*`" in out
    assert "### `source_control.gates.*`" in out
    # Each leaf op rendered as a bullet with the dotted path + summary.
    assert "- `source_control.review.read`" in out
    assert "- `source_control.review.refresh`" in out
    assert "- `source_control.gates.watch_set`" in out


def test_render_skill_md_flat_contract_skips_subheadings():
    out = render_skill_md(_flat_payload())
    assert "###" not in out  # no subheadings
    assert "- `issue_tracker.read`" in out
    assert "- `issue_tracker.create`" in out


def test_render_skill_md_no_ops_shows_empty_callout():
    out = render_skill_md({
        "name": "thing", "version": 1, "summary": "x", "ops": [],
    })
    assert "_No operations declared on this contract._" in out


def test_render_skill_md_includes_contract_notes():
    payload = _source_control_payload()
    payload["notes"] = "v1: review and gates nest under source_control."
    out = render_skill_md(payload)
    assert "## Notes" in out
    assert "v1: review and gates nest under source_control." in out


def test_render_skill_md_trailing_newline():
    out = render_skill_md(_source_control_payload())
    assert out.endswith("\n")
    # No double trailing newline.
    assert not out.endswith("\n\n")


# ── render_primer_md ─────────────────────────────────────────


def test_render_primer_md_emits_contract_frontmatter():
    out = render_primer_md(_source_control_payload())
    assert out.startswith("---\n")
    assert "contract: source_control@1" in out
    assert "Branch and commit state" in out


def test_render_primer_md_lists_every_op():
    out = render_primer_md(_source_control_payload())
    assert "- `source_control.review.read`" in out
    assert "- `source_control.review.refresh`" in out
    assert "- `source_control.gates.watch_set`" in out
    # Primer is flat — no subheadings.
    assert "###" not in out


def test_render_primer_md_with_provider_adds_capability_field():
    provider = {"name": "autonomy/github"}
    out = render_primer_md(_source_control_payload(), provider=provider)
    assert "capability: autonomy/github" in out


# ── Determinism / stability ──────────────────────────────────


def test_render_is_deterministic():
    """Two calls on the same payload produce byte-identical output."""
    p = _source_control_payload()
    assert render_skill_md(p) == render_skill_md(p)
    assert render_primer_md(p) == render_primer_md(p)


def test_op_order_preserved_within_group():
    """Within a group, ops render in payload order — important so a
    contract author controls the output by ordering ops.
    """
    p = _source_control_payload()
    # Reorder review_refresh BEFORE review_read in the payload.
    p["ops"][0], p["ops"][1] = p["ops"][1], p["ops"][0]
    out = render_skill_md(p)
    refresh_pos = out.index("review.refresh")
    read_pos = out.index("review.read")
    assert refresh_pos < read_pos
