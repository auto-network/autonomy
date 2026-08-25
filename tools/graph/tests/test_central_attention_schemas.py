"""Contract tests for the phase-one Central Attention Settings vocabulary.

The schemas are deliberately data-only. Cross-row immutability, monotonic
transitions, first-decision-wins, audience resolution, and delivery races are
trusted-service behavior covered by later beads, not by these validators.
"""

from __future__ import annotations

import copy
import json

import pytest

from tools.graph.schemas import central_attention as ca
from tools.graph.schemas.registry import (
    SchemaValidationError,
    declared_band,
    declared_home,
    get_schema,
    list_registered_set_ids,
    validate_payload,
)


SCHEMAS = {
    ca.ATTENTION_APPLICATION_SET_ID: "keyed_per_entity",
    ca.ATTENTION_ITEM_SET_ID: "keyed_per_entity",
    ca.ATTENTION_PRESENTATION_SET_ID: "keyed_per_entity",
    ca.ATTENTION_DELIVERY_SET_ID: "keyed_per_entity",
    ca.APPROVAL_REQUEST_SET_ID: "append_only_log",
    ca.APPROVAL_RESOLUTION_SET_ID: "append_only_log",
}


def application_payload() -> dict:
    return {
        "label": "Fleet",
        "icon_ref": "machine",
        "open_mode": "registered_renderer",
        "notification_classes": [
            {
                "notification_class": "machine_admission",
                "class_policy_revision": 1,
                "eligible_transition": "needs_attention",
                "push_policy": "fallback",
                "delivery_class": "approval",
                "budget_class": "operator_attention",
                "coalesce_scope": "event",
                "ttl_seconds": 3600,
                "urgency": "high",
                "privacy_renderer_id": "generic_attention",
                "route_builder_id": "central_approval",
                "destination_id": "activity",
            }
        ],
        "enabled": True,
    }


def item_payload() -> dict:
    return {
        "application_scope": "fleet",
        "notification_class": "machine_admission",
        "object_ref": "approval:appr_01j8v7ng5h6ya",
        "participant_role": "recipient",
        "attention_state": "needs_attention",
        "safe_title": "Fleet approval requested",
        "safe_summary": "A new machine wants to join.",
        "counterparty_ref": "machine:pending-01",
        "occurred_at": 1_777_000_000.0,
        "source_version": 1,
    }


def presentation_payload() -> dict:
    return {
        "seen_at": 1_777_000_005.0,
    }


def delivery_payload(*, state: str = "foreground_wait") -> dict:
    payload = {
        "event_id": "event_01j8v7ng5h6ya",
        "attention_id": "attn_01j8v7ng5h6ya",
        "source_version": 1,
        "application_scope": "fleet",
        "notification_class": "machine_admission",
        "class_policy_revision": 3,
        "delivery_class": "approval",
        "budget_class": "operator_attention",
        "coalesce_key": "event:event_01j8v7ng5h6ya",
        "urgency": "high",
        "privacy_renderer_id": "generic_attention",
        "route_builder_id": "central_approval",
        "destination_id": "activity",
        "source_guard": {
            "kind": "approval_state",
            "ref": "approval:appr_01j8v7ng5h6ya",
            "version": 1,
        },
        "created_at": 1_777_000_000.0,
        "expires_at": 1_777_003_600.0,
        "state": state,
        "state_version": 1,
        "updated_at": 1_777_000_000.0,
    }
    if state == "foreground_wait":
        payload.update(
            foreground_selected_at=1_777_000_000.0,
            fallback_due_at=1_777_000_020.0,
        )
    return payload


def request_payload() -> dict:
    return {
        "application_scope": "fleet",
        "kind": "fleet_machine_admission",
        "requester_ref": {
            "kind": "registered_service",
            "id": "fleet-enrollment",
            "label": "Fleet enrollment",
        },
        "decider": {"kind": "person", "id": "root-pub:operator"},
        "subject_ref": "machine:pending-01",
        "safe_review": {
            "machine_id": "pending-01",
            "comparison_pin": "512 804",
        },
        "request": {"machine_id": "pending-01"},
        "staged": {"enrollment_ref": "invite:01j8v7ng5h6ya"},
        "created_at": 1_777_000_000.0,
        "expires_at": 1_777_003_600.0,
        "source_version": 1,
    }


def resolution_payload() -> dict:
    return {
        "outcome": "granted",
        "decider_ref": "person:root-pub:operator",
        "resolved_at": 1_777_000_010.0,
        "decision": {"machine_name": "SJC dashboard"},
        "result_ref": "fleet-enrollment:result-01",
    }


CANONICAL = {
    ca.ATTENTION_APPLICATION_SET_ID: application_payload,
    ca.ATTENTION_ITEM_SET_ID: item_payload,
    ca.ATTENTION_PRESENTATION_SET_ID: presentation_payload,
    ca.ATTENTION_DELIVERY_SET_ID: delivery_payload,
    ca.APPROVAL_REQUEST_SET_ID: request_payload,
    ca.APPROVAL_RESOLUTION_SET_ID: resolution_payload,
}


