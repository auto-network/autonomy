"""Chat With primer builders — page-specific context for Claude sessions.

Each primer prepares a Claude session with:
  - What the agent is looking at (page data, current state)
  - What it can do here (available APIs and tools)
  - What the platform is (Starlette + Jinja2 + Alpine.js + Tailwind)
  - The purpose and exit condition for this Chat With session

Pattern mirrors tools/graph/primer.py:collect_primer_data():
  collect data as a pure dict → format as markdown string.

Usage:
    result = get_primer("experiment", "some-uuid")
    # result = {"primer_text": "..."}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow importing agents/ (sibling of tools/)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agents.experiments_db import get_experiment

# ── Constants ────────────────────────────────────────────────────

VALID_PAGE_TYPES = ["experiment"]

_TAILWIND_INPUT = Path(__file__).parent / "tailwind.input.css"

_PLATFORM_CONTEXT = """\
The dashboard runs on:
- **Backend:** Python / Starlette (ASGI)
- **Templating:** Jinja2
- **Frontend JS:** Alpine.js (reactive, declarative)
- **CSS:** Pre-built Tailwind CSS — served at `/static/tailwind.css`. \
Do NOT use CDN script tags. Source config: `tools/dashboard/tailwind.input.css`
- **Pattern:** Server-rendered HTML fragments, Alpine.js for interactivity, \
SSE for live updates

