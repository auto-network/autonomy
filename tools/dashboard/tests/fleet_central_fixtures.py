"""Hermetic Central composition for Fleet transport and HTTP behavior tests."""
from dataclasses import replace
from tools.dashboard import attention_routes, fleet_enrollment_approvals as fleet
from tools.dashboard.approval_kind_registry import build_production_registry
from tools.dashboard.approval_service import ApprovalService, HumanApprovalActor, InMemoryApprovalStore
from tools.dashboard.attention_index_service import AttentionIndexService, InMemoryAttentionIndexStore
from tools.dashboard.attention_registry import build_production_attention_registry


def configure_central(monkeypatch, store, root, now):
    monkeypatch.setattr(fleet, '_store', lambda: store)
    monkeypatch.setattr(fleet, '_anchor', lambda: root.public_hex)
    registry = build_production_registry(runtimes={fleet.KIND: fleet.build_approval_runtime()})
    approvals = ApprovalService(registry=registry, store=InMemoryApprovalStore(),
        personal_root_resolver=lambda: root.public_hex, clock=lambda: now,
        after_commit=lambda _kind, aid: fleet.reconcile(aid))
    index = AttentionIndexService(
        registry=build_production_attention_registry(approval_registry=registry,
            runtimes={(fleet.KIND, 'fleet'): fleet.build_attention_runtime(approvals)}),
        store=InMemoryAttentionIndexStore(),
    )
    monkeypatch.setattr(attention_routes, '_runtime', replace(attention_routes.approval_runtime(),
        approvals=approvals, index=index, approval_http=None,
        operator_result_projectors={fleet.KIND: fleet.project_result}))
    monkeypatch.setattr(attention_routes, 'resolve_human_approval_actor',
                        lambda _request: HumanApprovalActor._verified(root.public_hex))
    return approvals
