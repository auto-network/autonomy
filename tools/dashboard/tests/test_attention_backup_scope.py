"""The backup attention scope — the first non-approval producer path.

auto-e4e66: the production registry carries a ``backup`` application
whose four classes (backup_failed, backup_stale, restore_drill_failed,
offsite_unreachable) are registered closed code, publishable through
the exact same sealed-producer / publication-runtime seam approvals
use. These tests pin: the catalog shape, the sealing (no unregistered
publisher), the approval regression (nothing about the approval rows
changed), and a store-level publish of a backup item end to end.
"""
from __future__ import annotations

import pytest

from tools.dashboard.attention_index_service import (
    AttentionIndexError,
    AttentionIndexService,
    InMemoryAttentionIndexStore,
)
from tools.dashboard.attention_registry import (
    AttentionClassPolicy,
    AttentionProjectionPlan,
    AttentionPublicationRuntime,
    AttentionSourceEvidence,
    build_production_attention_registry,
)

BACKUP_KINDS = {
    "backup.failed": "backup_failed",
    "backup.stale": "backup_stale",
    "backup.drill_failed": "restore_drill_failed",
    "backup.offsite_unreachable": "offsite_unreachable",
}


def _stale_plan(source):
    return AttentionProjectionPlan(
        attention_id=source.get("attention_id", "attention:backup:hourly"),
        object_ref=source.get("object_ref", "backup:tier:hourly"),
        participant_role="recipient",
        attention_state="needs_attention",
        safe_title="Hourly backup is stale",
        safe_summary="No successful capture for 7.2h (threshold 3.0h).",
        counterparty_ref=None,
        occurred_at=source.get("occurred_at", 1000.0),
        source_version=source.get("source_version", 1),
    )


def _evidence(object_ref, source_version):
    return AttentionSourceEvidence(
        source_guard={
            "kind": "backup",
            "ref": object_ref,
            "version": source_version,
        },
        source_expires_at=2000.0,
    )


def _runtime(**kw):
    return AttentionPublicationRuntime(
        projection_planner=kw.get("planner", _stale_plan),
        source_evidence_builder=kw.get("evidence", _evidence),
        publication_enabled=kw.get("enabled", True),
    )


def _registry(*kinds):
    return build_production_attention_registry(runtimes={
        (kind, "backup"): _runtime() for kind in kinds
    })


class TestCatalog:
    def test_backup_scope_carries_exactly_the_four_classes(self):
        registry = build_production_attention_registry()
        application = registry.require_application("backup")
        assert application.label == "Backup"
        assert application.icon_ref == "attention.application.backup"
        assert {
            c.kind: c.notification_class for c in application.classes
        } == BACKUP_KINDS
        assert all(c.surface_category == "apps" for c in application.classes)

    def test_backup_policy_is_the_locked_profile(self):
        registry = build_production_attention_registry()
        for item in registry.require_application("backup").classes:
            payload = item.policy.to_payload()
            assert payload["budget_class"] == "system_health"
            assert payload["route_builder_id"] == "backup.page.v1"
            assert payload["destination_id"] == "backup.page"

    def test_policy_profiles_stay_closed(self):
        with pytest.raises(ValueError, match="registered exact policy"):
            AttentionClassPolicy(**{
                **AttentionClassPolicy.backup_phase_one().to_payload(),
                "budget_class": "unlimited",
            })

    def test_approval_rows_are_untouched(self):
        """The regression the bead demands: extending the catalog may
        not alter one approval-derived registration."""
        registry = build_production_attention_registry()
        approval_policy = AttentionClassPolicy.approval_phase_one().to_payload()
        for application in registry.applications:
            if application.application_scope == "backup":
                continue
            for item in application.classes:
                assert item.surface_category == "approvals"
                assert item.policy.to_payload() == approval_policy
                assert item.notification_class.startswith("approval.")


class TestPublication:
    def test_no_runtime_means_disabled(self):
        registry = build_production_attention_registry()
        with pytest.raises(AttentionIndexError) as exc:
            registry.producer("backup.stale", "backup")
        assert exc.value.code == "class_disabled"

    def test_unsealed_producer_is_refused(self):
        registry = _registry("backup.stale")
        service = AttentionIndexService(
            registry=registry, store=InMemoryAttentionIndexStore())

        class Impostor:
            registration = registry.require_class("backup", "backup_stale")

        with pytest.raises(AttentionIndexError):
            service.publish(Impostor(), {})

    def test_backup_item_publishes_end_to_end(self):
        registry = _registry("backup.stale")
        store = InMemoryAttentionIndexStore()
        service = AttentionIndexService(registry=registry, store=store)
        producer = registry.producer("backup.stale", "backup")
        record = service.publish(producer, {"source_version": 1})
        assert record.payload["application_scope"] == "backup"
        assert record.payload["notification_class"] == "backup_stale"
        assert record.payload["attention_state"] == "needs_attention"
        assert record.payload["safe_title"] == "Hourly backup is stale"

    def test_recovery_republish_advances_version(self):
        """The deriver's clear-on-recovery path: the same object_ref at
        a higher source version transitions the stored item."""
        registry = _registry("backup.stale")
        store = InMemoryAttentionIndexStore()
        service = AttentionIndexService(registry=registry, store=store)
        producer = registry.producer("backup.stale", "backup")
        service.publish(producer, {"source_version": 1})

        def resolved_plan(source):
            from dataclasses import replace
            return replace(_stale_plan(source), attention_state="resolved",
                           safe_title="Hourly backup recovered")

        registry2 = build_production_attention_registry(runtimes={
            ("backup.stale", "backup"): _runtime(planner=resolved_plan),
        })
        service2 = AttentionIndexService(registry=registry2, store=store)
        producer2 = registry2.producer("backup.stale", "backup")
        record = service2.publish(producer2, {"source_version": 2})
        assert record.payload["attention_state"] == "resolved"
        assert record.payload["source_version"] == 2
