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
import logging

logger = logging.getLogger(__name__)

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

import asyncio
import ipaddress
import time
from urllib.parse import urlsplit

#: The dashboard's own Service app label when onboarding publishes it.
DEFAULT_APP_LABEL = "dashboard"
#: The scope the dashboard publishes under by default (operator, 2026-09-26).
DEFAULT_PUBLISHER = "personal"
#: How long one status probe answers every tab's poll (the note: 2 s polls).
STATUS_CACHE_SECONDS = 2.0

_reload_task: "asyncio.Task | None" = None
_status_lock: "asyncio.Lock | None" = None
_status_cache: tuple[float, dict] | None = None


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def record(payload: dict) -> dict:
    """Write the operator's remote-access row (personal-homed singleton)."""
    from tools.graph import settings_ops
    from tools.graph.schemas.dashboard_remote_access import (
        REMOTE_ACCESS_KEY, REMOTE_ACCESS_REVISION, REMOTE_ACCESS_SET_ID)

    settings_ops.write_by_key(REMOTE_ACCESS_SET_ID, REMOTE_ACCESS_REVISION, REMOTE_ACCESS_KEY,
                              payload, org=None)
    _invalidate_status()
    return dict(payload)


def current() -> dict | None:
    """The recorded remote-access row, or None before onboarding chose."""
    from tools.graph import settings_ops
    from tools.graph.schemas.dashboard_remote_access import REMOTE_ACCESS_KEY, REMOTE_ACCESS_SET_ID

    row = settings_ops.read_set_key(REMOTE_ACCESS_SET_ID, REMOTE_ACCESS_KEY, org=None, peers=[])
    if row is None or not isinstance(row.get("payload"), dict):
        return None
    return dict(row["payload"])


# ── where a request came from ─────────────────────────────────────────────

def _host_of(origin: str) -> str:
    return (urlsplit(origin).hostname or "").rstrip(".").lower()


def validate_request_origin(mode: str, origin: object) -> str:
    """The origin recorded for the local and Tailscale modes is the one the
    operator's browser reached the dashboard on. It is taken from the request
    and must fit the mode (reviewer): local is a loopback, private, link-local
    or .local address; Tailscale is a .ts.net name or a CGNAT (100.64/10)
    address. Scheme http or https, no path. Raises ServicePublicationError."""
    from tools.dashboard import service_publication

    if not isinstance(origin, str) or not origin:
        raise service_publication.ServicePublicationError("origin_required", 400)
    parts = urlsplit(origin)
    host = (parts.hostname or "").rstrip(".").lower()
    if parts.scheme not in ("http", "https") or not host or parts.path not in ("", "/") \
            or parts.query or parts.fragment or parts.username or parts.password:
        raise service_publication.ServicePublicationError("origin_invalid", 400)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if mode == "local":
        fits = (host == "localhost" or host.endswith(".localhost") or host.endswith(".local")
                or (address is not None and (address.is_loopback or address.is_private
                                             or address.is_link_local)))
    elif mode == "tailscale":
        fits = host.endswith(".ts.net") or (
            address is not None and (
                address in ipaddress.ip_network("100.64.0.0/10")
                or address in ipaddress.ip_network("fd7a:115c:a1e0::/48")))
    else:
        fits = False
    if not fits:
        raise service_publication.ServicePublicationError("origin_invalid", 400)
    port = f":{parts.port}" if parts.port else ""
    # An IPv6 literal keeps its brackets: this string becomes every link's base.
    shown = f"[{host}]" if address is not None and address.version == 6 else host
    return f"{parts.scheme}://{shown}{port}"


def request_came_through_gateway(headers, relay_origin: str | None) -> bool:
    """True when the request arrived over the published relay route: the
    gateway strips every client X-Forwarded-* header and adds its own
    X-Forwarded-Host for the upstream, and the Host is the relay hostname.
    The publish and enrollment calls are local-listener operations and are
    refused on that path (reviewer, and the note's enrollment rule)."""
    host = (headers.get("host") or "").split(":")[0].rstrip(".").lower()
    if headers.get("x-forwarded-host"):
        return True
    return bool(relay_origin) and host == _host_of(relay_origin)


# ── the publish call ──────────────────────────────────────────────────────

