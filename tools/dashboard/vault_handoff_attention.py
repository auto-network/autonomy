"""Central attention for a hot reload that did not carry the vault (auto-wb6ok).

On 2026-09-27 a routine reload lost the vault: the reloader's hand-off notice
timed out on a busy incumbent, the incumbent was killed before its shutdown
snapshot, and every later reload stayed cold until a human happened to unlock.
Connectors, certificate reconciliation and session launches were down for 21
minutes and nothing said so. This module makes that state loud: after a
hand-off, a replacement worker whose vault is still locked, or whose reloader
reports a failed notice, publishes a ``machine.vault_handoff_failed`` item;
a later warm hand-off or an unlock resolves it.

It is the same machine scope, policy and publication seam as the TLS-expiry
item (:mod:`tools.dashboard.certificate_attention`), whose planner, evidence
builder and publisher are reused.
"""

from __future__ import annotations

import logging
import time

from tools.dashboard import certificate_attention
from tools.dashboard.attention_registry import AttentionPublicationRuntime

logger = logging.getLogger(__name__)

APPLICATION_SCOPE = certificate_attention.APPLICATION_SCOPE
KIND = "machine.vault_handoff_failed"


def publication_runtimes() -> dict:
    """(kind, scope) -> runtime, for build_production_attention_registry."""
    return {
        (KIND, APPLICATION_SCOPE): AttentionPublicationRuntime(
            projection_planner=certificate_attention._planner,
            source_evidence_builder=certificate_attention._evidence,
        ),
    }


def derive_condition(*, warm: bool, notice_failure: str | None,
                     machine_id: str, now: float | None = None) -> dict:
    """One condition row: needs attention when the vault is locked after a
    hand-off, or when the reloader could not deliver its notice (the vault
    then survived only through the incumbent's SIGTERM snapshot); resolved
    otherwise. The source version is the time in milliseconds, so each
    evaluation supersedes the last."""
    now = time.time() if now is None else now
    base = {"kind": KIND, "attention_id": f"machine:{machine_id}:vault-handoff",
            "object_ref": machine_id, "occurred_at": now,
            "source_version": int(now * 1000)}
    if not warm:
        cause = (f" The reloader's hand-off notice failed ({notice_failure})."
                 if notice_failure else "")
        return {**base, "attention_state": "needs_attention",
                "safe_title": "Vault is locked after a dashboard reload",
                "safe_summary": (
                    "A reload did not carry the unlocked vault to the new worker, so "
                    "serving connectors, certificate reconciliation and session "
                    f"launches that need it are down.{cause} Unlock the dashboard "
                    "at /unlock; the next reload carries it forward again.")}
    if notice_failure:
        return {**base, "attention_state": "needs_attention",
                "safe_title": "Dashboard reload hand-off notice failed",
                "safe_summary": (
                    f"The reloader could not reach the old worker ({notice_failure}). "
                    "The vault was still carried over by the old worker's shutdown "
                    "snapshot, but a notice that keeps failing is how the vault was "
                    "lost on 2026-09-27. Check the dashboard log for event-loop stalls.")}
    return {**base, "attention_state": "resolved",
            "safe_title": "Vault carried across the last reload", "safe_summary": None}


def _vault_warm() -> bool:
    from tools.graph import settings_ops

    return bool(settings_ops.personal_delegate_audited_is_warm())


def run_cycle(*, notice_failure: str | None = None) -> dict:
    """Evaluate this worker's vault after a hand-off (or an unlock) and
    publish. Never raises."""
    try:
        from tools.dashboard import attention_routes

        machine_id = certificate_attention._machine_id()
        if not machine_id:
            return {"outcome": "skipped", "reason": "no machine identity"}
        condition = derive_condition(warm=_vault_warm(), notice_failure=notice_failure,
                                     machine_id=machine_id)
        outcome = certificate_attention.publish_condition(
            attention_routes._runtime.index, condition)
        if condition["attention_state"] == "needs_attention":
            logger.warning("vault hand-off attention: %s (%s)",
                           condition["safe_title"], outcome)
        return {"outcome": outcome, "state": condition["attention_state"]}
    except Exception:
        logger.exception("vault hand-off attention cycle failed")
        return {"outcome": "error"}
