"""Typed payload shapes for the ``autonomy/github`` capability surface.

These are NOT Setting rows — they're the JSON-boundary payload shapes
returned by the capability operations declared in :mod:`agents.
capabilities.github.service` and :mod:`agents.capabilities.github.probe`.
Declaring them as ``SettingSchema`` subclasses (without calling
``register_schema``) lets the same typed-field metadata drive:

* generated TypeScript declarations (via ``graph set typegen --schemas``),
  which the dashboard's ``worktrees.js`` consumes for type-checked
  capability data flow;
* runtime validation of the dataclass-shaped outputs at the JSON
  boundary (tests cross-validate ``dataclass.to_dict()`` against
  ``SchemaCls.validate(...)``);
* future codegen of the Python-side input/output classes themselves
  (Phase 5/6 of the codegen migration sprint), at which point the
  dataclasses in ``service.py``/``probe.py`` collapse into thin
  constructors that read this metadata.

Spec: ``graph://865295b3-5cc`` § Capability layer impacts (2 — op
input/output types; 3 — probe result shapes).
"""

from __future__ import annotations

from dataclasses import fields as dc_fields, is_dataclass

from tools.graph.schemas.registry import SettingSchema, field


# ── Check entry (one merge-gate check) ───────────────────────


class CheckEntryV1(SettingSchema):
    """One normalized merge-gate check (CheckRun or StatusContext).

    Mirrors :class:`agents.capabilities.github.service.CheckEntry`. The
    Worktrees navigator renders one disc per check using these fields.
    """

    set_id = "agents.capabilities.github.check_entry"
    schema_revision = 1

    id: str = field(
        required=True,
        description="Stable identifier for the check (gh's id, name, or context)",
    )
    icon: str = field(
        required=True,
        description="1-2 character glyph derived from the label",
    )
    label: str = field(
        required=True,
        description="Operator-facing check name",
    )
    status: str = field(
        required=True,
        description="Normalized lifecycle state",
        enum=["pass", "fail", "running", "pending"],
    )
    detail: str = field(
        required=False,
        description="Optional longer-form failure / progress detail",
    )


# ── Review payload (one PR's review block) ───────────────────


class ReviewPayloadV1(SettingSchema):
    """Normalized review block for a single PR.

    Mirrors :class:`agents.capabilities.github.service.ReviewPayload`.
    ``base_sha`` and ``commit_shas`` enable stacked-PR support
    (Graphite/Sapling style).
    """

    set_id = "agents.capabilities.github.review_payload"
    schema_revision = 1

    number: int = field(
        required=False,
        description="GitHub PR number (None for synthesized non-PR branches)",
    )
    node_id: str = field(
        required=True,
        description="GitHub node ID / subscribable ID for PR-level subscription",
    )
    url: str = field(required=True, description="Canonical web URL of the PR")
    title: str = field(required=True, description="PR title")
    body: str = field(required=True, description="PR description body")
    head_sha: str = field(required=True, description="Head commit SHA of the PR")
    base_sha: str = field(
        required=True,
        description=(
            "Base commit SHA. For a single PR, the merge-base with "
            "``base_branch``. For a stacked PR, the previous PR's head_sha."
        ),
    )
    base_branch: str = field(
        required=True,
        description="Integration branch name the PR targets",
    )
    state: str = field(
        required=True,
        description="GitHub PR state",
        enum=["open", "closed", "merged", "unknown"],
    )
    is_draft: bool = field(
        required=True,
        description="True when the PR is in draft mode",
    )
    aggregate_state: str = field(
        required=True,
        description="Aggregate gate state surfaced as the PR badge color",
        enum=["green", "yellow"],
    )
    running: bool = field(
        required=True,
        description="True when at least one check is running or pending",
    )
    checks: list = field(
        required=True,
        description="Normalized merge-gate checks for the PR",
        element={
            "id": {"type": "string", "required": True},
            "icon": {"type": "string", "required": True},
            "label": {"type": "string", "required": True},
            "status": {"type": "string", "required": True,
                       "enum": ["pass", "fail", "running", "pending"]},
            "detail": {"type": "string"},
        },
    )
    commit_shas: list = field(
        required=True,
        description=(
            "Local SHAs in ``rev-list base..HEAD`` order assigned to this PR "
            "(stacked-PR aware)"
        ),
        element={"type": "string"},
    )


# ── Worktree-row exec result ─────────────────────────────────


class WorktreeGithubExecResultV1(SettingSchema):
    """Structured outcome of one row-scoped GitHub operation.

    Mirrors :class:`agents.capabilities.github.service.
    WorktreeGithubExecResult`. ``ok`` is True iff ``failure is None``.
    """

    set_id = "agents.capabilities.github.exec_result"
    schema_revision = 1

    operation: str = field(
        required=True,
        description="Op name (one of OP_REVIEW_READ / OP_REVIEW_REFRESH / OP_GATES_WATCH_SET)",
    )
    session_name: str = field(required=True, description="Worktree session id")
    repo_name: str = field(required=True, description="Worktree repo handle")
    ok: bool = field(
        required=True,
        description="True iff ``failure is None``",
    )
    stdout: str = field(
        required=True,
        description="Captured docker-exec stdout",
    )
    stderr: str = field(
        required=True,
        description="Captured docker-exec stderr",
    )
    exit_code: int = field(
        required=True,
        description="Process exit code (0 on success)",
    )
    timed_out: bool = field(
        required=True,
        description="True when the docker-exec hit the operation timeout",
    )
    container_name: str = field(
        required=False,
        description="Live container name backing the worktree row",
    )
    branch: str = field(
        required=False,
        description="Branch the operation targeted",
    )
    repo_slug: str = field(
        required=False,
        description="``owner/repo`` slug derived from the managed clone path",
    )
    command: list = field(
        required=True,
        description="Argv passed to ``docker exec``",
        element={"type": "string"},
    )
    failure: str = field(
        required=False,
        description=(
            "Canonical failure code (one of FAILURE_*); None on success"
        ),
    )
    error_message: str = field(
        required=False,
        description="Short operator-facing error message; None on success",
    )


