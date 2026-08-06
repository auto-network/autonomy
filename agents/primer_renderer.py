"""Render the workspace runtime primer from a WorkspaceV1.

The primer is layer 1 of the workspace context stack: the shared Autonomy
runtime (graph, bd, agent-browser, CrossTalk, session identity) plus the
    per-workspace conditional sections (writable repos, nested Docker, background
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

from agents.workspace_settings import REPO_ROOT, WorkspaceV1
from tools.graph import ops as graph_ops
from tools.graph.schemas.turn_correction import (
    SCHEMA_REVISION as TURN_CORRECTION_REVISION,
    SET_ID as TURN_CORRECTION_SET_ID,
    resolve_payload as resolve_turn_correction_payload,
)
from tools.graph.schemas.org_primer import (
    SCHEMA_REVISION as ORG_PRIMER_REVISION,
    SET_ID as ORG_PRIMER_SET_ID,
    resolve_markdown as resolve_primer_markdown,
    resolve_order as resolve_primer_order,
)
from tools.graph.schemas.workspace_primer import (
    SCHEMA_REVISION as WORKSPACE_PRIMER_REVISION,
    SET_ID as WORKSPACE_PRIMER_SET_ID,
)
from tools.graph.commit_policy import (
    WorkspaceCapabilityContext,
    describe_commit_policy,
    resolve_commit_policy,
)

TEMPLATE_DIR = Path(__file__).resolve().parent / "primers"

#: Long-form companion to the template's "Customizing This Primer"
#: section. Rendered for every org, so this note is kept ``published``
#: — a ``curated`` or ``raw`` note is invisible to peer orgs and the
#: pointer would be a dead link everywhere but ``autonomy``. Bare id,
#: not a ``graph://`` URI — the template renders it into a literal
#: ``graph read`` command, and that CLI does not accept the scheme.
PRIMER_CUSTOMIZATION_NOTE = "6acb146f-d16"

_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=False,
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
    undefined=jinja2.StrictUndefined,
)


def _workspace_queries_block(cap, workspace_id: str) -> str:
    """Render the workspace's named queries as an invocation block, or ``""``.

    Named queries are workspace data (``workspace_overrides.named_queries``
    on the enable Setting); this is the projection half of the mechanism —
    the workspace teaches the capability *and the agent's context* in one
    place. Rendered only when the implementation actually exposes a
    ``*-query`` command, so a capability version that predates named
    queries never advertises an invocation that would fail.
    """
    # Pure named-query helpers; shipped with the jira impl today but
    # provider-neutral (placeholder derivation over workspace data).
    from agents.capabilities.jira.backend import queries as named_queries

    rows = named_queries.list_queries(cap.workspace_overrides)
    if not rows:
        return ""
    command = next(
        (c for c in (cap.tool_target.expose_commands if cap.tool_target else ())
         if c.endswith("-query")),
        None,
    )
    if command is None:
        return ""
    invocations = [
        (
            f"{command} {row['name']}"
            + "".join(f" {p}=<value>" for p in row["params"]),
            row["summary"],
        )
        for row in rows
    ]
    width = max(len(inv) for inv, _ in invocations)
    lines = [
        f"{inv:<{width}}   # {summary}" if summary else inv
        for inv, summary in invocations
    ]
    return (
        f"**Workspace queries ({workspace_id})** — standardized views "
        f"taught to this workspace as data (`{command} --list` shows the "
        "live set):\n\n"
        "```bash\n" + "\n".join(lines) + "\n```"
    )


def _capability_primer_blocks(config: WorkspaceV1) -> list[dict]:
    """Build the per-capability primer projection rows for the template.

    Each row carries:

    * ``implementation`` — display heading (e.g. ``autonomy/jira``).
    * ``contract`` / ``contract_version`` — the contract this row binds.
    * ``mount_target`` — the deterministic container path where the
      capability package is visible (so the agent can inspect
      ``SKILL.md`` / ``primer.md`` / bundled scripts directly).
    * ``primer_text`` — content read from ``MaterializedCapability
      .primer_path`` if the file exists, otherwise an empty string. The
      primer file is the implementation's short, action-oriented surface
      (``jira-read KEY``, ``gh`` is authenticated, etc.); the renderer
      simply embeds it under the per-capability heading, followed by the
      workspace's named-query block when the enable Setting defines one.

    Returns rows in stable contract order — :class:`MaterializedCapability`
    is already sorted by contract, so iterating the input is sufficient.
    """
    rows: list[dict] = []
    for cap in config.capabilities:
        primer_text = ""
        if cap.primer_path:
            primer_file = REPO_ROOT / cap.primer_path
            if primer_file.is_file():
                primer_text = primer_file.read_text().rstrip()
        queries_block = _workspace_queries_block(cap, config.id)
        if queries_block:
            primer_text = (
                f"{primer_text}\n\n{queries_block}" if primer_text
                else queries_block
            )
        org_primer = getattr(cap, "org_primer", "").strip()
        if org_primer:
            org_block = (
                f"#### Organization-specific guidance ({config.graph_project})"
                f"\n\n{org_primer}"
            )
            primer_text = (
                f"{primer_text}\n\n{org_block}" if primer_text else org_block
            )
        rows.append({
            "implementation": cap.implementation,
            "contract": cap.contract,
            "contract_version": cap.contract_version,
            "delivery_mode": cap.delivery_mode,
            "mount_target": cap.mount_target,
            "primer_text": primer_text,
        })
    return rows


# ── Turn-correction settings projection ──────────────────────


# Mode → opening sentence the primer renders in front of the canonical
# command. Centralized here so tests can assert each mode produces
# distinct, mode-appropriate guidance and operators can override per
# workspace via ``instruction_template`` on the Setting payload.
_TURN_CORRECTION_INSTRUCTIONS: dict[str, str] = {
    "off": (
        "Turn-correction guidance is disabled for this workspace, but "
        "the command is documented here for reference. Do not volunteer "
        "corrections — only emit one if the operator explicitly asks."
    ),
    "conservative": (
        "When a user message is clearly garbled or ambiguous enough that "
        "your interpretation would materially affect the reply, emit a "
        "turn-correction suggestion. Skip low-value style edits that do "
        "not change meaning."
    ),
    "balanced": (
        "When you suspect a perception gap on the most recent user "
        "message — wording that obscures intent, transcription errors, "
        "missing words, or ambiguity that forces you to guess — emit a "
        "turn-correction suggestion immediately, then continue if the "
        "path forward is still clear."
    ),
    "aggressive": (
        "Assume every user message is a candidate. If a correction would "
        "make the shared transcript or your understanding even slightly "
        "better, emit it silently and keep working. Favor dictation, "
        "terminology, and ambiguity fixes; skip pure style nits. Only "
        "acknowledge it explicitly if you are too unsure to proceed and "
        "need to ask, 'Is this what you meant?'"
    ),
}


_TURN_CORRECTION_COMMAND = (
    "graph turn-correction suggest [corrected_text | --stdin] "
    "[--mode <off|conservative|balanced|aggressive>] "
    "[--reason <text>] [--confidence <0..1>] --json"
)


def _read_turn_correction_setting(config: WorkspaceV1) -> dict | None:
    """Return the resolved Setting payload for ``config``, or ``None``.

    Turn-correction policy is workspace-scoped by both owning org and
    workspace id: the row lives in the workspace's org DB under
    ``autonomy.workspace.turn_correction#1`` keyed by ``config.id``.

    A missing Setting is not an error — the renderer falls back to the
    schema defaults so primer output remains useful before any
    workspace-specific row has been authored.
    """
    try:
        members = graph_ops.read_set(
            TURN_CORRECTION_SET_ID,
            org=config.graph_project,
            peers=[],
            target_revision=TURN_CORRECTION_REVISION,
        )
    except Exception:
        return None
    for member in members.members:
        if member.key == config.id:
            return dict(member.payload) if isinstance(member.payload, dict) \
                else None
    return None


def _turn_correction_block(config: WorkspaceV1) -> dict:
    """Compose the turn-correction primer projection for ``config``.

    Resolves the ``autonomy.workspace.turn_correction#1`` Setting for
    ``(config.graph_project, config.id)``, layers it over the schema
    defaults, and returns a fully-populated dict the template can
    render without further conditionals beyond the ``enabled`` switch.
    """
    raw = _read_turn_correction_setting(config)
    resolved = resolve_turn_correction_payload(raw)
    aggressiveness = resolved["aggressiveness"]
    instruction = (
        resolved.get("instruction_template")
        or _TURN_CORRECTION_INSTRUCTIONS.get(
            aggressiveness, _TURN_CORRECTION_INSTRUCTIONS["balanced"]
        )
    )
    return {
        "enabled": bool(resolved["enabled"]),
        "aggressiveness": aggressiveness,
        "persist_accepts_to_graph": bool(
            resolved["persist_accepts_to_graph"]
        ),
        "instruction": instruction,
        "command": _TURN_CORRECTION_COMMAND,
    }


def _commit_policy_block(config: WorkspaceV1) -> dict:
    """Render the commit workflow policy primer block for *config*."""
    context = WorkspaceCapabilityContext(
        issue_tracker_enabled=any(
            cap.contract == "issue_tracker" for cap in config.capabilities
        ),
    )
    try:
        resolved = resolve_commit_policy(
            workspace_id=config.id,
            org=config.graph_project,
            context=context,
        )
    except Exception:
        return {"enabled": False, "text": ""}
    return {
        "enabled": True,
        "profile": resolved.profile,
        "errors": list(resolved.errors),
        "text": describe_commit_policy(resolved).rstrip(),
    }


def _overlay_markdown(
    config: WorkspaceV1,
    set_id: str,
    revision: int,
    key: str,
) -> str:
    """Resolve one primer overlay layer from its Setting rows.

    The Setting rows live in the workspace's own org DB and are read with
    ``peers=[]``, so overlay content never crosses an org boundary. That
    is the point of storing it here rather than in the platform repo,
    which is open source and has no per-org boundary at all.

    A layer is assembled from every row whose key is either ``key``
    itself or ``key:<block-name>``, concatenated in ``(order, key)``
    sequence. Splitting a layer into named blocks is what makes a single
    section independently switchable: each row carries its own
    ``enabled`` flag, so parking one block leaves the others rendering
    and leaves the parked text in place.

    Rows that are disabled or blank contribute nothing and, importantly,
    leave no blank gap — a layer whose every block is off resolves to
    ``""``, which the template treats the same as no rows at all.

    Settings are the sole source: there is no file fallback. A layer
    with no matching row renders nothing. The pre-migration
    ``agents/orgs/<org>/primer.md`` /
    ``agents/projects/<workspace>/primer.md`` files are no longer read.
    """
    try:
        members = graph_ops.read_set(
            set_id,
            org=config.graph_project,
            peers=[],
            target_revision=revision,
        )
    except Exception:
        return ""

    if members is None:
        return ""

    prefix = f"{key}:"
    blocks: list[tuple[int, str, str]] = []
    for member in members.members:
        if member.key != key and not member.key.startswith(prefix):
            continue
        payload = member.payload if isinstance(member.payload, dict) else None
        body = resolve_primer_markdown(payload).rstrip()
        if not body.strip():
            continue
        blocks.append((resolve_primer_order(payload), member.key, body))

    blocks.sort(key=lambda block: (block[0], block[1]))
    return "\n\n".join(body for _, _, body in blocks)


def render_workspace_primer(config: WorkspaceV1) -> str:
    """Render the workspace runtime primer for a given project config.

    Args:
        config: The workspace's launch shape, resolved entirely from
            graph Settings by ``agents.workspace_settings``. There is no
            ``agents/projects.yaml``; it was removed.

    Returns:
        The rendered markdown primer as a string.
    """
    template = _env.get_template("workspace.md.j2")
    writable_repos = [r for r in config.repos if r.writable]
    readonly_repos = [r for r in config.repos if not r.writable]
    workspace_primer = _overlay_markdown(
        config,
        WORKSPACE_PRIMER_SET_ID,
        WORKSPACE_PRIMER_REVISION,
        config.id,
    )
    org_primer = _overlay_markdown(
        config,
        ORG_PRIMER_SET_ID,
        ORG_PRIMER_REVISION,
        config.graph_project,
    )
    capability_blocks = _capability_primer_blocks(config)
    turn_correction = _turn_correction_block(config)
    commit_policy = _commit_policy_block(config)
    return template.render(
        config=config,
        writable_repos=writable_repos,
        readonly_repos=readonly_repos,
        workspace_primer=workspace_primer,
        org_primer=org_primer,
        org=config.graph_project,
        capability_blocks=capability_blocks,
        turn_correction=turn_correction,
        commit_policy=commit_policy,
        primer_customization_note=PRIMER_CUSTOMIZATION_NOTE,
    )
