"""Closed code registry for Central Attention applications and classes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
import re
from typing import Any, Callable, Iterable, Mapping

from tools.dashboard.approval_kind_registry import (
    ApprovalKindRegistry,
    PRODUCTION_APPROVAL_REGISTRY,
)


MAX_SOURCE_VERSION = 2**63 - 1
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_POLICY_VALUES = {
    "class_policy_revision": 1,
    "eligible_transition": "needs_attention",
    "push_policy": "fallback",
    "delivery_class": "normal",
    "budget_class": "operator_approval",
    "coalesce_scope": "object",
    "ttl_seconds": 21600,
    "urgency": "normal",
    "privacy_renderer_id": "web_push.generic.v1",
    "route_builder_id": "activity.approval.v1",
    "destination_id": "activity.approval",
}
_APPLICATION_META = {
    "worktrees": ("Worktrees", "attention.application.worktrees"),
    "jira": ("Jira", "attention.application.jira"),
    "links": ("Links", "attention.application.links"),
    "sessions": ("Sessions", "attention.application.sessions"),
    "mission_control": ("Mission Control", "attention.application.mission_control"),
    "vault": ("Vault", "attention.application.vault"),
    "relay": ("Relay", "attention.application.relay"),
    "fleet": ("Fleet", "attention.application.fleet"),
    "dropbox": ("Dropbox", "attention.application.dropbox"),
}


class AttentionIndexError(RuntimeError):
    """Bounded Central Attention service/registry failure."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(f"{code}: {message or code.replace('_', ' ')}")


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase code identifier")
    return value


def _bounded_version(value: Any) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_SOURCE_VERSION
    ):
        raise ValueError("source version is outside the signed 64-bit range")
    return value


@dataclass(frozen=True, slots=True)
class AttentionClassPolicy:
    class_policy_revision: int
    eligible_transition: str
    push_policy: str
    delivery_class: str
    budget_class: str
    coalesce_scope: str
    ttl_seconds: int
    urgency: str
    privacy_renderer_id: str
    route_builder_id: str
    destination_id: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.class_policy_revision, bool)
            or not isinstance(self.class_policy_revision, int)
            or isinstance(self.ttl_seconds, bool)
            or not isinstance(self.ttl_seconds, int)
            or any(
                not isinstance(value, str)
                for value in (
                    self.eligible_transition,
                    self.push_policy,
                    self.delivery_class,
                    self.budget_class,
                    self.coalesce_scope,
                    self.urgency,
                    self.privacy_renderer_id,
                    self.route_builder_id,
                    self.destination_id,
                )
            )
            or self.to_payload() != _POLICY_VALUES
        ):
            raise ValueError("phase one accepts only the exact revision-1 policy")

    @classmethod
    def approval_phase_one(cls) -> "AttentionClassPolicy":
        return cls(**_POLICY_VALUES)

    def to_payload(self) -> dict[str, Any]:
        return {
            "class_policy_revision": self.class_policy_revision,
            "eligible_transition": self.eligible_transition,
            "push_policy": self.push_policy,
            "delivery_class": self.delivery_class,
            "budget_class": self.budget_class,
            "coalesce_scope": self.coalesce_scope,
            "ttl_seconds": self.ttl_seconds,
            "urgency": self.urgency,
            "privacy_renderer_id": self.privacy_renderer_id,
            "route_builder_id": self.route_builder_id,
            "destination_id": self.destination_id,
        }


@dataclass(frozen=True, slots=True)
class AttentionSourceEvidence:
    source_guard: Mapping[str, Any]
    source_expires_at: float | None = None


@dataclass(frozen=True, slots=True)
class AttentionProjectionPlan:
    attention_id: str
    object_ref: str
    participant_role: str
    attention_state: str
    safe_title: str
    safe_summary: str | None
    counterparty_ref: str | None
    occurred_at: float
    source_version: int


ProjectionPlanner = Callable[[Any], AttentionProjectionPlan]
SourceEvidenceBuilder = Callable[[str, int], AttentionSourceEvidence]


@dataclass(frozen=True, slots=True)
class AttentionPublicationRuntime:
    projection_planner: ProjectionPlanner
    source_evidence_builder: SourceEvidenceBuilder
    publication_enabled: bool = True

    def __post_init__(self) -> None:
        if (
            not callable(self.projection_planner)
            or not callable(self.source_evidence_builder)
            or not isinstance(self.publication_enabled, bool)
        ):
            raise ValueError("attention publication runtime is incomplete")


