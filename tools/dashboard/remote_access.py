"""Remote access to the dashboard: the label question (F6 of graph://c9d72ea4-feb).

Onboarding asks one optional free-text question: the serving label the
dashboard's relay origin is published under, ``<app>.<label>.serve.auto.network``.
The default is the persona label the node derives on its own; a custom string
becomes the label's slug, ``<slug>-<20 hex>`` (service_publication
.normalize_persona_label), and the registry binds it once and never changes it.

Availability needs no registry call: the twenty-hex suffix is the persona's own
digest, so two personas can never own the same label. What remains is local:
within one persona the label is bound ONCE at the registry (a different slug is
then refused forever with label-invalid), so a persona that already has its
label gets exactly that answer; otherwise the slug must be well formed, must not
read as the platform, an infrastructure word or a reserved app label, and must
not read as a label the operator already publishes under. That is ``check_label``.
Known limit: the local reservations are the only record of the bound label; the
registry offers no read of it, so a node that lost its reservations would learn
the binding only from the registry's refusal at host registration.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from tools.dashboard import label_lookalike

#: The slug part of a persona label: normalize_persona_label keeps at most 42
#: characters before the digest.
SLUG_MAX = 42
_SLUG_RE = re.compile(r"^(?![a-z0-9]{2}--)[a-z0-9](?:[a-z0-9-]{0,40}[a-z0-9])?$")
_PERSONA_SUFFIX_RE = re.compile(r"-[0-9a-f]{20}$")


@dataclass(frozen=True)
class LabelCheck:
    ok: bool
    label: str
    code: str = ""        # malformed | unavailable | already_bound | platform_name | reserved_name | existing_label
    against: str = ""     # the protected or bound name the candidate reads as, or is
    reason: str = ""      # one sentence for the screen
    bound: bool = False   # the persona already has its permanent label (the screen skips the question)

    def as_dict(self) -> dict:
        return asdict(self)


class LabelCheckUnavailable(Exception):
    """The binding could not be read right now (not: there is none)."""


def bound_slug(org: str) -> str | None:
    """The slug this node's personal persona is already bound to, or None
    when the persona has no label yet (or no personal ledger yet).

    The registry binds ONE label per persona at its first host registration
    and refuses every other with label-invalid; the local reservations are
    the only record of it (the registry has no read of a persona's label).
    The personal persona is the default publisher; an organization publisher
    resolves its own persona when organization publishing lands. A read
    failure raises LabelCheckUnavailable rather than reading as "not bound".
    """
    from tools.dashboard import service_publication

    try:
        persona_pub, _display = service_publication._persona_for_org("personal")
    except service_publication.ServicePublicationError as exc:
        if exc.code in ("organization_not_founded", "persona_not_configured"):
            return None
        raise LabelCheckUnavailable(exc.code) from exc
    except Exception as exc:
        raise LabelCheckUnavailable(str(exc)) from exc
    try:
        label = service_publication.bound_persona_label("personal", persona_pub)
    except Exception as exc:
        raise LabelCheckUnavailable(str(exc)) from exc
    if not isinstance(label, str) or not label:
        return None
    slug = _PERSONA_SUFFIX_RE.sub("", label)
    return slug or None


def existing_labels(org: str) -> list[str]:
    """Labels the operator already publishes under, from their reservations in
    *org* and in the personal scope: app labels and persona-label slugs."""
    from tools.dashboard import service_publication

    seen: list[str] = []
    for scope in dict.fromkeys(("personal", org)):
        try:
            rows = service_publication.list_reservations(scope)
        except Exception:
            continue
        for row in rows:
            if not isinstance(row, dict) or row.get("state") == "released":
                continue
            app = row.get("app_label")
            if isinstance(app, str) and app and app not in seen:
                seen.append(app)
            persona_label = row.get("persona_label")
            if isinstance(persona_label, str):
                slug = _PERSONA_SUFFIX_RE.sub("", persona_label)
                if slug and slug not in seen:
                    seen.append(slug)
    return seen


def check_label(org: str, candidate: object, *, existing: list[str] | None = None,
                bound: str | None = None) -> LabelCheck:
    """Decide whether *candidate* may become the operator's serving-label slug.

    *bound* (default: looked up) is the slug the persona already carries; when
    present it is the only valid answer, because the registry never rebinds.
    """
    if not isinstance(candidate, str):
        return LabelCheck(False, "", "malformed", "", "The label must be text.")
    label = candidate.strip().lower()
    if not label or not _SLUG_RE.fullmatch(label) or not label_lookalike.is_well_formed(label):
        return LabelCheck(
            False, label, "malformed", "",
            f"Use 1 to {SLUG_MAX} lowercase letters, digits and hyphens, starting and ending "
            "with a letter or digit.",
        )
    if bound is None:
        try:
            bound = bound_slug(org)
        except LabelCheckUnavailable:
            return LabelCheck(False, label, "unavailable", "",
                              "The label cannot be checked right now; try again in a moment.")
    if bound:
        if label == bound:
            return LabelCheck(True, label, bound=True)
        return LabelCheck(
            False, label, "already_bound", bound,
            f"Your label is already \"{bound}\" and cannot change.", bound=True)
    conflict = label_lookalike.lookalike_conflict(
        label, existing if existing is not None else existing_labels(org))
    if conflict is None:
        return LabelCheck(True, label)
    reasons = {
        "platform_name": f"That reads as \"{conflict.against}\", which belongs to the platform.",
        "reserved_name": f"That reads as \"{conflict.against}\", which is reserved.",
        "existing_label": f"That reads like your existing label \"{conflict.against}\".",
    }
    return LabelCheck(False, label, conflict.code, conflict.against, reasons[conflict.code])


# ── the publish call and its record (graph://c9d72ea4-feb §10) ─────────────

#: The dashboard's own Service app label when onboarding publishes it.
DEFAULT_APP_LABEL = "dashboard"
#: The scope the dashboard publishes under by default (operator, 2026-09-26).
DEFAULT_PUBLISHER = "personal"


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def record(org: str, payload: dict) -> dict:
    """Write the operator's remote-access row (personal-homed singleton)."""
    from tools.graph import settings_ops
    from tools.graph.schemas.dashboard_remote_access import (
        REMOTE_ACCESS_KEY, REMOTE_ACCESS_REVISION, REMOTE_ACCESS_SET_ID)

    settings_ops.write_by_key(REMOTE_ACCESS_SET_ID, REMOTE_ACCESS_REVISION, REMOTE_ACCESS_KEY,
                              payload, org=None)
    return dict(payload)


def current() -> dict | None:
    """The recorded remote-access row, or None before onboarding chose."""
    from tools.graph import settings_ops
    from tools.graph.schemas.dashboard_remote_access import REMOTE_ACCESS_KEY, REMOTE_ACCESS_SET_ID

    row = settings_ops.read_set_key(REMOTE_ACCESS_SET_ID, REMOTE_ACCESS_KEY, org=None, peers=[])
    if row is None or not isinstance(row.get("payload"), dict):
        return None
    return dict(row["payload"])


async def publish(mode: str, *, org: str = DEFAULT_PUBLISHER, app_label: str | None = None,
                  request_origin: str | None = None) -> dict:
    """Perform the whole publish deterministically and idempotently.

    ``autonomy``: reserve the origin (or reuse it), bind this dashboard as the
    target on its plain listener (personal-gated by construction), activate,
    ask the certificate manager and the gateway to converge, and record the
    row. Re-running returns the same origin and makes no second reservation.
    ``tailscale`` / ``local``: record the origin the operator reached the
    dashboard on; nothing is published.
    """
    from tools.graph.schemas.dashboard_remote_access import REACH_MODES
    from tools.dashboard import service_publication

    if mode not in REACH_MODES:
        raise service_publication.ServicePublicationError("invalid_mode", 400)
    if mode != "autonomy":
        if not isinstance(request_origin, str) or not request_origin:
            raise service_publication.ServicePublicationError("origin_required", 400)
        return record(org, {"mode": mode, "origin": request_origin, "published_at": _utc_now()})

    label = app_label if app_label is not None else DEFAULT_APP_LABEL
    reservation, _created = service_publication.reserve_origin(org, label)
    reservation_id = reservation["reservation_id"]
    await service_publication.bind_service_target(
        org, reservation_id, None, service_publication.DASHBOARD_TARGET_DEFAULT_PORT,
        kind=service_publication.DASHBOARD_TARGET_KIND)
    service_publication.transition_reservation(org, reservation_id, "active")
    _converge()
    return record(org, {
        "mode": "autonomy",
        "origin": reservation["origin"],
        "reservation_id": reservation_id,
        "app_label": reservation["app_label"],
        "published_at": _utc_now(),
    })


def _converge() -> None:
    """Best effort: issue the certificate and reload the gateway now rather
    than at their next tick. Failures here never fail the publish; status
    reports them."""
    try:
        from tools.dashboard import service_certificate_manager

        service_certificate_manager.request_reconcile()
    except Exception:
        pass
    try:
        import asyncio

        from tools.dashboard import web_gateway_supervisor

        asyncio.get_running_loop().create_task(web_gateway_supervisor.request_reload())
    except Exception:
        pass


async def status() -> dict:
    """The staged progress onboarding polls: what is recorded, and for the
    relay mode where the publish stands (route, certificate, gate, advertised)."""
    row = current()
    if row is None:
        return {"mode": None, "origin": None, "recorded": False}
    result = {"mode": row["mode"], "origin": row["origin"], "recorded": True,
              "published_at": row.get("published_at")}
    if row["mode"] != "autonomy":
        return result
    from tools.dashboard import service_publication, service_status, web_gateway_supervisor
    from tools.dashboard import service_certificate_manager

    org = DEFAULT_PUBLISHER
    reservation_id = row["reservation_id"]
    result["reservation_id"] = reservation_id
    try:
        link = await service_status.service_status(org, reservation_id)
        result["route_state"] = link.get("state")
        result["stages"] = link.get("stages", [])
        result["failed_stage"] = link.get("failed_stage")
        result["detail"] = link.get("detail", "")
    except service_publication.ServicePublicationError as exc:
        result["route_state"] = "unavailable"
        result["failed_stage"] = "reservation"
        result["detail"] = exc.code
    identity = None
    try:
        member = service_publication._member_by_key(org, reservation_id)
        identity = service_publication.certificate_identity_for_payload(member.payload) if member else None
    except Exception:
        identity = None
    certificate = "pending"
    for state in service_certificate_manager.certificate_states():
        if state.get("org") == org and (identity is None or state.get("persona_label") == identity):
            certificate = {"current": "ok", "renewal_due": "ok", "issuing": "pending",
                           "missing": "pending"}.get(state.get("state"), "failed")
            if certificate == "failed":
                result["certificate_detail"] = state.get("reason", "")
            break
    result["certificate"] = certificate
    gateway = web_gateway_supervisor.status()
    result["advertised"] = reservation_id in (gateway.get("advertised_routes") or [])
    result["gateway_state"] = gateway.get("state")
    # The personal passkey gate is delivered by its own bead; until its helper
    # runs the route fails closed, and this is what the screen must say.
    result["gate"] = "up" if "dashboard-passkey" in (gateway.get("auth_helpers") or []) else "pending"
    result["enrollment"] = "closed"
    return result
