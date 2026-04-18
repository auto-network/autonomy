"""Render the workspace runtime primer from a ProjectConfig.

The primer is layer 1 of the workspace context stack: the shared Autonomy
runtime (graph, bd, agent-browser, CrossTalk, session identity) plus the
per-workspace conditional sections (writable repos, DinD, background
startup, graph scoping) driven by the workspace config flags.

This replaces the static per-workspace CLAUDE.md stubs in
agents/projects/*/CLAUDE.md.

Design refs:
    graph://eabec73c-baa  Workspaces & Orgs signpost
    graph://9bb529d0-da0  Draft template discussion
"""

from __future__ import annotations

from pathlib import Path

import jinja2

from agents.project_config import ProjectConfig

TEMPLATE_DIR = Path(__file__).resolve().parent / "primers"
PROJECTS_DIR = Path(__file__).resolve().parent / "projects"

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=False,
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
    undefined=jinja2.StrictUndefined,
)


def render_workspace_primer(config: ProjectConfig) -> str:
    """Render the workspace runtime primer for a given project config.

    Args:
        config: Parsed project entry from agents/projects.yaml.

    Returns:
        The rendered markdown primer as a string.
    """
    template = _env.get_template("workspace.md.j2")
    writable_repos = [r for r in config.repos if r.writable]
    readonly_repos = [r for r in config.repos if not r.writable]
    workspace_primer_path = PROJECTS_DIR / config.id / "primer.md"
    workspace_primer = (
        workspace_primer_path.read_text()
        if workspace_primer_path.is_file()
        else ""
    )
    return template.render(
        config=config,
        writable_repos=writable_repos,
        readonly_repos=readonly_repos,
        workspace_primer=workspace_primer,
    )
