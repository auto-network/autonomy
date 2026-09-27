"""Central attention for the served TLS certificate (auto-1ei8m part 2).

The renewal cron on Home failed silently for three months. Whatever the cause
of a future failure, the operator must hear about it before the certificate
expires: this deriver reads the served certificate's NotAfter and publishes a
``machine.tls_certificate_expiring`` item under 21 days, resolved once a fresh
certificate is served. The item opens the Machines page focused on this
machine, whose card explains the condition and names the remedy.

Registration of the class is closed substrate code (attention_registry); this
module supplies the publication runtime, the deriver and the loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from tools.dashboard.attention_registry import (
    AttentionIndexError,
    AttentionProjectionPlan,
    AttentionPublicationRuntime,
    AttentionSourceEvidence,
)
from tools.dashboard import tls_certificate

logger = logging.getLogger(__name__)

APPLICATION_SCOPE = "machine"
KIND = "machine.tls_certificate_expiring"
#: How often the served certificate is re-read.
CHECK_INTERVAL_S = 6 * 3600.0


def _planner(source: dict) -> AttentionProjectionPlan:
    return AttentionProjectionPlan(
        attention_id=source["attention_id"],
        object_ref=source["object_ref"],
        participant_role="recipient",
        attention_state=source["attention_state"],
        safe_title=source["safe_title"],
        safe_summary=source.get("safe_summary"),
        counterparty_ref=None,
        occurred_at=float(source["occurred_at"]),
        source_version=int(source["source_version"]),
    )


def _evidence(object_ref: str, source_version: int) -> AttentionSourceEvidence:
    return AttentionSourceEvidence(
        source_guard={"kind": "machine", "ref": object_ref, "version": source_version},
    )


def publication_runtimes() -> dict:
    """(kind, scope) -> runtime, for build_production_attention_registry."""
    return {
        (KIND, APPLICATION_SCOPE): AttentionPublicationRuntime(
            projection_planner=_planner,
            source_evidence_builder=_evidence,
        ),
    }


def _machine_id() -> str | None:
    try:
        from tools.network import machine_boot

        return machine_boot.machine_id(org="machine")
    except Exception:
        return None


#: Escalation stages of one certificate. The source version is
#: NotAfter * 4 + stage: monotonic as a certificate ages through the stages,
#: and any renewal (a later NotAfter) is larger than every stage of the old
#: one, so each escalation publishes (and pushes) once and a renewal resolves.
STAGE_CURRENT, STAGE_WARN, STAGE_URGENT, STAGE_EXPIRED = 0, 1, 2, 3
URGENT_DAYS = 7


def _stage(days: float | None) -> int:
    if days is None or days > tls_certificate.EXPIRY_WARNING_DAYS:
        return STAGE_CURRENT
    if days <= 0:
        return STAGE_EXPIRED
    if days <= URGENT_DAYS:
        return STAGE_URGENT
    return STAGE_WARN


def source_version(facts, stage: int) -> int:
    return (int(facts.not_after.timestamp()) if facts is not None else 0) * 4 + stage


def _on(facts) -> str:
    return facts.not_after.strftime("%-d %b %Y")


def derive_condition(facts, *, machine_id: str, now: datetime | None = None) -> dict:
    """One condition row for this machine's served certificate: needs
    attention under the warning window, again under seven days, again once
    expired; resolved otherwise. Titles carry the absolute date, so the item
    stays true between cycles."""
    now = now or datetime.now(timezone.utc)
    attention_id = f"machine:{machine_id}:tls-certificate"
    object_ref = machine_id
    days = facts.days_remaining(now) if facts is not None else None
    stage = _stage(days)
    base = {"kind": KIND, "attention_id": attention_id, "object_ref": object_ref,
            "occurred_at": now.timestamp(), "source_version": source_version(facts, stage)}
    if stage == STAGE_CURRENT:
        return {**base, "attention_state": "resolved",
                "safe_title": "Dashboard certificate is current", "safe_summary": None}
    remedy = ("The monthly renewal did not replace it: run tools/dashboard/renew-tls-cert.sh "
              "on this machine and check data/cert-renew.log.")
    if stage == STAGE_EXPIRED:
        title = f"Dashboard certificate expired on {_on(facts)}"
        summary = (f"The certificate this dashboard serves expired on {_on(facts)}; browsers and the "
                   f"fleet cannot connect to it. {remedy}")
    elif stage == STAGE_URGENT:
        title = f"Dashboard certificate expires on {_on(facts)} (under a week)"
        summary = f"The certificate this dashboard serves expires on {_on(facts)}. {remedy}"
    else:
        title = f"Dashboard certificate expires on {_on(facts)}"
        summary = f"The certificate this dashboard serves expires on {_on(facts)}. {remedy}"
    return {**base, "attention_state": "needs_attention", "safe_title": title, "safe_summary": summary}


def publish_condition(index, condition: dict) -> str:
    """Publish one condition through the sealed-producer seam; returns
    published | skipped | error:<code>. Resolved rows publish only when the
    item is currently open; an unchanged version never re-publishes."""
    attention_id = condition["attention_id"]
    try:
        current = index.store.get_item(attention_id)
    except Exception:
        current = None
    payload = getattr(current, "payload", None) or {}
    if condition["attention_state"] == "resolved":
        if payload.get("attention_state") != "needs_attention":
            return "skipped"
        try:
            stored_version = int(payload.get("source_version") or 0)
        except (TypeError, ValueError):
            stored_version = 0
        condition["source_version"] = max(int(condition["source_version"]), stored_version + 1)
    elif (payload.get("attention_state") == condition["attention_state"]
            and payload.get("source_version") == condition["source_version"]):
        return "skipped"
    try:
        producer = index.registry.producer(condition["kind"], APPLICATION_SCOPE)
        index.publish(producer, condition)
        return "published"
    except AttentionIndexError as exc:
        return "skipped" if exc.code == "stale_source" else f"error:{exc.code}"
    except Exception as exc:  # never let one row kill the cycle
        return f"error:{exc}"


def run_cycle(now: datetime | None = None) -> dict:
    """One pass: read the served certificate, derive, publish."""
    from tools.dashboard import attention_routes

    machine_id = _machine_id()
    if not machine_id:
        return {"outcome": "skipped", "reason": "no machine identity"}
    facts = tls_certificate.read_certificate()
    condition = derive_condition(facts, machine_id=machine_id, now=now)
    outcome = publish_condition(attention_routes._runtime.index, condition)
    return {"outcome": outcome, "state": condition["attention_state"],
            "title": condition["safe_title"]}


async def loop() -> None:
    """Runs for the life of the worker: once at start, then every six hours."""
    while True:
        try:
            outcome = await asyncio.to_thread(run_cycle)
            if outcome.get("outcome") == "published" or str(outcome.get("outcome", "")).startswith("error"):
                logger.info("certificate attention cycle: %s", outcome)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("certificate attention cycle failed")
        await asyncio.sleep(CHECK_INTERVAL_S)