@dataclass(frozen=True, slots=True)
class AttentionClassRegistration:
    kind: str
    application_scope: str
    producer_id: str | None
    notification_class: str
    surface_category: str
    review_renderer_id: str
    policy: AttentionClassPolicy
    approval_runtime_enabled: bool
    runtime: AttentionPublicationRuntime | None = None

    def __post_init__(self) -> None:
        _identifier(self.kind, "attention kind")
        _identifier(self.application_scope, "attention application")
        if self.producer_id is not None:
            _identifier(self.producer_id, "attention producer")
        _identifier(self.notification_class, "notification class")
        _identifier(self.review_renderer_id, "review renderer")
        if self.surface_category not in {"apps", "comms", "approvals"}:
            raise ValueError("unknown attention surface category")
        if not isinstance(self.policy, AttentionClassPolicy):
            raise ValueError("attention class policy is required")
        if not isinstance(self.approval_runtime_enabled, bool):
            raise ValueError("approval runtime gate must be boolean")
        if self.runtime is not None and not isinstance(
            self.runtime, AttentionPublicationRuntime,
        ):
            raise ValueError("attention publication runtime must be complete or absent")

    @property
    def publication_enabled(self) -> bool:
        return (
            self.approval_runtime_enabled
            and self.runtime is not None
            and self.runtime.publication_enabled
        )

    def policy_payload(self) -> dict[str, Any]:
        return {"notification_class": self.notification_class, **self.policy.to_payload()}


@dataclass(frozen=True, slots=True)
class AttentionApplicationRegistration:
    application_scope: str
    label: str
    icon_ref: str
    open_mode: str
    classes: tuple[AttentionClassRegistration, ...]

    def __post_init__(self) -> None:
        _identifier(self.application_scope, "attention application")
        _identifier(self.icon_ref, "attention icon")
        if (
            not isinstance(self.label, str)
            or not 1 <= len(self.label) <= 80
            or any(ord(char) < 32 or ord(char) == 127 for char in self.label)
        ):
            raise ValueError("attention application label is invalid")
        if self.open_mode not in {"application", "registered_renderer"}:
            raise ValueError("unknown attention application open mode")
        if not isinstance(self.classes, tuple) or not self.classes:
            raise ValueError("attention application requires classes")
        if any(
            not isinstance(item, AttentionClassRegistration)
            or item.application_scope != self.application_scope
            for item in self.classes
        ):
            raise ValueError("attention class application mismatch")

    @property
    def enabled(self) -> bool:
        return any(item.publication_enabled for item in self.classes)


@dataclass(frozen=True, slots=True)
class AttentionApplicationRecord:
    application_scope: str
    label: str
    payload: Mapping[str, Any]
    enabled: bool


_PRODUCER_SEAL = object()


@dataclass(frozen=True, slots=True)
class RegisteredAttentionProducer:
    registration: AttentionClassRegistration
    _seal: object
    _registry_token: object


class AttentionRegistry:
    """Validated application/class catalog; Settings is never executable authority."""

    def __init__(self, applications: Iterable[AttentionApplicationRegistration]):
        app_rows: dict[str, AttentionApplicationRegistration] = {}
        kinds: dict[str, AttentionClassRegistration] = {}
        classes: dict[tuple[str, str], AttentionClassRegistration] = {}
        for application in applications:
            if not isinstance(application, AttentionApplicationRegistration):
                raise ValueError("attention application registration has wrong type")
            if application.application_scope in app_rows:
                raise ValueError(
                    f"duplicate attention application: {application.application_scope}",
                )
            app_rows[application.application_scope] = application
            for item in application.classes:
                if item.kind in kinds:
                    raise ValueError(f"duplicate attention kind: {item.kind}")
                key = (item.application_scope, item.notification_class)
                if key in classes:
                    raise ValueError(f"duplicate attention class: {key!r}")
                kinds[item.kind] = item
                classes[key] = item
        if not app_rows:
            raise ValueError("attention registry cannot be empty")
        self._applications = MappingProxyType(app_rows)
        self._kinds = MappingProxyType(kinds)
        self._classes = MappingProxyType(classes)
        self._token = object()

    @property
    def applications(self) -> tuple[AttentionApplicationRegistration, ...]:
        return tuple(self._applications.values())

    @property
    def kind_bindings(self) -> Mapping[str, AttentionClassRegistration]:
        return self._kinds

    def application_records(self) -> tuple[AttentionApplicationRecord, ...]:
        records = []
        for application in self._applications.values():
            payload = {
                "label": application.label,
                "icon_ref": application.icon_ref,
                "open_mode": application.open_mode,
                "notification_classes": [
                    item.policy_payload() for item in application.classes
                ],
                "enabled": application.enabled,
            }
            records.append(AttentionApplicationRecord(
                application_scope=application.application_scope,
                label=application.label,
                payload=payload,
                enabled=application.enabled,
            ))
        return tuple(records)

    def require_application(self, application_scope: str) -> AttentionApplicationRegistration:
        try:
            return self._applications[application_scope]
        except (KeyError, TypeError) as exc:
            raise KeyError(application_scope) from exc

    def require_class(
        self, application_scope: str, notification_class: str,
    ) -> AttentionClassRegistration:
        try:
            return self._classes[(application_scope, notification_class)]
        except (KeyError, TypeError) as exc:
            raise KeyError((application_scope, notification_class)) from exc

    def producer(
        self, kind: str, application_scope: str, producer_id: str | None = None,
    ) -> RegisteredAttentionProducer:
        item = self._kinds.get(kind)
        if (
            item is None
            or item.application_scope != application_scope
            or item.producer_id != producer_id
        ):
            raise AttentionIndexError("not_found")
        if not item.publication_enabled:
            raise AttentionIndexError("class_disabled")
        return RegisteredAttentionProducer(item, _PRODUCER_SEAL, self._token)

    def owns(self, producer: Any) -> bool:
        return (
            isinstance(producer, RegisteredAttentionProducer)
            and producer._seal is _PRODUCER_SEAL
            and producer._registry_token is self._token
        )

    def snapshot_payload(self) -> list[dict[str, Any]]:
        output = []
        for application in self._applications.values():
            output.append({
                "application_scope": application.application_scope,
                "label": application.label,
                "icon_ref": application.icon_ref,
                "open_mode": application.open_mode,
                "enabled": application.enabled,
                "classes": [{
                    "kind": item.kind,
                    "producer_id": item.producer_id,
                    "surface_category": item.surface_category,
                    "review_renderer_id": item.review_renderer_id,
                    "publication_enabled": item.publication_enabled,
                    **item.policy_payload(),
                } for item in application.classes],
            })
        return output


