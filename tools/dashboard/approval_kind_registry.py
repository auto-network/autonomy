"""Closed, code-owned catalog for the Settings-native approval service.

This module declares what an approval *means*.  It intentionally contains no
route registration and performs no application work.  Per-kind migrations
later replace a descriptor's absent runtime with one complete adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping


_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase code identifier")
    return value


class RequesterPolicy(str, Enum):
    SESSION_PRINCIPAL = "session_principal"
    REGISTERED_SERVICE = "registered_service"
    INTERNAL_PRODUCER = "internal_producer"


class DeciderPolicy(str, Enum):
    PERSONAL_OPERATOR = "personal_operator"


class AuthorityRequirement(str, Enum):
    OPERATOR_SESSION = "operator_session"
    PERSONAL_ROOT = "personal_root"
    ORGANIZATION_SIGNING_KEY = "organization_signing_key"
    VAULT_POLICY = "vault_policy"


class ExpiryMode(str, Enum):
    NEVER = "never"
    FIXED = "fixed"
    BOUNDED = "bounded"
    TRUSTED_SOURCE_DEADLINE = "trusted_source_deadline"


@dataclass(frozen=True, slots=True)
class ApprovalExpiryPolicy:
    mode: ExpiryMode
    fixed_seconds: int | None = None
    minimum_seconds: int | None = None
    maximum_seconds: int | None = None
    default_seconds: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, ExpiryMode):
            raise ValueError("unknown approval expiry mode")
        values = (
            self.fixed_seconds,
            self.minimum_seconds,
            self.maximum_seconds,
            self.default_seconds,
        )
        if any(value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        )
               for value in values):
            raise ValueError("expiry durations must be positive integers")
        if self.mode is ExpiryMode.NEVER or self.mode is ExpiryMode.TRUSTED_SOURCE_DEADLINE:
            if any(value is not None for value in values):
                raise ValueError(f"{self.mode.value} expiry cannot carry duration bounds")
        elif self.mode is ExpiryMode.FIXED:
            if self.fixed_seconds is None or any(
                value is not None
                for value in (self.minimum_seconds, self.maximum_seconds, self.default_seconds)
            ):
                raise ValueError("fixed expiry requires only fixed_seconds")
        elif self.mode is ExpiryMode.BOUNDED:
            if self.fixed_seconds is not None or None in (
                self.minimum_seconds, self.maximum_seconds, self.default_seconds,
            ):
                raise ValueError("bounded expiry requires minimum, maximum, and default")
            assert self.minimum_seconds is not None
            assert self.maximum_seconds is not None
            assert self.default_seconds is not None
            if not self.minimum_seconds <= self.default_seconds <= self.maximum_seconds:
                raise ValueError("bounded expiry default must be within its bounds")


@dataclass(frozen=True, slots=True)
class ApplicationScopePolicy:
    fixed: str | None = None
    by_producer: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if (self.fixed is None) == (self.by_producer is None):
            raise ValueError("application scope policy requires exactly one source")
        if self.fixed is not None:
            _identifier(self.fixed, "fixed application scope")
        if self.by_producer is not None:
            clean = dict(self.by_producer)
            if not clean or any(
                not isinstance(key, str) or not _IDENTIFIER_RE.fullmatch(key)
                or not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value)
                for key, value in clean.items()
            ):
                raise ValueError("producer application map cannot be empty")
            object.__setattr__(self, "by_producer", MappingProxyType(clean))

    @property
    def applications(self) -> frozenset[str]:
        if self.fixed is not None:
            return frozenset({self.fixed})
        assert self.by_producer is not None
        return frozenset(self.by_producer.values())

    def resolve(self, producer_id: str | None = None) -> str:
        if self.fixed is not None:
            return self.fixed
        assert self.by_producer is not None
        if producer_id is None or producer_id not in self.by_producer:
            raise ValueError("producer is not registered for this application scope")
        return self.by_producer[producer_id]


@dataclass(frozen=True, slots=True)
class ApprovalAttentionClass:
    application_scope: str
    notification_class: str
    renderer_id: str


class ApprovalAttentionClassCatalog:
    def __init__(self, entries: Iterable[ApprovalAttentionClass]):
        rows: dict[tuple[str, str], ApprovalAttentionClass] = {}
        for entry in entries:
            if not isinstance(entry, ApprovalAttentionClass):
                raise ValueError("approval attention class entry has the wrong type")
            _identifier(entry.application_scope, "attention application")
            _identifier(entry.notification_class, "notification class")
            _identifier(entry.renderer_id, "renderer ID")
            key = (entry.application_scope, entry.notification_class)
            if key in rows:
                raise ValueError(f"duplicate approval attention class: {key!r}")
            if not all((entry.application_scope, entry.notification_class, entry.renderer_id)):
                raise ValueError("approval attention class fields are required")
            rows[key] = entry
        self._entries = MappingProxyType(rows)

    def require(self, application: str, notification_class: str, renderer_id: str) -> None:
        item = self._entries.get((application, notification_class))
        if item is None:
            raise ValueError(
                f"approval attention class is not registered: "
                f"{application}/{notification_class}"
            )
        if item.renderer_id != renderer_id:
            raise ValueError("approval attention class renderer mismatch")

    @property
    def entries(self) -> Mapping[tuple[str, str], ApprovalAttentionClass]:
        return self._entries


@dataclass(frozen=True, slots=True)
class ApprovalPlanningContext:
    approval_id: str
    planning_time: float
    requester_ref: Mapping[str, str]
    application_scope: str
    producer_id: str | None = None
    requester_principal_kind: str | None = None
    requester_org: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalDecisionContext:
    """Service-owned facts for validating one proposed human decision.

    ``decision_time`` is the exact instant already used by ApprovalService for
    expiry reconciliation and, if accepted, the resolution's ``resolved_at``.
    A kind validator must never sample a second clock for deadline authority.
    """

    approval_id: str
    decision_time: float


@dataclass(frozen=True, slots=True)
class ApprovalRequestPlan:
    subject_ref: str
    safe_review: Mapping[str, Any]
    request: Mapping[str, Any]
    staged: Mapping[str, Any] | None = None
    requested_expiry_seconds: int | None = None
    trusted_source_expires_at: float | None = None


RequestPlanner = Callable[
    [ApprovalPlanningContext, Mapping[str, Any]],
    ApprovalRequestPlan | Mapping[str, Any],
]
DecisionValidator = Callable[
    [ApprovalDecisionContext, Mapping[str, Any], Mapping[str, Any], bool],
    Mapping[str, Any],
]
ResultRefBuilder = Callable[[str, Mapping[str, Any], Mapping[str, Any]], str]


@dataclass(frozen=True, slots=True)
class ApprovalKindRuntime:
    request_planner: RequestPlanner
    decision_validator: DecisionValidator
    resolution_consumer_id: str
    result_ref_builder: ResultRefBuilder | None = None

    def __post_init__(self) -> None:
        if not callable(self.request_planner) or not callable(self.decision_validator):
            raise ValueError("approval kind requires one complete runtime")
        _identifier(self.resolution_consumer_id, "approval runtime consumer")
        if self.result_ref_builder is not None and not callable(self.result_ref_builder):
            raise ValueError("approval result reference builder must be callable")


@dataclass(frozen=True, slots=True)
class ApprovalKindRegistration:
    kind: str
    application_scope_policy: ApplicationScopePolicy
    notification_class: str
    renderer_id: str
    requester_policy: RequesterPolicy
    decider_policy: DeciderPolicy
    authority_requirement: AuthorityRequirement
    request_expiry_policy: ApprovalExpiryPolicy
    runtime: ApprovalKindRuntime | None = None

    def __post_init__(self) -> None:
        _identifier(self.kind, "approval kind")
        _identifier(self.notification_class, "notification class")
        _identifier(self.renderer_id, "renderer ID")
        if not isinstance(self.application_scope_policy, ApplicationScopePolicy):
            raise ValueError("application scope policy is required")
        if not isinstance(self.requester_policy, RequesterPolicy):
            raise ValueError("unknown requester policy")
        if not isinstance(self.decider_policy, DeciderPolicy):
            raise ValueError("unknown decider policy")
        if not isinstance(self.authority_requirement, AuthorityRequirement):
            raise ValueError("unknown authority requirement")
        if not isinstance(self.request_expiry_policy, ApprovalExpiryPolicy):
            raise ValueError("approval expiry policy is required")
        if self.runtime is not None and not isinstance(self.runtime, ApprovalKindRuntime):
            raise ValueError("approval kind runtime must be complete or absent")


@dataclass(frozen=True, slots=True)
class RegisteredApprovalProducer:
    """A code-minted producer handle; route bodies never construct this type."""

    producer_id: str
    label: str
    allowed_kinds: frozenset[str]
    stable_source_ids: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.producer_id, str) or not _IDENTIFIER_RE.fullmatch(self.producer_id)
            or not isinstance(self.label, str) or not self.label
            or not isinstance(self.allowed_kinds, frozenset) or not self.allowed_kinds
            or any(
                not isinstance(kind, str) or not _IDENTIFIER_RE.fullmatch(kind)
                for kind in self.allowed_kinds
            )
        ):
            raise ValueError("registered approval producer is incomplete")


class ApprovalKindRegistry:
    def __init__(
        self,
        registrations: Iterable[ApprovalKindRegistration],
        attention_classes: ApprovalAttentionClassCatalog,
        *,
        consumer_ids: Iterable[str],
    ):
        consumers = frozenset(consumer_ids)
        for consumer in consumers:
            _identifier(consumer, "approval consumer ID")
        kinds: dict[str, ApprovalKindRegistration] = {}
        for item in registrations:
            if not isinstance(item, ApprovalKindRegistration):
                raise ValueError("approval kind registration has the wrong type")
            if item.kind in kinds:
                raise ValueError(f"duplicate approval kind: {item.kind}")
            for application in item.application_scope_policy.applications:
                attention_classes.require(
                    application, item.notification_class, item.renderer_id,
                )
            if item.runtime is not None and item.runtime.resolution_consumer_id not in consumers:
                raise ValueError(
                    f"approval runtime consumer is not registered: "
                    f"{item.runtime.resolution_consumer_id}"
                )
            kinds[item.kind] = item
        self._kinds = MappingProxyType(kinds)
        self.attention_classes = attention_classes
        self.consumer_ids = consumers

    @property
    def kinds(self) -> Mapping[str, ApprovalKindRegistration]:
        return self._kinds

    def require(self, kind: str, *, enabled: bool = True) -> ApprovalKindRegistration:
        item = self._kinds.get(kind)
        if item is None:
            raise KeyError(kind)
        if enabled and item.runtime is None:
            raise RuntimeError(kind)
        return item


_PRODUCTION_ROWS = (
    ("commit_sign", "worktrees", RequesterPolicy.SESSION_PRINCIPAL,
     AuthorityRequirement.ORGANIZATION_SIGNING_KEY, ApprovalExpiryPolicy(ExpiryMode.NEVER)),
    ("jira_write", "jira", RequesterPolicy.SESSION_PRINCIPAL,
     AuthorityRequirement.OPERATOR_SESSION, ApprovalExpiryPolicy(ExpiryMode.NEVER)),
    ("link_publish", "links", RequesterPolicy.SESSION_PRINCIPAL,
     AuthorityRequirement.ORGANIZATION_SIGNING_KEY,
     ApprovalExpiryPolicy(ExpiryMode.TRUSTED_SOURCE_DEADLINE)),
    ("link_revoke", "links", RequesterPolicy.SESSION_PRINCIPAL,
     AuthorityRequirement.ORGANIZATION_SIGNING_KEY, ApprovalExpiryPolicy(ExpiryMode.NEVER)),
    ("dashboard_access", "sessions", RequesterPolicy.SESSION_PRINCIPAL,
     AuthorityRequirement.PERSONAL_ROOT,
     ApprovalExpiryPolicy(ExpiryMode.FIXED, fixed_seconds=7200)),
    ("visitor_token", "mission_control", RequesterPolicy.SESSION_PRINCIPAL,
     AuthorityRequirement.OPERATOR_SESSION, ApprovalExpiryPolicy(ExpiryMode.NEVER)),
    ("secure_setting", "vault", RequesterPolicy.SESSION_PRINCIPAL,
     AuthorityRequirement.OPERATOR_SESSION, ApprovalExpiryPolicy(ExpiryMode.NEVER)),
    ("mcp_peer_link", "relay", RequesterPolicy.REGISTERED_SERVICE,
     AuthorityRequirement.OPERATOR_SESSION, ApprovalExpiryPolicy(ExpiryMode.NEVER)),
    ("mcp_crosstalk", "relay", RequesterPolicy.REGISTERED_SERVICE,
     AuthorityRequirement.OPERATOR_SESSION, ApprovalExpiryPolicy(ExpiryMode.NEVER)),
    ("fleet_machine_admission", "fleet", RequesterPolicy.INTERNAL_PRODUCER,
     AuthorityRequirement.PERSONAL_ROOT,
     ApprovalExpiryPolicy(ExpiryMode.TRUSTED_SOURCE_DEADLINE)),
    ("external_service_access", None, RequesterPolicy.INTERNAL_PRODUCER,
     AuthorityRequirement.OPERATOR_SESSION, ApprovalExpiryPolicy(ExpiryMode.NEVER)),
    ("vault_open", "vault", RequesterPolicy.SESSION_PRINCIPAL,
     AuthorityRequirement.VAULT_POLICY,
     ApprovalExpiryPolicy(
         ExpiryMode.BOUNDED, minimum_seconds=1, maximum_seconds=300,
         default_seconds=60,
     )),
)


def build_production_registry(
    *, runtimes: Mapping[str, ApprovalKindRuntime] | None = None,
) -> ApprovalKindRegistry:
    """Build the exact production catalog with a bounded runtime delta.

    Registration metadata is code-owned and immutable.  Migrations may only
    activate a complete runtime for one of the twelve canonical kinds; they
    cannot add, remove, or relabel a kind through dependency injection.
    """
    supplied = {} if runtimes is None else dict(runtimes)
    expected = {row[0] for row in _PRODUCTION_ROWS}
    unknown = set(supplied) - expected
    if unknown or any(
        not isinstance(runtime, ApprovalKindRuntime)
        for runtime in supplied.values()
    ):
        raise ValueError(
            f"unknown or incomplete production approval runtime(s): {sorted(unknown)!r}"
        )
    registrations: list[ApprovalKindRegistration] = []
    classes: list[ApprovalAttentionClass] = []
    for kind, application, requester, authority, expiry in _PRODUCTION_ROWS:
        scope = (
            ApplicationScopePolicy(fixed=application)
            if application is not None
            else ApplicationScopePolicy(by_producer={
                "external_service.dropbox_enrollment": "dropbox",
            })
        )
        notification_class = f"approval.{kind}.requested"
        renderer_id = f"approval.{kind}.review"
        registrations.append(ApprovalKindRegistration(
            kind=kind,
            application_scope_policy=scope,
            notification_class=notification_class,
            renderer_id=renderer_id,
            requester_policy=requester,
            decider_policy=DeciderPolicy.PERSONAL_OPERATOR,
            authority_requirement=authority,
            request_expiry_policy=expiry,
            runtime=supplied.get(kind),
        ))
        for app in scope.applications:
            classes.append(ApprovalAttentionClass(app, notification_class, renderer_id))
    return ApprovalKindRegistry(
        registrations,
        ApprovalAttentionClassCatalog(classes),
        consumer_ids={
            runtime.resolution_consumer_id for runtime in supplied.values()
        },
    )


PRODUCTION_APPROVAL_REGISTRY = build_production_registry()
