"""Tests for source-aware search (auto-bounj, Round 7l).

Pre-Round-7l, ``db.search``'s ``LIMIT N`` applied to raw FTS rows: a
session matching a query 30 times produced 30 thought-rows competing
for the LIMIT slot, which crowded out other distinct sources. Hidden
in relevance mode (BM25 distributes rank across sources); visible in
recency mode (every hit shares one ``source.created_at``, so the
recent source's hits clustered and ate the LIMIT).

Round 7l made the unit of a search result the **source**:

  * LIMIT N applies to distinct source_ids (head row + capped excerpts
    per surviving source).
  * Multi-hit sources rank higher (log-shaped hit-count bonus) without
    eating other sources' slots.
  * Recency mode orders by ``s.created_at DESC`` at the source level —
    the actual N most-recent matching sources, not whichever cluster
    happened to land first.

This module pins those contracts; ``test_search_ranking.py`` /
``test_search_order_and_session_type.py`` cover the orthogonal
title-boost / order / session_type axes.
"""

from __future__ import annotations

import pytest

from tools.graph.cross_org import (
    OWN_ORG_BOOST,
    RRF_K,
    chronological_merge,
    rrf_merge,
)
from tools.graph.db import GraphDB
from tools.graph.models import Source, Thought


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin GRAPH_DB to a fresh tmp file for the test's duration."""
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    return db_path


def _seed_session_with_hits(
    db: GraphDB, *, title: str, term: str, hit_count: int,
    created_at: str | None = None,
    type_: str = "session",
    session_type: str | None = None,
) -> Source:
    metadata: dict = {}
    if session_type is not None:
        metadata["session_type"] = session_type
    kwargs: dict = dict(
        type=type_,
        platform="local",
        title=title,
        file_path=f"{type_}:{title.replace(' ', '_').lower()}",
        metadata=metadata,
    )
    if created_at is not None:
        kwargs["created_at"] = created_at
    src = Source(**kwargs)
    db.insert_source(src)
    for i in range(hit_count):
        db.insert_thought(Thought(
            source_id=src.id,
            content=f"discussion about {term} occurrence {i}",
            role="user",
            turn_number=i,
            tags=[],
        ))
    db.commit()
    return src


# ── Source-level LIMIT ───────────────────────────────────────────────


def test_search_limit_returns_at_most_n_distinct_sources(graph_db_env):
    """``db.search(limit=N)`` returns at most N distinct source_ids in
    its head-row positions, regardless of how many per-source excerpts
    follow.

    Pre-Round-7l, LIMIT applied to raw FTS rows; a multi-hit session
    could push the result below N distinct sources. Now LIMIT is
    enforced at the source level.
    """
    db = GraphDB(str(graph_db_env))
    try:
        # Seed 5 sources with 1 hit each.
        seeded = []
        for i in range(5):
            seeded.append(_seed_session_with_hits(
                db, title=f"sourcelimit row {i}",
                term="sourcelimit", hit_count=1,
                created_at=f"2026-04-{20+i:02d}T00:00:00Z",
            ))
        results = db.search("sourcelimit", limit=3)
        # Distinct source_ids from head rows (those whose ``id`` matches
        # ``source_id`` for source/thought/derivation result types).
        distinct_sids = []
        seen: set = set()
        for r in results:
            sid = r.get("source_id")
            if sid not in seen:
                seen.add(sid)
                distinct_sids.append(sid)
        assert len(distinct_sids) == 3, (
            f"Expected exactly 3 distinct sources at limit=3; "
            f"got {len(distinct_sids)} (sids={distinct_sids})"
        )
    finally:
        db.close()


