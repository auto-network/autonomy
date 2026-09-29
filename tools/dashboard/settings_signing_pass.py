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
import time
from typing import Any

from tools.graph import settings_ops
from tools.network.fleet_sync.materialize import LEDGER_EVENT_SET_ID
from tools.network.settingskit.envelope import EnvelopeFormatError

logger = logging.getLogger(__name__)

#: Rows per replicated transaction: 2 operations per row, well under
#: catalog.MAX_TRANSACTION_OPERATIONS (16,384).
CHUNK_ROWS = 4_000


def sign_org_store(org: str, *, apply: bool = True, chunk_rows: int = CHUNK_ROWS) -> dict[str, Any]:
    """Sign every unsigned settings row of the founded organization store
    *org* with this process's signer for it. Returns a report:
    ``{org, founded, unsigned, signed, persona_tier, refused: {reason: n},
    no_signer, transactions, seconds}``. With ``apply=False`` nothing is
    written and the report says what would be."""
    from tools.graph.schemas.registry import declared_signer

    started = time.perf_counter()
    report: dict[str, Any] = {
        "org": org, "founded": False, "unsigned": 0, "signed": 0, "persona_tier": 0,
        "refused": {}, "no_signer": False, "transactions": 0, "apply": bool(apply),
    }
    db = settings_ops._open(org)
    try:
        if settings_ops._store_is_founded(db) is None:
            return report
        report["founded"] = True
        rows = db.conn.execute(
            "SELECT id, set_id, schema_revision, key, payload, publication_state, "
            "deprecated, successor_id FROM settings WHERE signature IS NULL "
            "AND set_id != ? ORDER BY rowid", (LEDGER_EVENT_SET_ID,),
        ).fetchall()
        report["unsigned"] = len(rows)
        if not rows:
            return report
        pending: list[tuple] = []

        def flush() -> None:
            if not pending or not apply:
                pending.clear()
                return
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
            pending.clear()

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
                    deprecated=deprecated, successor_id=successor_id,
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
            if envelope[1] is None:
                # No signer held for this organization in this process:
                # nothing was, or will be, written by this call.
                report["no_signer"] = True
                pending.clear()
                break
            report["signed"] += 1
            pending.append((*envelope, row_id))
            if len(pending) >= chunk_rows:
                flush()
        flush()
        if not apply:
            report["transactions"] = -(-report["signed"] // max(1, chunk_rows)) if report["signed"] else 0
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


def run(*, apply: bool = True, orgs: list[str] | None = None) -> list[dict[str, Any]]:
    """The pass over every organization store (or *orgs*)."""
    reports = []
    for slug in (orgs if orgs is not None else org_slugs()):
        try:
            reports.append(sign_org_store(slug, apply=apply))
        except Exception as exc:   # one store's failure must not stop the rest
            logger.warning("settings signing pass: %r failed", slug, exc_info=True)
            reports.append({"org": slug, "error": f"{type(exc).__name__}: {exc}"})
    return reports


def run_after_unlock() -> None:
    """The unlock hook: on the fleet's singular-ownership machine only, sign
    what is unsigned. Best-effort; never raises into the unlock."""
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
