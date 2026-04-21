"""RRF (Reciprocal Rank Fusion) merge tests (auto-txg5.4).

Covers :func:`cross_org.rrf_merge` — the scoring primitive the cross-org
search uses to combine own-org + peer ranked lists into a single view.

Per graph://bcce359d-a1d § Merge algorithms: score = sum over input
lists of ``boost / (k + rank)`` where ``k = 60`` and own-org gets a
``1.5`` multiplier. Same UUID across lists accumulates contributions.
"""

from __future__ import annotations

import pytest

from tools.graph import cross_org
from tools.graph.cross_org import OWN_ORG_BOOST, RRF_K, rrf_merge


def _rows(ids: list[str]) -> list[dict]:
    return [{"id": i, "content": f"hit-{i}"} for i in ids]


# ── Scoring fundamentals ─────────────────────────────────────


def test_rrf_single_list_preserves_order():
    """One list, no merging, no scoring tie — output keeps input rank."""
    merged = rrf_merge(
        [("autonomy", _rows(["a", "b", "c"]))],
        limit=3,
        own_org="autonomy",
    )
    assert [r["id"] for r in merged] == ["a", "b", "c"]


def test_rrf_annotates_origin_org():
    merged = rrf_merge(
        [("autonomy", _rows(["a"]))],
        limit=10,
        own_org="autonomy",
    )
    assert merged[0]["org"] == "autonomy"


def test_rrf_attaches_score():
    merged = rrf_merge(
        [("autonomy", _rows(["a"]))],
        limit=10,
        own_org="autonomy",
    )
    # Own-org rank 1 → 1.5 / (60 + 1)
    assert merged[0]["rrf_score"] == pytest.approx(OWN_ORG_BOOST / (RRF_K + 1))


def test_rrf_truncates_to_limit():
    merged = rrf_merge(
        [("autonomy", _rows(["a", "b", "c", "d"]))],
        limit=2,
        own_org="autonomy",
    )
    assert len(merged) == 2


# ── Multi-list merging ───────────────────────────────────────


def test_rrf_merges_disjoint_lists_interleaved():
    """Own-org boost keeps top peer behind top own-org even at same rank."""
    merged = rrf_merge(
        [
            ("autonomy", _rows(["a1", "a2"])),
            ("anchore", _rows(["b1", "b2"])),
        ],
        limit=4,
        own_org="autonomy",
    )
    # own-org rank 1 beats peer rank 1 due to 1.5x boost
    assert merged[0]["id"] == "a1"
    # own-org rank 2 = 1.5 / 62 ≈ 0.0242
    # peer     rank 1 = 1.0 / 61 ≈ 0.0164
    # own-org rank 2 beats peer rank 1
    assert merged[1]["id"] == "a2"
    assert merged[2]["id"] == "b1"
    assert merged[3]["id"] == "b2"


def test_rrf_same_id_across_lists_accumulates():
    """A UUID that appears in multiple lists accumulates its contributions."""
    merged = rrf_merge(
        [
            ("autonomy", _rows(["shared", "a"])),
            ("anchore", _rows(["shared", "b"])),
        ],
        limit=10,
        own_org="autonomy",
    )
    shared = next(r for r in merged if r["id"] == "shared")
    expected = OWN_ORG_BOOST / (RRF_K + 1) + 1.0 / (RRF_K + 1)
    assert shared["rrf_score"] == pytest.approx(expected)


def test_rrf_same_id_keeps_first_seen_metadata():
    """Duplicate row across lists shouldn't overwrite own-org shape."""
    own = [{"id": "x", "title": "own-version"}]
    peer = [{"id": "x", "title": "peer-version"}]
    merged = rrf_merge(
        [("autonomy", own), ("anchore", peer)],
        limit=5,
        own_org="autonomy",
    )
    assert merged[0]["title"] == "own-version"
    # Origin still attributed to first-seen list (own-org).
    assert merged[0]["org"] == "autonomy"


def test_rrf_without_own_org_applies_no_boost():
    """When caller is peer-less (only_org) or admin-all, boost is 1.0 everywhere.

    Passing ``own_org=None`` makes every input list equivalent — useful
    for the ``--org all`` admin mode where there's no "home" org.
    """
    merged = rrf_merge(
        [
            ("autonomy", _rows(["a"])),
            ("anchore", _rows(["b"])),
        ],
        limit=5,
        own_org=None,
    )
    # With no boost, ranks are equal → first list wins the tie.
    assert merged[0]["id"] == "a"
    assert merged[1]["id"] == "b"
    # Scores match exactly.
    assert merged[0]["rrf_score"] == pytest.approx(merged[1]["rrf_score"])


# ── Edge cases ───────────────────────────────────────────────


def test_rrf_empty_lists_returns_empty():
    assert rrf_merge([], limit=10, own_org="autonomy") == []


def test_rrf_rows_without_key_are_skipped():
    """Rows missing the id field shouldn't crash the merge."""
    merged = rrf_merge(
        [("autonomy", [{"id": "a"}, {"no_id_here": True}, {"id": "b"}])],
        limit=10,
        own_org="autonomy",
    )
    assert [r["id"] for r in merged] == ["a", "b"]


def test_rrf_custom_key_field():
    merged = rrf_merge(
        [("autonomy", [{"source_id": "a"}])],
        limit=5,
        own_org="autonomy",
        key="source_id",
    )
    assert merged[0]["source_id"] == "a"


# ── Constants ────────────────────────────────────────────────


def test_rrf_k_and_boost_constants_match_spec():
    """Spec § Merge algorithms pins k=60 and own-org boost 1.5."""
    assert cross_org.RRF_K == 60
    assert cross_org.OWN_ORG_BOOST == 1.5


# ── Chronological merge ──────────────────────────────────────


def test_chronological_merge_sorts_across_orgs():
    a_rows = [
        {"id": "a1", "created_at": "2026-04-21T10:00:00Z"},
        {"id": "a2", "created_at": "2026-04-19T10:00:00Z"},
    ]
    b_rows = [
        {"id": "b1", "created_at": "2026-04-20T10:00:00Z"},
    ]
    merged = cross_org.chronological_merge(
        [("autonomy", a_rows), ("anchore", b_rows)],
        limit=10,
    )
    assert [r["id"] for r in merged] == ["a1", "b1", "a2"]
    # Every row annotated with its origin.
    origins = {r["id"]: r["org"] for r in merged}
    assert origins == {"a1": "autonomy", "b1": "anchore", "a2": "autonomy"}


def test_chronological_merge_truncates():
    rows = [
        {"id": f"x{i}", "created_at": f"2026-04-{i:02d}T00:00:00Z"}
        for i in range(1, 11)
    ]
    merged = cross_org.chronological_merge(
        [("autonomy", rows)], limit=3,
    )
    assert len(merged) == 3
    # Newest first.
    assert merged[0]["id"] == "x10"