def test_search_multi_hit_source_does_not_consume_limit_slots(graph_db_env):
    """A 30-hit session must not crowd out other distinct sources at
    LIMIT N — the regression test for the live bug that drove this
    bead. Pre-Round-7l, the 30 thought-rows from one session ate the
    LIMIT and other sources never appeared.
    """
    db = GraphDB(str(graph_db_env))
    try:
        # 1 session with 30 hits — pre-fix this would dominate.
        multihit = _seed_session_with_hits(
            db, title="multihit dense session",
            term="crowdedterm", hit_count=30,
            created_at="2026-04-15T00:00:00Z",
        )
        # 4 other sources with 1 hit each — should ALL still appear at
        # limit=5 even though the multihit session has 30 candidate
        # rows.
        singles = []
        for i in range(4):
            singles.append(_seed_session_with_hits(
                db, title=f"single hit row {i}",
                term="crowdedterm", hit_count=1,
                created_at=f"2026-04-{20+i:02d}T00:00:00Z",
            ))

        results = db.search("crowdedterm", limit=5)
        distinct_sids = {r.get("source_id") for r in results}
        # Every single-hit source must be visible alongside the multihit.
        assert multihit.id in distinct_sids
        for s in singles:
            assert s.id in distinct_sids, (
                f"Single-hit source {s.id} crowded out by 30-hit session "
                f"under limit=5 — got sids={distinct_sids}"
            )
    finally:
        db.close()


def test_search_recency_returns_n_most_recent_distinct_sources(graph_db_env):
    """Under ``order='recent'`` + LIMIT N, the result is the actual N
    most-recent matching SOURCES, not whichever cluster happened to
    land first.

    The original bug: searching "worktrees" + recency returned 3
    sessions when 11 matched, because one recent session's 30 hits ate
    the row-level LIMIT and only one source's worth of rows surfaced.
    """
    db = GraphDB(str(graph_db_env))
    try:
        # The dense session is the OLDEST so it can't sneak through on
        # recency alone; it should not crowd out the more recent
        # singletons.
        old_dense = _seed_session_with_hits(
            db, title="recency old dense",
            term="recencyterm", hit_count=30,
            created_at="2026-01-01T00:00:00Z",
        )
        # 5 newer sources with 1 hit each.
        newer = []
        for i in range(5):
            newer.append(_seed_session_with_hits(
                db, title=f"recency newer {i}",
                term="recencyterm", hit_count=1,
                created_at=f"2026-04-{20+i:02d}T00:00:00Z",
            ))

        results = db.search("recencyterm", limit=3, order="recent")
        # Distinct sources in result order (head rows first).
        ordered_sids: list[str] = []
        seen: set = set()
        for r in results:
            sid = r.get("source_id")
            if sid not in seen:
                seen.add(sid)
                ordered_sids.append(sid)
        assert len(ordered_sids) == 3, (
            f"Expected 3 distinct sources at recency+limit=3; "
            f"got {len(ordered_sids)}"
        )
        # The 3 most-recent sources must be the top 3 newer ones.
        expected_top3 = {newer[-1].id, newer[-2].id, newer[-3].id}
        assert set(ordered_sids) == expected_top3, (
            f"Recency mode should pick the 3 most-recent sources; got "
            f"{ordered_sids} (expected {expected_top3})"
        )
        # The old dense session must not have eaten a slot just because
        # it has 30 hits.
        assert old_dense.id not in ordered_sids
    finally:
        db.close()


# ── Multi-hit as a positive rank signal ──────────────────────────────


def test_search_multi_hit_source_outranks_single_hit_in_relevance(graph_db_env):
    """A source matching the query 30 times must rank higher than a
    source matching it once, in relevance mode (single appearance in
    the result, ranked above the single-hit peer).

    The bead's framing: 30 hits in one source is a strong signal —
    contributes to ranking, not to row-count.
    """
    db = GraphDB(str(graph_db_env))
    try:
        many = _seed_session_with_hits(
            db, title="hitcount many session",
            term="hitsignal", hit_count=30,
        )
        few = _seed_session_with_hits(
            db, title="hitcount single session",
            term="hitsignal", hit_count=1,
        )

        results = db.search("hitsignal", limit=10)
        # Distinct head rows in result order.
        seen: set = set()
        ordered: list[dict] = []
        for r in results:
            sid = r.get("source_id")
            if sid not in seen:
                seen.add(sid)
                ordered.append(r)
        sids_in_order = [r.get("source_id") for r in ordered]
        assert many.id in sids_in_order
        assert few.id in sids_in_order
        # Multi-hit source appears ONCE in the result (not 30 times).
        many_count = sum(1 for r in ordered if r.get("source_id") == many.id)
        assert many_count == 1, (
            f"Multi-hit source appeared {many_count} times in distinct "
            f"head rows; expected 1"
        )
        # Multi-hit ranks above single-hit.
        assert sids_in_order.index(many.id) < sids_in_order.index(few.id), (
            f"Multi-hit source {many.id} did not rank above single-hit "
            f"{few.id}; got order={sids_in_order!r}"
        )
        # The head row carries hit_count for the dashboard's match-count
        # display — so the card shows "30 matches" even when only the
        # capped excerpt set surfaces in the API payload.
        many_head = next(r for r in ordered if r.get("source_id") == many.id)
        assert many_head.get("hit_count") == 30, (
            f"Multi-hit head row missing/wrong hit_count: "
            f"{many_head.get('hit_count')!r}"
        )
    finally:
        db.close()


