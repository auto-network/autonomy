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
    python3 keyreg.py plan <goal> [--from <state>] [--rule <rule>]...
                                          # minimal schedule: fewest root openings, then steps
    python3 keyreg.py explain-current <goal> [--from <state>] [--rule <rule>]...
                                          # the recorded code order against that minimum
"""

from __future__ import annotations

import heapq
import json
import sys
from dataclasses import dataclass, field
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
ARTIFACT_ORIGINS = ("workflow", "given", "defect", "open")
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


def artifact_copies(data: dict) -> list[str]:
    """Every artifact copy: <id> for a shared artifact, <id>@<actor> for each
    actor's copy of a per-actor one."""
    copies = []
    for art_id, entry in sorted((data.get("artifacts") or {}).items()):
        if entry.get("per_actor"):
            copies.extend(f"{art_id}@{actor}" for actor in data.get("actors") or {})
        else:
            copies.append(art_id)
    return copies


def given_copies(data: dict) -> set[str]:
    """The copies an origin: given artifact declares established before the workflow."""
    given = set()
    for art_id, entry in (data.get("artifacts") or {}).items():
        if entry.get("origin") != "given":
            continue
        for ref in entry.get("given_by") or []:
            _mut, _, actor = ref.partition("@")
            if not entry.get("per_actor"):
                given.add(art_id)
            elif actor:
                given.add(f"{art_id}@{actor}")
            else:
                given.update(f"{art_id}@{a}" for a in data.get("actors") or {})
    return given


def workflow_producers(data: dict) -> dict[str, list[str]]:
    """artifact copy -> the workflow mutations (built or designed) producing it."""
    producers: dict[str, list[str]] = {}
    artifacts = data.get("artifacts") or {}
    for mut_id, entry in sorted(data["mutations"].items()):
        if not isinstance(entry, dict) or "opens" not in entry:
            continue
        for actor in entry.get("actors") or []:
            for ref in entry.get("produces") or []:
                base, _, named = ref.partition("@")
                copy = ref if named or not artifacts.get(base, {}).get("per_actor") else f"{base}@{actor}"
                producers.setdefault(copy, [])
                if mut_id not in producers[copy]:
                    producers[copy].append(mut_id)
    return producers


def _validate_origins(errors, data):
    """Every copy is given, produced by a workflow mutation, or a named
    defect or open question; starting states hold given copies only."""
    artifacts = data.get("artifacts") or {}
    given = given_copies(data)
    producers = workflow_producers(data)
    for copy in artifact_copies(data):
        base = copy.partition("@")[0]
        origin = artifacts[base].get("origin", "workflow")
        where = f"artifacts.{base}"
        if origin in ("defect", "open"):
            if producers.get(copy):
                errors.append(f"{where}: origin {origin}, but {copy} is produced by "
                              f"{', '.join(producers[copy])}; drop the origin")
            continue
        if copy not in given and not producers.get(copy):
            errors.append(f"{where}: {copy} is neither given nor produced by any workflow mutation")
    for goal_id, goal in (data.get("goals") or {}).items():
        for state_id, state in ((goal or {}).get("states") or {}).items():
            for ref in (state or {}).get("holds") or []:
                if ref.partition("@")[0] in artifacts and ref not in given:
                    errors.append(f"goals.{goal_id}.states.{state_id}.holds: '{ref}' is not "
                                  "a given copy (origin given, given_by covering it)")


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
        origin = entry.get("origin", "workflow")
        if origin not in ARTIFACT_ORIGINS:
            errors.append(f"{where}.origin: '{origin}' is not one of {list(ARTIFACT_ORIGINS)}")
        if (origin == "given") != bool(entry.get("given_by")):
            errors.append(f"{where}: origin given and given_by go together")
        if (origin == "defect") != bool(str(entry.get("defect", "")).strip()):
            errors.append(f"{where}: origin defect needs a 'defect' reason, and only it carries one")
        for ref in entry.get("given_by") or []:
            mut_id, _, actor = ref.partition("@")
            if mut_id not in data["mutations"]:
                errors.append(f"{where}.given_by: '{mut_id}' is not a mutation id")
            if actor and (actor not in actors or not entry.get("per_actor")):
                errors.append(f"{where}.given_by: '{ref}' names an actor, but {art_id} is not per_actor or the actor is unknown")
    _validate_origins(errors, data)
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
                if step.get("continues_window") is not None:
                    prior = [s for s in (goal.get("current_order") or [])[:i] if s.get("step") == step["continues_window"]]
                    if not step.get("ceremony"):
                        errors.append(f"{swhere}.continues_window: only a ceremony step continues an opening")
                    elif not prior or prior[-1].get("actor") != step.get("actor"):
                        errors.append(f"{swhere}.continues_window: must name an earlier ceremony step of the same actor")
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


