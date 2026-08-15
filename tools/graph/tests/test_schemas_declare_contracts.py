"""Every production Setting schema declares its payload's fields.

Declared fields are the contract. They are what ``validate_payload``
enforces, what ``graph set schema`` prints, what the exported JSON schema
and the generated TypeScript are built from. A schema declaring none
publishes nothing any of those can use, and enforcement has nothing to
enforce — the payload's shape then lives only in whatever its ``validate``
method happens to check, where no consumer can read it.

This is a check over the LIVE REGISTRY rather than a guard in
``__init_subclass__``. Tests legitimately define minimal throwaway schemas
with no fields, and ``test_no_annotations_is_fine`` asserts that stays
allowed; enforcing at construction breaks that contract and ~135 test
schemas with it. The guarantee that matters is about schemas the product
ships, and that is exactly what this asserts.
"""
from __future__ import annotations

import importlib
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def _import_all_production_schema_modules() -> None:
    for path in list((REPO_ROOT / "tools").rglob("*.py")) + list(
        (REPO_ROOT / "agents").rglob("*.py")
    ):
        s = str(path)
        if "/tests/" in s or path.name.startswith("test_"):
            continue
        try:
            if "SettingSchema)" not in path.read_text(errors="ignore"):
                continue
        except OSError:
            continue
        module = str(path.relative_to(REPO_ROOT))[:-3].replace("/", ".")
        try:
            importlib.import_module(module)
        except Exception:  # pragma: no cover - a module that cannot import
            continue        # is another test's problem, not this one's


@pytest.fixture(scope="module")
def registered_schemas():
    from tools.graph.schemas import registry as R

    _import_all_production_schema_modules()
    out = []
    for set_id in R.list_registered_set_ids():
        for revision in range(1, 12):
            cls = R.get_schema(set_id, revision)
            if cls is None:
                continue
            # The registry is process-global and other tests register throwaway
            # schemas into it. Only schemas defined in production modules are
            # shipped contracts; a class defined inside a test module is not.
            module = cls.__module__ or ""
            if ".tests." in module or module.rsplit(".", 1)[-1].startswith("test_"):
                continue
            out.append((set_id, revision, cls))
    assert out, "no schemas registered — the import sweep found nothing"
    return out


def test_every_schema_declares_at_least_one_field(registered_schemas):
    undeclared = [
        f"{set_id}#{revision} ({cls.__module__}.{cls.__name__})"
        for set_id, revision, cls in registered_schemas
        if not (getattr(cls, "_field_metadata", None) or {})
    ]
    assert not undeclared, (
        "these schemas declare no fields, so nothing can enforce, print or "
        "generate from their payload shape:\n  " + "\n  ".join(undeclared)
    )


def test_declared_types_are_recognised(registered_schemas):
    """A field's declared type must be one the enforcement layer understands.

    An unrecognised type name is not inert: ``enforce_declared_fields`` skips
    the check for it, so the field silently loses its type constraint. The
    fallback that produced ``"string"`` for a generic and for ``Any`` is the
    same failure wearing a different hat.
    """
    from tools.graph.schemas.registry import _JSON_TYPE_TO_PY

    known = set(_JSON_TYPE_TO_PY) | {"any"}
    bad = [
        f"{set_id}#{revision} {name}: {spec.get('type')!r}"
        for set_id, revision, cls in registered_schemas
        for name, spec in (getattr(cls, "_field_metadata", None) or {}).items()
        if spec.get("type") not in known
    ]
    assert not bad, "unrecognised declared type(s):\n  " + "\n  ".join(bad)


def test_every_declared_field_has_a_description(registered_schemas):
    """A declared field is a published contract; this is where it says what it means.

    It appears in ``graph set schema``, in the exported JSON schema and in the
    generated TypeScript, and the description is the only thing there telling a
    reader what the field is for. A field name is rarely self-explanatory to
    someone who did not write it.

    Asserted here rather than raised from ``field()`` for the same reason as the
    check above: a throwaway schema in a test publishes no contract, and
    enforcing at construction breaks those without protecting anything shipped.
    """
    undescribed = [
        f"{set_id}#{revision} {name}"
        for set_id, revision, cls in registered_schemas
        for name, spec in (getattr(cls, "_field_metadata", None) or {}).items()
        if not str(spec.get("description") or "").strip()
    ]
    assert not undescribed, (
        "these declared fields state no meaning, so nothing that renders the "
        "schema can explain them:\n  " + "\n  ".join(undescribed)
    )


def test_every_schema_declares_its_cardinality(registered_schemas):
    """How many rows exist at once is the first thing a schema must answer.

    The access-pattern decorator is that answer, and it is what tells a reader
    -- and codegen -- whether to expect one row, one per entity, or an
    append-only stream. Undeclared, the question was simply never asked, and
    the key strategy that follows from it cannot have been chosen either.

    A typed payload contract that is not a Setting row declares
    ``internal = True`` and stays out of the registry, so it is not asked a
    question it cannot answer.
    """
    undeclared = [
        f"{set_id}#{revision} ({cls.__module__}.{cls.__name__})"
        for set_id, revision, cls in registered_schemas
        if getattr(cls, "_access_pattern", None) is None
    ]
    assert not undeclared, (
        "these schemas declare no cardinality, so nothing states how many "
        "rows they have or what their key means:\n  " + "\n  ".join(undeclared)
    )


def test_internal_schemas_stay_out_of_the_registry(registered_schemas):
    """``internal = True`` means a payload contract, not a stored Setting.

    Anything walking the registry treats what it finds as a Setting -- the
    schema-meta flush writes every registered schema into every org database
    as a row. A payload shape that only borrows the field metadata must not
    be swept up in that.
    """
    leaked = [
        f"{set_id}#{revision} ({cls.__module__}.{cls.__name__})"
        for set_id, revision, cls in registered_schemas
        if getattr(cls, "internal", False)
    ]
    assert not leaked, (
        "these are marked internal but registered as Settings:\n  "
        + "\n  ".join(leaked)
    )