def test_search_excerpt_cap_does_not_lose_hit_count(graph_db_env):
    """Excerpts are capped at ``SEARCH_EXCERPTS_PER_SOURCE`` per source,
    but the head row's ``hit_count`` stays truthful — the dashboard
    "30 matches" stamp on the card must reflect the real density, not
    the visible excerpt count.
    """
    db = GraphDB(str(graph_db_env))
    try:
        src = _seed_session_with_hits(
            db, title="excerpt cap session",
            term="excerptterm", hit_count=30,
        )
        results = db.search("excerptterm", limit=5)
        rows_for_src = [r for r in results if r.get("source_id") == src.id]
        # Head + capped excerpts.
        from tools.graph.db import SEARCH_EXCERPTS_PER_SOURCE
        assert len(rows_for_src) <= SEARCH_EXCERPTS_PER_SOURCE
        head = rows_for_src[0]
        assert head.get("hit_count") == 30
    finally:
        db.close()


# ── Cross-org RRF merge on source-shaped lists ───────────────────────


def _src_row(sid: str, *, rank: float = -10.0, content: str = "x") -> dict:
    return {
        "source_id": sid,
        "id": sid,
        "rank": rank,
        "content": content,
        "result_type": "source",
        "source_title": f"src-{sid}",
        "source_created_at": "2026-04-29T00:00:00Z",
    }


def _excerpt_row(sid: str, *, eid: str, rank: float = -8.0,
                 turn: int = 1) -> dict:
    return {
        "source_id": sid,
        "id": eid,
        "rank": rank,
        "content": f"excerpt for {sid}",
        "result_type": "thought",
        "turn_number": turn,
        "source_title": f"src-{sid}",
        "source_created_at": "2026-04-29T00:00:00Z",
    }


def test_rrf_merge_keys_on_source_id_preserves_excerpt_rows():
    """When ``key='source_id'``, all rows of a surviving source ride
    the source's RRF slot together — head + tail rows of one source
    are not deduplicated apart, and the per-source rank position is
    counted once (not once per row)."""
    own = [
        _src_row("S1"), _excerpt_row("S1", eid="t1a"),
        _excerpt_row("S1", eid="t1b"),
        _src_row("S2"), _excerpt_row("S2", eid="t2a"),
    ]
    merged = rrf_merge(
        [("autonomy", own)], limit=5, own_org="autonomy", key="source_id",
    )
    # Both sources survive at limit=5; all their rows ride.
    sids = [r.get("source_id") for r in merged]
    assert sids.count("S1") == 3
    assert sids.count("S2") == 2
    # S1 sits at distinct-source rank 1 (own-org boost 1.5 / (60+1));
    # S2 sits at distinct-source rank 2 (1.5 / 62). The score is set
    # ONCE per source — every row of S1 carries the same score.
    s1_scores = {r["rrf_score"] for r in merged if r["source_id"] == "S1"}
    assert len(s1_scores) == 1
    assert s1_scores.pop() == pytest.approx(OWN_ORG_BOOST / (RRF_K + 1))


