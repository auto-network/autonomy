"""An org slug formats straight into a database filename, so it is checked.

``_org_db_path(None)`` used to return ``orgs/None.db``, and something created
it. The evidence is still on sjc-2: ``None.db-wal`` (4.1 MB) and
``None.db.stray-probe-artifact-...`` sit beside the four real org databases, and
the machine's fleet telemetry holds a ``scope=None`` pull row with 5078
iterations and 5078 failures. That ghost also sorted ahead of every real org in
the sync scope order and starved all four of them for 24 hours.

The scope discovery was fixed. The GENERATOR was not: the function still
accepted anything and formatted it into a path, so any caller holding a
None-able org could mint the ghost again.
"""

from __future__ import annotations

import pytest

from tools.graph.db import _org_db_path


def test_a_real_slug_still_resolves(tmp_path):
    """The guard runs before both branches, so both are pinned here.

    With an explicit root, that root IS the orgs directory and the local store
    sits beside it — measured, not assumed: my first version of this test
    asserted root/"orgs"/anchore.db and was wrong, which is the same mistake as
    reasoning about a path instead of reading one.
    """
    assert _org_db_path("anchore", tmp_path) == tmp_path / "anchore.db"
    # The two reserved local-store names are NOT organizations and must not be
    # resolved as one.
    for reserved in ("personal", "machine"):
        resolved = _org_db_path(reserved, tmp_path)
        assert resolved.name == f"{reserved}.db"
        assert resolved != tmp_path / f"{reserved}.db", (
            "a reserved local-store name must not resolve into the orgs dir"
        )


@pytest.mark.parametrize("slug", [None, "", ".", "a/b", "../escape", 42, object()])
def test_a_slug_that_cannot_be_a_filename_is_refused(slug, tmp_path):
    """Each of these previously produced a path. None.db is the one that
    actually happened; the others are the same mistake with a different value.
    """
    with pytest.raises(ValueError) as exc:
        _org_db_path(slug, tmp_path)
    assert "org slug" in str(exc.value)


def test_the_ghost_database_is_not_creatable_by_passing_none(tmp_path):
    """The specific regression: no orgs/None.db, and no file at all."""
    with pytest.raises(ValueError):
        _org_db_path(None, tmp_path)
    assert not list(tmp_path.rglob("None.db*")), (
        "passing None must not leave a path behind, let alone a file"
    )


def test_the_refusal_names_the_caller_as_the_bug(tmp_path):
    """The message has to send a reader to the call site. A path-shaped value
    formatted from a None is invisible; the error is the only thing that makes
    it visible."""
    with pytest.raises(ValueError) as exc:
        _org_db_path(None, tmp_path)
    message = str(exc.value)
    assert "None" in message, "says what it was given"
    assert "Fix the caller" in message, "says where the bug is"
