"""Settings-native ``vault_seal``: an agent asks the operator to vault a secret.

The write side of the vault already exists — ``graph vault seal`` seals a value
the caller HOLDS. What no path offered was the other direction: an agent that
needs a credential it does not have, and must ask the operator to deposit it.
This kind is that ask, on Central:

1. The session POSTs ``kind=vault_seal`` naming the credential, the tier and
   why it needs it. The planner freezes the destination from the BEARER (the
   ``<org>:name`` writeback key an org session may address, or the bare
   personal name) — the requester never chooses where its secret lands.
2. Central shows the request; the operator opens it, reads the description,
   types the secret and approves.
3. The browser first POSTs the value to ``/api/vault/deposit/{approval_id}``
   (``vault_routes``), which seals it into the frozen destination through the
   same ``settings_ops.write_by_key`` seam ``graph vault seal`` uses. Only the
   resulting row id crosses back.
4. The decision carries that ``setting_id`` and nothing else; the validator
   proves the row exists at the frozen destination before the resolution is
   committed. A granted ``vault_seal`` therefore always names a deposited
   secret — the record never says "granted" for a secret that was not written.
   (``vault_open`` orders these the other way round — Grant, then deliver —
   because its key must not ride a replicated resolution and delivery can be
   repeated; a deposit's proof is the row itself, so it comes first.)
5. The resolution commit reaches :class:`VaultSealCoordinator` as a Settings
   change event, which wakes the requesting session by task-notification
   (``graph vault read <name>`` is the next step; a secured name still goes
   through ``vault_open``).

The plaintext exists in the browser and, transiently, in the deposit handler.
It is never in a request, a decision, a resolution, an inbox row or a wake.

Mirrors mailbox_central.py / vault_open_central.py (planner, inbox text, HTTP
adapter, coordinator).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from tools.dashboard import api_auth
from tools.dashboard.approval_http_bridge import (
    ApprovalHttpBridgeError,
    ApprovalHttpKindAdapter,
    CanonicalLegacyDecision,
)
from tools.dashboard.approval_kind_registry import (
    ApprovalDecisionContext,
    ApprovalKindRuntime,
    ApprovalPlanningContext,
    ApprovalRequestPlan,
)
from tools.dashboard.approval_service import (
    ApprovalRequestRefused, ApprovalServiceError, ApprovalStatus,
)
from tools.dashboard.dashboard_access_central import (
    DashboardAccessCoordinator,
    _bounded_approval_id,
)
from tools.dashboard.mailbox_central import _requesting_session
from tools.graph import schemas, settings_ops
from tools.graph.schemas.vault_credential import (
    VAULT_AUDITED_SET_ID,
    VAULT_CREDENTIAL_REVISION,
    VAULT_SECURED_SET_ID,
)

logger = logging.getLogger(__name__)

KIND = "vault_seal"
APPLICATION_SCOPE = "vault"
RENDERER_ID = "approval.vault_seal.review"
CONSUMER_ID = "vault_seal.deposit.v1"

#: tier -> set id; the tier is the NAME of the destination set, never a field.
TIER_SET = {
    "secured": VAULT_SECURED_SET_ID,
    "audited": VAULT_AUDITED_SET_ID,
}
TIER_LABELS = {
    "secured": "Requires your approval each time it is read",
    "audited": "Released unattended to authorized sessions",
}
DEFAULT_TIER = "secured"

#: A credential name is the whole contract with what consumes it (``github.token``,
#: ``claude.oauth.refresh``): letters, digits, dots, dashes, underscores. The
#: organization prefix is server-derived, so ``:`` is refused outright.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_DESCRIPTION_CHARS = 2000
#: The vault stores UTF-8 text; a 64 KiB bound covers every key file in use
#: and stops a browser from posting an arbitrary blob through the sealer.
MAX_VALUE_BYTES = 64 * 1024
_ALLOWED_REQUEST_FIELDS = {"name", "tier", "description", "replace"}
_SETTING_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


def _clean_text(value: Any, *, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApprovalRequestRefused(f"vault_seal {label} must be a non-empty string")
    text = value.strip()
    if len(text) > maximum:
        raise ApprovalRequestRefused(f"vault_seal {label} must be at most {maximum} characters")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in text) or "\x7f" in text:
        raise ApprovalRequestRefused(f"vault_seal {label} must not contain control characters")
    return text


def routed_key(context: ApprovalPlanningContext, set_id: str, name: str) -> str:
    """The only store key this requester may address for ``name``.

    An organization session writes under its bearer-derived ``<org>:`` prefix
    into the operator's personal store (the same derivation ``graph vault
    seal`` and ``vault_open`` use); the operator's terminal naming an
    organization writes under that one (``context.operator_org``); a local or
    personal session otherwise names the operator's own bare key. The
    requester supplies a suffix only.
    """
    if context.operator_org:
        # The operator's terminal named the organization (auto-kx7uo).
        return schemas.derive_org_writeback_key(set_id, context.operator_org, name)
    kind = context.requester_principal_kind
    org = context.requester_org
    if kind == api_auth.ApiPrincipalKind.ORG_SESSION.value and org not in (None, "", "personal"):
        return schemas.derive_org_writeback_key(set_id, org, name)
    return name


def existing_row_id(set_id: str, key: str) -> str | None:
    """The live base row for ``key`` in the operator's store, or ``None``.

    A pure existence probe: nothing is decrypted, so it answers the same when
    the vault is cold. Both tiers live in the personal store, so ``org=None``.
    """
    return settings_ops._existing_base_id(set_id, VAULT_CREDENTIAL_REVISION, key, None)


def _one_line(value: Any) -> Any:
    """A description written on several lines, as one: Central's approval
    record keeps review text single-line (no control characters), so line
    breaks (LF, CRLF, CR) and tabs are joined with " · " rather than the
    request refused (auto-gf08k)."""
    if not isinstance(value, str):
        return value
    lines = [" ".join(line.split()) for line in value.replace("\r\n", "\n")
             .replace("\r", "\n").split("\n")]
    return " · ".join(line for line in lines if line)


def build_request_planner():
    def plan(context: ApprovalPlanningContext, body: Mapping[str, Any]) -> ApprovalRequestPlan:
        # The planner's OWN input checks raise ApprovalRequestRefused: each is
        # written for the requester and returned to them as the reason. What
        # a helper raises stays an opaque invalid_request (auto-gf08k).
        if not isinstance(body, Mapping) or set(body) - _ALLOWED_REQUEST_FIELDS:
            raise ApprovalRequestRefused(
                "vault_seal request accepts only name, tier, description, and replace"
            )
        name = body.get("name")
        if not isinstance(name, str) or ":" in name:
            raise ApprovalRequestRefused(
                "vault_seal name is an unprefixed credential name; the "
                "organization namespace is derived from your session"
            )
        if not _NAME_RE.fullmatch(name):
            raise ApprovalRequestRefused(
                "vault_seal name must be 1-128 characters of letters, digits, "
                "'.', '_' or '-'"
            )
        tier = body.get("tier", DEFAULT_TIER)
        if tier not in TIER_SET:
            raise ApprovalRequestRefused("vault_seal tier must be 'secured' or 'audited'")
        description = _clean_text(
            _one_line(body.get("description")), label="description",
            maximum=MAX_DESCRIPTION_CHARS,
        )
        replace = body.get("replace", False)
        if not isinstance(replace, bool):
            raise ApprovalRequestRefused("vault_seal replace must be a boolean")
        # The session name is proven from the frozen requester identity and
        # kept only to wake it; the row still stores no raw subject as identity.
        session = _requesting_session(context)
        set_id = TIER_SET[tier]
        key = routed_key(context, set_id, name)
        # Tier is immutable per name (mirrors ``graph vault seal``): a deposit
        # must never silently move a secret between release rules, and an
        # accidental re-request must not overwrite a live credential.
        other_tier = "audited" if tier == "secured" else "secured"
        if existing_row_id(TIER_SET[other_tier], key) is not None:
            raise ApprovalRequestRefused(
                f"{name!r} already exists at the {other_tier} tier; remove it or "
                f"request the {other_tier} tier"
            )
        if not replace and existing_row_id(set_id, key) is not None:
            raise ApprovalRequestRefused(
                f"{name!r} already exists at the {tier} tier; pass replace to "
                "rotate it"
            )
        label = context.requester_ref.get("label") or session
        safe_review = {
            "title": "Vault a secret",
            "detail": description,
            "requester_label": label,
            "approval_id": context.approval_id,
            "name": name,
            "key": key,
            "tier": tier,
            "tier_label": TIER_LABELS[tier],
            "replace": replace,
        }
        return ApprovalRequestPlan(
            subject_ref=f"vault-seal:{set_id}/{key}",
            safe_review=safe_review,
            request={
                "name": name, "tier": tier, "description": description,
                "replace": replace,
            },
            staged={"v": 1, "set_id": set_id, "key": key, "tier": tier, "session": session},
        )

    return plan


def frozen_destination(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The server-frozen destination of one request, or ``ValueError``."""
    staged = payload.get("staged")
    request = payload.get("request")
    if (
        not isinstance(staged, Mapping) or staged.get("v") != 1
        or not isinstance(request, Mapping)
        or staged.get("tier") not in TIER_SET
        or staged.get("set_id") != TIER_SET[staged["tier"]]
        or not isinstance(staged.get("key"), str) or not staged["key"]
    ):
        raise ValueError("this request has no frozen vault destination")
    return {
        "set_id": staged["set_id"],
        "key": staged["key"],
        "tier": staged["tier"],
        "name": request.get("name"),
        "replace": bool(request.get("replace", False)),
        "session": staged.get("session"),
    }


