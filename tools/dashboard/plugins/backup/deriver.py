"""Backup staleness/failure derivation → Central attention (auto-fnydv).

Doctrine (graph://c9fa6f75-481): assert the invariant, not the
activity. Every condition here derives from PERSISTED run rows — the
age of the newest complete capture, the verdict of the newest run, the
offsite verdict — so a dead cron, an unmounted NAS, and a wedged engine
all surface identically, from the dashboard process, which is the one
place that is still alive when the capture path is not.

Conditions and their identities (attention ids are stable; object_ref
is frozen per id by the index, so both use the tier, never the run):

- ``backup.failed``  id ``backup:failed:<tier>``  — the tier's newest
  run failed. Versioned by the run stamp; the next complete run emits
  the same id ``resolved`` at its own (higher) stamp.
- ``backup.stale``   id ``backup:stale:<tier>``   — no complete capture
  within staleness_multiple × interval. Versioned by wall clock;
  resolves the same way when a fresh capture lands.
- ``backup.offsite_unreachable`` id ``backup:offsite`` — the newest
  complete run's offsite verdict is ``failed``. ``skipped`` (not
  configured) is a choice, not an outage, and raises nothing.

``restore_drill_failed`` belongs to the drill bead (auto-mu7qf), which
reuses ``publish_conditions`` with its own condition rows.

A resolved condition is emitted ONLY while its item is currently open —
otherwise every healthy cycle would mint resolved items for failures
that never happened.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from tools.dashboard.attention_registry import AttentionIndexError
from tools.dashboard.plugins.backup.attention import APPLICATION_SCOPE
from tools.dashboard.plugins.backup.entrypoints.api import (
    _parse_at,
    tier_health,
)
from tools.dashboard.plugins.backup.entrypoints.schemas import TIERS

logger = logging.getLogger(__name__)

#: Cycle cadence for the background task. Runs are hourly; two minutes
#: keeps S1's <=5-minute visibility bound with margin to spare.
DERIVE_INTERVAL_S = 120.0


def _stamp_version(key: str | None) -> int:
    """Monotonic version from a run key's stamp (``tier:YYYYMMDD-HHMMSS``)."""
    if not key:
        return 0
    digits = "".join(ch for ch in key.split(":", 1)[-1] if ch.isdigit())
    try:
        return int(digits)
    except ValueError:
        return 0


def _occurred(run: dict | None, now: datetime) -> float:
    if run:
        at = _parse_at(run.get("finished_at") or run.get("started_at") or "")
        if at:
            return at.timestamp()
    return now.timestamp()


def _condition(kind: str, attention_id: str, object_ref: str, state: str,
               title: str, summary: str, occurred_at: float,
               version: int) -> dict:
    return {
        "kind": kind,
        "attention_id": attention_id,
        "object_ref": object_ref,
        "attention_state": state,
        "safe_title": title,
        "safe_summary": summary,
        "occurred_at": occurred_at,
        "source_version": version,
    }


