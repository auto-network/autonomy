#!/usr/bin/env python3
"""Collect bounded organization-scoped Agent Test statistics; never runs tests."""

from __future__ import annotations

import argparse
import json
import os
import ssl
import urllib.parse
import urllib.request
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dashboard", default=os.environ.get("AGENT_TEST_DASHBOARD", "https://localhost:8080"))
    args = parser.parse_args()
    limit = max(1, min(args.limit, 50))
    query = urllib.parse.urlencode({
        "repository": args.repository,
        "recent_limit": min(limit, 20),
        "ranked_limit": limit,
    })
    request = urllib.request.Request(
        f"{args.dashboard.rstrip('/')}/api/plugins/testing/summary?{query}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {os.environ['CROSSTALK_TOKEN']}",
        },
    )
    with urllib.request.urlopen(request, context=ssl._create_unverified_context(), timeout=10) as response:
        payload = json.loads(response.read().decode())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote bounded Agent Test statistics to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
