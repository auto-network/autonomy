"""``where_payload`` — schema-authorized payload predicate pushdown.

The owned Settings read path narrows on an immutable JSON payload field in
SQLite instead of materializing a whole set for a Python prefilter, but must
stay byte-exact against full-read-then-Python-filter. Exactness rests on two
things this file pins:

* a same-transaction live-layer guard — if any live row of the set carries an
  override (``supersedes``) or exclusion (``excludes``), the SQL narrowing is
  dropped and the full set is fetched, because an override can turn a stored
  value the JSON path cannot see into a resolved value that matches (or the
  reverse);
* a post-resolution predicate applied to the resolved members in BOTH paths,
  so the layer-free fast path and the full-fetch fallback agree with a plain
  Python filter.

Schema authorization is a static fact: a field must be declared in every
registered revision of a non-vaulted, registered set, and every value is a
bound parameter. Everything else is refused with a bounded ``ValueError``
before any predicate SQL runs.
"""
from __future__ import annotations

import json
from uuid import uuid4

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.registry import (
    SettingSchema,
    append_only_log,
    field,
    keyed_per_entity,
    register_upconverter,
    unregister_schema,
    vaulted,
)

LOG_SET_ID = "probe.pushdown.log"
KEYED_SET_ID = "probe.pushdown.keyed"
VAULT_SET_ID = "probe.pushdown.vault"
MIXED_SET_ID = "probe.pushdown.mixed"
UPCONVERT_SET_ID = "probe.pushdown.upconvert"
UNREG_SET_ID = "probe.pushdown.unregistered"


@pytest.fixture(scope="module")
def _schemas():
    """Register the probe schemas once for the module."""

    @append_only_log()
    class Log(SettingSchema):
        set_id = LOG_SET_ID
        schema_revision = 1
        repository: str = field(required=True, description="repo identity")
        run_id: str = field(required=True, description="owning run id")
        nodeid: str = field(required=True, description="test node id")

    @keyed_per_entity(key_strategy="org_slug")
    class Keyed(SettingSchema):
        set_id = KEYED_SET_ID
        schema_revision = 1
        repository: str = field(required=True, description="repo identity")

    @vaulted("audited")
    @keyed_per_entity(key_strategy="org_slug")
    class Vault(SettingSchema):
        set_id = VAULT_SET_ID
        schema_revision = 1
        repository: str = field(required=True, description="repo identity")

    # Two coexisting revisions where ``run_id`` is declared only in one of
    # them — a mix in which the field is not uniformly present.
    @keyed_per_entity(key_strategy="org_slug")
    class MixedA(SettingSchema):
        set_id = MIXED_SET_ID
        schema_revision = 1
        repository: str = field(required=True, description="repo identity")
        run_id: str = field(required=True, description="run id (rev 1 only)")

    @keyed_per_entity(key_strategy="org_slug")
    class MixedB(SettingSchema):
        set_id = MIXED_SET_ID
        schema_revision = 2
        repository: str = field(required=True, description="repo identity")

    # Two revisions, both declaring ``repository``, with an upconvert that
    # REWRITES the value — the case where stored != resolved with no override.
    @keyed_per_entity(key_strategy="org_slug")
    class UpA(SettingSchema):
        set_id = UPCONVERT_SET_ID
        schema_revision = 1
        repository: str = field(required=True, description="repo identity")

    @keyed_per_entity(key_strategy="org_slug")
    class UpB(SettingSchema):
        set_id = UPCONVERT_SET_ID
        schema_revision = 2
        repository: str = field(required=True, description="repo identity")

    register_upconverter(
        UPCONVERT_SET_ID, 1, 2,
        lambda p: {**p, "repository": "new"} if p.get("repository") == "old"
        else p,
    )

    yield
    for set_id, rev in (
        (LOG_SET_ID, 1), (KEYED_SET_ID, 1), (VAULT_SET_ID, 1),
        (MIXED_SET_ID, 1), (MIXED_SET_ID, 2),
        (UPCONVERT_SET_ID, 1), (UPCONVERT_SET_ID, 2),
    ):
        unregister_schema(set_id, rev)


@pytest.fixture
def acme(tmp_path, monkeypatch, _schemas):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    GraphDB.create_org_db("acme").close()
    yield
    GraphDB.close_all_pooled()


# ── writers ──────────────────────────────────────────────────


def _log(repository: str, run_id: str, nodeid: str) -> str:
    return settings_ops.add_setting(
        LOG_SET_ID, 1, str(uuid4()),
        {"repository": repository, "run_id": run_id, "nodeid": nodeid},
        org="acme", state="raw",
    )


def _keyed(key: str, repository: str) -> str:
    return settings_ops.add_setting(
        KEYED_SET_ID, 1, key, {"repository": repository}, org="acme")


