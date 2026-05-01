"""Skill / primer Markdown projection from capability contract metadata.

Pure functions over a contract's Setting payload (the ``ops``, ``summary``,
``notes`` fields validated by :class:`CapabilityContractV1`). Produces
the agent-facing ``SKILL.md`` and ``primer.md`` content that capability
providers ship today as hand-written Markdown — but rendered from the
same source the deterministic ops surface reads, so the two views can
no longer drift.

Generation is offline: the module never opens DB connections, never
hits the network. A caller (CLI, test, tool) loads the contract Setting
however it likes, hands the payload here, gets Markdown back.

Bead: auto-3.5 in the codegen migration sprint (graph://b8a3ae75-e5f).
Design: graph://865295b3-5cc § "Skill / primer projection".
"""

from __future__ import annotations

from typing import Any


# ── Op grouping (visual-nested namespaces) ───────────────────


def group_ops_by_prefix(ops: list[dict]) -> tuple[list[tuple[str, list[dict]]], list[dict]]:
    """Group operations by their first-underscore prefix.

    Returns a pair ``(grouped, ungrouped)``:

    * ``grouped`` — list of ``(prefix, ops_in_group)`` tuples, in
      first-appearance order. A "group" is the set of ops sharing the
      same first-underscore prefix where at least one OTHER prefix also
      appears in the contract (i.e., the prefix structure has signal).
    * ``ungrouped`` — ops without an underscore in their name, or all
      ops if there's only a single prefix (no nested-namespace signal).

    For the ``source_control`` contract — ``review_read``,
    ``review_refresh``, ``gates_watch_set`` — this returns two groups
    (``review`` with 2 ops, ``gates`` with 1 op) and an empty
    ungrouped list. For a contract with only flat names, it returns
    no groups and the full ops list as ungrouped.

    The shape mirrors how the github SKILL.md renders the
    ``source_control`` operations today (sub-bullets under
    ``review.*`` / ``gates.*`` headings).
    """
    by_prefix: list[tuple[str, list[dict]]] = []
    index: dict[str, list[dict]] = {}
    leftovers: list[dict] = []

    for op in ops:
        name = op.get("name", "")
        if "_" not in name:
            leftovers.append(op)
            continue
        prefix = name.split("_", 1)[0]
        if prefix not in index:
            index[prefix] = []
            by_prefix.append((prefix, index[prefix]))
        index[prefix].append(op)

    # If only one prefix appears AND there are no leftovers, the
    # "grouping" carries no signal — flatten back.
    if not leftovers and len(by_prefix) == 1:
        return [], list(ops)
    # If there are NO prefixes at all (all leftover), nothing to group.
    if not by_prefix:
        return [], list(ops)
    return by_prefix, leftovers


# ── Markdown rendering ───────────────────────────────────────


def _op_subheading(op: dict, prefix: str | None) -> str:
    """Format an op's display name. With a prefix, render dotted; flat
    otherwise.

    ``review_read`` under prefix ``review`` → ``review.read``.
    Flat ``branch_status`` (no group) stays ``branch_status``.
    """
    name = op["name"]
    if prefix and name.startswith(prefix + "_"):
        suffix = name[len(prefix) + 1:]
        return f"{prefix}.{suffix}"
    return name


def _render_op_block(op: dict, *, contract_name: str, prefix: str | None) -> list[str]:
    """Render a single op as Markdown bullet lines.

    Each op gets a leading bullet with the dotted full name
    (``contract.prefix.suffix`` or ``contract.name``) and the op's
    summary on the same line. Future enhancements (input/output
    rendering) extend this without changing the bullet's first line
    — consumers grep on the leading bullet to find ops.
    """
    display = _op_subheading(op, prefix)
    fq = f"{contract_name}.{display}"
    lines = [f"- `{fq}` — {op.get('summary', '').strip()}"]
    return lines


