"""Code-completeness checks for the key registry.

Each check returns a list of error strings (empty means the check passes).
tests/test_lints.py runs every check against the real registry — a failing
check fails the test suite and therefore the merge — and runs each check
against a deliberately broken copy to prove it can fail with a precise
message. Direct run: python3 lint.py (exit 0 iff all checks pass).

The checks:

1. fold_handlers_match: the fold handlers in tools/network/ledger/fold.py
   (every method named _h_*) and the registry's fold-sourced mutations are
   the same set, one entry per handler.
2. purpose_labels_match: every derivation-purpose string literal in the
   scanned trees (the autonomy/<name>/v<n> and autonomy.<name>.v<n>
   conventions) appears in the registry's purposes map, and vice versa.
3. code_anchors_resolve: every code anchor names an existing file, and its
   symbol (when given) appears in that file.
4. proof_refs_resolve: every proof reference names an existing Tamarin
   theory file containing the named lemma.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
FOLD_PATH = REPO_ROOT / "tools/network/ledger/fold.py"
TAMARIN_DIR = REPO_ROOT / "tools/network/storagekit/tamarin"
PURPOSE_SCAN_TREES = ("tools/network", "tools/vault")
PURPOSE_RE = re.compile(r'"(autonomy[./][A-Za-z0-9._/-]*[/.]v\d+)"')


def fold_handlers_match(registry: dict) -> list[str]:
    handlers = set(re.findall(r"^    def (_h_\w+)\(", FOLD_PATH.read_text(), re.M))
    claimed: dict[str, str] = {}
    errors: list[str] = []
    for mut_id, entry in registry["mutations"].items():
        source = entry["source"]
        if source["kind"] != "fold":
            continue
        symbol = source.get("symbol", "")
        if symbol in claimed:
            errors.append(
                f"fold handler {symbol} is claimed by two mutations: "
                f"{claimed[symbol]} and {mut_id}"
            )
        claimed[symbol] = mut_id
    for handler in sorted(handlers - set(claimed)):
        errors.append(
            f"fold handler {handler} in {FOLD_PATH.relative_to(REPO_ROOT)} has no "
            "mutation entry in registry.yaml"
        )
    for symbol in sorted(set(claimed) - handlers):
        errors.append(
            f"mutation {claimed[symbol]} claims fold handler {symbol}, which does "
            f"not exist in {FOLD_PATH.relative_to(REPO_ROOT)}"
        )
    return errors


def _scan_purpose_labels() -> set[str]:
    labels: set[str] = set()
    for tree in PURPOSE_SCAN_TREES:
        for path in (REPO_ROOT / tree).rglob("*.py"):
            if "test" in path.name or "tests" in path.parts:
                continue
            labels.update(PURPOSE_RE.findall(path.read_text(errors="ignore")))
    return labels


def purpose_labels_match(registry: dict) -> list[str]:
    in_code = _scan_purpose_labels()
    in_registry = set(registry["purposes"])
    errors = []
    for label in sorted(in_code - in_registry):
        errors.append(
            f"purpose label {label} appears in code but not in registry.yaml's "
            "purposes map"
        )
    for label in sorted(in_registry - in_code):
        errors.append(
            f"purposes map entry {label} appears in no scanned code file "
            f"(trees: {', '.join(PURPOSE_SCAN_TREES)})"
        )
    return errors


def _anchor_errors(owner: str, anchors: list[str]) -> list[str]:
    errors = []
    for anchor in anchors:
        path_part, _, symbol = anchor.partition(":")
        path = REPO_ROOT / path_part
        if not path.is_file():
            errors.append(f"{owner}: code anchor file does not exist: {path_part}")
            continue
        if symbol and not re.search(
            rf"\b{re.escape(symbol)}\b", path.read_text(errors="ignore")
        ):
            errors.append(
                f"{owner}: symbol '{symbol}' not found in {path_part}"
            )
    return errors


def code_anchors_resolve(registry: dict) -> list[str]:
    errors = []
    for key_id, entry in registry["keys"].items():
        errors.extend(_anchor_errors(f"keys.{key_id}", entry["code"]))
    for mut_id, entry in registry["mutations"].items():
        source = entry["source"]
        anchor = source["file"] + (":" + source["symbol"] if source.get("symbol") else "")
        errors.extend(_anchor_errors(f"mutations.{mut_id}", [anchor]))
    return errors


def proof_refs_resolve(registry: dict) -> list[str]:
    errors = []
    sections = list(registry["keys"].items()) + list(registry["mutations"].items())
    for owner_id, entry in sections:
        for proof in entry.get("proofs") or []:
            if proof["framework"] != "tamarin":
                continue
            theory_path = TAMARIN_DIR / f"{proof['theory']}.spthy"
            if not theory_path.is_file():
                errors.append(
                    f"{owner_id}: proof cites theory {proof['theory']}, but "
                    f"{theory_path.relative_to(REPO_ROOT)} does not exist"
                )
                continue
            if proof["lemma"] == "Observational_equivalence":
                # Diff-mode theories carry no named lemma: tamarin generates
                # the equivalence obligation. The harness's DIFF_EXPECTATIONS
                # table is the authority for which theories run in diff mode.
                harness = (TAMARIN_DIR / "run_tamarin.py").read_text()
                diff_block = re.search(
                    r"DIFF_EXPECTATIONS = \{(.*?)\n\}", harness, re.S
                )
                if diff_block is None or (
                    f'"{proof["theory"]}.spthy"' not in diff_block.group(1)
                ):
                    errors.append(
                        f"{owner_id}: proof cites Observational_equivalence for "
                        f"{proof['theory']}, which is not in run_tamarin.py's "
                        "DIFF_EXPECTATIONS"
                    )
                continue
            if f"lemma {proof['lemma']}" not in theory_path.read_text():
                errors.append(
                    f"{owner_id}: proof cites lemma {proof['lemma']}, not found in "
                    f"{theory_path.relative_to(REPO_ROOT)}"
                )
    return errors


ALL_CHECKS = (
    fold_handlers_match,
    purpose_labels_match,
    code_anchors_resolve,
    proof_refs_resolve,
)


def main() -> int:
    import keyreg

    registry = keyreg.load()
    failed = 0
    for check in ALL_CHECKS:
        errors = check(registry)
        status = "ok  " if not errors else "FAIL"
        print(f"[{status}] {check.__name__}")
        for err in errors:
            print(f"       {err}")
        failed += bool(errors)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    raise SystemExit(main())
