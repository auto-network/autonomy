"""The one-time signing pass over unsigned organization settings rows
(auto-qrmlg.6 S4). TEMPORARY: delete this module, its route and its unlock
hook once every founded store on the fleet reports zero unsigned rows and
``materialize.REQUIRE_SIGNED_ORG_ROWS`` has been switched on.

Every row in an organization database is signed (design of record
graph://21a0da9e-1c2); the population that predates S2 is not. This pass
signs each row IN PLACE with the key its set declares — the node's storage
delegate for a ``delegate``-signer set (D7: unattended), and never for a
``persona``-signer set (D8: only the persona may, inside the operator's
unlocked session; none is declared today, and such rows are reported, not
signed). The write goes through ``settings_ops._envelope_columns``, so the
same boundary that guards a fresh write guards the migration: a row the
fold refuses is left unsigned and counted by reason.

Runs where the audited delegate is warm: inside the dashboard process,
after an unlock (hook in unlock_routes) on the fleet's singular-ownership
machine, or on demand through ``POST /api/settings/signing-pass`` with
operator authority. Idempotent: only rows with no signature are touched,
so a second run costs one query per store.

An UPDATE that signs a row moves its logical address into the signer's
slot, which replicates as a tombstone of the unsigned address plus an
insert at the signed one under the same row id (materialize._delete /
_upsert; tested in test_signed_settings_boundary). Rows are signed in
chunks of ``CHUNK_ROWS`` per transaction because a replicated transaction
holds at most catalog.MAX_TRANSACTION_OPERATIONS operations (two per row
here). Chunking is safe today because unsigned rows still resolve; the
resolution rule that makes an unsigned organization row ineligible arrives
with the flag, after this pass has completed everywhere.

Ledger-event rows (``autonomy.org.ledger-event``) are never signed here:
they are self-signed in their wire and are what the fold is built from,
and the apply-side unsigned refusal exempts them for the same reason.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from tools.graph import settings_ops
from tools.network.fleet_sync.materialize import LEDGER_EVENT_SET_ID
from tools.network.settingskit.envelope import EnvelopeFormatError

logger = logging.getLogger(__name__)

#: Rows per replicated transaction: 2 operations per row, well under
#: catalog.MAX_TRANSACTION_OPERATIONS (16,384).
CHUNK_ROWS = 4_000

#: The unlock hook is OPT-IN. The vault hot-reload path (a worker respawn)
#: takes the same seam as an unlock, so with this on, every restart of the
#: dashboard started the pass on the node unasked — on Home at 19:16:43Z
#: on 2026-09-29, right after the outage the first run had caused. Off,
#: the pass runs only through the operator-authority route; the host flips
#: this on for a node once the pass has been measured there.
RUN_AFTER_UNLOCK = os.environ.get("AUTONOMY_SETTINGS_SIGNING_PASS_AFTER_UNLOCK", "") == "1"


class _OwnStore:
    """The pass's PRIVATE connection to the organization store: never the
    dashboard's pooled GraphDB handle. Live 2026-09-29 19:10-19:16Z: the
    first dry run ran on the pooled connection, so every request handler
    that read the same store queued behind 37,000 short statements, the
    event loop's own synchronous reads stalled 283 s, and Home's dashboard
    stopped answering until the worker was killed. A private connection
    shares only the FILE with the rest of the process: WAL readers never
    wait on it, and its short write transactions are the only thing a
    concurrent writer waits for. Opened the way the sync scheduler opens
    a store (SQLiteFleetSyncStore._open): with the capture catalog
    attached, so an UPDATE replicates."""

    def __init__(self, path):
        import sqlite3

        from tools.graph import sqlite_defaults
        from tools.network.fleet_sync.catalog import attach_active_production_catalog
        from tools.network.fleet_sync_connection import FleetSyncConnection

        self.path = path
        self.conn = sqlite3.connect(str(path), factory=FleetSyncConnection, timeout=30.0)
        sqlite_defaults.apply(self.conn)
        self.conn.row_factory = sqlite3.Row
        # None on a store whose fleet writers are not activated: writes
        # then replicate nowhere, which is that store's existing state.
        self.catalog = attach_active_production_catalog(self.conn)

    def close(self) -> None:
        self.conn.close()


def sign_org_store(org: str, *, apply: bool = True, chunk_rows: int = CHUNK_ROWS,
                   pause_s: float = 0.0) -> dict[str, Any]:
    """Sign every unsigned settings row of the founded organization store
    *org* with this process's signer for it. Returns a report:
    ``{org, founded, unsigned, signed, persona_tier, refused: {reason: n},
    no_signer, transactions, seconds}``. With ``apply=False`` nothing is
    written and the report says what would be. *pause_s* sleeps between
    chunks so a concurrent writer gets the file between them.

    Per-row work is the envelope only (canonicalize and sign, ~0.2 ms):
    the genesis, the signing context and the fold are looked up once per
    chunk (settings_ops.prepare_signer), on this pass's private
    connection.
    """
    from tools.graph.schemas.registry import declared_signer

    started = time.perf_counter()
    report: dict[str, Any] = {
        "org": org, "founded": False, "unsigned": 0, "signed": 0, "persona_tier": 0,
        "refused": {}, "no_signer": False, "transactions": 0, "apply": bool(apply),
    }
    from pathlib import Path

    path = settings_ops._db_path(org)
    if not path or not Path(path).is_file():
        report["seconds"] = round(time.perf_counter() - started, 3)
        return report
    db = _OwnStore(path)
    try:
        if settings_ops._store_is_founded(db) is None:
            return report
        report["founded"] = True
        ids = [str(r[0]) for r in db.conn.execute(
            "SELECT id FROM settings WHERE signature IS NULL AND set_id != ? ORDER BY rowid",
            (LEDGER_EVENT_SET_ID,),
        )]
        report["unsigned"] = len(ids)
        if not ids:
            return report
        for offset in range(0, len(ids), chunk_rows):
            chunk_ids = ids[offset:offset + chunk_rows]
            prepared = settings_ops.prepare_signer(db, org)
            if prepared.context is None:
                # No signer held for this organization in this process:
                # nothing was, or will be, written by this call.
                report["no_signer"] = True
                break
            placeholders = ",".join("?" * len(chunk_ids))
            rows = db.conn.execute(
                "SELECT id, set_id, schema_revision, key, payload, publication_state, "
                f"deprecated, successor_id FROM settings WHERE id IN ({placeholders}) "
                "AND signature IS NULL", chunk_ids,
            ).fetchall()
            pending: list[tuple] = []
            for row in rows:
                row_id, set_id, revision, key, payload, state, deprecated, successor_id = (
                    str(row[0]), str(row[1]), int(row[2]), str(row[3]), row[4], str(row[5]),
                    bool(row[6]), row[7],
                )
                if declared_signer(set_id, revision) == "persona":
                    report["persona_tier"] += 1
                    continue
                try:
                    stored_payload = json.loads(payload) if isinstance(payload, str) else payload
                except ValueError:
                    report["refused"]["payload_unreadable"] = report["refused"].get("payload_unreadable", 0) + 1
                    continue
                try:
                    envelope = settings_ops._envelope_columns(
                        db, org, set_id, revision, key, state, stored_payload,
                        deprecated=deprecated, successor_id=successor_id, prepared=prepared,
                    )
                except settings_ops.SettingsSignerRefused as refused:
                    report["refused"][refused.reason] = report["refused"].get(refused.reason, 0) + 1
                    continue
                except EnvelopeFormatError as exc:
                    # A row the envelope cannot encode (a non-finite float, a
                    # foreign type): left unsigned and named, never the whole
                    # store's failure.
                    reason = "envelope:" + str(exc)[:80]
                    report["refused"][reason] = report["refused"].get(reason, 0) + 1
                    continue
                report["signed"] += 1
                pending.append((*envelope, row_id))
            if pending and apply:
                db.conn.execute("BEGIN IMMEDIATE")
                try:
                    db.conn.executemany(
                        "UPDATE settings SET signed_at=?, signing_key=?, signature=?, "
                        "witness=?, terminal_persona=? WHERE id=? AND signature IS NULL",
                        pending,
                    )
                    db.conn.commit()
                except BaseException:
                    db.conn.rollback()
                    raise
                report["transactions"] += 1
            elif pending:
                report["transactions"] += 1   # what apply would commit
            if pause_s and offset + chunk_rows < len(ids):
                time.sleep(pause_s)
        return report
    finally:
        db.close()
        report["seconds"] = round(time.perf_counter() - started, 3)


def org_slugs() -> list[str]:
    """Every organization this node holds a store for, personal and machine
    and followed mirrors excluded (a followed mirror holds no persona)."""
    from tools.graph import org_ops

    try:
        return [ref.slug for ref in org_ops.list_orgs()
                if ref.slug not in ("personal", "machine") and ref.type != "followed"]
    except Exception:
        return []


def run(*, apply: bool = True, orgs: list[str] | None = None, pause_s: float = 0.05) -> list[dict[str, Any]]:
    """The pass over every organization store (or *orgs*)."""
    reports = []
    for slug in (orgs if orgs is not None else org_slugs()):
        try:
            reports.append(sign_org_store(slug, apply=apply, pause_s=pause_s))
        except Exception as exc:   # one store's failure must not stop the rest
            logger.warning("settings signing pass: %r failed", slug, exc_info=True)
            reports.append({"org": slug, "error": f"{type(exc).__name__}: {exc}"})
    return reports


def run_after_unlock() -> None:
    """The unlock hook: on the fleet's singular-ownership machine only, sign
    what is unsigned. Best-effort; never raises into the unlock."""
    if not RUN_AFTER_UNLOCK:
        logger.info("settings signing pass: unlock hook is off (AUTONOMY_SETTINGS_SIGNING_PASS_AFTER_UNLOCK != 1)")
        return
    try:
        from tools.network import fleet_tunnel_server

        eligibility = fleet_tunnel_server.state()
        if not eligibility.allowed:
            logger.info(
                "settings signing pass: skipped, not the singular-ownership machine (reason=%s)",
                getattr(eligibility, "reason", None),
            )
            return
        reports = run(apply=True)
        touched = [r for r in reports if r.get("signed") or r.get("refused") or r.get("persona_tier") or r.get("error")]
        if touched:
            logger.warning("settings signing pass after unlock: %s", json.dumps(touched, default=str)[:4000])
        else:
            logger.info("settings signing pass after unlock: nothing unsigned in %d store(s)", len(reports))
    except Exception:
        logger.warning("settings signing pass after unlock failed", exc_info=True)