def row_matches_destination(setting_id: str, destination: Mapping[str, Any]) -> bool:
    """Whether ``setting_id`` is a live row at exactly the frozen destination.

    Reads the store's rows for the frozen key, never the row named by the
    decision: a setting id from another set or key cannot be smuggled in as
    proof of deposit. Nothing is decrypted.
    """
    layers = settings_ops.layers_for(
        destination["set_id"], destination["key"], org=None,
    )
    base = layers.get("base")
    candidates = []
    if isinstance(base, Mapping) and base.get("id"):
        candidates.append(base["id"])
    for override in layers.get("overrides") or []:
        if isinstance(override, Mapping) and override.get("id"):
            candidates.append(override["id"])
    return setting_id in candidates


def _validate_decision(
    _context: ApprovalDecisionContext,
    request_payload: Mapping[str, Any],
    decision: Mapping[str, Any],
    is_grant: bool,
) -> dict[str, Any]:
    if not is_grant:
        if decision:
            raise ValueError("a declined vault_seal carries no decision payload")
        return {}
    if not isinstance(decision, Mapping) or set(decision) != {"setting_id"}:
        raise ValueError("vault_seal approval carries only the deposited setting_id")
    setting_id = decision.get("setting_id")
    if not isinstance(setting_id, str) or not _SETTING_ID_RE.fullmatch(setting_id):
        raise ValueError("vault_seal setting_id is malformed")
    destination = frozen_destination(request_payload)
    if not row_matches_destination(setting_id, destination):
        raise ValueError("the named row is not a deposit at the frozen destination")
    return {
        "setting_id": setting_id,
        "set_id": destination["set_id"],
        "key": destination["key"],
        "tier": destination["tier"],
    }


