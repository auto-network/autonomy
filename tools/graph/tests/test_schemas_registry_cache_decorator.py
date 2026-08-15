"""Unit tests for the ``@cache(ttl=...)`` decorator + ``cache_expires_at``.

Covers:

* The decorator stamps ``_cache_ttl_seconds`` (int seconds) and NOTHING
  else. Caching policy and cardinality are orthogonal, so ``@cache``
  stacks above an access-pattern decorator rather than occupying that
  slot; it used to write ``_access_pattern`` and ``_key_strategy``, which
  made the two mutually exclusive and silently destroyed a real key
  strategy when stacked.
* Bad arguments (missing ttl, non-timedelta, zero/negative) raise.
* :func:`cache_expires_at` returns ISO-8601 = updated_at + ttl for
  cache schemas, ``None`` for non-cache schemas.
* :meth:`SettingSchema.export_json_schema` surfaces ``cache_ttl_seconds``
  for cache schemas, omits it for everything else.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tools.graph.schemas.registry import (
    SCHEMAS,
    UPCONVERTERS,
    SchemaValidationError,
    SettingSchema,
    append_only_log,
    cache,
    cache_expires_at,
    field,
    keyed_per_entity,
    register_schema,
    singleton,
)


@pytest.fixture(autouse=True)
def _isolate_registry():
    schemas_snap = dict(SCHEMAS)
    upcon_snap = dict(UPCONVERTERS)
    try:
        yield
    finally:
        SCHEMAS.clear()
        SCHEMAS.update(schemas_snap)
        UPCONVERTERS.clear()
        UPCONVERTERS.update(upcon_snap)


# ── @cache decorator ──────────────────────────────────────────


def test_cache_sets_only_its_own_ttl():
    @cache(ttl=timedelta(days=30))
    class V1(SettingSchema):
        set_id = "x.cache"
        schema_revision = 1

    assert V1._cache_ttl_seconds == 30 * 24 * 3600  # 2_592_000
    # It claims no cardinality: that is the access-pattern decorators' slot.
    assert V1.__dict__.get("_access_pattern") is None
    assert V1.__dict__.get("_key_strategy") is None


def test_cache_stacks_with_an_access_pattern_in_either_order():
    """Orthogonal decorators compose, and order does not change the result."""
    @cache(ttl=timedelta(hours=1))
    @keyed_per_entity(key_strategy="org_slug")
    class Outer(SettingSchema):
        set_id = "x.cache.outer"
        schema_revision = 1

    @keyed_per_entity(key_strategy="org_slug")
    @cache(ttl=timedelta(hours=1))
    class Inner(SettingSchema):
        set_id = "x.cache.inner"
        schema_revision = 1

    for cls in (Outer, Inner):
        assert cls._cache_ttl_seconds == 3600
        assert cls._access_pattern == "keyed_per_entity"
        assert cls._key_strategy == "org_slug"


def test_two_access_patterns_on_one_class_are_refused():
    """A schema has one cardinality; stacking two used to silently pick one."""
    with pytest.raises(SchemaValidationError, match="two access patterns"):
        @singleton
        @append_only_log
        class _V1(SettingSchema):
            set_id = "x.cache.two"
            schema_revision = 1


def test_cache_rejects_int_seconds():
    with pytest.raises(TypeError):
        @cache(ttl=3600)  # type: ignore[arg-type]
        class _V1(SettingSchema):
            set_id = "x.cache"
            schema_revision = 1


def test_cache_rejects_zero_ttl():
    with pytest.raises(ValueError):
        @cache(ttl=timedelta(seconds=0))
        class _V1(SettingSchema):
            set_id = "x.cache"
            schema_revision = 1


def test_cache_rejects_negative_ttl():
    with pytest.raises(ValueError):
        @cache(ttl=timedelta(seconds=-5))
        class _V1(SettingSchema):
            set_id = "x.cache"
            schema_revision = 1


def test_cache_returns_class_for_chaining():
    @cache(ttl=timedelta(seconds=60))
    class V1(SettingSchema):
        set_id = "x.cache"
        schema_revision = 1

    assert isinstance(V1, type)
    assert issubclass(V1, SettingSchema)


def test_cache_ttl_seconds_fractional_truncated():
    """Fractional seconds in the timedelta truncate to int — the column
    is integer seconds, not float, so ``timedelta(milliseconds=...)``
    rounds down rather than introducing sub-second precision the column
    doesn't carry.
    """
    @cache(ttl=timedelta(seconds=1, milliseconds=500))
    class V1(SettingSchema):
        set_id = "x.cache"
        schema_revision = 1

    assert V1._cache_ttl_seconds == 1


# ── export_json_schema surfaces cache_ttl_seconds ────────────


def test_export_json_schema_includes_cache_ttl_for_cache_schema():
    @cache(ttl=timedelta(days=30))
    class V1(SettingSchema):
        set_id = "x.cache"
        schema_revision = 1
        name: str = field(required=True, description="N")

    js = V1.export_json_schema()
    assert js["cache_ttl_seconds"] == 30 * 24 * 3600
    # @cache claims no cardinality, so none is exported unless one is stacked.
    assert js["access_pattern"] is None
    assert js["set_id"] == "x.cache"
    assert js["schema_revision"] == 1


def test_export_json_schema_omits_cache_ttl_for_non_cache_schema():
    @singleton
    class V1(SettingSchema):
        set_id = "x.solo"
        schema_revision = 1

    js = V1.export_json_schema()
    assert "cache_ttl_seconds" not in js
    assert js["access_pattern"] == "singleton"


def test_export_json_schema_omits_cache_ttl_for_undecorated_schema():
    class V1(SettingSchema):
        set_id = "x.bare"
        schema_revision = 1

    js = V1.export_json_schema()
    assert "cache_ttl_seconds" not in js
    assert js["access_pattern"] is None


# ── cache_expires_at helper ──────────────────────────────────


def test_cache_expires_at_returns_iso_for_registered_cache_schema():
    @cache(ttl=timedelta(days=30))
    class V1(SettingSchema):
        set_id = "x.cache"
        schema_revision = 1

    register_schema("x.cache", 1, V1)

    out = cache_expires_at("x.cache", 1, "2026-05-01T00:00:00Z")
    assert out == "2026-05-31T00:00:00Z"


def test_cache_expires_at_returns_none_for_non_cache_schema():
    @singleton
    class V1(SettingSchema):
        set_id = "x.solo"
        schema_revision = 1

    register_schema("x.solo", 1, V1)

    assert cache_expires_at("x.solo", 1, "2026-05-01T00:00:00Z") is None


def test_cache_expires_at_returns_none_for_unregistered_schema():
    assert cache_expires_at("x.unknown", 1, "2026-05-01T00:00:00Z") is None


def test_cache_expires_at_handles_dst_boundary_month():
    """The substrate stores UTC strings, so the "DST boundary" only
    matters for callers that compute expiry in a local zone — we
    operate purely in UTC.  This test verifies the timestamp arithmetic
    crosses the spring-forward day cleanly when the input/output
    happens to span it.
    """
    @cache(ttl=timedelta(days=2))
    class V1(SettingSchema):
        set_id = "x.cache"
        schema_revision = 1

    register_schema("x.cache", 1, V1)

    # 2026 spring forward in US/Eastern is 2026-03-08.
    out = cache_expires_at("x.cache", 1, "2026-03-07T22:00:00Z")
    assert out == "2026-03-09T22:00:00Z"


def test_cache_expires_at_at_unix_epoch():
    @cache(ttl=timedelta(seconds=60))
    class V1(SettingSchema):
        set_id = "x.cache"
        schema_revision = 1

    register_schema("x.cache", 1, V1)
    assert cache_expires_at(
        "x.cache", 1, "1970-01-01T00:00:00Z",
    ) == "1970-01-01T00:01:00Z"
