"""Compatibility bridge from the canonical requester HTTP API to Settings.

The bridge contains no production kind adapters by default.  A migration must
activate the approval runtime, the matching attention publisher, and one
complete HTTP adapter before a kind can leave the legacy rendezvous.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import math
import re
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping

from tools.dashboard import api_auth
from tools.dashboard.approval_kind_registry import (
    ApprovalKindRegistry,
    RequesterPolicy,
)
from tools.dashboard.approval_service import (
    ApprovalRecord,
    ApprovalService,
    ApprovalServiceError,
    ApprovalStatus,
    HumanApprovalActor,
)
from tools.dashboard.attention_registry import AttentionRegistry


CENTRAL_APPROVAL_ID_PREFIX = "central-"
MAX_REQUESTER_WAIT_SECONDS = 60.0
RESULT_RECHECK_SECONDS = 1.0
MAX_PROJECTED_JSON_BYTES = 64 * 1024
MAX_PROJECTED_JSON_DEPTH = 8
MAX_PROJECTED_JSON_NODES = 2048
MAX_PROJECTED_CONTAINER_MEMBERS = 256
MAX_PROJECTED_KEY_BYTES = 256
_CENTRAL_ID_RE = re.compile(r"^central-[A-Za-z0-9._:-]{22,248}$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")


class ApprovalHttpBridgeError(RuntimeError):
    """Bounded bridge failure suitable for an HTTP status mapping."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(f"{code}: {message or code.replace('_', ' ')}")


@dataclass(frozen=True, slots=True)
class CanonicalLegacyDecision:
    outcome: str
    decision: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.outcome not in {"granted", "declined"} or not isinstance(
            self.decision, Mapping,
        ):
            raise ValueError("legacy decision mapping is incomplete")


RequestProjector = Callable[[Mapping[str, Any]], Mapping[str, Any]]
ResultProjector = Callable[[ApprovalStatus], Mapping[str, Any] | None]
LegacyDecisionMapper = Callable[[Mapping[str, Any]], CanonicalLegacyDecision]


@dataclass(frozen=True, slots=True)
class ApprovalHttpKindAdapter:
    kind: str
    request_projector: RequestProjector
    result_projector: ResultProjector
    legacy_decision_mapper: LegacyDecisionMapper

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not _KIND_RE.fullmatch(self.kind):
            raise ValueError("approval HTTP adapter kind is invalid")
        if not all(callable(item) for item in (
            self.request_projector,
            self.result_projector,
            self.legacy_decision_mapper,
        )):
            raise ValueError("approval HTTP adapter must be complete")


def has_central_approval_prefix(approval_id: Any) -> bool:
    return isinstance(approval_id, str) and approval_id.startswith(
        CENTRAL_APPROVAL_ID_PREFIX,
    )


def is_central_approval_id(approval_id: Any) -> bool:
    return isinstance(approval_id, str) and bool(_CENTRAL_ID_RE.fullmatch(approval_id))