def build_approval_runtime() -> ApprovalKindRuntime:
    return ApprovalKindRuntime(
        request_planner=build_request_planner(),
        decision_validator=_validate_decision,
        resolution_consumer_id=CONSUMER_ID,
        result_ref_builder=lambda approval_id, payload, _decision: payload["subject_ref"],
    )


def inbox_text(status: ApprovalStatus) -> tuple[str, str | None]:
    """The inbox's title and summary for one approval of this kind."""
    review = status.request.payload.get("safe_review") or {}
    name = review.get("name") or "a secret"
    label = review.get("requester_label") or "A session"
    return f"Vault a secret: {name}", f"{label} needs {name} vaulted."


def project_result(status: ApprovalStatus) -> Mapping[str, Any] | None:
    """What the requester (and the operator's review) learn from a grant.

    The deposit happened before the decision, so the resolution IS the
    application result: no consumer, no retry, no second store. Value-free.
    """
    resolution = status.resolution
    if resolution is None or resolution.payload.get("outcome") != "granted":
        return None
    decision = resolution.payload.get("decision")
    if not isinstance(decision, Mapping) or not decision.get("setting_id"):
        raise RuntimeError("vault_seal resolution names no deposited row")
    request = status.request.payload.get("request") or {}
    return {
        "approved": True,
        "execution": {
            "ok": True,
            "name": request.get("name"),
            "set_id": decision.get("set_id"),
            "key": decision.get("key"),
            "tier": decision.get("tier"),
            "setting_id": decision.get("setting_id"),
        },
    }