Experiment variants render inside `<iframe>` elements. Each variant must be a \
self-contained HTML document (full `<html>` with `<head>` and `<body>`) that \
references `/static/tailwind.css` for styling.\
"""


# ── Experiment primer ────────────────────────────────────────────


def _collect_experiment_data(experiment_id: str) -> dict:
    """Collect raw data for an experiment primer. Returns structured dict."""
    exp = get_experiment(experiment_id)
    if not exp:
        raise ValueError(f"Experiment not found: {experiment_id!r}")

    # Parse fixture
    fixture_raw = exp.get("fixture")
    fixture_parsed = None
    if fixture_raw:
        try:
            fixture_parsed = json.loads(fixture_raw) if isinstance(fixture_raw, str) else fixture_raw
        except (json.JSONDecodeError, TypeError):
            fixture_parsed = fixture_raw  # keep as-is if unparseable

    # Series info — series feature may not be implemented yet; handle gracefully
    series_id = exp.get("series_id")
    series_name = exp.get("series_name")
    iteration_count = exp.get("iteration_count")  # populated by series query if available

    return {
        "experiment_id": experiment_id,
        "title": exp.get("title", "Untitled Experiment"),
        "description": exp.get("description") or "",
        "status": exp.get("status", "pending"),
        "created_at": exp.get("created_at", ""),
        "fixture": fixture_parsed,
        "variants": exp.get("variants", []),
        "series_id": series_id,
        "series_name": series_name,
        "iteration_count": iteration_count,
    }


def _format_experiment_primer(data: dict) -> str:
    """Render experiment data as a Chat With primer markdown string."""
    exp_id = data["experiment_id"]
    title = data["title"]
    description = data["description"]
    status = data["status"]
    fixture = data["fixture"]
    variants = data["variants"]
    series_id = data["series_id"]
    series_name = data["series_name"]
    iteration_count = data["iteration_count"]

    lines = []

    # ── Header ───────────────────────────────────────────────
    lines.append(f"# Design Studio: {title}\n")

    # ── Purpose ──────────────────────────────────────────────
    lines.append("## Purpose\n")
    lines.append(
        "You are iterating on a UI component with the user. Refine the design through "
        "conversation until the user approves a variant, then create an implementation "
        "bead with the winning variant specs.\n"
    )
    lines.append(
        "**Exit condition:** The user approves a variant → you create an implementation "
        "bead via `graph bead` documenting the winning design, its full HTML, and all "
        "design decisions made during the session.\n"
    )
    lines.append("---\n")

    # ── Current State ─────────────────────────────────────────
    lines.append("## Current State\n")
    lines.append(f"- **Experiment ID:** `{exp_id}`")
    lines.append(f"- **Title:** {title}")
    lines.append(f"- **Status:** {status}")
    if description:
        lines.append(f"- **Description:** {description}")

    if series_id:
        lines.append(f"- **Series ID:** `{series_id}`")
        if series_name:
            lines.append(f"- **Series Name:** {series_name}")
        if iteration_count is not None:
            lines.append(f"- **Iteration:** #{iteration_count} in series")
        else:
            lines.append(f"- **Iteration:** (series iteration count not yet available)")
    else:
        lines.append("- **Series:** None (standalone experiment)")

    lines.append("\n---\n")

    # ── Fixture ───────────────────────────────────────────────
    lines.append("## Fixture\n")
    lines.append("The fixture is shared JSON data passed to every variant as context:\n")
    if fixture is not None:
        lines.append("```json")
        lines.append(json.dumps(fixture, indent=2))
        lines.append("```")
    else:
        lines.append("*(no fixture — variants are standalone UI designs)*")
    lines.append("\n---\n")

    # ── Current Variants ──────────────────────────────────────
    lines.append("## Current Variant HTML\n")
    if variants:
        lines.append(
            f"This experiment has {len(variants)} variant(s). "
            "Each variant is the full source HTML rendered in an iframe:\n"
        )
        for v in variants:
            vid = v.get("id", "unknown")
            html = v.get("html", "")
            selected = v.get("selected", 0)
            rank = v.get("rank")
            meta = []
            if selected:
                meta.append("selected")
            if rank is not None:
                meta.append(f"rank {rank}")
            meta_str = f" *({"  ,".join(meta)})*" if meta else ""
            lines.append(f"### Variant: `{vid}`{meta_str}\n")
            lines.append("```html")
            lines.append(html)
            lines.append("```\n")
    else:
        lines.append("*(no variants yet)*\n")

    lines.append("---\n")

    # ── Platform Context ──────────────────────────────────────
    lines.append("## Platform Context\n")
    lines.append(_PLATFORM_CONTEXT)
    lines.append("\n---\n")

    # ── Tools Available ───────────────────────────────────────
    lines.append("## Tools Available\n")
    lines.append("### Create next iteration\n")
    lines.append(
        "POST a new experiment to create the next iteration. "
        "The user will see the new variants in the gallery immediately.\n"
    )

    # Show series_id in the example if we have one
    series_example = f'\n  "series_id": "{series_id}",' if series_id else ""

    lines.append("### Preferred: `graph ui-exp` file watcher\n")
    lines.append(
        "Write each variant as an HTML file in a working directory. "
        "The `graph ui-exp` tool watches the directory and auto-posts changes to the "
        "experiment API. The browser updates in real-time via SSE.\n\n"
        "```bash\n"
        "# Set up a working directory with one .html file per variant\n"
        "mkdir -p /tmp/variants\n"
        "# Write your variant HTML files:\n"
        "#   /tmp/variants/variant-a.html\n"
        "#   /tmp/variants/variant-b.html\n"
        "# Each filename (without .html) becomes the variant ID.\n"
        "\n"
    )
    if series_id:
        lines.append(f"# Start watching (appends to this experiment's series):\n")
        lines.append(f"graph ui-exp \"Iteration N: <description>\" /tmp/variants/ --series {series_id}\n")
    else:
        lines.append(f"# Start watching (creates a new series):\n")
        lines.append(f"graph ui-exp \"<experiment title>\" /tmp/variants/\n")
    lines.append(
        "```\n\n"
        "The watcher runs in the foreground. Edit any .html file and save — the browser "
        "auto-updates. Add new .html files for new variants. Delete files to remove variants. "
        "Ctrl+C to stop watching.\n\n"
        "**Important:** Each variant must be a complete self-contained HTML document with "
        "`<link rel=\"stylesheet\" href=\"/static/tailwind.css\">` in the head for Tailwind styling.\n"
    )
    lines.append("### Alternative: direct API\n")
    lines.append(
        "```bash\n"
        "curl -sk https://localhost:8080/api/experiments \\\n"
        "  -X POST -H 'Content-Type: application/json' \\\n"
        "  -d '{\n"
    )
    series_field = f'"series_id": "{series_id}",' if series_id else '"series_id": "<optional>",'
    lines.append(f'  "title": "Iteration N: <description>",\n')
    lines.append(f'  {series_field}\n')
    lines.append('  "variants": [{"id": "variant-a", "html": "<full HTML>"}]\n')
    lines.append("  }'\n```\n")

    # ── Screenshots ──────────────────────────────────────────
    lines.append("### Screenshots\n")
    lines.append(
        "The user's browser auto-captures a screenshot of the experiment page after each "
        "variant loads. When using `graph ui-exp`, the screenshot is automatically copied "
        "into your working directory as `screenshot.png`. Read it any time to see exactly "
        "what the user sees:\n\n"
        "  `<your-variants-dir>/screenshot.png`\n\n"
        "The screenshot updates automatically after each iteration.\n"
    )
    # Check if a screenshot already exists for the current experiment
    screenshot_path = Path(f"data/experiments/{exp_id}/screenshot.png")
    if screenshot_path.exists():
        abs_path = str(screenshot_path.resolve())
        lines.append(f"**A screenshot already exists — read it now:** `{abs_path}`\n")
    lines.append("---\n")

    # ── Workflow ──────────────────────────────────────────────
    lines.append("## Workflow\n")
    lines.append(
        "1. **First:** Read the latest screenshot (if one exists above) to see what the user sees.\n"
        "2. Study the fixture and current variant HTML above.\n"
        "3. Ask the user what aspect of the design they want to refine.\n"
        "4. Start `graph ui-exp` watching a variants directory (see Tools above).\n"
        "5. Write improved variant HTML to .html files in the watched directory — "
        "the browser updates automatically on every save.\n"
        "6. Read the latest screenshot to verify your changes rendered correctly.\n"
        "7. The user reviews and gives feedback. Edit the files and save again. Repeat.\n"
        "8. **When approved:** run `graph bead` to create an implementation bead with "
        "the winning variant's full HTML, all design decisions, and context from this session.\n"
    )

    return "\n".join(lines)


def build_experiment_primer(experiment_id: str) -> dict:
    """Build a Chat With primer for the experiment/design-studio page.

    Returns {"primer_text": str}.
    Raises ValueError if the experiment is not found.
    """
    data = _collect_experiment_data(experiment_id)
    primer_text = _format_experiment_primer(data)
    return {"primer_text": primer_text}


# ── Registry ─────────────────────────────────────────────────────

_BUILDERS = {
    "experiment": build_experiment_primer,
}


def get_primer(page_type: str, context_id: str) -> dict:
    """Route to the correct primer builder for page_type.

    Returns {"primer_text": str}.
    Raises ValueError for unknown page_type or missing context.
    """
    builder = _BUILDERS.get(page_type)
    if builder is None:
        raise ValueError(
            f"Unknown page type: {page_type!r}. Valid types: {VALID_PAGE_TYPES}"
        )
    return builder(context_id)
