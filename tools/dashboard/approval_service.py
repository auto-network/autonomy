"""Settings-native central approval origin authority.

The service owns request derivation and the first valid human/cancellation/
expiry answer.  It deliberately does not expose HTTP routes and never invokes
the application that consumes a granted resolution.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import threading
import time
import unicodedata
from typing import Any, Callable, Mapping, Protocol

from tools.dashboard import api_auth
from tools.dashboard.approval_kind_registry import (
    ApprovalDecisionContext,
    ApprovalExpiryPolicy,
    ApprovalKindRegistration,
    ApprovalKindRegistry,
    ApprovalPlanningContext,
    ApprovalRequestPlan,
    ExpiryMode,
    RegisteredApprovalProducer,
    RequesterPolicy,
)
from tools.graph import settings_ops
from tools.graph.schemas.central_attention import (
    APPROVAL_REQUEST_SET_ID,
    APPROVAL_RESOLUTION_SET_ID,
    CENTRAL_ATTENTION_REVISION,
    ApprovalRequestV1,
    ApprovalResolutionV1,
)
from tools.graph.schemas.personal_identity import PERSONAL_IDENTITY_SET_ID


logger = logging.getLogger(__name__)

_FORGED_OUTER_FIELDS = frozenset({
    "approval_id",
    "kind",
    "session",
    "requester_ref",
    "decider",
    "audience",
    "application_scope",
    "organization",
    "org",
    "producer_id",
    "source_approval_id",
    "source_identity",
})
_ROOT_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{16,256}$")
_ALLOWED_HUMAN_METHODS = frozenset({"bootstrap", "passkey", "password"})
_ACTOR_SEAL = object()
_REQUESTER_DOMAIN = "dashboard.approval.requester"
_CENTRAL_ID_PREFIX = "central-"


class ApprovalServiceError(RuntimeError):
    """Bounded domain failure; staged data and foreign records never appear."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        resolution: "ApprovalRecord | None" = None,
    ):
        self.code = code
        self.resolution = resolution
        super().__init__(f"{code}: {message or code.replace('_', ' ')}")


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: str
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ApprovalStatus:
    state: str
    request: ApprovalRecord
    resolution: ApprovalRecord | None


@dataclass(frozen=True, slots=True)
class HumanApprovalActor:
    decider_ref: str
    _seal: object | None = None

    @classmethod
    def _verified(cls, decider_ref: str) -> "HumanApprovalActor":
        return cls(decider_ref=decider_ref, _seal=_ACTOR_SEAL)

    @property
    def verified(self) -> bool:
        return self._seal is _ACTOR_SEAL


class ApprovalStore(Protocol):
    def get_request(self, approval_id: str) -> ApprovalRecord | None: ...
    def get_resolution(self, approval_id: str) -> ApprovalRecord | None: ...
    def append_request(self, approval_id: str, payload: Mapping[str, Any]) -> ApprovalRecord: ...
    def append_resolution(self, approval_id: str, payload: Mapping[str, Any]) -> ApprovalRecord: ...


