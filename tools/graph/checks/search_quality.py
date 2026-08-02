#!/usr/bin/env python3
"""Run the curated, privacy-safe graph-search relevance benchmark.

This tool deliberately consumes checked-in relevance judgments instead of
production query/click logs. It exercises the real ``graph search`` transport,
including per-org routing, then reports MRR, Recall@K, and nDCG@K.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess
from typing import Callable


DEFAULT_CASES = Path(__file__).with_name("search_quality_cases.json")


def distinct_source_ids(rows: list[dict]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for row in rows:
        sid = row.get("source_id") or row.get("id")
        if sid and sid not in seen:
            seen.add(sid)
            ordered.append(sid)
    return ordered


def relevance_grade(source_id: str, relevant: dict[str, int]) -> int:
    return next(
        (grade for prefix, grade in relevant.items()
         if source_id.startswith(prefix)),
        0,
    )


def score_ranking(
    ranked_ids: list[str], relevant: dict[str, int], *, cutoff: int,
) -> dict[str, float | int | None]:
    relevant_ranks = [
        index + 1
        for index, sid in enumerate(ranked_ids)
        if relevance_grade(sid, relevant) > 0
    ]
    first_rank = min(relevant_ranks) if relevant_ranks else None
    reciprocal_rank = 1.0 / first_rank if first_rank else 0.0

    found_prefixes = {
        prefix
        for prefix in relevant
        for sid in ranked_ids[:cutoff]
        if sid.startswith(prefix)
    }
    recall = len(found_prefixes) / len(relevant) if relevant else 0.0

    gains = [
        (2 ** relevance_grade(sid, relevant)) - 1
        for sid in ranked_ids[:cutoff]
    ]
    dcg = sum(
        gain / math.log2(index + 2)
        for index, gain in enumerate(gains)
    )
    ideal_gains = sorted(
        ((2 ** grade) - 1 for grade in relevant.values()), reverse=True,
    )[:cutoff]
    ideal_dcg = sum(
        gain / math.log2(index + 2)
        for index, gain in enumerate(ideal_gains)
    )
    ndcg = dcg / ideal_dcg if ideal_dcg else 0.0
    return {
        "first_relevant_rank": first_rank,
        "reciprocal_rank": reciprocal_rank,
        "recall": recall,
        "ndcg": ndcg,
    }


def evaluate_cases(
    cases: list[dict],
    search: Callable[[str], list[str]],
    *,
    cutoff: int = 10,
) -> dict:
    rows = []
    for case in cases:
        ranked = search(case["query"])
        score = score_ranking(ranked, case["relevant"], cutoff=cutoff)
        rows.append({"query": case["query"], **score})
    count = len(rows)
    aggregate = {
        "cases": count,
        "mrr": sum(r["reciprocal_rank"] for r in rows) / count,
        f"recall@{cutoff}": sum(r["recall"] for r in rows) / count,
        f"ndcg@{cutoff}": sum(r["ndcg"] for r in rows) / count,
    }
    return {"aggregate": aggregate, "results": rows}


def graph_search(
    query: str, *, org: str, limit: int, ranker: str,
) -> list[str]:
    command = [
        "graph", "search", query,
        "--only-org", org,
        "--limit", str(limit),
        "--ranker", ranker,
        "--json",
    ]
    completed = subprocess.run(
        command, text=True, capture_output=True, check=True,
    )
    rows = json.loads(completed.stdout)
    return distinct_source_ids(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--org", default="autonomy")
    parser.add_argument("--ranker", choices=("legacy", "smart"), default="legacy")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--cutoff", type=int, default=10)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    manifest = json.loads(args.cases.read_text())
    result = evaluate_cases(
        manifest["cases"],
        lambda query: graph_search(
            query, org=args.org, limit=args.limit, ranker=args.ranker,
        ),
        cutoff=args.cutoff,
    )
    result["ranker"] = args.ranker
    result["org"] = args.org
    if args.as_json:
        print(json.dumps(result, indent=2))
        return 0

    for row in result["results"]:
        rank = row["first_relevant_rank"] or "-"
        print(
            f"{str(rank):>3}  RR={row['reciprocal_rank']:.3f}  "
            f"R@{args.cutoff}={row['recall']:.3f}  "
            f"nDCG={row['ndcg']:.3f}  {row['query']}"
        )
    aggregate = result["aggregate"]
    print(
        f"\n{args.ranker}: MRR={aggregate['mrr']:.3f}  "
        f"Recall@{args.cutoff}={aggregate[f'recall@{args.cutoff}']:.3f}  "
        f"nDCG@{args.cutoff}={aggregate[f'ndcg@{args.cutoff}']:.3f}  "
        f"({aggregate['cases']} cases)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