def render_skill_md(
    contract: dict,
    *,
    provider: dict | None = None,
) -> str:
    """Render the agent-facing SKILL.md for a capability contract.

    ``contract`` is the payload of an ``autonomy.capability.contract#1``
    Setting (``{name, version, summary, ops, notes?}``).

    ``provider`` is an optional capability-impl manifest dict. When
    provided, the generated frontmatter and the body's introductory
    paragraph mention the provider; otherwise the output is
    contract-only.

    The rendering is structural and stable: a future renderer can
    re-emit the same content from the same input deterministically, so
    a pre-commit drift check (analogous to the typegen ``--check``
    mode) can compare on-disk hand-written content against the
    generated baseline.
    """
    name = contract["name"]
    version = contract["version"]
    summary = contract.get("summary", "").strip()
    ops = contract.get("ops") or []

    lines: list[str] = []

    # Frontmatter — matches the existing skill file convention.
    lines.append("---")
    if provider is not None and provider.get("name"):
        lines.append(f"name: {provider['name']}")
    else:
        lines.append(f"name: {name}@{version}")
    if summary:
        # Single-line description, frontmatter expects no embedded newlines.
        lines.append(f"description: {summary}")
    lines.append("---")
    lines.append("")

    # Title
    title_subject = (
        provider.get("name") if provider and provider.get("name")
        else f"{name}@{version}"
    )
    lines.append(f"# {title_subject}")
    lines.append("")

    if summary:
        lines.append(summary)
        lines.append("")

    if provider is not None and provider.get("notes"):
        notes = str(provider["notes"]).strip()
        if notes:
            lines.append(notes)
            lines.append("")

    if not ops:
        lines.append("_No operations declared on this contract._")
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    # Operations.
    lines.append("## Operations")
    lines.append("")
    lines.append(
        f"This capability implements the `{name}@{version}` contract. "
        "Operations follow the contract's deterministic surface; "
        "agentic call sites should reach for these names rather than "
        "vendor-specific commands when the substrate exposes them."
    )
    lines.append("")

    grouped, ungrouped = group_ops_by_prefix(ops)

    if ungrouped and not grouped:
        for op in ungrouped:
            lines.extend(_render_op_block(op, contract_name=name, prefix=None))
        lines.append("")
    else:
        for op in ungrouped:
            lines.extend(_render_op_block(op, contract_name=name, prefix=None))
        if ungrouped and grouped:
            lines.append("")
        for prefix, group_ops in grouped:
            lines.append(f"### `{name}.{prefix}.*`")
            lines.append("")
            for op in group_ops:
                lines.extend(_render_op_block(op, contract_name=name, prefix=prefix))
            lines.append("")

    if contract.get("notes"):
        notes = str(contract["notes"]).strip()
        if notes:
            lines.append("## Notes")
            lines.append("")
            lines.append(notes)
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_primer_md(
    contract: dict,
    *,
    provider: dict | None = None,
) -> str:
    """Render the agentic primer for a capability contract.

    Primers are the brief, scannable companion to ``SKILL.md``: a
    one-paragraph description of the contract plus a flat list of every
    operation. Codegen uses the same source as the skill rendering, so
    the two views report the same operation inventory — they CANNOT
    drift apart.
    """
    name = contract["name"]
    version = contract["version"]
    summary = contract.get("summary", "").strip()
    ops = contract.get("ops") or []

    lines: list[str] = []
    lines.append("---")
    if provider is not None and provider.get("name"):
        lines.append(f"capability: {provider['name']}")
    lines.append(f"contract: {name}@{version}")
    lines.append("---")
    lines.append("")

    if summary:
        lines.append(summary)
        lines.append("")

    if not ops:
        lines.append("_No operations declared._")
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    lines.append(f"Operations on `{name}@{version}`:")
    lines.append("")
    grouped, ungrouped = group_ops_by_prefix(ops)
    for op in ungrouped:
        lines.extend(_render_op_block(op, contract_name=name, prefix=None))
    for prefix, group_ops in grouped:
        for op in group_ops:
            lines.extend(_render_op_block(op, contract_name=name, prefix=prefix))
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def list_op_names(contract: dict) -> list[str]:
    """Return the dotted operation names a contract declares.

    Helper consumers (tests, drift checks) reach for the same source
    of truth used by the skill / primer renderers above.
    """
    name = contract["name"]
    out: list[str] = []
    grouped, ungrouped = group_ops_by_prefix(contract.get("ops") or [])
    for op in ungrouped:
        out.append(f"{name}.{_op_subheading(op, None)}")
    for prefix, group_ops in grouped:
        for op in group_ops:
            out.append(f"{name}.{_op_subheading(op, prefix)}")
    return out


def _ensure_dict(arg: Any, label: str) -> dict:
    if not isinstance(arg, dict):
        raise TypeError(f"{label} must be a dict, got {type(arg).__name__}")
    return arg
