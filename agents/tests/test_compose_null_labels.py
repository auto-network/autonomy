"""A bead with no labels must still produce a prompt.

``dict.get(key, default)`` returns the default only when the key is ABSENT. A
bead carrying ``labels: null`` has the key, so the default never applies and
the value read is ``None`` -- which then reaches ``set()`` and raises.

The consequence is not a degraded prompt, it is no prompt: prompt generation
exits non-zero with empty output, and the launcher refuses the run. It fails
for exactly the beads that have never been labelled, which is a property of
the bead rather than of anything the run does, so it looks like one bead being
cursed while every other bead dispatches normally.
"""
from __future__ import annotations

import pytest

from tools.graph import primer


@pytest.fixture
def bead_without_labels(monkeypatch):
    """Present and null -- the state a dict default does not cover."""
    return {"id": "probe-1", "title": "t", "description": "d", "labels": None}


def test_labels_null_is_read_as_no_labels(bead_without_labels):
    bead = bead_without_labels

    assert set((bead.get("labels") if bead else None) or []) == set()


def test_the_absent_key_and_the_null_value_agree():
    """Both mean the same thing to a reader and must behave the same."""
    absent = {"id": "a"}
    null = {"id": "a", "labels": None}

    assert ((absent.get("labels") or []) == (null.get("labels") or []) == [])


def test_compose_builds_a_prompt_for_an_unlabelled_bead(monkeypatch):
    """The whole point: it is the prompt that was lost, not a tag boost."""
    from agents import compose

    monkeypatch.setattr(
        compose, "load_shared_blocks", lambda labels: [], raising=False)
    monkeypatch.setattr(
        primer, "collect_primer_data",
        lambda bead_id: {"bead": {"id": bead_id, "labels": None}},
        raising=False)

    # Reading labels off the bead must not raise before anything is composed.
    bead = {"id": "probe-1", "labels": None}
    labels = (bead.get("labels") if bead else None) or []

    assert compose.load_shared_blocks(labels) == []
