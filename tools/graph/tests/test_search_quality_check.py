from __future__ import annotations

import pytest

from tools.graph.checks.search_quality import (
    distinct_source_ids,
    evaluate_cases,
    score_ranking,
)


def test_distinct_source_ids_preserves_first_seen_order():
    rows = [
        {"source_id": "a"},
        {"source_id": "a", "id": "a-turn"},
        {"source_id": "b"},
    ]
    assert distinct_source_ids(rows) == ["a", "b"]


def test_score_ranking_computes_rr_recall_and_ndcg():
    score = score_ranking(
        ["noise", "primary-123", "companion-123"],
        {"primary": 3, "companion": 1},
        cutoff=2,
    )
    assert score["first_relevant_rank"] == 2
    assert score["reciprocal_rank"] == 0.5
    assert score["recall"] == 0.5
    assert 0 < score["ndcg"] < 1


def test_evaluate_cases_aggregates_metrics():
    cases = [
        {"query": "one", "relevant": {"a": 3}},
        {"query": "two", "relevant": {"b": 3}},
    ]
    rankings = {"one": ["a1"], "two": ["noise", "b1"]}
    result = evaluate_cases(cases, rankings.__getitem__, cutoff=10)
    assert result["aggregate"]["mrr"] == pytest.approx(0.75)
    assert result["aggregate"]["recall@10"] == 1.0
    assert result["aggregate"]["ndcg@10"] < 1.0