def build_http_adapter() -> ApprovalHttpKindAdapter:
    def project_request(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        request = payload.get("request")
        if not isinstance(request, Mapping) or not request.get("name"):
            raise RuntimeError("vault_seal request is unavailable")
        return {
            "name": request.get("name"),
            "tier": request.get("tier"),
            "description": request.get("description"),
            "replace": bool(request.get("replace", False)),
        }

    def map_decision(body: Mapping[str, Any]) -> CanonicalLegacyDecision:
        if body == {"approved": False}:
            return CanonicalLegacyDecision("declined", {})
        if (
            isinstance(body, Mapping)
            and set(body) == {"approved", "setting_id"}
            and body.get("approved") is True
        ):
            return CanonicalLegacyDecision("granted", {"setting_id": body["setting_id"]})
        raise ApprovalHttpBridgeError("invalid_decision")

    return ApprovalHttpKindAdapter(
        kind=KIND,
        request_projector=project_request,
        result_projector=project_result,
        legacy_decision_mapper=map_decision,
    )


# ── Wake the requester ──────────────────────────────────────────────────


def notification(status: ApprovalStatus) -> dict[str, Any] | None:
    """The value-free wake a decided request sends its session, or ``None``."""
    resolution = status.resolution
    if resolution is None:
        return None
    request = status.request.payload.get("request") or {}
    name = request.get("name") or "secret"
    approval_id = status.request.approval_id
    spec = {"notification_id": f"vault-seal:{approval_id}", "kind": "vault-seal"}
    outcome = resolution.payload.get("outcome")
    if outcome == "granted":
        tier = request.get("tier") or DEFAULT_TIER
        how = (
            "It releases unattended: run `graph vault read " + name + "`."
            if tier == "audited"
            else "It is secured: `graph vault read " + name + "` asks the operator to release it."
        )
        spec.update(
            status="deposited",
            summary=f"Vault secret deposited: {name}",
            body=f"The operator vaulted {name!r} at the {tier} tier. {how}",
        )
    elif outcome == "declined":
        spec.update(
            status="declined",
            summary=f"Vault request declined: {name}",
            body="The operator declined to vault this secret. Ask again with more context if it is still needed.",
        )
    else:
        spec.update(
            status=str(outcome or "closed"),
            summary=f"Vault request closed: {name}",
            body=f"The request to vault {name!r} ended without a deposit ({outcome}).",
        )
    return spec


def _notify(session: str | None, spec: Mapping[str, Any]) -> str:
    """Best-effort, deduped task-notification into the requesting session."""
    if not isinstance(session, str) or not session:
        return "absent"
    from tools.dashboard import session_notify

    return session_notify.deliver_task_notification_sync(
        session,
        spec["notification_id"],
        kind=spec["kind"],
        status=spec["status"],
        summary=spec["summary"],
        body=spec.get("body", ""),
    )


class VaultSealCoordinator(DashboardAccessCoordinator):
    """Tells the requesting session how its ``vault_seal`` request ended.

    Runs once per approval change event. The deposit itself never happens
    here — it happened in the browser's deposit call before the Grant — so
    the only reconciliation is the wake, deduped by notification id.
    """

    def __init__(self, *, approvals, notify=_notify) -> None:
        super().__init__(approvals=approvals, consumer=None)
        self._notify = notify

    def reconcile_exact(self, approval_id: str) -> ApprovalStatus | None:
        try:
            status = self.approvals.status(_bounded_approval_id(approval_id))
        except (ApprovalServiceError, ValueError) as exc:
            if isinstance(exc, ValueError) or exc.code == "not_found":
                return None
            raise
        payload = status.request.payload
        if payload.get("kind") != KIND:
            return None
        spec = notification(status)
        if spec is not None:
            try:
                self._notify((payload.get("staged") or {}).get("session"), spec)
            except Exception:
                # The resolution is committed; a wake failure never rolls it back.
                logger.warning("vault_seal: requester wake failed for %s", approval_id)
        return status


__all__ = [
    "APPLICATION_SCOPE",
    "CONSUMER_ID",
    "KIND",
    "MAX_VALUE_BYTES",
    "RENDERER_ID",
    "TIER_LABELS",
    "TIER_SET",
    "VaultSealCoordinator",
    "build_approval_runtime",
    "build_http_adapter",
    "existing_row_id",
    "frozen_destination",
    "inbox_text",
    "notification",
    "project_result",
    "routed_key",
    "row_matches_destination",
]
