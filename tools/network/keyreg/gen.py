"""Generated views of the key registry.

Reads registry.yaml and writes four artifacts into generated/:

- key-graph.mmd     — Mermaid diagram: derivation edges solid, seal edges
                      dashed, nodes colored by custody class.
- key-register.md   — the per-key register as markdown, mirroring the crib
                      sheet's section 9 layout so the two can be diffed.
- proof-coverage.md — one row per key and per mutation: the Tamarin lemmas
                      covering it, or GAP.
- registry.json     — the whole registry as sorted, stable JSON for
                      dashboard and Key Ceremony Atlas consumption.

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

CUSTODY_STYLE = {
    "cold": "fill:#1e3a5f,color:#dbeafe,stroke:#3b82f6",
    "memory": "fill:#4a2545,color:#fce7f3,stroke:#ec4899",
    "disk": "fill:#3f3f1f,color:#fef9c3,stroke:#eab308",
    "public": "fill:#1f3f2f,color:#dcfce7,stroke:#22c55e",
}


def gen_mermaid(registry: dict) -> str:
    lines = [
        "%% Generated from registry.yaml by gen.py — do not edit by hand.",
        "%% Solid arrows: derivation (parent to child). Dashed arrows: this",
        "%% key's material is sealed to the recipient key. Node color is the",
        "%% custody class. Seal edges whose recipient is prose rather than a",
        "%% registered key id are omitted here and remain in the register.",
        "graph TD",
    ]
    for custody in CUSTODY_ORDER:
        lines.append(f"    classDef {custody} {CUSTODY_STYLE[custody]}")
    for key_id, entry in sorted(registry["keys"].items()):
        suffix = " (designed)" if entry.get("status") == "designed" else ""
        lines.append(
            f'    {key_id}["{key_id}{suffix}"]:::{entry["custody"]["class"]}'
        )
    for child, parent, fn in keyreg.derivation_edges(registry):
        lines.append(f"    {parent} -->|{fn}| {child}")
    for key_id, recipient, purpose in keyreg.seal_edges(registry):
        if recipient in registry["keys"]:
            label = purpose or "sealed"
            lines.append(f"    {key_id} -.->|{label}| {recipient}")
    return "\n".join(lines) + "\n"


def gen_register(registry: dict) -> str:
    lines = [
        "# Per-key register — generated from registry.yaml by gen.py; do not edit.",
        "",
        "The layout mirrors the crib sheet's section 9 register (graph note",
        "1e005d5c-c11) so the two can be compared side by side.",
        "",
    ]
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
            else:
                lines.append("- **minted** at random")
            lines.append(f"- **reaches** {d['reaches'].strip()}")
            lines.append(f"- **snapshot** {d['snapshot'].strip()}")
            lines.append(f"- **live?** {d['live'].strip()}")
            lines.append(f"- **revoke** {d['revoke'].strip()}")
            lines.append(f"- **bound** {d['bound'].strip()}")
            if entry["code"]:
                lines.append("- **code** " + " · ".join(f"`{a}`" for a in entry["code"]))
            lines.append("- **crib** " + ", ".join(entry["crib"]))
            lines.append("")
    return "\n".join(lines)


def gen_coverage(registry: dict) -> str:
    lines = [
        "# Proof coverage — generated from registry.yaml by gen.py; do not edit.",
        "",
        "One row per key and per mutation: the machine-checked lemmas covering",
        "it, or GAP. Gap rows feed the formal-modeling queue on tracker note",
        "8277c76c-ad1.",
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
    lines.append(f"{total - gaps} of {total} entries carry at least one proof; {gaps} gaps.")
    return "\n".join(lines) + "\n"


def gen_json(registry: dict) -> str:
    return json.dumps(registry, sort_keys=True, indent=2) + "\n"


ARTIFACTS = {
    "key-graph.mmd": gen_mermaid,
    "key-register.md": gen_register,
    "proof-coverage.md": gen_coverage,
    "registry.json": gen_json,
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