def _amend(base_id: str, *, key: str, patch: dict | None,
           excludes: bool = False) -> str:
    """Insert a raw override or exclusion row directly.

    ``override_setting`` / exclusion verbs guard against amending a row the
    schema says is replaced; the read path must resolve such layers wherever
    they came from, so the test writes one straight into the table.
    """
    sid = str(uuid4())
    db = GraphDB(org="acme")
    try:
        base = db.conn.execute(
            "SELECT set_id, schema_revision, publication_state "
            "FROM settings WHERE id = ?", (base_id,)).fetchone()
        col = "excludes" if excludes else "supersedes"
        db.conn.execute(
            f"INSERT INTO settings(id, set_id, schema_revision, key, payload, "
            f"publication_state, {col}, created_at, updated_at) "
            f"VALUES(?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
            (sid, base["set_id"], base["schema_revision"], key,
             json.dumps(patch or {}), base["publication_state"], base_id),
        )
        db.conn.commit()
    finally:
        db.close()
    return sid


def _read(set_id: str, **kwargs):
    return settings_ops.read_owned_set(set_id, org="acme", **kwargs).members


def _full_then_filter(set_id: str, field_name: str, want) -> list:
    members = settings_ops.read_owned_set(set_id, org="acme").members
    if isinstance(want, (list, tuple, set)):
        allowed = set(want)
        return [m for m in members if m.payload.get(field_name) in allowed]
    return [m for m in members if m.payload.get(field_name) == want]


# ── equality / IN / empty / multi-field parity ───────────────


def test_equality_matches_full_then_filter(acme):
    _log("repoA", "r1", "t1")
    _log("repoA", "r1", "t2")
    _log("repoB", "r2", "t3")

    got = _read(LOG_SET_ID, where_payload={"repository": "repoA"})
    expected = _full_then_filter(LOG_SET_ID, "repository", "repoA")

    assert sorted(m.id for m in got) == sorted(m.id for m in expected)
    assert len(got) == 2


def test_finite_in_matches_full_then_filter(acme):
    _log("repoA", "r1", "t1")
    _log("repoA", "r2", "t2")
    _log("repoB", "r3", "t3")

    got = _read(LOG_SET_ID, where_payload={"run_id": ["r1", "r3"]})
    expected = _full_then_filter(LOG_SET_ID, "run_id", ["r1", "r3"])

    assert sorted(m.id for m in got) == sorted(m.id for m in expected)
    assert {m.payload["run_id"] for m in got} == {"r1", "r3"}


def test_empty_in_returns_no_members_and_no_query(acme):
    _log("repoA", "r1", "t1")

    got = settings_ops.read_owned_set(
        LOG_SET_ID, org="acme", where_payload={"run_id": []})
    assert got.members == []
    # zeroed drop accounting — the short-circuit precedes any DB read.
    assert not any(got.dropped.values())


def test_multi_field_and_matches_full_then_filter(acme):
    _log("repoA", "r1", "t1")
    _log("repoA", "r2", "t2")
    _log("repoB", "r1", "t3")

    got = _read(LOG_SET_ID, where_payload={"repository": "repoA", "run_id": "r1"})

    assert len(got) == 1
    assert got[0].payload["nodeid"] == "t1"


def test_none_preserves_current_behavior(acme):
    _log("repoA", "r1", "t1")
    _log("repoB", "r2", "t2")

    assert len(_read(LOG_SET_ID)) == 2
    assert len(_read(LOG_SET_ID, where_payload=None)) == 2


# ── layer-free fast path pushes SQL and stays exact ──────────


class _ConnProxy:
    """Forwards to a real connection while recording executed SQL."""

    def __init__(self, real, calls):
        self._real = real
        self._calls = calls

    def execute(self, sql, *args, **kwargs):
        self._calls.append(sql)
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_layer_free_set_uses_sql_and_returns_identical_members(acme, monkeypatch):
    _log("repoA", "r1", "t1")
    _log("repoB", "r2", "t2")

    calls: list[str] = []
    orig_open = settings_ops._open_read

    def _spy_open(org, set_id=None):
        db = orig_open(org, set_id)
        db.conn = _ConnProxy(db.conn, calls)
        return db

    monkeypatch.setattr(settings_ops, "_open_read", _spy_open)
    got = _read(LOG_SET_ID, where_payload={"repository": "repoA"})

    # The row SELECT carried the payload predicate (layer-free fast path).
    assert any("json_extract(payload" in sql for sql in calls)
    assert [m.payload["repository"] for m in got] == ["repoA"]


# ── override / exclusion fallback (the guard that matters) ───


def test_matching_to_nonmatching_override_is_exact(acme):
    base = _keyed("acme", "repoA")
    _amend(base, key="acme", patch={"repository": "repoB"})

    got = _read(KEYED_SET_ID, where_payload={"repository": "repoA"})
    expected = _full_then_filter(KEYED_SET_ID, "repository", "repoA")

    assert [m.id for m in got] == [m.id for m in expected] == []


