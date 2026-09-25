"""Loader, validator, and query interface for the key registry.

The registry (registry.yaml) is the machine-readable inventory of every key
and mutation in the vault + storage system. schema.json is the canonical
contract; this module enforces the same constraints natively so validation
works in environments without a jsonschema library, and uses jsonschema
additionally when it is importable. Design: graph note 879ccc1e-b49.

Command line:
    python3 keyreg.py validate            # exit 0 iff the registry is valid
    python3 keyreg.py key <id>            # one key's full entry as YAML
    python3 keyreg.py class <custody>     # ids of every key in a custody class
    python3 keyreg.py edges               # derivation + seal edges, one per line
    python3 keyreg.py reachable <id>      # keys transitively derivable from <id>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REGISTRY_PATH = HERE / "registry.yaml"
SCHEMA_PATH = HERE / "schema.json"

KEY_KINDS = {"seed", "signing", "kem", "symmetric", "derived-secret"}
CUSTODY_CLASSES = {"cold", "memory", "disk", "public"}
KEY_STATUSES = {"built", "designed"}
DESCRIPTOR_FIELDS = ("reaches", "snapshot", "live", "revoke", "bound")
MUTATION_SOURCE_KINDS = {"fold", "ceremony", "module-op", "route"}
PROOF_FRAMEWORKS = {"tamarin", "tla"}
OPENS = ("root", "persona", "delegate", "none")
#: Openings that need a human root window: the persona key is derived from the
#: personal root in the browser, so a persona signature is a root opening.
WINDOW_OPENS = {"root", "persona"}
#: The key ids a step holds by virtue of what it opens.
OPENS_KEYS = {
    "root": {"personal_root_seed", "persona_signing_key"},
    "persona": {"persona_signing_key"},
    "delegate": {"agent_delegate_signing_key"},
    "none": set(),
}


class RegistryError(Exception):
    """Raised by load() when validation fails; carries every error found."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


def _require_str(errors, where, obj, field, required=True):
    value = obj.get(field)
    if value is None:
        if required:
            errors.append(f"{where}: missing field '{field}'")
        return
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}.{field}: must be a non-empty string")


def _validate_key(errors, key_id, entry, all_key_ids):
    where = f"keys.{key_id}"
    if not isinstance(entry, dict):
        errors.append(f"{where}: must be a mapping")
        return
    kind = entry.get("kind")
    if kind not in KEY_KINDS:
        errors.append(f"{where}.kind: '{kind}' is not one of {sorted(KEY_KINDS)}")
    status = entry.get("status", "built")
    if status not in KEY_STATUSES:
        errors.append(f"{where}.status: '{status}' is not one of {sorted(KEY_STATUSES)}")

    custody = entry.get("custody")
    if not isinstance(custody, dict):
        errors.append(f"{where}: missing field 'custody'")
    else:
        if custody.get("class") not in CUSTODY_CLASSES:
            errors.append(
                f"{where}.custody.class: '{custody.get('class')}' is not one of "
                f"{sorted(CUSTODY_CLASSES)}"
            )
        _require_str(errors, f"{where}.custody", custody, "forced_by")

    if "derivation" not in entry:
        errors.append(f"{where}: missing field 'derivation' (null means minted at random)")
    else:
        derivation = entry["derivation"]
        if derivation is not None:
            if not isinstance(derivation, dict):
                errors.append(f"{where}.derivation: must be null or a mapping")
            else:
                _require_str(errors, f"{where}.derivation", derivation, "parent")
                _require_str(errors, f"{where}.derivation", derivation, "fn")
                parent = derivation.get("parent")
                if isinstance(parent, str) and parent not in all_key_ids:
                    errors.append(
                        f"{where}.derivation.parent: '{parent}' is not a key id "
                        "in this registry"
                    )

    descriptor = entry.get("descriptor")
    if not isinstance(descriptor, dict):
        errors.append(f"{where}: missing field 'descriptor'")
    else:
        for field in DESCRIPTOR_FIELDS:
            _require_str(errors, f"{where}.descriptor", descriptor, field)

    code = entry.get("code")
    if not isinstance(code, list):
        errors.append(f"{where}: missing field 'code' (a list; may be empty only when status is 'designed')")
    elif status == "built" and not code:
        errors.append(f"{where}.code: a built key needs at least one code anchor")

    crib = entry.get("crib")
    if not isinstance(crib, list) or not crib:
        errors.append(f"{where}: missing field 'crib' (at least one crib-sheet section)")

    for i, proof in enumerate(entry.get("proofs") or []):
        pwhere = f"{where}.proofs[{i}]"
        if not isinstance(proof, dict):
            errors.append(f"{pwhere}: must be a mapping")
            continue
        if proof.get("framework") not in PROOF_FRAMEWORKS:
            errors.append(f"{pwhere}.framework: must be one of {sorted(PROOF_FRAMEWORKS)}")
        _require_str(errors, pwhere, proof, "theory")
        _require_str(errors, pwhere, proof, "lemma")


