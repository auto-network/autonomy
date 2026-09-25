"""Generated views of the key registry.

Reads registry.yaml and writes five artifacts into generated/:

- key-graph.mmd     — Mermaid diagram: derivation edges solid, seal edges
                      dashed, nodes colored by custody class.
- key-register.md   — the per-key register as markdown, mirroring the crib
                      sheet's section 9 layout so the two can be diffed.
- proof-coverage.md — one row per key and per mutation: the Tamarin lemmas
                      covering it, or GAP.
- registry.json     — the whole registry as sorted, stable JSON for
                      dashboard and Key Ceremony Atlas consumption.
- workflows.md      — mutation authority, preconditions, effects and refusals.
- workflow-goals.md — actors, artifacts with their producers and consumers,
                      the workflow mutations' opens/requires/produces, and
                      each goal: starting states, recorded current order,
                      and known defects.

The generated files are committed; tests/test_gen.py regenerates them and
fails if the committed copies differ, so the views cannot silently drift
from the data. Regenerate with: python3 gen.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GENERATED = HERE / "generated"

sys.path.insert(0, str(HERE))
import keyreg  # noqa: E402

CUSTODY_ORDER = ("cold", "memory", "disk", "public")

# Validated against the dataviz skill's palette checks (light surface):
# lightness band, chroma floor, CVD separation, and normal-vision floor all
# pass; the contrast WARN is relieved by every node carrying a text label.
CUSTODY_STYLE = {
    "cold": "fill:#2a78d6,color:#ffffff,stroke:#104281,stroke-width:1px",
    "memory": "fill:#eb6834,color:#0b0b0b,stroke:#8a3315,stroke-width:1px",
    "disk": "fill:#1baf7a,color:#0b0b0b,stroke:#0b5e41,stroke-width:1px",
    "public": "fill:#eda100,color:#0b0b0b,stroke:#7a5300,stroke-width:1px",
}

# Cluster order is the reading order: the identity root feeds everything, so
# it comes first; consumers follow.
GROUP_TITLES = {
    "identity-armor": "Personal identity & armor",
    "browser": "Browser session",
    "recovery": "Recovery",
    "org-authority": "Org authority",
    "domain-storage": "Domain storage",
    "vault-classes": "Vault policy classes",
    "sealed-stores": "Sealed stores",
    "fleet": "Fleet",
}


def _label(key_id: str, entry: dict) -> str:
    name = key_id.replace("_", " ")
    if entry.get("status") == "designed":
        name += " ⋄"
    return name


def gen_mermaid(registry: dict) -> str:
    lines = [
        '%%{init: {"theme": "base", "flowchart": {"defaultRenderer": "elk", "nodeSpacing": 26, '
        '"rankSpacing": 42, "curve": "basis", "useMaxWidth": false}, '
        '"themeVariables": {"fontSize": "15px", "clusterBkg": "#f4f4f2", '
        '"clusterBorder": "#c3c2b7"}}}%%',
        "graph LR",
        "    %% Generated from registry.yaml by gen.py — do not edit by hand.",
        "    %% Clusters are the registry's group field (one subsystem each).",
        "    %% Solid arrows: derivation (parent to child). Dashed arrows: the",
        "    %% key's material is sealed to the recipient key. Node color is",
        "    %% custody class: blue cold, orange memory, green disk. A diamond",
        "    %% marks a design-only key. Prose seals remain in the register.",
    ]
    for custody in CUSTODY_ORDER:
        lines.append(f"    classDef {custody} {CUSTODY_STYLE[custody]}")
    by_group: dict[str, list[str]] = {g: [] for g in GROUP_TITLES}
    for key_id, entry in sorted(registry["keys"].items()):
        by_group.setdefault(entry.get("group", "other"), []).append(key_id)
    for group, ids in by_group.items():
        if not ids:
            continue
        title = GROUP_TITLES.get(group, group)
        lines.append(f'    subgraph {group.replace("-", "_")}["{title}"]')
        lines.append("        direction TB")
        for key_id in ids:
            entry = registry["keys"][key_id]
            lines.append(
                f'        {key_id}["{_label(key_id, entry)}"]'
                f':::{entry["custody"]["class"]}'
            )
        lines.append("    end")
    for child, parent, fn in keyreg.derivation_edges(registry):
        lines.append(f"    {parent} -->|{fn}| {child}")
    for key_id, recipient, purpose in keyreg.seal_edges(registry):
        if recipient in registry["keys"]:
            lines.append(f"    {key_id} -.-> {recipient}")
    return "\n".join(lines) + "\n"


def gen_register(registry: dict) -> str:
    lines = [
        "# Per-key register — generated from registry.yaml by gen.py; do not edit.",
        "",
        "The layout mirrors the crib sheet's section 9 register (graph note",
        "1e005d5c-c11) so the two can be compared side by side.",
        "",
        "Start with the [reading guide](../GUIDE.md). See also the",
        "[workflow register](workflows.md) and [proof references](proof-coverage.md).",
        "Built means an implementation is recorded, not that every surrounding workflow is deployed.",
        "Custody describes intended storage; read each snapshot's stated conditions separately.",
        "",
        "## Find a key",
        "",
        "| Key | Subsystem | Kind | Custody | Status |",
        "|---|---|---|---|---|",
    ]
    for key_id, entry in sorted(registry["keys"].items()):
        lines.append(
            f"| [{key_id}](#key-{key_id}) | {entry.get('group', 'other')} | "
            f"{entry['kind']} | {entry['custody']['class']} | {entry.get('status', 'built')} |"
        )
    lines.append("")
    for custody in CUSTODY_ORDER:
        ids = keyreg.keys_by_custody(registry, custody)
        if not ids:
            continue
        lines.append(f"## {custody.upper()}")
        lines.append("")
        for key_id in ids:
            entry = registry["keys"][key_id]
            d = entry["descriptor"]
            status = "" if entry.get("status", "built") == "built" else " · DESIGNED"
            lines.append(f'<a id="key-{key_id}"></a>')
            lines.append(
                f"### {key_id} — {entry['kind']}, {custody} ({entry['custody']['forced_by'].strip()}){status}"
            )
            lines.append("")
            derivation = entry.get("derivation")
            if derivation:
                inputs = ", ".join(derivation.get("inputs", []))
                extra = f"({inputs})" if inputs else ""
                lines.append(
                    f"- **derived** from `{derivation['parent']}` via {derivation['fn']}{extra}"
                )
                if derivation.get("purpose"):
                    lines.append(f"- **derivation purpose** `{derivation['purpose']}`")
            else:
                lines.append("- **minted** at random")
            lines.append(f"- **reaches** {d['reaches'].strip()}")
            lines.append(f"- **snapshot** {d['snapshot'].strip()}")
            lines.append(f"- **live?** {d['live'].strip()}")
            lines.append(f"- **revoke** {d['revoke'].strip()}")
            lines.append(f"- **bound** {d['bound'].strip()}")
            for seal in entry.get("seals_to", []):
                lines.append(
                    f"- **sealed to** {seal['recipient']}"
                    + (f" — {seal['record']}" if seal.get("record") else "")
                    + (f"; purpose `{seal['purpose']}`" if seal.get("purpose") else "")
                )
            for record in entry.get("signs", []):
                lines.append(f"- **signs** {record}")
            if entry["code"]:
                lines.append("- **code** " + " · ".join(f"`{a}`" for a in entry["code"]))
            lines.append("- **crib** " + ", ".join(entry["crib"]))
            if entry.get("notes"):
                lines.append(f"- **notes** {entry['notes'].strip()}")
            lines.append("")
    return "\n".join(lines)


def gen_workflows(registry: dict) -> str:
    """Render the existing mutation data without inferring authorization logic."""
    lines = [
        "# Workflow register", "",
        "Generated from registry.yaml by gen.py; do not edit.", "",
        "A mutation records a key-lifecycle operation, not necessarily a complete user ceremony.",
        "Authority lists name participants or authority sources; they do not encode AND/OR policy.",
        "Read preconditions, notes, and the linked implementation together.",
        "See the [reading guide](../GUIDE.md) and [key register](key-register.md).", "",
        "## Find a workflow", "",
    ]
    for mutation_id in sorted(registry["mutations"]):
        anchor = mutation_id.replace(".", "-").replace(":", "-")
        lines.append(f"- [{mutation_id}](#workflow-{anchor})")
    for mutation_id, entry in sorted(registry["mutations"].items()):
        anchor = mutation_id.replace(".", "-").replace(":", "-")
        lines.extend(["", f'<a id="workflow-{anchor}"></a>',
                      f"## {mutation_id}", "",
                      f"**Status:** {entry.get('status', 'built')}", "",
                      "**Authority:** " + ", ".join(entry["authority"]), ""])
        for title, values in (
            ("Preconditions", entry.get("preconditions", [])),
            *((label.capitalize(), values) for label, values in entry["effects"].items()),
            ("Refusals", entry.get("refusals", [])),
        ):
            if values:
                lines.extend([f"**{title}**", ""])
                lines.extend(f"- {value}" for value in values)
                lines.append("")
        if "opens" in entry:
            lines.extend([
                f"**Workflow:** actors {', '.join(entry['actors'])}; opens {entry['opens']}; "
                f"requires {format_requires(entry['requires'])}; "
                f"produces {', '.join(entry['produces'])}"
                + (f"; rule {entry['rule']}" if entry.get("rule") else ""), ""])
        source = entry["source"]
        symbol = f":{source['symbol']}" if source.get("symbol") else ""
        lines.extend([f"**Source:** `{source['file']}{symbol}` ({source['kind']})",
                      "", "**Crib:** " + ", ".join(entry["crib"])])
        if entry.get("notes"):
            lines.extend(["", entry["notes"].strip()])
    return "\n".join(lines) + "\n"


def format_requires(exprs) -> str:
    def one(expr):
        if isinstance(expr, str):
            return expr
        op, subs = next(iter(expr.items()))
        joiner = " AND " if op == "all" else " OR "
        return "(" + joiner.join(one(sub) for sub in subs) + ")"
    return " AND ".join(one(expr) for expr in exprs) if exprs else "nothing"


def _consumers(registry: dict) -> dict[str, list[str]]:
    """artifact copy -> the workflow mutations and goals requiring it (a bare
    per-actor ref in a mutation binds to each executing actor)."""
    artifacts = registry.get("artifacts") or {}
    consumers: dict[str, list[str]] = {c: [] for c in keyreg.artifact_copies(registry)}

    def note(ref, actor, who):
        base, _, named = ref.partition("@")
        if base not in artifacts:
            return
        copy = ref if named or not artifacts[base].get("per_actor") else f"{base}@{actor}"
        if who not in consumers[copy]:
            consumers[copy].append(who)

    for mut_id, entry in sorted(registry["mutations"].items()):
        if "opens" not in entry:
            continue
        for actor in entry["actors"]:
            for expr in entry["requires"]:
                for ref in keyreg._expr_refs(expr):
                    note(ref, actor, mut_id)
    for goal_id, goal in sorted((registry.get("goals") or {}).items()):
        for expr in goal["requires"]:
            for ref in keyreg._expr_refs(expr):
                note(ref, None, f"goal {goal_id}")
    return consumers


def gen_workflow_goals(registry: dict) -> str:
    lines = [
        "# Workflow goals — generated from registry.yaml by gen.py; do not edit.", "",
        "Artifacts are workflow prerequisites that are not keys. Workflow mutations",
        "record what each step opens (root and persona need a human root window;",
        "delegate and none run on a machine), what it requires (AND/OR) and what it",
        "produces. A bare per-actor artifact inside a mutation binds to the executing",
        "actor. The graph is monotone: freshness conditions such as a ledger head",
        "being present at adoption are outside it and are modeled in TLA+.",
        "See the [workflow register](workflows.md) and the [reading guide](../GUIDE.md).", "",
        "## Actors", "",
    ]
    for actor_id, entry in (registry.get("actors") or {}).items():
        lines.append(f"- **{actor_id}** — {entry['description']}")
    producers = keyreg.workflow_producers(registry)
    given = keyreg.given_copies(registry)
    consumers = _consumers(registry)
    artifacts = registry.get("artifacts") or {}
    lines.extend(["", "## Artifacts", "",
                  "One row per copy: a per-actor artifact has one copy per actor. Origin",
                  "given = held by a starting state, established before the workflow by",
                  "the listed mutations; the planner never re-produces it.", "",
                  "| Artifact copy | Status | Origin | Produced by (workflow) | Given by | Required by |",
                  "|---|---|---|---|---|---|"])
    for copy in keyreg.artifact_copies(registry):
        base, _, actor = copy.partition("@")
        entry = artifacts[base]
        origin = entry.get("origin", "workflow")
        if copy in given:
            origin = "given"
        elif origin == "given":
            origin = "workflow"
        given_by = [r for r in entry.get("given_by") or []
                    if copy in given and (not actor or r.partition("@")[2] in ("", actor))]
        if origin == "defect":
            made = f"**none — defect:** {entry['defect']}"
        elif origin == "open":
            made = "**none — open question**"
        else:
            made = ", ".join(producers.get(copy) or []) or "-"
        anchor = f'<a id="artifact-{base}"></a>' if not actor or actor == next(iter(registry["actors"])) else ""
        lines.append(f"| {anchor}{copy} | {entry.get('status', 'built')} | {origin} | {made} | "
                     f"{', '.join(r.partition('@')[0] for r in given_by) or '-'} | "
                     f"{', '.join(consumers.get(copy) or []) or '-'} |")
    lines.extend(["", "Artifact details:", ""])
    for art_id, entry in sorted((registry.get("artifacts") or {}).items()):
        lines.append(f"- **{art_id}** — {entry['description'].strip()} Code: "
                     + " · ".join(f"`{a}`" for a in entry["code"])
                     + (f". {entry['notes'].strip()}" if entry.get("notes") else ""))
    lines.extend(["", "## Workflow mutations", "",
                  "| Mutation | Status | Actors | Opens | Requires | Produces |",
                  "|---|---|---|---|---|---|"])
    for mut_id, entry in sorted(registry["mutations"].items()):
        if "opens" not in entry:
            continue
        status = entry.get("status", "built") + (f" (rule {entry['rule']})" if entry.get("rule") else "")
        lines.append(f"| {mut_id} | {status} | {', '.join(entry['actors'])} | {entry['opens']} | "
                     f"{format_requires(entry['requires'])} | {', '.join(entry['produces'])} |")
    for goal_id, goal in sorted((registry.get("goals") or {}).items()):
        lines.extend(["", f"## Goal {goal_id}", "", goal["description"].strip(), "",
                      f"**Requires:** {format_requires(goal['requires'])}", ""])
        if goal.get("notes"):
            lines.extend([goal["notes"].strip(), ""])
        lines.extend(["### Starting states", ""])
        for state_id, state in goal["states"].items():
            lines.append(f"- **{state_id}** — {state['description']} Holds: {', '.join(state['holds'])}")
        if goal.get("current_order"):
            lines.extend(["", "### Recorded current order", "",
                          "| Step | Actor | Root opening | Runs | Only from |", "|---|---|---|---|---|"])
            for step in goal["current_order"]:
                lines.append(f"| {step['step']} | {step['actor']} | {'yes' if step['ceremony'] else 'no'} | "
                             f"{', '.join(step['runs'])} | {', '.join(step.get('only_from') or []) or 'all'} |")
        if goal.get("known_defects"):
            lines.extend(["", "### Known defects (named by lint.py, not failed)", ""])
            for defect in goal["known_defects"]:
                what = defect.get("artifact") and f"artifact {defect['artifact']} has no built producer" or (
                    f"step {defect['step']} runs {defect['mutation']} without {defect['missing']}")
                lines.append(f"- {what} — {defect['ref']}")
        lines.extend(_scenario_lines(registry, goal_id, goal))
        for proof in goal.get("proofs") or []:
            lines.append(f"- proof: {proof['framework']} {proof['theory']}: {proof['lemma']}")
    return "\n".join(lines) + "\n"


def _scenario_lines(registry: dict, goal_id: str, goal: dict) -> list[str]:
    return []


def gen_coverage(registry: dict) -> str:
    lines = [
        "# Proof coverage — generated from registry.yaml by gen.py; do not edit.",
        "",
        "One row per key and per mutation: the recorded model references,",
        "or GAP where no reference is recorded. Gap rows feed the modeling queue on tracker note",
        "8277c76c-ad1.",
        "",
        "References locate evidence under a model's assumptions; this generator does not run proofs.",
        "A GAP is a missing reference, not a demonstrated vulnerability. Counts are inventory",
        "counts, not a percentage of system security proved. See [MODEL.md](../../storagekit/tamarin/MODEL.md).",
        "",
        "| entry | kind | proofs |",
        "|---|---|---|",
    ]
    gaps = 0
    for section, kind in (("keys", "key"), ("mutations", "mutation")):
        for entry_id, entry in sorted(registry[section].items()):
            proofs = entry.get("proofs") or []
            if proofs:
                cell = "<br>".join(f"{p['theory']}: {p['lemma']}" for p in proofs)
            else:
                cell = "**GAP**"
                gaps += 1
            lines.append(f"| {entry_id} | {kind} | {cell} |")
    total = len(registry["keys"]) + len(registry["mutations"])
    lines.append("")
    lines.append(f"{total - gaps} of {total} entries carry at least one model reference; {gaps} gaps.")
    return "\n".join(lines) + "\n"


def gen_json(registry: dict) -> str:
    return json.dumps(registry, sort_keys=True, indent=2) + "\n"


ARTIFACTS = {
    "key-graph.mmd": gen_mermaid,
    "key-register.md": gen_register,
    "workflows.md": gen_workflows,
    "proof-coverage.md": gen_coverage,
    "registry.json": gen_json,
    "workflow-goals.md": gen_workflow_goals,
}


def generate() -> dict[str, str]:
    registry = keyreg.load()
    return {name: fn(registry) for name, fn in ARTIFACTS.items()}


def main() -> int:
    GENERATED.mkdir(exist_ok=True)
    for name, content in generate().items():
        (GENERATED / name).write_text(content)
        print(f"wrote generated/{name} ({len(content)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