# ── Workflow planning ───────────────────────────────────────────────────────
#
# The workflow mutations (those with opens/requires/produces) form an AND/OR
# graph over artifacts. A per-actor artifact referenced bare inside a mutation
# binds to the executing actor. A step whose opening is root or persona needs
# a human root window; consecutive window steps of one actor share a window,
# and a window step of another actor closes it (a window never spans another
# party's ceremony). Steps opening delegate or none run on a machine and
# neither open nor close a window. The graph is monotone: an artifact once
# held stays held. Freshness conditions (a head present at adoption) are not
# expressible here and are modeled in tools/network/TLA/OrgAdmission.tla.


class PlanError(Exception):
    pass


@dataclass(frozen=True)
class Instance:
    """One workflow mutation executed by one actor, refs bound."""
    mutation: str
    actor: str
    opens: str
    requires: tuple
    produces: frozenset

    @property
    def needs_window(self) -> bool:
        return self.opens in WINDOW_OPENS

    def label(self) -> str:
        return f"{self.mutation}[{self.actor}]"


def _bind(data: dict, ref: str, actor: str) -> str:
    base, _, named = ref.partition("@")
    if base in data["keys"] or named:
        return ref
    if (data.get("artifacts") or {}).get(base, {}).get("per_actor"):
        return f"{base}@{actor}"
    return ref


def _bind_expr(data: dict, expr, actor: str):
    if isinstance(expr, str):
        return _bind(data, expr, actor)
    op, subs = next(iter(expr.items()))
    return (op, tuple(_bind_expr(data, sub, actor) for sub in subs))


def _norm_expr(expr):
    """Goal/state requirement (already explicit) in the bound tuple form."""
    if isinstance(expr, str):
        return expr
    op, subs = next(iter(expr.items()))
    return (op, tuple(_norm_expr(sub) for sub in subs))


def _held(ref: str, held: frozenset, opens: str) -> bool:
    return ref in held or ref in OPENS_KEYS.get(opens, set())


def satisfied(expr, held: frozenset, opens: str = "none") -> bool:
    if isinstance(expr, str):
        return _held(expr, held, opens)
    op, subs = expr
    test = all if op == "all" else any
    return test(satisfied(sub, held, opens) for sub in subs)


def _all_satisfied(exprs, held, opens="none") -> bool:
    return all(satisfied(expr, held, opens) for expr in exprs)


def unmet(expr, held: frozenset, opens: str = "none") -> list[str]:
    """Human-readable unmet requirements of *expr* (empty when satisfied)."""
    if satisfied(expr, held, opens):
        return []
    if isinstance(expr, str):
        return [expr]
    op, subs = expr
    if op == "all":
        return [ref for sub in subs for ref in unmet(sub, held, opens)]
    return ["any of (" + " | ".join(
        " & ".join(unmet(sub, frozenset(), opens) or ["-"]) for sub in subs) + ")"]


def _unmet_all(exprs, held, opens="none") -> list[str]:
    return [ref for expr in exprs for ref in unmet(expr, held, opens)]


def rule_names(data: dict) -> set[str]:
    return {m["rule"] for m in data["mutations"].values() if m.get("rule")}