def test_nonmatching_to_matching_override_is_exact(acme):
    # Base value MISSES the predicate; an override makes the RESOLVED value
    # match. A naive raw-row pushdown would never fetch the base and would
    # wrongly return nothing — the live-layer guard forces a full fetch.
    base = _keyed("acme", "repoB")
    _amend(base, key="acme", patch={"repository": "repoA"})

    got = _read(KEYED_SET_ID, where_payload={"repository": "repoA"})
    expected = _full_then_filter(KEYED_SET_ID, "repository", "repoA")

    assert [m.payload["repository"] for m in got] == ["repoA"]
    assert [m.id for m in got] == [m.id for m in expected]


def test_exclusion_is_exact(acme):
    # A base that matches the predicate, excluded by a live exclusion row.
    # Full resolution drops the key; the pushdown path must agree, which it
    # only does by taking the full-fetch fallback the guard triggers.
    base = _keyed("acme", "repoA")
    _amend(base, key="acme", patch=None, excludes=True)

    got = _read(KEYED_SET_ID, where_payload={"repository": "repoA"})
    expected = _full_then_filter(KEYED_SET_ID, "repository", "repoA")

    assert got == [] and expected == []


def test_target_revision_upconvert_is_exact(acme):
    # Stored value MISSES the predicate; the upconvert to revision 2 rewrites
    # it to a value that MATCHES. SQL narrowing on the stored value would drop
    # the row, and the post-resolution re-apply could not recover it — so a
    # target_revision read must skip narrowing and stay exact.
    settings_ops.add_setting(
        UPCONVERT_SET_ID, 1, "acme", {"repository": "old"}, org="acme")

    got = settings_ops.read_owned_set(
        UPCONVERT_SET_ID, org="acme", target_revision=2,
        where_payload={"repository": "new"}).members
    expected = [
        m for m in settings_ops.read_owned_set(
            UPCONVERT_SET_ID, org="acme", target_revision=2).members
        if m.payload.get("repository") == "new"
    ]

    assert [m.payload["repository"] for m in got] == ["new"]
    assert [m.id for m in got] == [m.id for m in expected]


# ── validation: bounded ValueError before unsafe SQL ─────────


def test_unknown_field_is_refused(acme):
    _log("repoA", "r1", "t1")
    with pytest.raises(ValueError):
        _read(LOG_SET_ID, where_payload={"nope": "x"})


def test_malformed_scalar_value_is_refused(acme):
    with pytest.raises(ValueError):
        _read(LOG_SET_ID, where_payload={"repository": 5})


def test_mapping_value_is_refused(acme):
    with pytest.raises(ValueError):
        _read(LOG_SET_ID, where_payload={"repository": {"a": "b"}})


def test_bytes_value_is_refused(acme):
    with pytest.raises(ValueError):
        _read(LOG_SET_ID, where_payload={"repository": b"repoA"})


def test_nested_sequence_value_is_refused(acme):
    with pytest.raises(ValueError):
        _read(LOG_SET_ID, where_payload={"run_id": ["ok", ["bad"]]})


def test_unregistered_set_is_refused(acme):
    with pytest.raises(ValueError):
        _read(UNREG_SET_ID, where_payload={"repository": "repoA"})


def test_mixed_revision_field_is_refused(acme):
    # ``run_id`` is declared only in revision 1 of the mixed set; a field must
    # be present in EVERY applicable revision to be pushdown-safe.
    with pytest.raises(ValueError):
        _read(MIXED_SET_ID, where_payload={"run_id": "r1"})


def test_field_present_in_all_revisions_is_allowed(acme):
    # ``repository`` is declared in both revisions, so it is safe even though
    # the set has a revision mix.
    _keyed_read = settings_ops.read_owned_set(
        MIXED_SET_ID, org="acme", where_payload={"repository": "repoA"})
    assert _keyed_read.members == []


def test_vaulted_set_is_refused(acme):
    with pytest.raises(ValueError):
        _read(VAULT_SET_ID, where_payload={"repository": "repoA"})


# ── federated reads never expose the predicate ───────────────


def test_predicate_refused_on_federated_read(acme):
    _log("repoA", "r1", "t1")
    # peers=None (subscription-resolved) is a federated read.
    with pytest.raises(ValueError):
        settings_ops.read_set(
            LOG_SET_ID, org="acme", where_payload={"repository": "repoA"})


def test_predicate_refused_with_pinned_peer(acme):
    with pytest.raises(ValueError):
        settings_ops.read_set(
            LOG_SET_ID, org="acme", peers=["beta"],
            where_payload={"repository": "repoA"})


def test_owned_read_allows_the_predicate(acme):
    _log("repoA", "r1", "t1")
    got = settings_ops.read_set(
        LOG_SET_ID, org="acme", peers=[],
        where_payload={"repository": "repoA"})
    assert [m.payload["repository"] for m in got.members] == ["repoA"]