class TestRegistration:
    @pytest.mark.parametrize(("set_id", "pattern"), SCHEMAS.items())
    def test_exact_schema_contract_is_registered(self, set_id, pattern):
        schema = get_schema(set_id, 1)
        assert schema is not None
        assert schema.export_json_schema()["access_pattern"] == pattern
        assert declared_home(set_id) == "personal"
        assert declared_band(set_id, 1) == ("raw", "raw")

    def test_all_six_are_visible_to_package_consumers(self):
        assert set(SCHEMAS) <= set(list_registered_set_ids())

    def test_no_machine_execution_lease_schema_exists(self):
        assert "dashboard.approval.execution-lease" not in list_registered_set_ids()

    @pytest.mark.parametrize(
        "set_id, key_strategy, absent_payload_field",
        [
            (ca.ATTENTION_APPLICATION_SET_ID, "application_scope", "application_scope"),
            (ca.ATTENTION_ITEM_SET_ID, "attention_id", "attention_id"),
            (ca.ATTENTION_PRESENTATION_SET_ID, "attention_id", "attention_id"),
            (ca.ATTENTION_DELIVERY_SET_ID, "delivery_id", "delivery_id"),
            (ca.APPROVAL_REQUEST_SET_ID, "approval_id", "approval_id"),
            (ca.APPROVAL_RESOLUTION_SET_ID, "approval_id", "approval_id"),
        ],
    )
    def test_row_key_is_authoritative_and_not_repeated(
        self, set_id, key_strategy, absent_payload_field
    ):
        schema = get_schema(set_id, 1)
        assert schema is not None
        exported = schema.export_json_schema()
        assert exported["key_strategy"] == key_strategy
        assert absent_payload_field not in exported["properties"]


class TestCanonicalPayloads:
    @pytest.mark.parametrize("set_id", SCHEMAS)
    def test_canonical_payload_validates_and_json_round_trips(self, set_id):
        payload = CANONICAL[set_id]()
        validate_payload(set_id, 1, json.loads(json.dumps(payload)))

    @pytest.mark.parametrize(
        "state, additions",
        [
            ("background_due", {}),
            (
                "background_due",
                {
                    "foreground_selected_at": 1_777_000_000.0,
                    "fallback_due_at": 1_777_000_020.0,
                    "updated_at": 1_777_000_020.0,
                },
            ),
            (
                "background_released",
                {
                    "released_at": 1_777_000_021.0,
                    "updated_at": 1_777_000_021.0,
                },
            ),
            (
                "foreground_applied",
                {
                    "acknowledged_at": 1_777_000_022.0,
                    "visibility_proof_epoch": 8,
                    "ack_epoch": 12,
                    "updated_at": 1_777_000_022.0,
                },
            ),
            (
                "foreground_applied",
                {
                    "foreground_selected_at": 1_777_000_000.0,
                    "fallback_due_at": 1_777_000_020.0,
                    "released_at": 1_777_000_021.0,
                    "acknowledged_at": 1_777_000_022.0,
                    "visibility_proof_epoch": 8,
                    "ack_epoch": 12,
                    "updated_at": 1_777_000_022.0,
                },
            ),
            (
                "ineligible",
                {
                    "released_at": 1_777_000_021.0,
                    "cancel_reason": "source_resolved",
                    "updated_at": 1_777_000_021.0,
                },
            ),
            ("expired", {"cancel_reason": "ttl_expired"}),
        ],
    )
    def test_delivery_accepts_each_durable_envelope(self, state, additions):
        payload = delivery_payload(state=state)
        payload.update(additions)
        validate_payload(ca.ATTENTION_DELIVERY_SET_ID, 1, payload)


def _mutate(base, mutation):
    payload = base()
    mutation(payload)
    return payload


class TestApplicationValidation:
    @pytest.mark.parametrize(
        "mutation",
        [
            lambda p: p.update(default_push_policy="fallback"),
            lambda p: p.update(application_scope="Fleet!"),
            lambda p: p.update(notification_classes=[]),
            lambda p: p["notification_classes"].append(
                copy.deepcopy(p["notification_classes"][0])
            ),
            lambda p: p["notification_classes"][0].update(
                class_policy_revision=0
            ),
            lambda p: p["notification_classes"][0].update(
                eligible_transition="resolved"
            ),
            lambda p: p["notification_classes"][0].update(push_policy="always"),
            lambda p: p["notification_classes"][0].update(coalesce_scope="person"),
            lambda p: p["notification_classes"][0].update(ttl_seconds=float("inf")),
            lambda p: p["notification_classes"][0].pop("route_builder_id"),
            lambda p: p["notification_classes"][0].update(route="/fleet"),
        ],
    )
    def test_invalid_or_broadening_policy_shapes_are_rejected(self, mutation):
        with pytest.raises(SchemaValidationError):
            validate_payload(
                ca.ATTENTION_APPLICATION_SET_ID,
                1,
                _mutate(application_payload, mutation),
            )


