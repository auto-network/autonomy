"""A field that holds a location on this machine says what should be there.

``check_setting`` asks every field declaring ``exists`` whether the thing is
present, and is the only way an installation can be told what a new machine
still owes rather than discovering it by failing to do something. It reaches
exactly as far as the declarations go -- so a path field that declares nothing
is invisible to the check written to find it.

That is not hypothetical. Of the six fields naming a host path across every
shipped schema, one declared ``exists`` and five did not, which is why the
inventory of "what does this box still need" could not be derived at all.

Two rules, and the second is what keeps this from reopening:

* every field known to hold a host path declares ``exists``;
* every NEW field whose NAME says it holds a path either declares ``exists``
  or is listed here as an exception, with the reason it is not a location on
  this machine.

Declaring ``exists`` is free of consequence at write time -- it is asked by a
readiness verb and never by a write (``test_a_readiness_check_never_runs_at_write``
in ``test_generic_check.py`` pins that), because whether a file is present is a
fact about the world rather than about the value.
"""
from __future__ import annotations

import pytest

from tools.graph import schemas
from tools.graph.schemas import registry


#: Fields that hold an absolute location on the machine running the check.
#: Every one must say what should be there. This list is the countable
#: outstanding set: it shrinks only when a field stops holding a host path.
HOST_PATH_FIELDS = {
    ("autonomy.artifact-path", "path"),
    ("autonomy.credential-file", "path"),
    ("autonomy.harness.bootstrap", "path"),
    ("autonomy.workspace.mount", "host_path"),
    ("autonomy.workspace", "repos[].base_source"),
    ("autonomy.workspace", "repos[].local_path"),
}

#: Fields whose name says "path" and which are NOT a location on this machine.
#: Each needs a reason, because the default for a path-named field is that it
#: is one.
NOT_A_HOST_PATH = {
    ("autonomy.workspace.mount", "container_path"):
        "inside the container, identical on every machine",
    ("autonomy.workspace", "repos[].mount"):
        "inside the workspace, identical on every machine",
    ("autonomy.network.serve-cert", "key_path"):
        "a portable basename, deliberately not a path",
    ("autonomy.capability.impl", "skill_path"):
        "repo-local, so it resolves against a checkout rather than a machine",
    ("autonomy.capability.impl", "primer_path"):
        "repo-local, so it resolves against a checkout rather than a machine",
    ("autonomy.capability.impl", "package_root"):
        "repo-local, so it resolves against a checkout rather than a machine",
}


def _walk_fields():
    """Every (set_id, dotted-field, spec) across every registered schema."""
    seen = set()
    for key, cls in registry.SCHEMAS.items():
        set_id = key.split("#", 1)[0]
        meta = getattr(cls, "_field_metadata", None) or {}

        def walk(d, prefix=""):
            for name, spec in (d or {}).items():
                if not isinstance(spec, dict):
                    continue
                element = spec.get("element")
                if isinstance(element, dict):
                    yield from walk(element, f"{prefix}{name}[].")
                addr = (set_id, prefix + name)
                if addr not in seen:
                    seen.add(addr)
                    yield addr, spec

        yield from walk(meta)


@pytest.mark.parametrize("set_id,field_name", sorted(HOST_PATH_FIELDS))
def test_a_host_path_says_what_should_be_there(set_id, field_name):
    """Undeclared, the field is invisible to the readiness check."""
    spec = dict(_walk_fields()).get((set_id, field_name))

    assert spec is not None, f"{set_id}.{field_name} no longer exists"
    assert spec.get("exists") in registry.VALID_EXISTS, (
        f"{set_id}.{field_name} holds a location on this machine and does not "
        f"say what should be there, so nothing can report it missing")


def test_a_new_path_named_field_is_a_deliberate_decision():
    """The ratchet.

    A field named for a path is a location on this machine until someone says
    otherwise. Naming one and declaring nothing is how the gap reopened
    silently the first time -- so it fails here instead, at the moment it is
    written, where the person who knows the answer is standing.
    """
    undeclared = []
    for (set_id, field_name), spec in _walk_fields():
        leaf = field_name.rsplit(".", 1)[-1].removeprefix("[].")
        if not (leaf == "path" or leaf.endswith("_path")):
            continue
        if (set_id, field_name) in NOT_A_HOST_PATH:
            continue
        if spec.get("exists"):
            continue
        undeclared.append(f"{set_id}.{field_name}")

    assert undeclared == [], (
        "a path-named field declares no readiness check and is not listed as "
        "something other than a host path: " + ", ".join(sorted(undeclared)))


def test_every_exception_is_still_a_real_field():
    """An exception outliving its field silently widens the rule."""
    live = dict(_walk_fields())
    stale = [f"{s}.{f}" for (s, f) in NOT_A_HOST_PATH if (s, f) not in live]

    assert stale == [], (
        "an exception names a field that no longer exists: " + ", ".join(stale))