def build_production_attention_registry(
    *,
    approval_registry: ApprovalKindRegistry = PRODUCTION_APPROVAL_REGISTRY,
    runtimes: Mapping[tuple[str, str], AttentionPublicationRuntime] | None = None,
) -> AttentionRegistry:
    if not isinstance(approval_registry, ApprovalKindRegistry):
        raise ValueError("production approval registry has the wrong type")
    canonical_kinds = PRODUCTION_APPROVAL_REGISTRY.kinds
    if set(approval_registry.kinds) != set(canonical_kinds):
        raise ValueError("production approval catalog kinds do not match")
    if dict(approval_registry.attention_classes.entries) != dict(
        PRODUCTION_APPROVAL_REGISTRY.attention_classes.entries
    ):
        raise ValueError("production approval attention catalog does not match")
    for kind, canonical in canonical_kinds.items():
        candidate = approval_registry.kinds[kind]
        if replace(candidate, runtime=None) != replace(canonical, runtime=None):
            raise ValueError(f"production approval metadata differs for {kind}")

    supplied = {} if runtimes is None else dict(runtimes)
    rows: dict[str, list[AttentionClassRegistration]] = {
        scope: [] for scope in _APPLICATION_META
    }
    expected_runtime_keys: set[tuple[str, str]] = set()
    policy = AttentionClassPolicy.approval_phase_one()
    for kind, approval in approval_registry.kinds.items():
        for application_scope in approval.application_scope_policy.applications:
            if application_scope not in rows:
                raise ValueError(f"unknown production attention application: {application_scope}")
            key = (kind, application_scope)
            expected_runtime_keys.add(key)
            producer_id = None
            if approval.application_scope_policy.by_producer is not None:
                matches = [
                    candidate
                    for candidate, scope in approval.application_scope_policy.by_producer.items()
                    if scope == application_scope
                ]
                if len(matches) != 1:
                    raise ValueError("attention producer/application mapping is ambiguous")
                producer_id = matches[0]
            rows[application_scope].append(AttentionClassRegistration(
                kind=kind,
                application_scope=application_scope,
                producer_id=producer_id,
                notification_class=approval.notification_class,
                surface_category="approvals",
                review_renderer_id=approval.renderer_id,
                policy=policy,
                approval_runtime_enabled=approval.runtime is not None,
                runtime=supplied.get(key),
            ))
    unknown = set(supplied) - expected_runtime_keys
    if unknown:
        raise ValueError(f"unknown attention runtime binding(s): {sorted(unknown)!r}")
    applications = []
    for scope, (label, icon_ref) in _APPLICATION_META.items():
        classes = tuple(rows[scope])
        if not classes:
            raise ValueError(f"production attention application has no classes: {scope}")
        applications.append(AttentionApplicationRegistration(
            application_scope=scope,
            label=label,
            icon_ref=icon_ref,
            open_mode="registered_renderer",
            classes=classes,
        ))
    return AttentionRegistry(applications)


__all__ = [
    "MAX_SOURCE_VERSION",
    "AttentionIndexError",
    "AttentionApplicationRecord",
    "AttentionApplicationRegistration",
    "AttentionClassPolicy",
    "AttentionClassRegistration",
    "AttentionProjectionPlan",
    "AttentionPublicationRuntime",
    "AttentionRegistry",
    "AttentionSourceEvidence",
    "RegisteredAttentionProducer",
    "_bounded_version",
    "build_production_attention_registry",
]