def instances(data: dict, rules=()) -> list[Instance]:
    """Every executable (mutation, actor) pair: built workflow mutations, and
    designed ones whose rule is enabled."""
    unknown = set(rules) - rule_names(data)
    if unknown:
        raise PlanError(f"unknown rule(s) {sorted(unknown)}; rules are {sorted(rule_names(data))}")
    out = []
    for mut_id, entry in sorted(data["mutations"].items()):
        if "opens" not in entry:
            continue
        if entry.get("status", "built") != "built" and entry.get("rule") not in rules:
            continue
        for actor in entry["actors"]:
            out.append(Instance(
                mutation=mut_id, actor=actor, opens=entry["opens"],
                requires=tuple(_bind_expr(data, e, actor) for e in entry["requires"]),
                produces=frozenset(_bind(data, r, actor) for r in entry["produces"]),
            ))
    return out


def _goal(data: dict, goal_id: str) -> dict:
    goals = data.get("goals") or {}
    if goal_id not in goals:
        raise PlanError(f"'{goal_id}' is not a goal; goals are: {', '.join(sorted(goals))}")
    return goals[goal_id]


def _start(data: dict, goal_id: str, state_id: str | None) -> tuple[str, frozenset]:
    states = _goal(data, goal_id)["states"]
    state_id = state_id or next(iter(states))
    if state_id not in states:
        raise PlanError(f"'{state_id}' is not a state of {goal_id}; states are: {', '.join(states)}")
    return state_id, frozenset(states[state_id]["holds"])


@dataclass
class Plan:
    goal: str
    state: str
    rules: tuple
    steps: list = field(default_factory=list)      # (Instance, window label | None, new artifacts)
    openings: dict = field(default_factory=dict)   # actor -> root windows

    @property
    def total_openings(self) -> int:
        return sum(self.openings.values())


def plan(data: dict, goal_id: str, state_id: str | None = None, rules=()) -> Plan:
    """Exhaustive search (Dijkstra) for the schedule reaching *goal_id* with
    the fewest root windows, then the fewest steps. Ties break on instance
    order, so the result is deterministic."""
    goal = _goal(data, goal_id)
    state_id, start = _start(data, goal_id, state_id)
    target = tuple(_norm_expr(e) for e in goal["requires"])
    insts = instances(data, rules)
    counter = 0
    frontier = [(0, 0, counter, start, None, ())]
    best: dict = {}
    while frontier:
        opens, nsteps, _, held, window, path = heapq.heappop(frontier)
        if _all_satisfied(target, held):
            result = Plan(goal=goal_id, state=state_id, rules=tuple(rules),
                          openings={a: 0 for a in data["actors"]})
            current, seen, have = None, {}, start
            for index in path:
                inst = insts[index]
                label = None
                if inst.needs_window:
                    if current != inst.actor:
                        seen[inst.actor] = seen.get(inst.actor, 0) + 1
                        result.openings[inst.actor] += 1
                        current = inst.actor
                    label = f"{inst.actor}#{seen[inst.actor]}"
                result.steps.append((inst, label, sorted(inst.produces - have)))
                have = have | inst.produces
            return result
        key = (held, window)
        if key in best and best[key] <= (opens, nsteps):
            continue
        best[key] = (opens, nsteps)
        for index, inst in enumerate(insts):
            if inst.produces <= held or not _all_satisfied(inst.requires, held, inst.opens):
                continue
            if inst.needs_window:
                cost, next_window = (0 if window == inst.actor else 1), inst.actor
            else:
                cost, next_window = 0, window
            counter += 1
            heapq.heappush(frontier, (opens + cost, nsteps + 1, counter,
                                      held | inst.produces, next_window, path + (index,)))
    raise PlanError(f"goal {goal_id} is unreachable from state {state_id} with rules {list(rules)}")


# ── The recorded current order ──────────────────────────────────────────────

@dataclass
class StepRun:
    step: str
    actor: str
    ceremony: bool
    held_before: frozenset
    held_after: frozenset
    runs: list = field(default_factory=list)   # (mutation, new artifacts, unmet refs)

    def new(self) -> list:
        return [(m, new) for m, new, miss in self.runs if new]


@dataclass
class CurrentRun:
    goal: str
    state: str
    steps: list
    reached: bool
    ceremonies: dict