class InMemoryApprovalStore:
    """Hermetic store for service tests; it preserves append refusal semantics."""

    def __init__(self):
        self._requests: dict[str, ApprovalRecord] = {}
        self._resolutions: dict[str, list[ApprovalRecord]] = {}

    @staticmethod
    def _copy(payload: Mapping[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    def get_request(self, approval_id: str) -> ApprovalRecord | None:
        row = self._requests.get(approval_id)
        return None if row is None else ApprovalRecord(row.approval_id, self._copy(row.payload))

    def get_resolution(self, approval_id: str) -> ApprovalRecord | None:
        rows = self._resolutions.get(approval_id, ())
        return (
            ApprovalRecord(rows[0].approval_id, self._copy(rows[0].payload))
            if rows else None
        )

    def append_request(self, approval_id: str, payload: Mapping[str, Any]) -> ApprovalRecord:
        if approval_id in self._requests:
            raise RuntimeError("request already exists")
        stored = ApprovalRecord(approval_id, self._copy(payload))
        self._requests[approval_id] = stored
        return ApprovalRecord(approval_id, self._copy(stored.payload))

    def append_resolution(self, approval_id: str, payload: Mapping[str, Any]) -> ApprovalRecord:
        if self.get_resolution(approval_id) is not None:
            raise RuntimeError("resolution already exists")
        stored = ApprovalRecord(approval_id, self._copy(payload))
        self._resolutions.setdefault(approval_id, []).append(stored)
        return ApprovalRecord(approval_id, self._copy(stored.payload))

    def resolution_count(self, approval_id: str) -> int:
        return len(self._resolutions.get(approval_id, ()))

    def close(self) -> None:
        return None


class SettingsApprovalStore:
    """Personal Settings persistence used by the production service."""

    @staticmethod
    def _read(set_id: str, approval_id: str) -> ApprovalRecord | None:
        rows = settings_ops.read_set(set_id, org=None, peers=[]).to_dict()
        row = rows.get(approval_id)
        if row is None or not isinstance(row.payload, dict):
            return None
        try:
            if set_id == APPROVAL_REQUEST_SET_ID:
                ApprovalRequestV1.validate(row.payload)
            else:
                ApprovalResolutionV1.validate(row.payload)
        except Exception as exc:
            raise RuntimeError("stored approval row is invalid") from exc
        return ApprovalRecord(approval_id=approval_id, payload=dict(row.payload))

    def get_request(self, approval_id: str) -> ApprovalRecord | None:
        return self._read(APPROVAL_REQUEST_SET_ID, approval_id)

    def get_resolution(self, approval_id: str) -> ApprovalRecord | None:
        return self._read(APPROVAL_RESOLUTION_SET_ID, approval_id)

    def append_request(self, approval_id: str, payload: Mapping[str, Any]) -> ApprovalRecord:
        settings_ops.add_setting(
            APPROVAL_REQUEST_SET_ID,
            CENTRAL_ATTENTION_REVISION,
            approval_id,
            dict(payload),
            org=None,
            state="raw",
        )
        return ApprovalRecord(approval_id, dict(payload))

    def append_resolution(self, approval_id: str, payload: Mapping[str, Any]) -> ApprovalRecord:
        settings_ops.add_setting(
            APPROVAL_RESOLUTION_SET_ID,
            CENTRAL_ATTENTION_REVISION,
            approval_id,
            dict(payload),
            org=None,
            state="raw",
        )
        return ApprovalRecord(approval_id, dict(payload))

    def close(self) -> None:
        return None


class _ApprovalLocks:
    """Bounded process-wide keyed critical sections shared by all instances."""

    _locks = tuple(threading.RLock() for _ in range(257))

    @classmethod
    def for_id(cls, approval_id: str) -> threading.RLock:
        if not isinstance(approval_id, str) or not _SOURCE_ID_RE.fullmatch(approval_id):
            raise ApprovalServiceError("invalid_request", "invalid approval ID")
        digest = hashlib.sha256(approval_id.encode("utf-8")).digest()
        return cls._locks[int.from_bytes(digest[:4], "big") % len(cls._locks)]


def _bounded_principal_text(value: Any, *, label: str, maximum_bytes: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise ApprovalServiceError("unauthenticated", f"invalid {label}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise ApprovalServiceError("unauthenticated", f"invalid {label}") from exc
    if len(encoded) > maximum_bytes:
        raise ApprovalServiceError("unauthenticated", f"invalid {label}")
    return value


def canonical_session_requester_id(principal: api_auth.ApiPrincipal) -> str:
    """Derive the durable, scope-bound identity of one session requester.

    The row deliberately stores neither the raw session subject nor its
    organization.  Local and organization sessions therefore cannot collide
    even when their display handles happen to match.
    """
    if not isinstance(principal, api_auth.ApiPrincipal) or principal.kind not in (
        api_auth.ApiPrincipalKind.LOCAL_SESSION,
        api_auth.ApiPrincipalKind.ORG_SESSION,
    ):
        raise ApprovalServiceError("unauthenticated")
    subject = _bounded_principal_text(
        principal.subject, label="session subject", maximum_bytes=512,
    )
    if principal.kind is api_auth.ApiPrincipalKind.LOCAL_SESSION:
        if principal.org is not None:
            raise ApprovalServiceError("unauthenticated", "local session cannot carry org")
        org = None
    else:
        org = _bounded_principal_text(
            principal.org, label="session organization", maximum_bytes=128,
        )
    canonical = json.dumps(
        [_REQUESTER_DOMAIN, 1, principal.kind.value, org, subject],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(hashlib.sha256(canonical).digest()).decode(
        "ascii",
    ).rstrip("=")


def resolve_personal_root_public_key() -> str:
    """Resolve the public personal root, including compatible armor-only rows."""
    try:
        rows = settings_ops.read_set(
            PERSONAL_IDENTITY_SET_ID, org=None, peers=[],
        ).to_dict()
        row = rows.get("default")
        if row is None or not isinstance(row.payload, dict):
            raise ApprovalServiceError("not_configured")
        payload = row.payload
        explicit = payload.get("root_pub")
        if explicit is not None and (
            not isinstance(explicit, str) or not _ROOT_RE.fullmatch(explicit)
        ):
            raise ApprovalServiceError("not_configured")
        armor = payload.get("armored_private_key")
        if not isinstance(armor, str):
            raise ApprovalServiceError("not_configured")
        from tools.network.idkit.armor import armor_root_pub
        derived = armor_root_pub(armor)
        if not isinstance(derived, str) or not _ROOT_RE.fullmatch(derived):
            raise ApprovalServiceError("not_configured")
        if (
            explicit is not None
            and derived is not None
            and not hmac.compare_digest(explicit, derived)
        ):
            raise ApprovalServiceError("not_configured")
        root = explicit or derived
        if root is None:
            raise ApprovalServiceError("not_configured")
        return root
    except ApprovalServiceError:
        raise
    except Exception as exc:
        raise ApprovalServiceError("not_configured") from exc


def resolve_human_approval_actor(
    request: Any,
    *,
    root_resolver: Callable[[], str] = resolve_personal_root_public_key,
) -> HumanApprovalActor:
    """Mint a human actor only from a verified, human-unlocked cookie session."""
    principal = api_auth.principal_from_request(request)
    if principal.kind is not api_auth.ApiPrincipalKind.OPERATOR_COOKIE or not principal.subject:
        raise ApprovalServiceError("unauthenticated")
    try:
        from tools.dashboard import unlock_routes
        session = unlock_routes.session_from_request(request)
    except Exception as exc:
        raise ApprovalServiceError("unauthenticated") from exc
    if not isinstance(session, dict):
        raise ApprovalServiceError("unauthenticated")
    sid = session.get("sid")
    method = session.get("method")
    if (
        not isinstance(sid, str)
        or not hmac.compare_digest(sid, principal.subject)
        or method not in _ALLOWED_HUMAN_METHODS
    ):
        raise ApprovalServiceError("unauthenticated")
    try:
        root = root_resolver()
    except ApprovalServiceError:
        raise
    except Exception as exc:
        raise ApprovalServiceError("not_configured") from exc
    if not isinstance(root, str) or not _ROOT_RE.fullmatch(root):
        raise ApprovalServiceError("not_configured")
    return HumanApprovalActor._verified(root)


def normalize_unix_milliseconds_deadline(value: Any) -> float | None:
    """Normalize a trusted Link/Fleet native millisecond deadline."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("trusted source deadline must be Unix milliseconds")
    if value == 0:
        return None
    try:
        seconds = float(value) / 1000.0
    except OverflowError as exc:
        raise ValueError("trusted source deadline must be finite") from exc
    if not math.isfinite(seconds):
        raise ValueError("trusted source deadline must be finite")
    return round(seconds, 3)


class ApprovalService:
    def __init__(
        self,
        *,
        registry: ApprovalKindRegistry,
        store: ApprovalStore | None = None,
        personal_root_resolver: Callable[[], str] = resolve_personal_root_public_key,
        session_label_resolver: Callable[[str], str | None] | None = None,
        registered_service_label_resolver: Callable[[str], str | None] | None = None,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
        after_commit: Callable[[str, str], None] | None = None,
    ):
        self.registry = registry
        self.store = store or SettingsApprovalStore()
        self._root_resolver = personal_root_resolver
        self._session_label = session_label_resolver or (lambda _subject: None)
        self._service_label = registered_service_label_resolver or (lambda _subject: None)
        self._clock = clock
        self._id_factory = id_factory or (
            lambda: _CENTRAL_ID_PREFIX + secrets.token_urlsafe(24)
        )
        self._after_commit = after_commit

    def _registration(self, kind: str) -> ApprovalKindRegistration:
        try:
            return self.registry.require(kind)
        except KeyError as exc:
            raise ApprovalServiceError("unknown_kind") from exc
        except RuntimeError as exc:
            raise ApprovalServiceError("kind_disabled") from exc

    @staticmethod
    def _validate_outer(body: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(body, Mapping):
            raise ApprovalServiceError("invalid_request")
        if _FORGED_OUTER_FIELDS.intersection(body):
            raise ApprovalServiceError("invalid_request", "routing selectors are server-derived")
        try:
            return json.loads(json.dumps(dict(body)))
        except (TypeError, ValueError) as exc:
            raise ApprovalServiceError("invalid_request") from exc

    def _root(self) -> str:
        try:
            root = self._root_resolver()
        except ApprovalServiceError:
            raise
        except Exception as exc:
            raise ApprovalServiceError("not_configured") from exc
        if not isinstance(root, str) or not _ROOT_RE.fullmatch(root):
            raise ApprovalServiceError("not_configured")
        return root

    def _requester_from_principal(
        self, registration: ApprovalKindRegistration, principal: api_auth.ApiPrincipal,
    ) -> dict[str, str]:
        policy = registration.requester_policy
        if policy is RequesterPolicy.SESSION_PRINCIPAL:
            if principal.kind not in (
                api_auth.ApiPrincipalKind.LOCAL_SESSION,
                api_auth.ApiPrincipalKind.ORG_SESSION,
            ) or not principal.subject:
                raise ApprovalServiceError("unauthenticated")
            requester = {
                "kind": "session",
                "id": canonical_session_requester_id(principal),
            }
            label = self._session_label(principal.subject)
        elif policy is RequesterPolicy.REGISTERED_SERVICE:
            if principal.kind is not api_auth.ApiPrincipalKind.MCP_SERVICE or not principal.subject:
                raise ApprovalServiceError("unauthenticated")
            requester = {"kind": "registered_service", "id": principal.subject}
            label = self._service_label(principal.subject)
        else:
            raise ApprovalServiceError("unauthenticated")
        if label:
            requester["label"] = label
        return requester

    @staticmethod
    def _requester_from_producer(
        registration: ApprovalKindRegistration,
        producer: RegisteredApprovalProducer,
    ) -> dict[str, str]:
        if registration.requester_policy is not RequesterPolicy.INTERNAL_PRODUCER:
            raise ApprovalServiceError("unauthenticated")
        if registration.kind not in producer.allowed_kinds:
            raise ApprovalServiceError("unauthenticated")
        return {
            "kind": "internal_service",
            "id": producer.producer_id,
            "label": producer.label,
        }

    def create_from_principal(
        self,
        kind: str,
        principal: api_auth.ApiPrincipal,
        body: Mapping[str, Any],
    ) -> ApprovalRecord:
        registration = self._registration(kind)
        safe_body = self._validate_outer(body)
        requester = self._requester_from_principal(registration, principal)
        approval_id = self._id_factory()
        if not isinstance(approval_id, str) or not _SOURCE_ID_RE.fullmatch(approval_id):
            raise ApprovalServiceError("invalid_request", "approval ID generator failed")
        return self._create(
            registration, requester, safe_body, approval_id=approval_id,
            producer_id=None, stable_retry=False,
            requester_principal_kind=principal.kind.value,
            requester_org=principal.org,
        )

    def create_from_producer(
        self,
        kind: str,
        producer: RegisteredApprovalProducer,
        body: Mapping[str, Any],
        *,
        source_approval_id: str | None = None,
    ) -> ApprovalRecord:
        registration = self._registration(kind)
        safe_body = self._validate_outer(body)
        requester = self._requester_from_producer(registration, producer)
        if source_approval_id is not None:
            if (
                not producer.stable_source_ids
                or not _SOURCE_ID_RE.fullmatch(source_approval_id)
            ):
                raise ApprovalServiceError(
                    "invalid_request", "source approval ID is not authorized",
                )
            approval_id = source_approval_id
            stable_retry = True
        else:
            approval_id = self._id_factory()
            stable_retry = False
            if not isinstance(approval_id, str) or not _SOURCE_ID_RE.fullmatch(approval_id):
                raise ApprovalServiceError("invalid_request", "approval ID generator failed")
        return self._create(
            registration, requester, safe_body, approval_id=approval_id,
            producer_id=producer.producer_id, stable_retry=stable_retry,
            requester_principal_kind=None,
            requester_org=None,
        )

    @staticmethod
    def _coerce_plan(raw: ApprovalRequestPlan | Mapping[str, Any]) -> ApprovalRequestPlan:
        if isinstance(raw, ApprovalRequestPlan):
            plan = raw
        elif not isinstance(raw, Mapping):
            raise ApprovalServiceError("invalid_request")
        else:
            allowed = {
                "subject_ref", "safe_review", "request", "staged",
                "requested_expiry_seconds", "trusted_source_expires_at",
            }
            if set(raw) - allowed:
                raise ApprovalServiceError("invalid_request")
            try:
                plan = ApprovalRequestPlan(**dict(raw))
            except TypeError as exc:
                raise ApprovalServiceError("invalid_request") from exc
        if (
            not isinstance(plan.subject_ref, str)
            or not isinstance(plan.safe_review, Mapping)
            or not isinstance(plan.request, Mapping)
            or (plan.staged is not None and not isinstance(plan.staged, Mapping))
        ):
            raise ApprovalServiceError("invalid_request")
        return plan

    @staticmethod
    def _expiry_from_plan(
        policy: ApprovalExpiryPolicy,
        plan: ApprovalRequestPlan,
        created_at: float,
    ) -> tuple[float | None, int | float | None]:
        bounded = plan.requested_expiry_seconds
        source = plan.trusted_source_expires_at
        if policy.mode is ExpiryMode.NEVER:
            if bounded is not None or source is not None:
                raise ApprovalServiceError("invalid_request")
            return None, None
        if policy.mode is ExpiryMode.FIXED:
            if bounded is not None or source is not None:
                raise ApprovalServiceError("invalid_request")
            assert policy.fixed_seconds is not None
            return created_at + policy.fixed_seconds, policy.fixed_seconds
        if policy.mode is ExpiryMode.BOUNDED:
            if source is not None:
                raise ApprovalServiceError("invalid_request")
            seconds = policy.default_seconds if bounded is None else bounded
            if (
                isinstance(seconds, bool)
                or not isinstance(seconds, int)
                or policy.minimum_seconds is None
                or policy.maximum_seconds is None
                or not policy.minimum_seconds <= seconds <= policy.maximum_seconds
            ):
                raise ApprovalServiceError("invalid_request")
            return created_at + seconds, seconds
        if bounded is not None:
            raise ApprovalServiceError("invalid_request")
        if source is None:
            return None, None
        if isinstance(source, bool) or not isinstance(source, (int, float)):
            raise ApprovalServiceError("invalid_request")
        deadline = float(source)
        if not math.isfinite(deadline):
            raise ApprovalServiceError("invalid_request")
        deadline = round(deadline, 3)
        if deadline <= created_at:
            raise ApprovalServiceError("source_expired")
        return deadline, deadline

    @staticmethod
    def _stored_effective_expiry(
        registration: ApprovalKindRegistration,
        payload: Mapping[str, Any],
    ) -> int | float | None:
        policy = registration.request_expiry_policy
        created = payload.get("created_at")
        expires = payload.get("expires_at")
        if policy.mode is ExpiryMode.NEVER:
            return None
        if policy.mode is ExpiryMode.FIXED:
            return policy.fixed_seconds
        if policy.mode is ExpiryMode.TRUSTED_SOURCE_DEADLINE:
            return None if expires is None else round(float(expires), 3)
        if not isinstance(created, (int, float)) or not isinstance(expires, (int, float)):
            raise ApprovalServiceError("request_conflict")
        duration = float(expires) - float(created)
        rounded = round(duration)
        if (
            not math.isclose(duration, rounded, abs_tol=1e-6)
            or policy.minimum_seconds is None
            or policy.maximum_seconds is None
            or not policy.minimum_seconds <= rounded <= policy.maximum_seconds
        ):
            raise ApprovalServiceError("request_conflict")
        return rounded

    @staticmethod
    def _semantic(payload: Mapping[str, Any], effective_expiry: Any) -> bytes:
        value = {
            key: payload.get(key)
            for key in (
                "application_scope", "kind", "requester_ref", "decider",
                "subject_ref", "safe_review", "request", "staged", "source_version",
            )
        }
        value["effective_expiry"] = effective_expiry
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    def _create(
        self,
        registration: ApprovalKindRegistration,
        requester: Mapping[str, str],
        body: Mapping[str, Any],
        *,
        approval_id: str,
        producer_id: str | None,
        stable_retry: bool,
        requester_principal_kind: str | None,
        requester_org: str | None,
    ) -> ApprovalRecord:
        lock = _ApprovalLocks.for_id(approval_id)
        with lock:
            existing = self._store_call(self.store.get_request, approval_id)
            if existing is not None and not stable_retry:
                raise ApprovalServiceError("request_conflict")
            planning_time = (
                float(existing.payload["created_at"])
                if existing is not None else self._now(None)
            )
            try:
                application = registration.application_scope_policy.resolve(producer_id)
            except ValueError as exc:
                raise ApprovalServiceError("unauthenticated") from exc
            context = ApprovalPlanningContext(
                approval_id=approval_id,
                planning_time=planning_time,
                requester_ref=dict(requester),
                application_scope=application,
                producer_id=producer_id,
                requester_principal_kind=requester_principal_kind,
                requester_org=requester_org,
            )
            assert registration.runtime is not None
            try:
                plan = self._coerce_plan(registration.runtime.request_planner(context, body))
            except ApprovalServiceError:
                raise
            except Exception as exc:
                raise ApprovalServiceError("invalid_request") from exc
            expires_at, effective_expiry = self._expiry_from_plan(
                registration.request_expiry_policy, plan, planning_time,
            )
            payload: dict[str, Any] = {
                "application_scope": application,
                "kind": registration.kind,
                "requester_ref": dict(requester),
                "decider": {"kind": "person", "id": self._root()},
                "subject_ref": plan.subject_ref,
                "safe_review": dict(plan.safe_review),
                "request": dict(plan.request),
                "created_at": planning_time,
                "source_version": 1,
            }
            if plan.staged is not None:
                payload["staged"] = dict(plan.staged)
            if expires_at is not None:
                payload["expires_at"] = expires_at
            try:
                ApprovalRequestV1.validate(payload)
            except Exception as exc:
                raise ApprovalServiceError("invalid_request") from exc
            if existing is not None:
                stored_effective = self._stored_effective_expiry(registration, existing.payload)
                if not hmac.compare_digest(
                    self._semantic(existing.payload, stored_effective),
                    self._semantic(payload, effective_expiry),
                ):
                    raise ApprovalServiceError("request_conflict")
                return existing
            written = self._store_call(self.store.append_request, approval_id, payload)
            self._wake("request", approval_id)
            return written

    def get_request(self, approval_id: str, *, now: float | None = None) -> ApprovalRecord:
        with _ApprovalLocks.for_id(approval_id):
            request = self._require_request(approval_id)
            self._reconcile_expiry_locked(request, self._now(now))
            return request

    def get_resolution(
        self, approval_id: str, *, now: float | None = None,
    ) -> ApprovalRecord | None:
        with _ApprovalLocks.for_id(approval_id):
            request = self._require_request(approval_id)
            resolution = self._store_call(self.store.get_resolution, approval_id)
            return resolution or self._reconcile_expiry_locked(request, self._now(now))

    def status(self, approval_id: str, *, now: float | None = None) -> ApprovalStatus:
        with _ApprovalLocks.for_id(approval_id):
            request = self._require_request(approval_id)
            resolution = self._store_call(self.store.get_resolution, approval_id)
            resolution = resolution or self._reconcile_expiry_locked(request, self._now(now))
            return ApprovalStatus(
                state="resolved" if resolution is not None else "open",
                request=request,
                resolution=resolution,
            )

    def status_for_principal(
        self,
        approval_id: str,
        principal: api_auth.ApiPrincipal,
        *,
        now: float | None = None,
    ) -> ApprovalStatus:
        """Return requester status only after authenticating the frozen session.

        This generic seam is deliberately session-only. Registered services
        and internal producers keep status inside their already-authenticated
        application routes.
        """
        with _ApprovalLocks.for_id(approval_id):
            request = self._require_request(approval_id)
            self._authorize_session_requester(request, principal)
            resolution = self._store_call(self.store.get_resolution, approval_id)
            resolution = resolution or self._reconcile_expiry_locked(
                request, self._now(now),
            )
            return ApprovalStatus(
                state="resolved" if resolution is not None else "open",
                request=request,
                resolution=resolution,
            )

    def reconcile_expiry(
        self, approval_id: str, *, now: float | None = None,
    ) -> ApprovalRecord | None:
        with _ApprovalLocks.for_id(approval_id):
            request = self._require_request(approval_id)
            existing = self._store_call(self.store.get_resolution, approval_id)
            return existing or self._reconcile_expiry_locked(request, self._now(now))

    def decide(
        self,
        approval_id: str,
        actor: HumanApprovalActor,
        *,
        outcome: str,
        decision: Mapping[str, Any],
        now: float | None = None,
    ) -> ApprovalRecord:
        with _ApprovalLocks.for_id(approval_id):
            request = self._require_request(approval_id)
            self._authorize_human(request, actor)
            if outcome not in ("granted", "declined") or not isinstance(decision, Mapping):
                raise ApprovalServiceError("invalid_decision")
            existing = self._store_call(self.store.get_resolution, approval_id)
            if existing is not None:
                return existing
            decision_time = self._now(now)
            expired = self._reconcile_expiry_locked(request, decision_time)
            if expired is not None:
                raise ApprovalServiceError("expired", resolution=expired)
            registration = self._registration(str(request.payload["kind"]))
            assert registration.runtime is not None
            try:
                validated = registration.runtime.decision_validator(
                    ApprovalDecisionContext(
                        approval_id=approval_id,
                        decision_time=decision_time,
                    ),
                    request.payload,
                    dict(decision),
                    outcome == "granted",
                )
            except Exception as exc:
                raise ApprovalServiceError("invalid_decision") from exc
            if not isinstance(validated, Mapping):
                raise ApprovalServiceError("invalid_decision")
            payload: dict[str, Any] = {
                "outcome": outcome,
                "decider_ref": actor.decider_ref,
                "resolved_at": decision_time,
                "decision": dict(validated),
            }
            if outcome == "granted" and registration.runtime.result_ref_builder is not None:
                try:
                    result_ref = registration.runtime.result_ref_builder(
                        approval_id, request.payload, dict(validated),
                    )
                except Exception as exc:
                    raise ApprovalServiceError("invalid_decision") from exc
                if not isinstance(result_ref, str):
                    raise ApprovalServiceError("invalid_decision")
                payload["result_ref"] = result_ref
            return self._commit_resolution(approval_id, payload)

    def cancel_from_principal(
        self,
        approval_id: str,
        principal: api_auth.ApiPrincipal,
        *,
        now: float | None = None,
    ) -> ApprovalRecord:
        with _ApprovalLocks.for_id(approval_id):
            request = self._require_request(approval_id)
            self._authorize_cancel_principal(request, principal)
            return self._cancel_locked(request, self._now(now))

    def cancel_from_producer(
        self,
        approval_id: str,
        producer: RegisteredApprovalProducer,
        *,
        now: float | None = None,
    ) -> ApprovalRecord:
        with _ApprovalLocks.for_id(approval_id):
            request = self._require_request(approval_id)
            expected = request.payload.get("requester_ref")
            if (
                not isinstance(expected, Mapping)
                or expected.get("kind") != "internal_service"
                or expected.get("id") != producer.producer_id
                or request.payload.get("kind") not in producer.allowed_kinds
            ):
                raise ApprovalServiceError("wrong_requester")
            return self._cancel_locked(request, self._now(now))

    def _cancel_locked(self, request: ApprovalRecord, now: float) -> ApprovalRecord:
        existing = self._store_call(self.store.get_resolution, request.approval_id)
        if existing is not None:
            return existing
        expired = self._reconcile_expiry_locked(request, now)
        if expired is not None:
            raise ApprovalServiceError("expired", resolution=expired)
        return self._commit_resolution(request.approval_id, {
            "outcome": "canceled",
            "resolved_at": now,
        })

    @staticmethod
    def _authorize_human(request: ApprovalRecord, actor: HumanApprovalActor) -> None:
        decider = request.payload.get("decider")
        if (
            not isinstance(actor, HumanApprovalActor)
            or not actor.verified
            or not isinstance(decider, Mapping)
            or decider.get("kind") != "person"
            or not isinstance(decider.get("id"), str)
            or not hmac.compare_digest(actor.decider_ref, decider["id"])
        ):
            raise ApprovalServiceError("wrong_decider")

    @staticmethod
    def _authorize_cancel_principal(
        request: ApprovalRecord, principal: api_auth.ApiPrincipal,
    ) -> None:
        expected = request.payload.get("requester_ref")
        if not isinstance(expected, Mapping):
            raise ApprovalServiceError("wrong_requester")
        kind = expected.get("kind")
        valid_kind = (
            kind == "session"
            and principal.kind in (
                api_auth.ApiPrincipalKind.LOCAL_SESSION,
                api_auth.ApiPrincipalKind.ORG_SESSION,
            )
        ) or (
            kind == "registered_service"
            and principal.kind is api_auth.ApiPrincipalKind.MCP_SERVICE
        )
        if not valid_kind or not principal.subject:
            raise ApprovalServiceError("wrong_requester")
        if kind == "session":
            try:
                actual = canonical_session_requester_id(principal)
            except ApprovalServiceError as exc:
                raise ApprovalServiceError("wrong_requester") from exc
        else:
            actual = principal.subject
        expected_id = expected.get("id")
        if (
            not isinstance(expected_id, str)
            or not hmac.compare_digest(actual, expected_id)
        ):
            raise ApprovalServiceError("wrong_requester")

    @staticmethod
    def _authorize_session_requester(
        request: ApprovalRecord, principal: api_auth.ApiPrincipal,
    ) -> None:
        expected = request.payload.get("requester_ref")
        if (
            not isinstance(expected, Mapping)
            or expected.get("kind") != "session"
            or principal.kind not in (
                api_auth.ApiPrincipalKind.LOCAL_SESSION,
                api_auth.ApiPrincipalKind.ORG_SESSION,
            )
        ):
            raise ApprovalServiceError("wrong_requester")
        try:
            actual = canonical_session_requester_id(principal)
        except ApprovalServiceError as exc:
            raise ApprovalServiceError("wrong_requester") from exc
        expected_id = expected.get("id")
        if (
            not isinstance(expected_id, str)
            or not hmac.compare_digest(actual, expected_id)
        ):
            raise ApprovalServiceError("wrong_requester")

    def _require_request(self, approval_id: str) -> ApprovalRecord:
        request = self._store_call(self.store.get_request, approval_id)
        if request is None:
            raise ApprovalServiceError("not_found")
        return request

    def _reconcile_expiry_locked(
        self, request: ApprovalRecord, now: float,
    ) -> ApprovalRecord | None:
        deadline = request.payload.get("expires_at")
        if deadline is None or now < float(deadline):
            return None
        existing = self._store_call(self.store.get_resolution, request.approval_id)
        if existing is not None:
            return existing
        return self._commit_resolution(request.approval_id, {
            "outcome": "expired",
            "resolved_at": now,
        })

    def _commit_resolution(
        self, approval_id: str, payload: Mapping[str, Any],
    ) -> ApprovalRecord:
        try:
            ApprovalResolutionV1.validate(dict(payload))
        except Exception as exc:
            raise ApprovalServiceError("invalid_decision") from exc
        written = self._store_call(self.store.append_resolution, approval_id, payload)
        self._wake("resolution", approval_id)
        return written

    def _now(self, supplied: float | None) -> float:
        try:
            value = float(self._clock() if supplied is None else supplied)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ApprovalServiceError("invalid_request") from exc
        if not math.isfinite(value):
            raise ApprovalServiceError("invalid_request")
        return value

    @staticmethod
    def _store_call(call: Callable[..., Any], *args: Any) -> Any:
        try:
            return call(*args)
        except ApprovalServiceError:
            raise
        except Exception as exc:
            raise ApprovalServiceError("storage_unavailable") from exc

    def _wake(self, record_type: str, approval_id: str) -> None:
        if self._after_commit is None:
            return
        try:
            self._after_commit(record_type, approval_id)
        except Exception:
            logger.exception("central approval after-commit callback failed")

    def close(self) -> None:
        close = getattr(self.store, "close", None)
        if callable(close):
            close()