def central_stable_approval_id(namespace: str, source_id: str) -> str:
    """Derive one non-reversible, deterministic migrated producer ID."""
    if (
        not isinstance(namespace, str)
        or not _KIND_RE.fullmatch(namespace)
        or len(namespace.encode("ascii")) > 64
        or not isinstance(source_id, str)
        or not source_id
    ):
        raise ApprovalHttpBridgeError("invalid_request")
    try:
        encoded = source_id.encode("utf-8")
    except UnicodeError as exc:
        raise ApprovalHttpBridgeError("invalid_request") from exc
    if len(encoded) > 1024:
        raise ApprovalHttpBridgeError("invalid_request")
    digest = hashlib.sha256(json.dumps(
        ["dashboard.approval.stable-id", 1, namespace, source_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return f"{CENTRAL_APPROVAL_ID_PREFIX}{namespace}-{digest}"


class ApprovalHttpRegistry:
    """Closed adapter map joined to the exact approval/attention runtimes."""

    def __init__(
        self,
        *,
        approvals: ApprovalKindRegistry,
        attention: AttentionRegistry,
        adapters: Mapping[str, ApprovalHttpKindAdapter] | None = None,
    ):
        if not isinstance(approvals, ApprovalKindRegistry):
            raise ValueError("approval HTTP registry requires approval catalog")
        if not isinstance(attention, AttentionRegistry):
            raise ValueError("approval HTTP registry requires attention catalog")
        supplied = {} if adapters is None else dict(adapters)
        for kind, adapter in supplied.items():
            if not isinstance(adapter, ApprovalHttpKindAdapter) or adapter.kind != kind:
                raise ValueError("approval HTTP adapter key mismatch")
            try:
                registration = approvals.kinds[kind]
            except KeyError as exc:
                raise ValueError(f"unknown approval HTTP kind: {kind}") from exc
            if registration.requester_policy is not RequesterPolicy.SESSION_PRINCIPAL:
                raise ValueError("generic requester HTTP is session-principal-only")
            applications = registration.application_scope_policy.applications
            if len(applications) != 1:
                raise ValueError("generic requester HTTP requires one fixed application")
            application = next(iter(applications))
            attention.require_class(application, registration.notification_class)
        self.approvals = approvals
        self.attention = attention
        self._adapters = MappingProxyType(supplied)

    @property
    def adapters(self) -> Mapping[str, ApprovalHttpKindAdapter]:
        return self._adapters

    def claims_kind(self, kind: str) -> bool:
        """Whether this kind has begun the Settings HTTP migration.

        A claimed but incomplete kind must fail closed as disabled rather than
        silently falling back to the legacy store.
        """
        if not isinstance(kind, str):
            return False
        if kind in self._adapters:
            return True
        registration = self.approvals.kinds.get(kind)
        if registration is None:
            return False
        if registration.runtime is not None:
            return True
        applications = registration.application_scope_policy.applications
        if len(applications) != 1:
            return False
        try:
            attention = self.attention.require_class(
                next(iter(applications)), registration.notification_class,
            )
        except (KeyError, StopIteration):
            return False
        return attention.runtime is not None or attention.approval_runtime_enabled

    def adapter_for_create(self, kind: str) -> ApprovalHttpKindAdapter | None:
        adapter = self._adapters.get(kind)
        if adapter is None:
            return None
        try:
            registration = self.approvals.require(kind)
            application = next(iter(registration.application_scope_policy.applications))
            attention = self.attention.require_class(
                application, registration.notification_class,
            )
        except (KeyError, RuntimeError, StopIteration) as exc:
            raise ApprovalHttpBridgeError("kind_disabled") from exc
        if not attention.publication_enabled:
            raise ApprovalHttpBridgeError("kind_disabled")
        return adapter

    def require_for_status(self, kind: str) -> ApprovalHttpKindAdapter:
        adapter = self.adapter_for_create(kind)
        if adapter is None:
            raise ApprovalHttpBridgeError("kind_disabled")
        return adapter


class ApprovalWaitHub:
    """Thread-safe, result-free wake hints bounded by active held requests."""

    def __init__(self):
        self._lock = threading.Lock()
        self._waiters: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}
        self._closed = False

    @property
    def waiter_count(self) -> int:
        with self._lock:
            return sum(len(items) for items in self._waiters.values())

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def notify(self, _record_type: str, approval_id: str) -> None:
        with self._lock:
            targets = tuple(self._waiters.get(approval_id, ()))
        for loop, event in targets:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                continue

    async def wait(self, approval_id: str, timeout: float) -> bool:
        if timeout <= 0:
            return not self.closed
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        target = (loop, event)
        with self._lock:
            if self._closed:
                return False
            self._waiters.setdefault(approval_id, set()).add(target)
        try:
            try:
                await asyncio.wait_for(event.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        finally:
            with self._lock:
                items = self._waiters.get(approval_id)
                if items is not None:
                    items.discard(target)
                    if not items:
                        self._waiters.pop(approval_id, None)
        return not self.closed

    def close(self) -> None:
        with self._lock:
            self._closed = True
            targets = tuple(
                target for items in self._waiters.values() for target in items
            )
            self._waiters.clear()
        for loop, event in targets:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                continue


class ApprovalHttpBridge:
    def __init__(
        self,
        *,
        approvals: ApprovalService,
        registry: ApprovalHttpRegistry,
        wait_hub: ApprovalWaitHub | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if approvals.registry is not registry.approvals:
            raise ValueError("approval HTTP bridge must share the approval registry")
        self.approvals = approvals
        self.registry = registry
        self.wait_hub = wait_hub or ApprovalWaitHub()
        self._monotonic = monotonic

    def migrated_kind(self, kind: str) -> bool:
        try:
            return self.registry.adapter_for_create(kind) is not None
        except ApprovalHttpBridgeError:
            return False

    def claims_kind(self, kind: str) -> bool:
        return self.registry.claims_kind(kind)

    def create(
        self,
        kind: str,
        principal: api_auth.ApiPrincipal,
        request_payload: Mapping[str, Any],
    ) -> str:
        adapter = self.registry.adapter_for_create(kind)
        if adapter is None:
            raise ApprovalHttpBridgeError("kind_disabled")
        try:
            row = self.approvals.create_from_principal(kind, principal, request_payload)
        except ApprovalServiceError as exc:
            raise ApprovalHttpBridgeError(exc.code) from exc
        if not is_central_approval_id(row.approval_id):
            raise ApprovalHttpBridgeError("unavailable")
        return row.approval_id

    @staticmethod
    def _bounded_json_object(
        value: Any, *, code: str,
    ) -> tuple[dict[str, Any], bytes]:
        if not isinstance(value, Mapping):
            raise ApprovalHttpBridgeError(code)
        nodes = 0
        active: set[int] = set()

        def copy_json(current: Any, depth: int) -> Any:
            nonlocal nodes
            nodes += 1
            if nodes > MAX_PROJECTED_JSON_NODES or depth > MAX_PROJECTED_JSON_DEPTH:
                raise ApprovalHttpBridgeError(code)
            if isinstance(current, Mapping):
                identity = id(current)
                if identity in active or len(current) > MAX_PROJECTED_CONTAINER_MEMBERS:
                    raise ApprovalHttpBridgeError(code)
                active.add(identity)
                try:
                    copied: dict[str, Any] = {}
                    for index, (key, child) in enumerate(current.items()):
                        if index >= MAX_PROJECTED_CONTAINER_MEMBERS:
                            raise ApprovalHttpBridgeError(code)
                        if not isinstance(key, str):
                            raise ApprovalHttpBridgeError(code)
                        try:
                            encoded_key = key.encode("utf-8")
                        except UnicodeError as exc:
                            raise ApprovalHttpBridgeError(code) from exc
                        if not encoded_key or len(encoded_key) > MAX_PROJECTED_KEY_BYTES:
                            raise ApprovalHttpBridgeError(code)
                        copied[key] = copy_json(child, depth + 1)
                    return copied
                finally:
                    active.discard(identity)
            if isinstance(current, list):
                identity = id(current)
                if identity in active or len(current) > MAX_PROJECTED_CONTAINER_MEMBERS:
                    raise ApprovalHttpBridgeError(code)
                active.add(identity)
                try:
                    copied_list = []
                    for index, child in enumerate(current):
                        if index >= MAX_PROJECTED_CONTAINER_MEMBERS:
                            raise ApprovalHttpBridgeError(code)
                        copied_list.append(copy_json(child, depth + 1))
                    return copied_list
                finally:
                    active.discard(identity)
            if current is None or isinstance(current, (str, bool, int)):
                return current
            if isinstance(current, float) and math.isfinite(current):
                return current
            raise ApprovalHttpBridgeError(code)

        try:
            copied = copy_json(value, 0)
            canonical = json.dumps(
                copied,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
                ensure_ascii=False,
            ).encode("utf-8")
        except ApprovalHttpBridgeError:
            raise
        except Exception as exc:
            raise ApprovalHttpBridgeError(code) from exc
        if (
            not isinstance(copied, dict)
            or len(canonical) > MAX_PROJECTED_JSON_BYTES
        ):
            raise ApprovalHttpBridgeError(code)
        return copied, canonical

    @classmethod
    def _json_mapping(cls, value: Any, *, code: str) -> dict[str, Any]:
        copied, _canonical = cls._bounded_json_object(value, code=code)
        return copied

    @classmethod
    def _canonical(cls, value: Any, *, code: str) -> bytes:
        _copied, canonical = cls._bounded_json_object(value, code=code)
        return canonical

    def _read(
        self, approval_id: str, principal: api_auth.ApiPrincipal,
    ) -> tuple[ApprovalStatus, ApprovalHttpKindAdapter]:
        if not is_central_approval_id(approval_id):
            raise ApprovalHttpBridgeError("not_found")
        try:
            status = self.approvals.status_for_principal(approval_id, principal)
        except ApprovalServiceError as exc:
            code = "not_found" if exc.code in {
                "not_found", "wrong_requester", "unauthenticated",
            } else ("unavailable" if exc.code == "storage_unavailable" else exc.code)
            raise ApprovalHttpBridgeError(code) from exc
        adapter = self.registry.require_for_status(str(status.request.payload.get("kind")))
        return status, adapter

    def envelope(
        self, approval_id: str, principal: api_auth.ApiPrincipal,
    ) -> dict[str, Any]:
        status, adapter = self._read(approval_id, principal)
        payload = status.request.payload
        try:
            projected_request = adapter.request_projector(payload)
        except ApprovalHttpBridgeError:
            raise
        except Exception as exc:
            raise ApprovalHttpBridgeError("unavailable") from exc
        request_projection = self._json_mapping(
            projected_request, code="unavailable",
        )
        if not request_projection:
            raise ApprovalHttpBridgeError("unavailable")
        result: dict[str, Any] | None = None
        resolution = status.resolution
        if resolution is not None:
            outcome = resolution.payload.get("outcome")
            if outcome in {"declined", "canceled", "expired"}:
                result = {"approved": False, "outcome": outcome}
            elif outcome == "granted":
                try:
                    projected = adapter.result_projector(status)
                except ApprovalHttpBridgeError:
                    raise
                except Exception as exc:
                    raise ApprovalHttpBridgeError("unavailable") from exc
                if projected is not None:
                    result = self._json_mapping(projected, code="unavailable")
                    if result.get("approved") is not True:
                        raise ApprovalHttpBridgeError("unavailable")
            else:
                raise ApprovalHttpBridgeError("unavailable")
        requester = payload.get("requester_ref")
        label = requester.get("label") if isinstance(requester, Mapping) else None
        if not isinstance(label, str) or not label:
            label = "Authenticated session"
        return {
            "id": approval_id,
            "kind": payload["kind"],
            "session": label,
            "request": request_projection,
            "result": result,
        }

    async def held_envelope(
        self,
        approval_id: str,
        principal: api_auth.ApiPrincipal,
        wait_seconds: float,
    ) -> dict[str, Any]:
        try:
            wait_value = float(wait_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ApprovalHttpBridgeError("invalid_request") from exc
        if not math.isfinite(wait_value) or wait_value < 0:
            raise ApprovalHttpBridgeError("invalid_request")
        deadline = self._monotonic() + min(wait_value, MAX_REQUESTER_WAIT_SECONDS)
        while True:
            result = await asyncio.to_thread(self.envelope, approval_id, principal)
            if result["result"] is not None:
                return result
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return result
            active = await self.wait_hub.wait(
                approval_id, min(remaining, RESULT_RECHECK_SECONDS),
            )
            if not active:
                raise ApprovalHttpBridgeError("unavailable")

    def cancel(
        self, approval_id: str, principal: api_auth.ApiPrincipal,
    ) -> dict[str, Any]:
        if not is_central_approval_id(approval_id):
            raise ApprovalHttpBridgeError("not_found")
        if principal.kind not in (
            api_auth.ApiPrincipalKind.LOCAL_SESSION,
            api_auth.ApiPrincipalKind.ORG_SESSION,
        ):
            raise ApprovalHttpBridgeError("not_found")
        try:
            resolution = self.approvals.cancel_from_principal(approval_id, principal)
        except ApprovalServiceError as exc:
            code = "not_found" if exc.code in {
                "not_found", "wrong_requester", "unauthenticated",
            } else ("expired" if exc.code == "expired" else "unavailable")
            raise ApprovalHttpBridgeError(code) from exc
        if resolution.payload.get("outcome") != "canceled":
            raise ApprovalHttpBridgeError("approval_conflict")
        return self.envelope(approval_id, principal)

    def decide_legacy(
        self,
        approval_id: str,
        actor: HumanApprovalActor,
        body: Mapping[str, Any],
    ) -> None:
        if not is_central_approval_id(approval_id):
            raise ApprovalHttpBridgeError("not_found")
        try:
            request = self.approvals.get_request(approval_id)
            adapter = self.registry.require_for_status(str(request.payload.get("kind")))
            try:
                mapped = adapter.legacy_decision_mapper(body)
            except ApprovalHttpBridgeError:
                raise
            except Exception as exc:
                raise ApprovalHttpBridgeError("invalid_decision") from exc
            if not isinstance(mapped, CanonicalLegacyDecision):
                raise ApprovalHttpBridgeError("invalid_decision")
            decision = self._json_mapping(mapped.decision, code="invalid_decision")
            resolution = self.approvals.decide(
                approval_id,
                actor,
                outcome=mapped.outcome,
                decision=decision,
            )
        except ApprovalHttpBridgeError:
            raise
        except ApprovalServiceError as exc:
            if exc.code in {"not_found", "wrong_decider", "unauthenticated"}:
                code = "not_found"
            elif exc.code == "invalid_decision":
                code = "invalid_decision"
            elif exc.code == "expired":
                code = "expired"
            else:
                code = "unavailable"
            raise ApprovalHttpBridgeError(code) from exc
        persisted = resolution.payload.get("decision")
        if (
            resolution.payload.get("outcome") != mapped.outcome
            or not isinstance(persisted, Mapping)
            or self._canonical(persisted, code="unavailable")
            != self._canonical(decision, code="invalid_decision")
        ):
            raise ApprovalHttpBridgeError("approval_conflict")

    def close(self) -> None:
        self.wait_hub.close()


__all__ = [
    "CENTRAL_APPROVAL_ID_PREFIX",
    "MAX_REQUESTER_WAIT_SECONDS",
    "RESULT_RECHECK_SECONDS",
    "ApprovalHttpBridge",
    "ApprovalHttpBridgeError",
    "ApprovalHttpKindAdapter",
    "ApprovalHttpRegistry",
    "ApprovalWaitHub",
    "CanonicalLegacyDecision",
    "central_stable_approval_id",
    "has_central_approval_prefix",
    "is_central_approval_id",
]