def test_rrf_merge_source_level_limit_keeps_row_groups_intact():
    """``LIMIT`` counts distinct sources, not rows: at limit=2 with
    three sources (each emitting head+excerpt rows), exactly two
    sources survive — and BOTH their rows ride."""
    own = [
        _src_row("S1"), _excerpt_row("S1", eid="t1"),
        _src_row("S2"), _excerpt_row("S2", eid="t2"),
        _src_row("S3"), _excerpt_row("S3", eid="t3"),
    ]
    merged = rrf_merge(
        [("autonomy", own)], limit=2, own_org="autonomy", key="source_id",
    )
    # S3 dropped (rank 3); S1 + S2 survive with their excerpt rows.
    sids = {r["source_id"] for r in merged}
    assert sids == {"S1", "S2"}
    # Each surviving source still has its head + excerpt row.
    assert sum(1 for r in merged if r["source_id"] == "S1") == 2
    assert sum(1 for r in merged if r["source_id"] == "S2") == 2


def test_rrf_merge_cross_org_accumulates_per_source():
    """A source that appears in own + peer lists accumulates RRF
    contributions from both — once per (org, source), not per (org,
    row)."""
    own = [_src_row("shared"), _excerpt_row("shared", eid="t1")]
    peer = [_src_row("shared"), _excerpt_row("shared", eid="t2"),
            _excerpt_row("shared", eid="t3")]
    merged = rrf_merge(
        [("autonomy", own), ("anchore", peer)],
        limit=5, own_org="autonomy", key="source_id",
    )
    # Only one source — but we keep its row payloads from the FIRST
    # org to surface it (own_org wins the tie).
    sids = {r["source_id"] for r in merged}
    assert sids == {"shared"}
    # Score = own (rank 1, boost 1.5) + peer (rank 1, boost 1.0).
    expected = OWN_ORG_BOOST / (RRF_K + 1) + 1.0 / (RRF_K + 1)
    for row in merged:
        assert row["rrf_score"] == pytest.approx(expected)
    # Own-org row payloads survive (peer's excerpt count is 2 vs own's
    # 1 — the merge keeps own's shape for content fidelity).
    assert all(r.get("org") == "autonomy" for r in merged)


def test_chronological_merge_source_level_limit():
    """``chronological_merge`` truncates at the distinct-source level —
    not raw rows. A 30-hit session contributes one source slot; its
    rows ride together."""
    a_rows = [
        # Source A: head + 1 excerpt, all share source_created_at
        {"source_id": "A", "id": "A", "source_created_at": "2026-04-29T10:00:00Z", "content": "headA"},
        {"source_id": "A", "id": "A-t1", "source_created_at": "2026-04-29T10:00:00Z", "content": "excerptA"},
        # Source B
        {"source_id": "B", "id": "B", "source_created_at": "2026-04-28T10:00:00Z", "content": "headB"},
    ]
    b_rows = [
        # Source C (most recent)
        {"source_id": "C", "id": "C", "source_created_at": "2026-04-30T10:00:00Z", "content": "headC"},
        {"source_id": "C", "id": "C-t1", "source_created_at": "2026-04-30T10:00:00Z", "content": "excerptC"},
    ]
    merged = chronological_merge(
        [("autonomy", a_rows), ("anchore", b_rows)],
        limit=2, time_field="source_created_at", key="source_id",
    )
    # Limit=2 → two distinct sources: C (newest) and A. B is dropped.
    sids = []
    seen: set = set()
    for r in merged:
        sid = r["source_id"]
        if sid not in seen:
            seen.add(sid)
            sids.append(sid)
    assert sids == ["C", "A"]
    # Both rows of A ride; both rows of C ride.
    assert sum(1 for r in merged if r["source_id"] == "A") == 2
    assert sum(1 for r in merged if r["source_id"] == "C") == 2
    # B is excluded.
    assert all(r["source_id"] != "B" for r in merged)


def test_chronological_merge_falls_back_to_id_when_no_source_id():
    """Legacy callers passing rows without ``source_id`` still get
    per-row inclusion via the ``id`` fallback — preserves the
    pre-Round-7l shape for direct ``cross_org.chronological_merge``
    callers that haven't yet adopted source-aware rows."""
    a_rows = [
        {"id": "a1", "created_at": "2026-04-21T10:00:00Z"},
        {"id": "a2", "created_at": "2026-04-19T10:00:00Z"},
    ]
    b_rows = [
        {"id": "b1", "created_at": "2026-04-20T10:00:00Z"},
    ]
    merged = chronological_merge(
        [("autonomy", a_rows), ("anchore", b_rows)], limit=10,
    )
    assert [r["id"] for r in merged] == ["a1", "b1", "a2"]
