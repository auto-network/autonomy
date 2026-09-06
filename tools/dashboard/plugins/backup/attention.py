"""The backup plugin's Central Attention publication seam (auto-fnydv).

The registry rows exist as closed code (attention_registry, auto-e4e66);
this module supplies the PUBLICATION RUNTIMES those rows require before
anything can publish — composed into build_production_attention_registry
exactly like an approval attention runtime. The planner is a plain
projection: the deriver hands it a complete source dict, so policy
(what is failing, what cleared) lives in deriver.py and this file stays
a translation layer.
"""
from __future__ import annotations

from tools.dashboard.attention_registry import (
    AttentionProjectionPlan,
    AttentionPublicationRuntime,
    AttentionSourceEvidence,
)

APPLICATION_SCOPE = "backup"

#: kind -> notification class, mirroring the registry's closed table.
KINDS = {
    "backup.failed": "backup_failed",
    "backup.stale": "backup_stale",
    "backup.drill_failed": "restore_drill_failed",
    "backup.offsite_unreachable": "offsite_unreachable",
}


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
        source_guard={
            "kind": "backup",
            "ref": object_ref,
            "version": source_version,
        },
    )


def publication_runtimes() -> dict:
    """(kind, scope) -> runtime, for build_production_attention_registry."""
    return {
        (kind, APPLICATION_SCOPE): AttentionPublicationRuntime(
            projection_planner=_planner,
            source_evidence_builder=_evidence,
        )
        for kind in KINDS
    }
