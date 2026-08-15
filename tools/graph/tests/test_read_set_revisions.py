"""A key can hold rows at more than one revision, and resolution must say which.

Stored-row uniqueness is per ``(set_id, schema_revision, key,
publication_state)``, so nothing stops two revisions living under one key — and
a schema whose generations deliberately coexist, with no upconvert chain
between them, will have exactly that.

Resolution picks one base per key. Revision has to take part in that choice:
without it the two rows tie on publication state, tie again on ``created_at``
whenever they were written in the same second, and the winner is whichever the
database happened to return first. A newer generation of a value then becomes
unreadable while its row sits right there — and asking for it by revision
returns nothing, because the row that cannot reach the target was chosen before
anyone checked.
"""
from __future__ import annotations

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.registry import (
    SettingSchema,
    field,
    keyed_per_entity,
)

SET_ID = "probe.revisions.coexist"


@pytest.fixture(scope="module")
def two_generations():
    """Two revisions, no upconvert chain between them — the deliberate case."""
    @keyed_per_entity(key_strategy="org_slug")
    class Gen1(SettingSchema):
        set_id = SET_ID
        schema_revision = 1
        legacy: str = field(required=True, description="the older generation")

    @keyed_per_entity(key_strategy="org_slug")
    class Gen2(SettingSchema):
        set_id = SET_ID
        schema_revision = 2
        sealed: str = field(required=True, description="the newer generation")

    return Gen1, Gen2


@pytest.fixture
def acme(tmp_path, monkeypatch, two_generations):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    GraphDB.create_org_db("acme").close()
    yield
    GraphDB.close_all_pooled()


def _write(revision: int, payload: dict) -> None:
    settings_ops.add_setting(SET_ID, revision, "acme", payload, org="acme")


def _read(**kwargs):
    return settings_ops.read_set(SET_ID, org="acme", peers=[], **kwargs).members


@pytest.mark.parametrize("rev1_first", [True, False], ids=["rev1-first", "rev2-first"])
def test_the_newer_generation_wins_whatever_the_write_order(acme, rev1_first):
    """Insertion order must not decide which generation a reader sees."""
    writes = [(1, {"legacy": "old"}), (2, {"sealed": "new"})]
    for revision, payload in writes if rev1_first else reversed(writes):
        _write(revision, payload)

    members = _read()

    assert [m.stored_revision for m in members] == [2]
    assert members[0].payload == {"sealed": "new"}


def test_asking_for_a_revision_returns_the_row_stored_at_it(acme):
    """Both directions, with both rows present."""
    _write(1, {"legacy": "old"})
    _write(2, {"sealed": "new"})

    assert [m.payload for m in _read(target_revision=1)] == [{"legacy": "old"}]
    assert [m.payload for m in _read(target_revision=2)] == [{"sealed": "new"}]


def test_an_unreachable_revision_still_drops_with_its_reason(acme):
    """Only the older row exists and cannot be upconverted.

    Nothing can serve the request, so the answer is still empty — but it is
    empty because the row genuinely cannot be shaped, and the drop accounting
    has to keep saying so.
    """
    _write(1, {"legacy": "old"})

    result = settings_ops.read_set(
        SET_ID, org="acme", peers=[], target_revision=2,
    )

    assert result.members == []
    assert result.dropped.no_upconvert_path == 1


def test_a_row_above_the_target_is_not_downgraded(acme):
    _write(2, {"sealed": "new"})

    result = settings_ops.read_set(
        SET_ID, org="acme", peers=[], target_revision=1,
    )

    assert result.members == []
    assert result.dropped.above_target_no_downgrade == 1


def test_publication_state_still_outranks_revision(acme):
    """Revision is a tie-break, not a promotion.

    Which rows are visible across organizations is decided by publication
    state, and a newer revision does not get to change that answer.
    """
    _write(1, {"legacy": "old"})
    settings_ops.promote_setting(
        settings_ops.resolve_set_key(SET_ID, "acme", org="acme", peers=[])["id"],
        "canonical", org="acme",
    )
    _write(2, {"sealed": "new"})

    members = _read()

    assert [m.stored_revision for m in members] == [1]
    assert members[0].state == "canonical"