def _validate_mutation(errors, mut_id, entry):
    where = f"mutations.{mut_id}"
    if not isinstance(entry, dict):
        errors.append(f"{where}: must be a mapping")
        return
    if entry.get("status", "built") not in KEY_STATUSES:
        errors.append(f"{where}.status: must be one of {sorted(KEY_STATUSES)}")
    source = entry.get("source")
    if not isinstance(source, dict):
        errors.append(f"{where}: missing field 'source'")
    else:
        if source.get("kind") not in MUTATION_SOURCE_KINDS:
            errors.append(
                f"{where}.source.kind: '{source.get('kind')}' is not one of "
                f"{sorted(MUTATION_SOURCE_KINDS)}"
            )
        _require_str(errors, f"{where}.source", source, "file")
    authority = entry.get("authority")
    if not isinstance(authority, list) or not authority:
        errors.append(f"{where}: missing field 'authority' (at least one entry)")
    if "effects" not in entry:
        errors.append(f"{where}: missing field 'effects'")
    crib = entry.get("crib")
    if not isinstance(crib, list) or not crib:
        errors.append(f"{where}: missing field 'crib' (at least one crib-sheet section)")


def _ref_errors(errors, where, ref, data, *, actor_bound: bool):
    """A ref is a key id, or an artifact id with an optional @actor suffix.
    actor_bound: a bare per-actor artifact is allowed (it binds to the
    executing actor); elsewhere a per-actor artifact must name its actor."""
    if not isinstance(ref, str) or not ref:
        errors.append(f"{where}: a reference must be a non-empty string")
        return
    base, _, actor = ref.partition("@")
    artifacts = data.get("artifacts") or {}
    if base in data["keys"] and not actor:
        return
    if base not in artifacts:
        errors.append(f"{where}: '{ref}' is neither an artifact nor a key id")
        return
    per_actor = bool(artifacts[base].get("per_actor"))
    if actor:
        if not per_actor:
            errors.append(f"{where}: '{ref}' names an actor but {base} is not per_actor")
        elif actor not in (data.get("actors") or {}):
            errors.append(f"{where}: '{ref}' names unknown actor '{actor}'")
    elif per_actor and not actor_bound:
        errors.append(f"{where}: per-actor artifact '{base}' must name its actor (<id>@<actor>)")


def _expr_refs(expr):
    """Every ref inside a requirement expression."""
    if isinstance(expr, str):
        yield expr
    elif isinstance(expr, dict):
        for sub in (expr.get("all") or expr.get("any") or []):
            yield from _expr_refs(sub)


def _expr_shape_errors(errors, where, expr):
    if isinstance(expr, str):
        return
    if not isinstance(expr, dict) or len(expr) != 1 or next(iter(expr)) not in ("all", "any"):
        errors.append(f"{where}: a requirement is a ref, {{all: [...]}} or {{any: [...]}}")
        return
    subs = next(iter(expr.values()))
    if not isinstance(subs, list) or not subs:
        errors.append(f"{where}: all/any needs a non-empty list")
        return
    for i, sub in enumerate(subs):
        _expr_shape_errors(errors, f"{where}[{i}]", sub)