def _age_text(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def is_backup_source(runs: list[dict], config: dict,
                     reports_exist: bool | None = None) -> bool:
    """Does this machine show any evidence of being a backup SOURCE?

    STOPGAP (2026-09-08) for the fleet-machine false alarm, ahead of the
    real designation primitive (auto-2fz3b / auto-l1n6t): the plugin's
    enable row is org-homed and therefore SYNCS, so every fleet machine
    switches the plugin on, finds zero run rows, and alarms "backup is
    stale" about a machine that was never meant to back anything up —
    with unscoped attention ids that then collide fleet-wide.

    Evidence, any one of which means "this machine is supposed to back
    up": it has captured at least once, an offsite provider is
    configured here, or the capture engine's report directory exists.
    A machine with NONE of these has no backup configuration at all, so
    its silence is a true statement, not a suppressed failure. A real
    source keeps alarming: the moment a provider is configured or one
    run lands, evidence exists forever after (run rows are retained and
    the report directory persists), so this can never quiet a machine
    that has ever backed up.
    """
    if runs:
        return True
    if (config.get("offsite_provider") or "").strip():
        return True
    if reports_exist is None:
        from tools.dashboard.plugins.backup import reconcile as reconcile_mod
        try:
            reports_exist = reconcile_mod.default_report_root().is_dir()
        except Exception:
            reports_exist = False
    return bool(reports_exist)


def derive_conditions(runs: list[dict], config: dict,
                      now: datetime | None = None,
                      reports_exist: bool | None = None) -> list[dict]:
    """Pure derivation: run rows + config → attention condition rows.

    Emits BOTH directions (needs_attention and resolved) for every
    condition it evaluates; ``publish_conditions`` drops resolved rows
    whose item is not currently open.
    """
    if not is_backup_source(runs, config, reports_exist):
        return []
    now = now or datetime.now(timezone.utc)
    conditions: list[dict] = []
    newest_complete_overall: dict | None = None
    for tier in TIERS:
        health = tier_health(runs, config, tier, now=now)
        newest = health.get("last_run")
        stale = (health["age_seconds"] is None
                 or health["age_seconds"] > health["stale_after_seconds"])
        failing = bool(newest) and newest.get("verdict") == "failed"

        if failing:
            reasons = newest.get("failures") or ["unknown failure"]
            conditions.append(_condition(
                "backup.failed", f"backup:failed:{tier}",
                f"backup:tier:{tier}", "needs_attention",
                f"{tier.capitalize()} backup failed",
                f"{newest.get('key')}: {reasons[0]}",
                _occurred(newest, now), _stamp_version(newest.get("key")),
            ))
        elif newest:
            conditions.append(_condition(
                "backup.failed", f"backup:failed:{tier}",
                f"backup:tier:{tier}", "resolved",
                f"{tier.capitalize()} backup recovered",
                f"{newest.get('key')} completed",
                _occurred(newest, now), _stamp_version(newest.get("key")),
            ))

        threshold = _age_text(health["stale_after_seconds"])
        if stale:
            # Wall-clock versioned, so re-raising after a resolve works;
            # skip_if_open stops a still-stale tier from rewriting the
            # item every cycle just to bump the age text.
            row = _condition(
                "backup.stale", f"backup:stale:{tier}",
                f"backup:tier:{tier}", "needs_attention",
                f"{tier.capitalize()} backup is stale",
                f"No complete capture for "
                f"{_age_text(health['age_seconds'])} "
                f"(threshold {threshold})",
                now.timestamp(), int(now.timestamp()),
            )
            row["skip_if_open"] = True
            conditions.append(row)
        else:
            conditions.append(_condition(
                "backup.stale", f"backup:stale:{tier}",
                f"backup:tier:{tier}", "resolved",
                f"{tier.capitalize()} backup is current",
                f"Last complete capture "
                f"{_age_text(health['age_seconds'])} ago",
                now.timestamp(), int(now.timestamp()),
            ))

        tier_complete = [r for r in runs
                         if r.get("key", "").startswith(f"{tier}:")
                         and r.get("verdict") == "complete"]
        for run in tier_complete:
            if (newest_complete_overall is None
                    or _stamp_version(run.get("key"))
                    > _stamp_version(newest_complete_overall.get("key"))):
                newest_complete_overall = run

    if newest_complete_overall is not None:
        offsite = newest_complete_overall.get("offsite")
        version = _stamp_version(newest_complete_overall.get("key"))
        occurred = _occurred(newest_complete_overall, now)
        if offsite == "failed":
            conditions.append(_condition(
                "backup.offsite_unreachable", "backup:offsite",
                "backup:offsite", "needs_attention",
                "Offsite backup push failed",
                f"{newest_complete_overall.get('key')}: restic push failed "
                f"(local capture is intact)",
                occurred, version,
            ))
        elif offsite == "complete":
            conditions.append(_condition(
                "backup.offsite_unreachable", "backup:offsite",
                "backup:offsite", "resolved",
                "Offsite backup push recovered",
                f"{newest_complete_overall.get('key')} pushed offsite",
                occurred, version,
            ))
    return conditions


def publish_conditions(index, conditions: list[dict]) -> dict:
    """Publish condition rows through the sealed-producer seam.

    Resolved rows publish only when their item is currently open;
    stale-version and identity errors are contained per condition so
    one bad row cannot silence the rest.
    """
    published: list[str] = []
    skipped: list[str] = []
    errors: dict[str, str] = {}
    for condition in conditions:
        attention_id = condition["attention_id"]
        try:
            current = index.store.get_item(attention_id)
        except Exception:
            current = None
        payload = getattr(current, "payload", None) or {}
        if condition["attention_state"] == "resolved":
            if payload.get("attention_state") != "needs_attention":
                skipped.append(attention_id)
                continue
            # A resolve must always advance past the stored version, or
            # a raise and its clear inside the same wall-clock second
            # can never transition the item.
            try:
                stored_version = int(payload.get("source_version") or 0)
            except (TypeError, ValueError):
                stored_version = 0
            condition["source_version"] = max(
                int(condition["source_version"]), stored_version + 1)
        elif condition.pop("skip_if_open", False) \
                and payload.get("attention_state") == "needs_attention":
            skipped.append(attention_id)
            continue
        elif (payload.get("attention_state") == condition["attention_state"]
                and payload.get("source_version")
                == condition["source_version"]):
            # Same fact at the same version: a rewrite would only churn
            # the settings store every cycle.
            skipped.append(attention_id)
            continue
        condition.pop("skip_if_open", None)
        try:
            producer = index.registry.producer(
                condition["kind"], APPLICATION_SCOPE)
            index.publish(producer, condition)
            published.append(attention_id)
        except AttentionIndexError as exc:
            if exc.code == "stale_source":
                skipped.append(attention_id)  # already at this version
            else:
                errors[attention_id] = exc.code
        except Exception as exc:  # never let one row kill the cycle
            errors[attention_id] = str(exc)
    if errors:
        logger.warning("backup attention publish errors: %s", errors)
    return {"published": published, "skipped": skipped, "errors": errors}


def run_cycle(now: datetime | None = None) -> dict:
    """One deriver pass: ingest reports, derive, publish.

    Runs synchronously (called via asyncio.to_thread from the
    background task); every filesystem touch inside reconcile() is
    bounded there.
    """
    from tools.dashboard import attention_routes
    from tools.dashboard.plugins.backup import reconcile as reconcile_mod
    from tools.dashboard.plugins.backup.entrypoints.api import (
        _read_config,
        _rows,
    )
    from tools.dashboard.plugins.backup.entrypoints.schemas import RUN_SET_ID

    reconciled = reconcile_mod.reconcile()
    try:
        from tools.dashboard.plugins.backup import drill as drill_mod
        drill_mod.finalize_abandoned()
    except Exception:
        logger.exception("abandoned-drill finalization failed")
    try:
        # Refresh the cached credential status here, in the worker
        # thread — the vault decrypt cost lives in this cycle so the
        # request path never pays it.
        from tools.dashboard.plugins.backup import credentials
        credentials.offsite_env(_read_config())
    except Exception:
        logger.exception("credential status refresh failed")
    runs = _rows(RUN_SET_ID)
    conditions = derive_conditions(runs, _read_config(), now=now)
    outcome = publish_conditions(attention_routes._runtime.index, conditions)
    return {"reconcile": reconciled, **outcome}
