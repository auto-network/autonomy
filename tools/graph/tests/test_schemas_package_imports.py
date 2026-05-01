"""Tests pinning the public ``tools.graph.schemas`` import surface.

The package is the schemas authoring surface. Schema modules import
``SettingSchema``, ``register_schema``, ``field``, and the access-pattern
decorators directly from ``tools.graph.schemas``; the ``registry``
submodule is internal. These tests pin the names that must remain
importable from the package and listed in ``__all__``.
"""

from __future__ import annotations


def test_authoring_api_importable_from_package():
    """The four authoring symbols must be importable from the package."""
    from tools.graph.schemas import (
        SettingSchema,
        field,
        append_only_log,
        singleton,
        keyed_per_entity,
    )

    assert SettingSchema is not None
    assert callable(field)
    assert callable(append_only_log)
    assert callable(singleton)
    assert callable(keyed_per_entity)


def test_authoring_api_listed_in_dunder_all():
    """``__all__`` must list the authoring symbols — pins the public
    surface against accidental removal during future refactors.
    """
    import tools.graph.schemas as pkg

    for name in (
        "SettingSchema",
        "field",
        "append_only_log",
        "singleton",
        "keyed_per_entity",
        "register_schema",
        "validate_payload",
        "flush_schema_meta",
    ):
        assert name in pkg.__all__, f"{name!r} missing from __all__"


def test_decorators_from_package_work_end_to_end():
    """A schema authored using only package-level imports works the same
    as one using ``tools.graph.schemas.registry``.
    """
    from tools.graph.schemas import (
        SettingSchema,
        append_only_log,
        field,
    )

    @append_only_log(key="uuid_v4")
    class V1(SettingSchema):
        set_id = "x.y"
        schema_revision = 1
        tile_id: str = field(required=True, description="Tile reference")

    assert V1._access_pattern == "append_only_log"
    assert V1._key_strategy == "uuid_v4"
    assert V1._field_metadata["tile_id"]["required"] is True
