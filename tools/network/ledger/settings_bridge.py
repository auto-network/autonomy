"""Ledger events ride Settings (bead auto-dqemk, operator ruling 2026-09-06).

Two one-way pumps between an org's ``LedgerStore`` and the org-homed
append-only set ``autonomy.org.ledger-event#1``:

* **publish** — every event in the store gets a row keyed by its event id,
  payload ``{"wire": <canonical JSON, verbatim>}``. ``LedgerStore.append``
  calls :func:`publish_event` best-effort after its own transaction commits
  (a row write must never fail a ledger append); :func:`reconcile` backfills
  anything missed, idempotent by key.
* **ingest** — rows whose event id the store does not hold are fed through
  ``LedgerStore.append_wire``, which re-verifies the content hash and the
  embedded author signature and maintains this store's own parents/heads
  indexes. Delivery order does not matter: unappendable rows (parent not yet
  arrived) are retried each pass until a pass makes no progress.

The set is the TRANSPORT, never the store — the ledger tables stay LOCAL in
fleet-sync policy and are rebuilt per node by ``absorb``/``append_wire``.
A store with no genesis ignores foreign rows entirely: founding arrives via
the join flow, never from replicated rows.

Production wiring: the fleet-sync scheduler calls :func:`reconcile` after
each successful org-scope pull (the post-sync sweep the bead names), and
``LedgerStore.append`` publishes fresh events inline.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SET_ID = "autonomy.org.ledger-event"
REVISION = 1


def _existing_keys(slug: str) -> set[str]:
    from tools.graph import settings_ops

    members = settings_ops.read_owned_set(SET_ID, org=slug).members
    return {str(m.key) for m in members}


def publish_event(slug: str, event_id: str, wire: str) -> bool:
    """Write one event's row; True on write, False when it already exists
    or the write is impossible here (no org DB, schema mismatch). Never
    raises — callers on the append path must not fail on transport."""
    try:
        from tools.graph import settings_ops

        if event_id in _existing_keys(slug):
            return False
        settings_ops.add_setting(
            SET_ID, REVISION, event_id, {"wire": wire},
            org=slug, state="published",
        )
        return True
    except Exception:
        logger.debug("ledger-event row publish skipped for %s", slug, exc_info=True)
        return False


def reconcile(slug: str) -> dict:
    """Publish missing rows for local events; absorb foreign rows into the
    local store. Returns ``{"published": n, "absorbed": n, "unappendable": n}``.

    Safe to run any time; every step is idempotent. A store that does not
    exist or has no genesis publishes nothing and absorbs nothing.
    """
    from tools.graph import settings_ops
    from .store import LedgerStore, org_ledger_db_path

    report = {"published": 0, "absorbed": 0, "unappendable": 0}
    path = org_ledger_db_path(slug)
    if not path.exists():
        return report
    # Read-only probe before any LedgerStore open: opening a store CREATES
    # the ledger tables and writes a WAL/meta row, which must not happen to
    # a database that never held a ledger (and must not take a write lock
    # on a file the sync engine may be mid-install on).
    import sqlite3

    try:
        probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            has_ledger = probe.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='ledger_events' LIMIT 1"
            ).fetchone() is not None
        finally:
            probe.close()
    except sqlite3.Error:
        return report
    if not has_ledger:
        return report
    with LedgerStore(path) as store:
        if store.ledger.genesis_id is None:
            return report
        rows = {
            str(m.key): (m.payload or {}).get("wire")
            for m in settings_ops.read_owned_set(SET_ID, org=slug).members
        }
        # Publish local events the set lacks.
        for event in store.events():
            if event.event_id not in rows:
                if publish_event(slug, event.event_id, event.to_json().decode("utf-8")):
                    report["published"] += 1
        # Absorb foreign rows the store lacks; retry until a pass makes no
        # progress so parents arriving in any order still converge.
        pending = {
            key: wire for key, wire in rows.items()
            if wire and key not in store.ledger
        }
        while pending:
            progressed = []
            for key, wire in pending.items():
                try:
                    store.append_wire(wire.encode("utf-8"))
                except Exception:
                    continue
                progressed.append(key)
                report["absorbed"] += 1
            if not progressed:
                break
            for key in progressed:
                pending.pop(key, None)
        report["unappendable"] = len(pending)
        if pending:
            logger.warning(
                "ledger-event ingest for %s left %d row(s) unappendable "
                "(bad wire or parents never arrived)", slug, len(pending),
            )
    return report
