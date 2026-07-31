"""Duplicate-title invariants for Design Studio storage."""

from __future__ import annotations

import pytest

from agents import design_db


@pytest.fixture
def isolated_design_db(tmp_path, monkeypatch):
    monkeypatch.setattr(design_db, "DB_PATH", tmp_path / "experiments.db")
    monkeypatch.setattr(design_db, "_initialized", False)


def _create(title: str, **kwargs) -> str:
    return design_db.create_design(
        title=title,
        variants=[{"id": "main", "html": "<main>Design</main>"}],
        **kwargs,
    )


def test_new_design_rejects_exact_current_title_duplicate(isolated_design_db):
    first_revision = _create("Release review")

    with pytest.raises(design_db.DuplicateDesignTitleError) as exc_info:
        _create("Release review")

    assert exc_info.value.existing == [
        {
            "design_id": first_revision,
            "latest_revision_id": first_revision,
            "title": "Release review",
            "revision_count": 1,
        }
    ]


def test_revision_and_explicit_force_bypass_new_design_guard(isolated_design_db):
    design_id = _create("Release review")

    revision_id = _create("Release review", design_id=design_id)
    duplicate_id = _create("Release review", force=True)

    assert design_db.get_design(revision_id)["design_id"] == design_id
    assert design_db.get_design(revision_id)["revision_seq"] == 2
    assert duplicate_id != design_id
    assert design_db.get_design(duplicate_id)["revision_seq"] == 1


def test_guard_compares_latest_design_title(isolated_design_db):
    design_id = _create("Early working title")
    _create("Final title", design_id=design_id)

    new_design_id = _create("Early working title")

    assert new_design_id != design_id