# ── Probe result ─────────────────────────────────────────────


class ProbeResultV1(SettingSchema):
    """Normalized capability-probe outcome for one workspace session.

    Mirrors :class:`agents.capabilities.github.probe.ProbeResult`. The
    runner in ``auto-hjr1d`` will surface this shape uniformly across
    every capability so dashboards can answer "ready, unavailable, or
    degraded — and why" without provider-specific branching.
    """

    set_id = "agents.capabilities.github.probe_result"
    schema_revision = 1

    contract: str = field(
        required=True,
        description="Capability contract this probe answers for",
    )
    contract_version: int = field(
        required=True,
        description="Pinned canonical contract version",
    )
    implementation: str = field(
        required=True,
        description="Implementation identifier (e.g. ``autonomy/github``)",
    )
    implementation_version: int = field(
        required=True,
        description="Pinned canonical implementation version",
    )
    delivery_mode: str = field(
        required=True,
        description="How the implementation reaches the workspace at launch",
        enum=["image_baked", "mounted_tools", "host_proxy", "hybrid"],
    )
    state: str = field(
        required=True,
        description="Probe outcome",
        enum=["ready", "unavailable", "degraded"],
    )
    reason: str = field(
        required=False,
        description=(
            "Why the probe is not ``ready`` (one of "
            "``no_live_container`` / ``tool_missing`` / ``env_missing`` / "
            "``probe_failed``); None when ``state == 'ready'``"
        ),
    )
    missing_tools: list = field(
        required=True,
        description="Tools the probe expected to find on PATH but did not",
        element={"type": "string"},
    )
    missing_env: list = field(
        required=True,
        description="Env vars the probe expected to be set but were not",
        element={"type": "string"},
    )
    missing_secret_files: list = field(
        required=True,
        description="Secret files the probe expected to find but did not",
        element={"type": "string"},
    )
    details: dict = field(
        required=True,
        description=(
            "Free-form diagnostic details. May carry ``exit_code``, "
            "``stderr`` (truncated), and ``timed_out`` for "
            "``probe_failed`` outcomes."
        ),
    )


# ── Codegen entry-point list ─────────────────────────────────


# Stable list of capability schemas exposed for typegen. The CLI invokes
# typegen with this set via ``graph set typegen --schemas`` so the
# generated ``capability-github.d.ts`` stays in lockstep with the Python
# declarations. Tests pin the list against drift.
CODEGEN_SCHEMAS: tuple[type[SettingSchema], ...] = (
    CheckEntryV1,
    ReviewPayloadV1,
    WorktreeGithubExecResultV1,
    ProbeResultV1,
)


def link_dataclass_to_schema(
    dc_cls: type, schema_cls: type[SettingSchema]
) -> None:
    """Wire a runtime dataclass to its declarative typed schema.

    Sets ``dc_cls.payload_schema = schema_cls`` and asserts the
    dataclass's field set matches the schema's ``_field_metadata`` keys
    one-for-one. Any drift between the runtime constructor and the
    codegen source raises :class:`RuntimeError` at import time, so
    silent shape mismatches can't accumulate between dataclass edits
    and schema edits — and so the generated ``capability-github.d.ts``
    can't claim a field the dataclass doesn't actually carry.

    Used by ``service.py`` and ``probe.py`` once their dataclasses are
    declared, so the import-time link is the source of truth for the
    relationship and there's no place in the codebase where a
    dataclass field exists without a corresponding schema field.
    """
    if not is_dataclass(dc_cls):
        raise RuntimeError(
            f"link_dataclass_to_schema: {dc_cls.__name__} is not a dataclass"
        )
    runtime_fields = {f.name for f in dc_fields(dc_cls)}
    schema_fields = set(schema_cls._field_metadata.keys())
    if runtime_fields != schema_fields:
        only_runtime = runtime_fields - schema_fields
        only_schema = schema_fields - runtime_fields
        msg = (
            f"{dc_cls.__name__} fields don't match {schema_cls.__name__}:"
        )
        if only_runtime:
            msg += f"\n  only on dataclass: {sorted(only_runtime)}"
        if only_schema:
            msg += f"\n  only on schema:    {sorted(only_schema)}"
        raise RuntimeError(msg)
    dc_cls.payload_schema = schema_cls


__all__ = [
    "CheckEntryV1",
    "ReviewPayloadV1",
    "WorktreeGithubExecResultV1",
    "ProbeResultV1",
    "CODEGEN_SCHEMAS",
    "link_dataclass_to_schema",
]
