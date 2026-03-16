"""Agent prompt composer.

Assembles the full agent prompt from:
1. Dynamic context primer (bead-specific, generated from the graph)
2. Shared instruction blocks (tool guidelines, experience report template)
3. Dispatcher directives (readonly mode, output expectations)

Usage:
    python -m agents.compose <bead-id> [--output FILE]
"""

from __future__ import annotations
import argparse
import sys
from pathlib import Path

SHARED_DIR = Path(__file__).parent / "shared"


def load_shared_blocks() -> list[str]:
    """Load all markdown files from agents/shared/ in sorted order."""
    blocks = []
    if not SHARED_DIR.exists():
        return blocks
    for md_file in sorted(SHARED_DIR.glob("*.md")):
        blocks.append(md_file.read_text().strip())
    return blocks


def compose_prompt(bead_id: str) -> str:
    """Compose the full agent prompt for a bead.

    Returns a markdown string ready to be passed as the agent's prompt.
    """
    # Import here to avoid circular deps when running outside the repo
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools.graph.primer import generate_primer

    sections = []

    # 1. Dynamic primer — bead-specific context from the graph
    primer = generate_primer(bead_id)
    sections.append(primer)

    # 2. Shared instruction blocks
    shared = load_shared_blocks()
    if shared:
        sections.append("\n---\n")
        sections.extend(shared)

    # 3. Dispatcher directives
    sections.append("""
---

# Dispatcher Directives

You are an autonomous agent dispatched by the Autonomy dispatcher.

- You are running with `bd --readonly`. Do not attempt to modify beads.
- Complete the task described above.
- Write your decision to `/workspace/output/decision.json` when done.
- Write `experience_report.md` to `/workspace/output/` with operational feedback.
- Write ALL output files to `/workspace/output/` — this is the only directory that persists after the container exits.
- If you discover new work, include it in the decision file's `discovered_beads` array.
- If you are blocked, write a BLOCKED decision immediately — do not spin.
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