async def publish(mode: str, *, org: str = DEFAULT_PUBLISHER, app_label: str | None = None,
                  label: str | None = None, request_origin: str | None = None,
                  origin: str | None = None) -> dict:
    """Perform the whole publish deterministically and idempotently.

    ``autonomy``: reserve the origin (or reuse it), bind this dashboard as the
    target on its plain listener (personal-gated by construction), activate
    (a publication paused by an earlier switch resumes), ask the certificate
    manager and the gateway to converge, and record the row. Re-running
    returns the same origin and makes no second reservation.
    ``tailscale`` / ``local``: record the validated origin the operator
    reached the dashboard on, and PAUSE the relay publication if one exists so
    the dashboard is not reachable remotely while the setting says otherwise.
    """
    from tools.graph.schemas.dashboard_remote_access import REACH_MODES
    from tools.dashboard import service_publication

    if mode not in REACH_MODES:
        raise service_publication.ServicePublicationError("invalid_mode", 400)
    previous = current()
    if mode != "autonomy":
        # Tailscale: the operator may name the Tailnet origin explicitly (they
        # usually choose from the local address, where the request's own
        # origin is not the Tailnet one). A typed name becomes every link's
        # base, so it must be a name in the certificate this node serves;
        # with no such name in the certificate it is recorded as unverified.
        origin = validate_request_origin(
            mode, origin if (mode == "tailscale" and origin) else request_origin)
        row = {"mode": mode, "origin": origin, "published_at": _utc_now()}
        if mode == "tailscale":
            row["origin_verified"] = _tailnet_origin_verified(origin)
        paused = _pause_relay_publication(previous)
        if paused is not None:
            row["paused_relay"] = paused
        return record(row)

    if label is not None:
        check = check_label(org, label)
        if not check.ok:
            raise service_publication.ServicePublicationError("label_" + check.code, 400, check.reason)
        label = check.label
    app = app_label if app_label is not None else DEFAULT_APP_LABEL
    reservation, _created = service_publication.reserve_origin(org, app, persona_slug=label)
    reservation_id = reservation["reservation_id"]
    await service_publication.bind_service_target(
        org, reservation_id, None, service_publication.DASHBOARD_TARGET_DEFAULT_PORT,
        kind=service_publication.DASHBOARD_TARGET_KIND)
    service_publication.transition_reservation(org, reservation_id, "active")
    # The route and the certificate both ride the publisher's serving
    # connector: a live publication is its reason to run, so it is
    # reconciled now, before the certificate and the gateway converge on it.
    # Awaited on purpose (unlike the bind/activate routes): this is the one
    # onboarding publish, and its first certificate attempt should find the
    # connector up rather than wait a retry interval.
    await _reconcile_serving(org)
    _converge()
    row = {
        "mode": "autonomy",
        "origin": reservation["origin"],
        "publisher": org,
        "reservation_id": reservation_id,
        "app_label": reservation["app_label"],
        "published_at": _utc_now(),
    }
    # Links keep working before the relay route is live: remember the local
    # or Tailnet origin this publish was made from (reviewer, auto-w622e).
    local_origin = _local_origin_of(request_origin) or (previous or {}).get("local_origin")
    if local_origin:
        row["local_origin"] = local_origin
    saved = record(row)
    # The gate opens with a one-time enrollment token while no gate passkey is
    # enrolled (F3): the reach step lands on its link once the route is live.
    try:
        from tools.dashboard import passkey_gate

        if passkey_gate.enrolled_count() == 0 and not passkey_gate.enrollment_state()["open"]:
            passkey_gate.open_enrollment(opened_by="onboarding")
    except Exception:
        logger.warning("could not open gate enrollment after the publish", exc_info=True)
    return saved


def _tailnet_origin_verified(origin: str) -> bool:
    """True when the origin's host is a .ts.net name in the served certificate.
    Raises origin_not_this_node when the certificate names Tailnet hosts and
    this is not one of them (a typo, or another node's name); False when the
    certificate names none (nothing to check against: recorded unverified)."""
    from tools.dashboard import service_publication, tls_certificate

    facts = tls_certificate.read_certificate()
    tailnet_names = [name for name in (facts.names if facts else ()) if name.endswith(".ts.net")]
    host = _host_of(origin)
    if not tailnet_names:
        return False
    if host in tailnet_names:
        return True
    raise service_publication.ServicePublicationError(
        "origin_not_this_node", 400,
        f"this node's certificate is for {', '.join(tailnet_names)}")


def suggested_tailnet_origin(environ=None) -> str | None:
    """The Tailnet origin this node would serve on, from the served
    certificate's .ts.net name and the published TLS port; None without one."""
    import os

    from tools.dashboard import tls_certificate

    env = os.environ if environ is None else environ
    facts = tls_certificate.read_certificate(environ=env)
    name = facts.tailnet_name if facts else None
    if not name:
        return None
    port = (env.get("DASHBOARD_PORT") or "8080").strip()
    return f"https://{name}" if port == "443" else f"https://{name}:{port}"


def _local_origin_of(request_origin: str | None) -> str | None:
    from tools.dashboard import service_publication

    for mode in ("local", "tailscale"):
        try:
            return validate_request_origin(mode, request_origin)
        except service_publication.ServicePublicationError:
            continue
    return None