def simulate_current(data: dict, goal_id: str, state_id: str | None = None) -> CurrentRun:
    """Replay the goal's recorded current_order from a state. A run whose
    requirements are unmet produces nothing (the code path refuses)."""
    goal = _goal(data, goal_id)
    state_id, held = _start(data, goal_id, state_id)
    steps, ceremonies = [], {a: 0 for a in data["actors"]}
    for step in goal.get("current_order") or []:
        if step.get("only_from") and state_id not in step["only_from"]:
            continue
        # continues_window: this ceremony runs inside the actor's previous
        # opening (the same root prompt), so it is not another opening, for
        # the count and for explain-current alike.
        continues = bool(step.get("continues_window"))
        record = StepRun(step["step"], step["actor"], bool(step["ceremony"]) and not continues, held, held)
        for mut_id in step["runs"]:
            entry = data["mutations"][mut_id]
            inst = Instance(mut_id, step["actor"], entry["opens"],
                            tuple(_bind_expr(data, e, step["actor"]) for e in entry["requires"]),
                            frozenset(_bind(data, r, step["actor"]) for r in entry["produces"]))
            missing = _unmet_all(inst.requires, held, inst.opens)
            new = [] if missing else sorted(inst.produces - held)
            if not missing:
                held = held | inst.produces
            record.runs.append((mut_id, new, missing))
        record.held_after = held
        if record.ceremony:
            ceremonies[record.actor] += 1
        steps.append(record)
    target = tuple(_norm_expr(e) for e in goal["requires"])
    return CurrentRun(goal_id, state_id, steps, _all_satisfied(target, held), ceremonies)


def _closure(held: frozenset, insts, allow) -> tuple[frozenset, dict]:
    """Fixpoint of *held* under the instances *allow* admits; returns the
    closure and, per new artifact, the instance that first produced it."""
    producer: dict = {}
    changed = True
    while changed:
        changed = False
        for inst in insts:
            if not allow(inst) or inst.produces <= held:
                continue
            if _all_satisfied(inst.requires, held, inst.opens):
                for ref in inst.produces - held:
                    producer[ref] = inst
                held = held | inst.produces
                changed = True
    return held, producer


@dataclass
class Opening:
    step: str
    actor: str
    extra: bool
    reason: str
    missing: list          # (artifact, producer label or None)


def explain_current(data: dict, goal_id: str, state_id: str | None = None, rules=()):
    """Every root opening of the current order, classified against the
    minimal schedule under *rules*:

    - mergeable: every requirement of what the opening newly produced was
      obtainable in the actor's previous window (its held set closed under
      machine steps and the actor's own window steps); the artifacts listed
      are the ones that window closed without.
    - machine: what the opening newly produced is obtainable by machine
      steps from what was held just before it (plus what the actor could have
      produced in its previous window); the artifacts listed were absent when
      that previous window closed.
    - needed: neither holds; the artifacts listed come only from another
      party's ceremony.
    """
    current = simulate_current(data, goal_id, state_id)
    minimal = plan(data, goal_id, current.state, rules)
    insts = instances(data, rules)
    machine = lambda inst: not inst.needs_window  # noqa: E731
    openings = []
    previous: dict = {}
    for record in current.steps:
        if not record.ceremony:
            continue
        prior = previous.get(record.actor)
        previous[record.actor] = record
        new_runs = record.new()
        if not new_runs:
            openings.append(Opening(record.step, record.actor, True,
                                    "produced nothing new", []))
            continue
        own = lambda inst, a=record.actor: machine(inst) or inst.actor == a  # noqa: E731
        by_id = {(i.mutation, i.actor): i for i in insts}
        run_insts = [by_id[(m, record.actor)] for m, _new in new_runs]
        if prior is not None:
            p_closure, p_producer = _closure(prior.held_after, insts, own)
            if all(_all_satisfied(i.requires, p_closure, i.opens) for i in run_insts):
                needed = sorted({ref for i in run_insts
                                 for ref in _unmet_all(i.requires, prior.held_after, i.opens)})
                openings.append(Opening(
                    record.step, record.actor, True,
                    f"mergeable into {prior.step}: {prior.step} closed without",
                    [(ref, p_producer[ref].label() if ref in p_producer else None) for ref in needed]))
                continue
            actor_products = frozenset(
                ref for ref, inst in p_producer.items()
                if inst.actor == record.actor and inst.needs_window)
        else:
            actor_products = frozenset()
        wanted = frozenset(ref for _m, new in new_runs for ref in new)
        m_closure, m_producer = _closure(record.held_before | actor_products, insts, machine)
        if wanted <= m_closure:
            chain = {m_producer[ref] for ref in wanted if ref in m_producer}
            base = prior.held_after if prior is not None else frozenset()
            absent = sorted({ref for inst in chain
                             for ref in _unmet_all(inst.requires, base, inst.opens)})
            openings.append(Opening(
                record.step, record.actor, True,
                "needs no window: machine steps "
                + ", ".join(sorted(i.label() for i in chain))
                + (f" produce it; absent when {prior.step} closed" if prior else " produce it"),
                [(ref, (p_producer[ref].label() if prior and ref in p_producer else None))
                 for ref in absent]))
            continue
        if prior is None:
            openings.append(Opening(record.step, record.actor, False,
                                    f"needed: first opening of {record.actor}", []))
            continue
        missing = sorted({ref for i in run_insts
                          for ref in _unmet_all(i.requires, prior.held_after, i.opens)})
        openings.append(Opening(record.step, record.actor, False,
                                f"needed: not obtainable in {prior.step}'s window; it lacked",
                                [(ref, None) for ref in missing]))
    return current, minimal, openings


