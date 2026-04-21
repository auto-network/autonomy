"""Render the workspace runtime primer from a WorkspaceV1.

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

import re
from pathlib import Path

import jinja2

from agents.workspace_settings import WorkspaceV1

TEMPLATE_DIR = Path(__file__).resolve().parent / "primers"
PROJECTS_DIR = Path(__file__).resolve().parent / "projects"
ORGS_DIR = Path(__file__).resolve().parent / "orgs"

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=False,
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
    undefined=jinja2.StrictUndefined,
)


class PrimerOverlayDriftError(ValueError):
    """A per-workspace primer overlay contradicts projects.yaml.

    Raised when free-form overlay text asserts a repo mount is writable
    (or read-only) in a way that does not match ``config.repos[*].writable``.
    Prevents recurrences of the kind of silent drift found during the
    adversarial review (graph://d73cd9c7-c6f, bead auto-yj7o), where a
    ``writable: false → true`` flip in projects.yaml left the NG overlay
    still telling agents the repo was read-only.
    """


_WRITABILITY_WINDOW = 160  # chars before/after a mount path match

_WRITABLE_RE = re.compile(r"\bwritable\b", re.IGNORECASE)
_READONLY_RE = re.compile(r"\bread[\s-]?only\b", re.IGNORECASE)


def _find_overlay_writability_drift(
    config: WorkspaceV1, overlay_text: str
) -> list[str]:
    """Return drift messages for overlay claims that contradict config.repos.

    For each repo in ``config.repos``, scan ``overlay_text`` for the mount
    path (bounded so ``/workspace/enterprise`` does not match a substring of
    ``/workspace/enterprise_ng``) and inspect a small window around each hit
    for the words ``writable`` and ``read-only``. If the only claim in that
    window contradicts ``repo.writable``, emit a message. Windows that
    mention both words are treated as contextual commentary and skipped.
    """
    if not overlay_text:
        return []
    messages: list[str] = []
    seen: set[str] = set()
    for repo in config.repos:
        for match in re.finditer(re.escape(repo.mount), overlay_text):
            before = overlay_text[match.start() - 1] if match.start() > 0 else ""
            after = (
                overlay_text[match.end()] if match.end() < len(overlay_text) else ""
            )
            if before.isalnum() or before == "_":
                continue
            if after.isalnum() or after == "_":
                continue
            start = max(0, match.start() - _WRITABILITY_WINDOW)
            end = min(len(overlay_text), match.end() + _WRITABILITY_WINDOW)
            window = overlay_text[start:end]
            says_writable = bool(_WRITABLE_RE.search(window))
            says_readonly = bool(_READONLY_RE.search(window))
            if says_writable and says_readonly:
                continue
            msg: str | None = None
            if repo.writable and says_readonly:
                msg = (
                    f"overlay describes {repo.mount} as read-only "
                    f"but projects.yaml marks it writable=true"
                )
            elif not repo.writable and says_writable:
                msg = (
                    f"overlay describes {repo.mount} as writable "
                    f"but projects.yaml marks it writable=false"
                )
            if msg and msg not in seen:
                seen.add(msg)
                messages.append(msg)
    return messages


def render_workspace_primer(config: WorkspaceV1) -> str:
    """Render the workspace runtime primer for a given project config.

    Args:
        config: Parsed project entry from agents/projects.yaml.

    Returns:
        The rendered markdown primer as a string.

    Raises:
        PrimerOverlayDriftError: if the per-workspace overlay asserts a
            repo-mount writability that contradicts ``config.repos``.
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
    org_primer_path = ORGS_DIR / config.graph_project / "primer.md"
    org_primer = (
        org_primer_path.read_text()
        if org_primer_path.is_file()
        else ""
    )
    drift = _find_overlay_writability_drift(config, workspace_primer)
    if drift:
        raise PrimerOverlayDriftError(
            f"primer overlay for project {config.id!r} contradicts "
            f"projects.yaml: " + "; ".join(drift)
        )
    return template.render(
        config=config,
        writable_repos=writable_repos,
        readonly_repos=readonly_repos,
        workspace_primer=workspace_primer,
        org_primer=org_primer,
        org=config.graph_project,
    )