def _pause_relay_publication(previous: dict | None) -> dict | None:
    """Pause the relay publication an earlier choice made, if any, and ask the
    gateway to reload; the paused reservation resumes when the operator
    chooses the relay again. Returns what was paused, for the row and status."""
    from tools.dashboard import service_publication

    if not previous:
        return None
    reservation_id = previous.get("reservation_id")
    publisher = previous.get("publisher") or DEFAULT_PUBLISHER
    if previous.get("mode") != "autonomy" or not reservation_id:
        return previous.get("paused_relay")
    try:
        service_publication.transition_reservation(publisher, reservation_id, "paused")
    except service_publication.ServicePublicationError as exc:
        if exc.code not in ("reservation_not_found", "reservation_released"):
            raise
        return None
    _converge(certificate=False)
    return {"publisher": publisher, "reservation_id": reservation_id, "origin": previous.get("origin")}


async def _reconcile_serving(org: str) -> None:
    """Best effort, off the event loop; the outcome is logged by the
    supervisor and reported by status() as ``connector``."""
    try:
        from tools.dashboard.link_serving_supervisor import reconcile_after_publication

        await asyncio.to_thread(reconcile_after_publication, org)
    except Exception:
        pass


def _converge(*, certificate: bool = True) -> None:
    """Best effort: issue the certificate and reload the gateway now rather
    than at their next tick. Failures here never fail the publish; status
    reports them. The reload task is kept so it is never garbage-collected
    mid-flight."""
    global _reload_task
    if certificate:
        try:
            from tools.dashboard import service_certificate_manager

            service_certificate_manager.request_reconcile()
        except Exception:
            pass
    try:
        from tools.dashboard import web_gateway_supervisor

        _reload_task = asyncio.get_running_loop().create_task(web_gateway_supervisor.request_reload())
    except Exception:
        pass


# ── status ────────────────────────────────────────────────────────────────

def _invalidate_status() -> None:
    global _status_cache
    _status_cache = None


async def status(*, enrollment_link: bool = False) -> dict:
    """The staged progress onboarding polls: what is recorded and, for the
    relay mode, where the publish stands. One live probe answers every poll
    within STATUS_CACHE_SECONDS (single-flight: concurrent tabs share it)."""
    global _status_lock, _status_cache
    if _status_lock is None:
        _status_lock = asyncio.Lock()
    async with _status_lock:
        if _status_cache is not None and time.monotonic() - _status_cache[0] < STATUS_CACHE_SECONDS:
            result = dict(_status_cache[1])
        else:
            result = await _status_uncached()
            _status_cache = (time.monotonic(), result)
            result = dict(result)
    # The enrollment link carries the one-time token: added outside the
    # shared cache, only for a caller the route judged local (never through
    # the gated route).
    if enrollment_link and result.get("enrollment") == "open" and result.get("origin"):
        try:
            from tools.dashboard import passkey_gate

            url = passkey_gate.enrollment_url(result["origin"])
        except Exception:
            url = None
        if url:
            result["enrollment_url"] = url
    return result


async def _status_uncached() -> dict:
    row = current()
    if row is None:
        # Not recorded yet: what onboarding needs to ask well, and nothing else.
        try:
            bound = bound_slug(DEFAULT_PUBLISHER)
        except LabelCheckUnavailable:
            bound = None
        return {"mode": None, "origin": None, "recorded": False,
                "bound_label": bound, "tailnet_origin": suggested_tailnet_origin()}
    result = {"mode": row["mode"], "origin": row["origin"], "recorded": True,
              "published_at": row.get("published_at")}
    if row["mode"] != "autonomy":
        if row["mode"] == "tailscale":
            result["origin_verified"] = bool(row.get("origin_verified"))
        paused = row.get("paused_relay")
        if paused:
            result["relay_publication"] = "paused"
            result["relay_origin"] = paused.get("origin")
        return result
    from tools.dashboard import service_publication, service_status, web_gateway_supervisor
    from tools.dashboard import service_certificate_manager

    org = row.get("publisher") or DEFAULT_PUBLISHER
    reservation_id = row["reservation_id"]
    result["publisher"] = org
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
                # The manager retries a failed issuance on its own interval
                # (its first try often races the connector, Windows runs 6
                # and 9), so a failure is "retrying" until the route is
                # advertised; the detail carries the last reason. Only a
                # state the manager has given up on reads as failed.
                if state.get("state") == "issuance_failed":
                    certificate = "retrying"
            break
    result["certificate"] = certificate
    # The publisher's serving connector: the relay route and the DNS-01
    # certificate both ride it, so its last reconcile outcome is the first
    # thing to read when the certificate or the route does not come up.
    try:
        from tools.dashboard.link_serving_supervisor import get_supervisor
        result["connector"] = get_supervisor().last_outcome(org)
    except Exception:
        result["connector"] = None
    gateway = web_gateway_supervisor.status()
    result["advertised"] = reservation_id in (gateway.get("advertised_routes") or [])
    result["gateway_state"] = gateway.get("state")
    # The personal passkey gate is delivered by its own bead; until its helper
    # runs the route fails closed, and this is what the screen must say.
    result["gate"] = "up" if "dashboard-passkey" in (gateway.get("auth_helpers") or []) else "pending"
    try:
        from tools.dashboard import passkey_gate

        enrollment = passkey_gate.enrollment_state()
        result["enrollment"] = "open" if enrollment["open"] else "closed"
        result["enrolled"] = passkey_gate.enrolled_count()
        if enrollment["open"]:
            result["enrollment_expires_at"] = enrollment["expires_at"]
    except Exception:
        result["enrollment"] = "unknown"
    return result