class TestItemAndPresentationValidation:
    @pytest.mark.parametrize(
        "base, set_id, mutation",
        [
            (item_payload, ca.ATTENTION_ITEM_SET_ID, lambda p: p.update(route="/fleet")),
            (item_payload, ca.ATTENTION_ITEM_SET_ID, lambda p: p.update(recipient="me")),
            (item_payload, ca.ATTENTION_ITEM_SET_ID, lambda p: p.update(source_version=-1)),
            (item_payload, ca.ATTENTION_ITEM_SET_ID, lambda p: p.update(occurred_at=float("nan"))),
            (presentation_payload, ca.ATTENTION_PRESENTATION_SET_ID, lambda p: p.update(decision="granted")),
            (presentation_payload, ca.ATTENTION_PRESENTATION_SET_ID, lambda p: p.update(attention_state="resolved")),
            (presentation_payload, ca.ATTENTION_PRESENTATION_SET_ID, lambda p: p.update(seen_at=float("inf"))),
            (presentation_payload, ca.ATTENTION_PRESENTATION_SET_ID, lambda p: p.pop("seen_at")),
        ],
    )
    def test_semantic_smuggling_and_invalid_values_are_rejected(
        self, base, set_id, mutation
    ):
        with pytest.raises(SchemaValidationError):
            validate_payload(set_id, 1, _mutate(base, mutation))


class TestDeliveryValidation:
    @pytest.mark.parametrize(
        "mutation",
        [
            lambda p: p.update(recipient="person:someone"),
            lambda p: p.update(route="/activity"),
            lambda p: p.update(expires_at=p["created_at"]),
            lambda p: p.update(updated_at=p["expires_at"] + 1),
            lambda p: p.update(state_version=0),
            lambda p: p.update(source_version=-1),
            lambda p: p.update(state="foreground_applied"),
            lambda p: p.update(state="background_released"),
            lambda p: p.update(state="background_due"),
            lambda p: p.update(fallback_due_at=p["expires_at"] + 1),
            lambda p: p.update(source_guard={"kind": "approval_state", "route": "/x"}),
            lambda p: (
                p.pop("fallback_due_at"),
                p.update(state="background_due"),
            ),
            lambda p: (
                p.pop("foreground_selected_at"),
                p.update(state="background_due"),
            ),
            lambda p: p.update(
                state="foreground_applied",
                acknowledged_at=1_777_000_022.0,
                visibility_proof_epoch=8,
                ack_epoch=12,
            ),
            lambda p: p.update(
                state="background_released",
                released_at=1_777_000_021.0,
            ),
            lambda p: p.update(
                state="background_released",
                foreground_selected_at=1_777_000_000.0,
                fallback_due_at=1_777_000_020.0,
                released_at=1_777_000_019.0,
                updated_at=1_777_000_020.0,
            ),
            lambda p: p.update(
                state="foreground_applied",
                released_at=1_777_000_022.0,
                acknowledged_at=1_777_000_021.0,
                visibility_proof_epoch=8,
                ack_epoch=12,
                updated_at=1_777_000_022.0,
            ),
        ],
    )
    def test_incoherent_or_unsafe_latch_shapes_are_rejected(self, mutation):
        with pytest.raises(SchemaValidationError):
            validate_payload(
                ca.ATTENTION_DELIVERY_SET_ID,
                1,
                _mutate(delivery_payload, mutation),
            )


class TestApprovalValidation:
    @pytest.mark.parametrize(
        "mutation",
        [
            lambda p: p.update(source_version=2),
            lambda p: p.update(expires_at=p["created_at"]),
            lambda p: p["decider"].update(kind="organization_authority"),
            lambda p: p["safe_review"].update(html="<button>Grant</button>"),
            lambda p: p["safe_review"].update(route="/fleet"),
            lambda p: p["staged"].update(bearer_token="secret"),
            lambda p: p["staged"].update(password="secret"),
            lambda p: p.update(audience={"kind": "person", "id": "forged"}),
        ],
    )
    def test_request_rejects_authority_and_secret_smuggling(self, mutation):
        with pytest.raises(SchemaValidationError):
            validate_payload(
                ca.APPROVAL_REQUEST_SET_ID,
                1,
                _mutate(request_payload, mutation),
            )

    @pytest.mark.parametrize(
        "mutation",
        [
            lambda p: p.pop("decider_ref"),
            lambda p: p.update(outcome="approved"),
            lambda p: p.update(resolved_at=float("nan")),
            lambda p: p.update(request_digest="underspecified"),
        ],
    )
    def test_resolution_rejects_incomplete_or_unsupported_shapes(self, mutation):
        with pytest.raises(SchemaValidationError):
            validate_payload(
                ca.APPROVAL_RESOLUTION_SET_ID,
                1,
                _mutate(resolution_payload, mutation),
            )

    @pytest.mark.parametrize("outcome", ["canceled", "expired"])
    def test_non_human_resolution_does_not_require_decider(self, outcome):
        payload = resolution_payload()
        payload["outcome"] = outcome
        payload.pop("decider_ref")
        payload.pop("decision")
        payload.pop("result_ref")
        validate_payload(ca.APPROVAL_RESOLUTION_SET_ID, 1, payload)
