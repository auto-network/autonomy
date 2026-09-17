"""``graph maintenance drop-retired-tables`` — the housekeeping half of the
2026-09-08 entities/entity_mentions retirement, as a verb.

The correctness change was their DERIVED policy (58ce99d4): the tables stop
replicating with no DDL at all. Physically dropping them and purging their
winner-catalog addresses is housekeeping that ``GraphDB.drop_retired_entity_
tables`` runs in bounded batches, deliberately never on the open path
(4a6ea64a: running it there held the write lock past every opener's timeout
and took the graph API down). It has to be invoked on purpose, per store.

Until now that meant a ``python -c`` one-liner. On SJC it was never run:
570,450 of its 730,008 personal catalog rows were retired-table addresses,
``--verify-catalog`` failed on the first one, and the doctor's canary read
as a six-to-one corruption (2026-09-17). This verb is the sanctioned way.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Any

from ..db import GraphDB

logger = logging.getLogger(__name__)

DEFAULT_BATCH = 20_000


@dataclass
class DropReport:
    by_store: dict[str, dict] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def addresses_purged(self) -> int:
        return sum(int(r.get("addresses_purged") or 0) for r in self.by_store.values())


def run_drop_retired(*, org: str | None = None, batch: int = DEFAULT_BATCH) -> DropReport:
    """Drop the retired tables and purge their catalog addresses in every
    store, or in *org* alone. Each store is its own bounded operation; a
    failure in one is recorded and the next store still runs. Idempotent."""
    from ..cross_org import all_store_slugs

    report = DropReport()
    if org is not None:
        if org not in all_store_slugs():
            raise SystemExit(f"unknown store slug: {org!r}")
        slugs = [org]
    else:
        slugs = sorted(all_store_slugs())
    for slug in slugs:
        try:
            db = GraphDB.for_org(slug, mode="rw")
            result = db.drop_retired_entity_tables(batch=batch)
            report.by_store[slug] = result
            logger.info("drop-retired-tables %s: %s", slug, result)
        except Exception as exc:  # noqa: BLE001 — one store must not stop the rest
            report.errors[slug] = f"{type(exc).__name__}: {exc}"
            logger.warning("drop-retired-tables %s failed: %s", slug, exc)
    return report


def cmd_drop_retired(args: Any) -> None:
    """``graph maintenance drop-retired-tables`` argparse callback."""
    if not logger.handlers and not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    report = run_drop_retired(
        org=getattr(args, "org", None),
        batch=getattr(args, "batch", DEFAULT_BATCH),
    )
    print(json.dumps({
        "addresses_purged": report.addresses_purged,
        "by_store": report.by_store,
        "errors": report.errors,
    }, sort_keys=True))