# ── the origin every link uses (auto-w622e) ───────────────────────────────

def relay_route_live(reservation_id: str | None) -> bool:
    """True while the gateway advertises the dashboard's relay route with its
    passkey gate helper running: only then is the relay origin reachable."""
    if not reservation_id:
        return False
    try:
        from tools.dashboard import web_gateway_supervisor

        gateway = web_gateway_supervisor.status()
    except Exception:
        return False
    return (reservation_id in (gateway.get("advertised_routes") or [])
            and "dashboard-passkey" in (gateway.get("auth_helpers") or []))


def dashboard_public_origin() -> str | None:
    """The dashboard's public origin for every operator-facing link, from the
    recorded remote-access row; None only on a node not yet onboarded (links
    then stay paths). In relay mode the relay origin is returned only while
    its route is advertised with the gate up; until then the local or Tailnet
    origin the publish was made from. Never derives a hostname, never raises."""
    try:
        row = current()
    except Exception:
        return None
    if not row:
        return None
    origin = row.get("origin")
    if row.get("mode") == "autonomy" and not relay_route_live(row.get("reservation_id")):
        origin = row.get("local_origin")
    return origin if isinstance(origin, str) and origin else None


def _tailnet_name_from_certificate(cert_path) -> str | None:
    """The .ts.net DNS name in the dashboard's local TLS certificate, if any.
    A file read; no network call."""
    from pathlib import Path

    path = Path(cert_path)
    if not path.is_file():
        return None
    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(path.read_bytes())
        names = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except Exception:
        return None
    for name in names:
        name = name.rstrip(".").lower()
        if name.endswith(".ts.net"):
            return name
    return None


def seed_origin_from_certificate(*, environ=None, cert_path=None) -> dict | None:
    """Once at startup, on a node onboarded before the remote-access step
    existed: when no row is recorded, take the Tailnet name from
    DASHBOARD_DOMAIN or from the local certificate's .ts.net SAN and record
    the Tailscale origin (https://<name>:8080), so links become absolute.
    Returns the row written, or None when nothing was seeded."""
    import os
    from pathlib import Path

    env = os.environ if environ is None else environ
    try:
        if current() is not None:
            return None
    except Exception:
        return None
    name = (env.get("DASHBOARD_DOMAIN") or "").strip().rstrip(".").lower()
    if not name:
        if cert_path is None:
            cert_path = env.get("AUTONOMY_TLS_CERT") or str(
                Path(env.get("AUTONOMY_DATA_ROOT", "data")) / "tls.crt")
        name = _tailnet_name_from_certificate(cert_path) or ""
    if not name or not name.endswith(".ts.net"):
        return None
    port = (env.get("DASHBOARD_PORT") or "8080").strip()
    if not port.isdigit() or not 0 < int(port) < 65536:
        port = "8080"
    origin = f"https://{name}" if port == "443" else f"https://{name}:{port}"
    # A name that came from the certificate itself is verified; one that came
    # from DASHBOARD_DOMAIN is checked against the certificate like a typed one.
    try:
        verified = _tailnet_origin_verified_or_false(origin, cert_path)
        return record({"mode": "tailscale", "origin": origin, "origin_verified": verified,
                       "published_at": _utc_now()})
    except Exception:
        return None


def _tailnet_origin_verified_or_false(origin: str, cert_path=None) -> bool:
    from tools.dashboard import tls_certificate

    facts = tls_certificate.read_certificate(cert_path) if cert_path is not None else tls_certificate.read_certificate()
    names = [name for name in (facts.names if facts else ()) if name.endswith(".ts.net")]
    return _host_of(origin) in names