def _validate_workflow(errors, data):
    actors = data.get("actors") or {}
    artifacts = data.get("artifacts") or {}
    for actor_id, entry in actors.items():
        if not isinstance(entry, dict) or not str(entry.get("description", "")).strip():
            errors.append(f"actors.{actor_id}: missing field 'description'")
    for art_id, entry in artifacts.items():
        where = f"artifacts.{art_id}"
        if not isinstance(entry, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        for field_name in ("description", "store", "home"):
            _require_str(errors, where, entry, field_name)
        code = entry.get("code")
        if not isinstance(code, list) or not code:
            errors.append(f"{where}: missing field 'code' (at least one code anchor)")
        if entry.get("status", "built") not in KEY_STATUSES:
            errors.append(f"{where}.status: must be one of {sorted(KEY_STATUSES)}")
    for mut_id, entry in data["mutations"].items():
        if not isinstance(entry, dict):
            continue
        where = f"mutations.{mut_id}"
        fields = [f for f in ("actors", "opens", "requires", "produces") if f in entry]
        if not fields:
            if "rule" in entry:
                errors.append(f"{where}.rule: only a workflow mutation (with opens) carries a rule")
            continue
        for missing in sorted({"actors", "opens", "requires", "produces"} - set(fields)):
            errors.append(f"{where}: workflow field '{missing}' is required once any of actors/opens/requires/produces is present")
        opens = entry.get("opens")
        if opens not in OPENS:
            errors.append(f"{where}.opens: '{opens}' is not one of {list(OPENS)}")
        for actor in entry.get("actors") or []:
            if actor not in actors:
                errors.append(f"{where}.actors: unknown actor '{actor}'")
        requires = entry.get("requires")
        if not isinstance(requires, list):
            errors.append(f"{where}.requires: must be a list")
            requires = []
        for i, expr in enumerate(requires):
            _expr_shape_errors(errors, f"{where}.requires[{i}]", expr)
            for ref in _expr_refs(expr):
                _ref_errors(errors, f"{where}.requires", ref, data, actor_bound=True)
                if ref in data["keys"] and ref not in OPENS_KEYS.get(opens, set()):
                    errors.append(
                        f"{where}.requires: key '{ref}' is not held by a step that opens '{opens}'"
                    )
        produces = entry.get("produces")
        if not isinstance(produces, list) or not produces:
            errors.append(f"{where}.produces: at least one artifact")
            produces = []
        for ref in produces:
            if isinstance(ref, str) and ref.partition("@")[0] in data["keys"]:
                errors.append(f"{where}.produces: '{ref}' is a key; produces names artifacts")
            else:
                _ref_errors(errors, f"{where}.produces", ref, data, actor_bound=True)
        status = entry.get("status", "built")
        if status == "designed" and not entry.get("rule"):
            errors.append(f"{where}: a designed workflow mutation needs a 'rule' the planner can enable")
        if status == "built" and entry.get("rule"):
            errors.append(f"{where}.rule: a built mutation is always enabled; rule is for designed ones")
    for goal_id, goal in (data.get("goals") or {}).items():
        where = f"goals.{goal_id}"
        if not isinstance(goal, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        _require_str(errors, where, goal, "description")
        requires = goal.get("requires")
        if not isinstance(requires, list) or not requires:
            errors.append(f"{where}: missing field 'requires'")
            requires = []
        for i, expr in enumerate(requires):
            _expr_shape_errors(errors, f"{where}.requires[{i}]", expr)
            for ref in _expr_refs(expr):
                _ref_errors(errors, f"{where}.requires", ref, data, actor_bound=False)
        states = goal.get("states")
        if not isinstance(states, dict) or not states:
            errors.append(f"{where}: missing field 'states' (at least one starting state)")
            states = {}
        for state_id, state in states.items():
            for ref in (state or {}).get("holds") or []:
                _ref_errors(errors, f"{where}.states.{state_id}.holds", ref, data, actor_bound=False)
        rules = {m.get("rule") for m in data["mutations"].values() if isinstance(m, dict) and m.get("rule")}
        for i, step in enumerate(goal.get("current_order") or []):
            swhere = f"{where}.current_order[{i}]"
            if step.get("actor") not in actors:
                errors.append(f"{swhere}.actor: unknown actor '{step.get('actor')}'")
            for state_id in step.get("only_from") or []:
                if state_id not in states:
                    errors.append(f"{swhere}.only_from: unknown state '{state_id}'")
            for mut_id in step.get("runs") or []:
                mut = data["mutations"].get(mut_id)
                if mut is None:
                    errors.append(f"{swhere}.runs: unknown mutation '{mut_id}'")
                    continue
                if "opens" not in mut:
                    errors.append(f"{swhere}.runs: '{mut_id}' carries no workflow fields")
                elif step.get("actor") not in (mut.get("actors") or []):
                    errors.append(f"{swhere}.runs: actor '{step.get('actor')}' does not execute '{mut_id}'")
                if mut.get("status", "built") != "built":
                    errors.append(f"{swhere}.runs: '{mut_id}' is designed; the current order runs built code only")
                if mut.get("opens") in WINDOW_OPENS and not step.get("ceremony"):
                    errors.append(f"{swhere}.runs: '{mut_id}' opens {mut.get('opens')}, so step {step.get('step')} must be a ceremony")
        for i, scenario in enumerate(goal.get("scenarios") or []):
            if scenario.get("from") not in states:
                errors.append(f"{where}.scenarios[{i}].from: unknown state '{scenario.get('from')}'")
            for rule in scenario.get("rules") or []:
                if rule not in rules:
                    errors.append(f"{where}.scenarios[{i}].rules: no designed mutation carries rule '{rule}'")


def validate(data) -> list[str]:
    """Return every validation error, as '<location>: <problem>' strings."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["registry: top level must be a mapping"]
    if data.get("version") != 1:
        errors.append("registry.version: must be 1")
    for section in ("keys", "mutations", "purposes"):
        if not isinstance(data.get(section), dict):
            errors.append(f"registry: missing top-level map '{section}'")
    if errors:
        return errors

    all_key_ids = set(data["keys"])
    for key_id, entry in data["keys"].items():
        _validate_key(errors, key_id, entry, all_key_ids)
    for mut_id, entry in data["mutations"].items():
        _validate_mutation(errors, mut_id, entry)
    for label, entry in data["purposes"].items():
        if not isinstance(entry, dict) or not str(entry.get("description", "")).strip():
            errors.append(f"purposes.{label}: missing field 'description'")
    for section in ("actors", "artifacts", "goals"):
        if section in data and not isinstance(data[section], dict):
            errors.append(f"registry: '{section}' must be a mapping")
    if not errors:
        _validate_workflow(errors, data)

    try:
        import jsonschema
    except ImportError:
        pass
    else:
        schema = json.loads(SCHEMA_PATH.read_text())
        checker = jsonschema.Draft202012Validator(schema)
        for err in checker.iter_errors(data):
            path = ".".join(str(p) for p in err.absolute_path) or "registry"
            errors.append(f"{path}: {err.message}")

    return sorted(set(errors))


def load(path: Path = REGISTRY_PATH) -> dict:
    """Load and validate the registry; raise RegistryError on any problem."""
    data = yaml.safe_load(path.read_text())
    errors = validate(data)
    if errors:
        raise RegistryError(errors)
    return data


def key(data: dict, key_id: str) -> dict:
    try:
        return data["keys"][key_id]
    except KeyError:
        raise KeyError(
            f"'{key_id}' is not a registered key; ids are: {', '.join(sorted(data['keys']))}"
        ) from None


def keys_by_custody(data: dict, custody_class: str) -> list[str]:
    return sorted(
        kid for kid, entry in data["keys"].items()
        if entry["custody"]["class"] == custody_class
    )


def derivation_edges(data: dict) -> list[tuple[str, str, str]]:
    """(child, parent, fn) for every derived key."""
    edges = []
    for kid, entry in sorted(data["keys"].items()):
        derivation = entry.get("derivation")
        if derivation:
            edges.append((kid, derivation["parent"], derivation["fn"]))
    return edges


def seal_edges(data: dict) -> list[tuple[str, str, str]]:
    """(key, recipient, purpose) for every seals_to entry."""
    edges = []
    for kid, entry in sorted(data["keys"].items()):
        for seal in entry.get("seals_to") or []:
            edges.append((kid, seal["recipient"], seal.get("purpose", "")))
    return edges


def reachable(data: dict, key_id: str) -> list[str]:
    """Keys transitively derivable from key_id (derivation edges only)."""
    key(data, key_id)  # raise on unknown id
    children: dict[str, list[str]] = {}
    for child, parent, _fn in derivation_edges(data):
        children.setdefault(parent, []).append(child)
    seen: set[str] = set()
    frontier = [key_id]
    while frontier:
        current = frontier.pop()
        for child in children.get(current, []):
            if child not in seen:
                seen.add(child)
                frontier.append(child)
    return sorted(seen)


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in {"validate", "key", "class", "edges", "reachable"}:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        data = load()
    except RegistryError as exc:
        print(f"registry.yaml: {len(exc.errors)} validation error(s)", file=sys.stderr)
        for err in exc.errors:
            print(f"  {err}", file=sys.stderr)
        return 1
    command = argv[0]
    if command == "validate":
        print(
            f"registry.yaml valid: {len(data['keys'])} keys, "
            f"{len(data['mutations'])} mutations, {len(data['purposes'])} purposes"
        )
    elif command == "key":
        print(yaml.safe_dump({argv[1]: key(data, argv[1])}, sort_keys=False))
    elif command == "class":
        print("\n".join(keys_by_custody(data, argv[1])))
    elif command == "edges":
        for child, parent, fn in derivation_edges(data):
            print(f"derive  {parent} -> {child}  [{fn}]")
        for kid, recipient, purpose in seal_edges(data):
            print(f"seal    {kid} -> {recipient}  [{purpose}]")
    elif command == "reachable":
        print("\n".join(reachable(data, argv[1])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