def _parse_plan_args(args: list[str]) -> tuple[str, str | None, list[str]]:
    if not args:
        raise PlanError("usage: plan|explain-current <goal> [--from <state>] [--rule <rule>]...")
    goal_id, state_id, rules = args[0], None, []
    rest = args[1:]
    while rest:
        flag = rest.pop(0)
        if flag in ("--from", "--rule") and rest:
            value = rest.pop(0)
            if flag == "--from":
                state_id = value
            else:
                rules.append(value)
        else:
            raise PlanError(f"unexpected argument '{flag}'")
    return goal_id, state_id, rules


def format_plan(result: Plan) -> str:
    lines = [f"goal {result.goal} from {result.state}"
             + (f" with rules {', '.join(result.rules)}" if result.rules else " (built rules)"),
             "root openings: " + ", ".join(f"{a} {n}" for a, n in result.openings.items())
             + f" (total {result.total_openings}); steps {len(result.steps)}", ""]
    for n, (inst, window, new) in enumerate(result.steps, 1):
        where = f"window {window}" if window else f"machine ({inst.opens})"
        lines.append(f"{n:2}. {inst.label():48} {where:20} -> {', '.join(new) or '-'}")
    return "\n".join(lines)


def format_explain(current: CurrentRun, minimal: Plan, openings: list) -> str:
    lines = [f"goal {current.goal} from {current.state}: current order "
             + ("reaches" if current.reached else "DOES NOT reach") + " the goal",
             "current root openings: " + ", ".join(f"{a} {n}" for a, n in current.ceremonies.items()),
             "minimal root openings: " + ", ".join(f"{a} {n}" for a, n in minimal.openings.items())
             + (f" (rules {', '.join(minimal.rules)})" if minimal.rules else " (built rules)"), ""]
    for record in current.steps:
        for mut_id, new, missing in record.runs:
            if missing:
                lines.append(f"refused: {record.step} {mut_id} lacks {', '.join(missing)}")
    lines.append("")
    for opening in openings:
        tag = "EXTRA " if opening.extra else "needed"
        detail = ", ".join(ref + (f" (from {src})" if src else "") for ref, src in opening.missing)
        lines.append(f"{tag} {opening.step:10} {opening.actor:8} {opening.reason}"
                     + (f": {detail}" if detail else ""))
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in {"validate", "key", "class", "edges", "reachable",
                                   "plan", "explain-current"}:
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
    elif command in ("plan", "explain-current"):
        try:
            goal_id, state_id, rules = _parse_plan_args(argv[1:])
            if command == "plan":
                print(format_plan(plan(data, goal_id, state_id, rules)))
            else:
                print(format_explain(*explain_current(data, goal_id, state_id, rules)))
        except PlanError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
