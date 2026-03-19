"""Agent prompt composer.

Assembles the full agent prompt from:
1. Dynamic context (bead-specific, generated from the graph) — pure context, no instructions
2. Shared instruction blocks (tool guidelines = single source of truth, experience report template)
3. Dispatcher directives (runtime-specific only: output paths, worktree info)

Usage:
    python -m agents.compose <bead-id> [--output FILE]
"""

from __future__ import annotations
import argparse
import sys
from pathlib import Path

SHARED_DIR = Path(__file__).parent / "shared"


def load_shared_blocks(labels: list[str] | None = None) -> list[str]:
    """Load shared markdown blocks.

    Always loads agents/shared/*.md. If the bead has labels matching
    subdirectory names (e.g. label "dashboard" → agents/shared/dashboard/),
    those are included too.
    """
    blocks = []
    if not SHARED_DIR.exists():
        return blocks
    for md_file in sorted(SHARED_DIR.glob("*.md")):
        blocks.append(md_file.read_text().strip())
    for label in (labels or []):
        subdir = SHARED_DIR / label
        if subdir.is_dir():
            for md_file in sorted(subdir.glob("*.md")):
                blocks.append(md_file.read_text().strip())
    return blocks


def compose_prompt(bead_id: str) -> str:
    """Compose the full agent prompt for a bead.

    Returns a markdown string ready to be passed as the agent's prompt.

    Assembly order:
    1. format_for_agent(primer_data) — pure context with follow-on commands
    2. agents/shared/*.md — tool_guidelines.md (single source of truth for
       all tool instructions) + experience_report.md (template)
    3. Dispatcher directives — ONLY runtime-specific bits not already in
       tool_guidelines.md (readonly mode, output paths, etc.)
    """
    # Import here to avoid circular deps when running outside the repo
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools.graph.primer import collect_primer_data, format_for_agent
    from tools.dashboard.dao.beads import get_bead

    sections = []

    # 1. Dynamic context — bead-specific data from the graph
    #    Pure context: task description, provenance, notes, pitfalls, siblings.
    #    No instructions, no CLI docs, no workflow guidance.
    primer_data = collect_primer_data(bead_id)
    sections.append(format_for_agent(primer_data))

    # 2. Shared instruction blocks (tool_guidelines.md + experience_report.md)
    #    tool_guidelines.md is THE single source of truth for:
    #    - Workspace location, graph CLI, bd readonly commands
    #    - Decision file schema (status, scores, time_breakdown, etc.)
    #    - Working style guidance
    #    Label-matched subdirs (e.g. shared/dashboard/) are included when
    #    the bead has a matching label.
    bead = get_bead(bead_id)
    labels = bead.get("labels", []) if bead else []
    shared = load_shared_blocks(labels)
    if shared:
        sections.append("\n---\n")
        sections.extend(shared)

    # 3. Dispatcher directives — runtime-specific only
    #    Everything else (tool docs, decision schema, working style) is
    #    already covered by tool_guidelines.md. This section contains ONLY
    #    what's unique to this dispatch invocation.
    sections.append("""
---

# Dispatcher Directives

You are an autonomous agent dispatched by the Autonomy dispatcher.
Complete the task described above. See tool_guidelines.md sections above
for workspace layout, tool usage, decision file schema, and working style.
""")

    return "\n\n".join(sections)


def main():
    parser = argparse.ArgumentParser(description="Compose agent prompt for a bead")
    parser.add_argument("bead_id", help="Bead ID to generate prompt for")
    parser.add_argument("--output", "-o", help="Write to file instead of stdout")
    args = parser.parse_args()

    prompt = compose_prompt(args.bead_id)

    if args.output:
        Path(args.output).write_text(prompt)
        print(f"Wrote {len(prompt)} chars to {args.output}", file=sys.stderr)
    else:
        print(prompt)


if __name__ == "__main__":
    main()
