"""A schema that ships must register when the package is imported.

A schema module registers its set as an import side effect, so a module the
package does not import registers only in whichever process happens to import
it directly. That process works; every other one behaves as though the set does
not exist. It is not a read failure with a message — the set is simply absent,
and every consequence is silent and elsewhere:

  - a write is refused, because writes to an unregistered schema are refused
  - validation passes vacuously, because there is nothing to validate against
  - a reference to it reports ``unknown_target``, indistinguishable from a typo
    naming a set nobody ever wrote

The last one is the reason this is a gate rather than a convention. The
reference checker's answer to "does this target exist" is a fact about the
process asking, so a set registered in one place and not another makes the
diagnostic tool disagree with the software it is diagnosing, and the honest
report — "not in this registry" — reads as a broken declaration.

Two modules were in exactly that state when this was written, both of them
sets a workspace's own rows point at. The test that covered one of them had
imported it directly, which made that test pass while the CLI could not see
the set at all.
"""
from __future__ import annotations

from pathlib import Path

import tools.graph.schemas as schemas

_NOT_A_SCHEMA = {"__init__", "registry"}


def _modules_on_disk() -> set[str]:
    directory = Path(schemas.__file__).parent
    return {path.stem for path in directory.glob("*.py")
            if path.stem not in _NOT_A_SCHEMA and not path.stem.startswith("_")}


def test_every_schema_module_is_imported_by_the_package():
    missing = sorted(name for name in _modules_on_disk()
                     if not hasattr(schemas, name))

    assert not missing, (
        "these schema modules exist but the package does not import them, so "
        "their sets register only in a process that imports them directly:\n  "
        + "\n  ".join(missing)
        + "\n\nAdd `from . import <module>` to tools/graph/schemas/__init__.py."
    )


def test_importing_the_package_is_enough_to_resolve_a_declared_target():
    """The property the reference checker depends on.

    Every set named by a ``references`` or ``key_references`` declaration must
    be registered by package import alone — otherwise the checker reports a
    correct declaration as an unknown target, and a reader goes looking for a
    typo that is not there.
    """
    unresolvable: list[str] = []
    for set_id in schemas.list_registered_set_ids():
        # Schemas invented by other tests register in the same process and
        # deliberately name targets that do not exist. The gate is about what
        # ships, so it walks only that.
        if set_id.startswith("probe."):
            continue
        for revision in range(1, 12):
            schema = schemas.get_schema(set_id, revision)
            if schema is None:
                continue
            targets = {
                spec["references"]
                for spec in (getattr(schema, "_field_metadata", None) or {}).values()
                if isinstance(spec, dict) and spec.get("references")
            }
            targets |= set((getattr(schema, "_key_references", None) or {}).values())
            for target in targets:
                if not any(schemas.get_schema(target, r) for r in range(1, 12)):
                    unresolvable.append(f"{set_id}#{revision} -> {target}")

    assert not unresolvable, (
        "declared references naming sets that are not registered:\n  "
        + "\n  ".join(sorted(set(unresolvable)))
    )
