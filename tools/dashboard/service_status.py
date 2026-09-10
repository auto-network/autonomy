"""Honest per-link status and end-to-end refresh for published Services
(auto-q5xni; design of record graph://b2211170-8f3).

``get_published_links`` is a read model: it says a reservation is *active*,
which is a fact about Settings, not about whether anyone can reach the link.
This module answers the second question, one reservation at a time, in the
four-word vocabulary the Published Links screen renders:

* ``Live`` -- a public HEAD reached the application through the tunnel;
* ``Down`` -- it did not, and for a link served on THIS machine the first
  stage of the serve path that failed is named;
* ``Checking`` -- the public edge answered nothing yet (the relay issues a
  certificate on the first hit) and the caller chose not to wait;
* ``Paused`` -- the operator paused the reservation; nothing is probed.

*Remote* is a location badge, never a health state: a link whose target is
bound to another machine is probed with the public HEAD only, because that
request travels the public path and can verify a link served anywhere.

The serve path has seven stages, each with an existing check:

====  ============  ======================================================
 #    stage         existing check
====  ============  ======================================================
 1    reservation   NamespaceReservationV1 state (active / paused)
 2    target        service_publication.resolve_service_target
 3    certificate   link_serving_supervisor.serve_cert_state
 4    connector     control ``connector-status`` (serving, serving_slot)
 5    lease         control ``host-leases`` (status) / ``serve-host`` (refresh)
 6    gateway       web_gateway_supervisor.status().advertised_routes
 7    public        HEAD https://<host>/ from this process
====  ============  ======================================================

Stage 7 exercises DNS -> TLS -> Caddy -> relay -> lease -> connector -> app
in one request, so it also proves the auto-nh1po machine pin end to end.

``refresh_service`` is the mutating twin of ``service_status``: it brings
the connector up (``ServingSupervisor.ensure``), RE-DECLARES the serve host
with the serving machine (``serve-host`` with ``machine``), and then runs
the same probe. Re-declaring is how every serve link published before the
machine pin existed becomes pinned without a dashboard restart.
"""

from __future__ import annotations

import asyncio
import http.client
import re
import socket
import ssl
import time
from datetime import datetime, timezone

from tools.dashboard import service_publication
from tools.dashboard.service_publication import ServicePublicationError

STAGES = (
    "reservation",
    "target",
    "certificate",
    "connector",
    "lease",
    "gateway",
    "public",
)

LIVE = "Live"
DOWN = "Down"
CHECKING = "Checking"
PAUSED = "Paused"

#: One public HEAD attempt: DNS + TLS + relay dial + the app's first byte.
PUBLIC_TIMEOUT_S = 6.0
#: Pause before the single retry that covers on-demand TLS issuance.
PUBLIC_RETRY_DELAY_S = 1.5
#: Control ops against the local connector are answered from memory.
CONTROL_TIMEOUT_S = 2.0

_MACHINE_HEX_RE = re.compile(r"^[0-9a-f]{64}\Z")
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# --- stage 7: the public HEAD -------------------------------------------


def head_public_origin(host: str, *, timeout: float = PUBLIC_TIMEOUT_S) -> int:
    """One HEAD to ``https://<host>/``; returns the status code, raises on
    any failure before a status line (DNS, connect, TLS, timeout).

    HEAD, not GET: the app's body is never read and the probe cannot be
    mistaken for a visitor. Redirects are not followed -- a 301 is the app
    answering, which is all that is asked."""
    if not _HOSTNAME_RE.match(host or ""):
        raise ValueError("invalid public hostname")
    context = ssl.create_default_context()
    connection = http.client.HTTPSConnection(host, 443, timeout=timeout, context=context)
    try:
        connection.request(
            "HEAD", "/", headers={"User-Agent": "autonomy-serve-probe/1", "Host": host}
        )
        response = connection.getresponse()
        return int(response.status)
    finally:
        connection.close()


def classify_public_status(status: int | None) -> tuple[str, str]:
    """Map one HEAD outcome to (state, detail).

    ANY application-level status is Live: 200, 301, 401, 404, 405 (an app
    that refuses HEAD) and even 500 all mean the request crossed the
    tunnel and the app spoke. The gateway codes 502/503/504 are what edges
    say when they could NOT reach the app -- the relay's "temporarily
    unavailable" page, the local Caddy's paused/unavailable responses, an
    upstream timeout -- so they are Down. No status at all is the caller's
    to interpret (see :func:`probe_public`)."""
    if status is None:
        return DOWN, "no response from the public edge"
    if status in (502, 503, 504):
        return DOWN, f"public edge answered {status} without reaching the app"
    return LIVE, f"app answered {status}"


async def probe_public(
    host: str,
    *,
    head=None,
    retries: int = 1,
    retry_delay: float = PUBLIC_RETRY_DELAY_S,
    timeout: float = PUBLIC_TIMEOUT_S,
) -> dict:
    """The end-to-end verdict for one public hostname.

    A connection-level failure on the first hit (no status line: DNS, TLS,
    connect, timeout) is retried once by default, because the relay's
    on-demand TLS issues the certificate during that first hit and a fresh
    name can answer nothing the first time. With ``retries=0`` such a first
    hit is reported as Checking rather than Down: the caller has chosen not
    to wait, and "not yet" is not "broken"."""
    head = head or head_public_origin
    attempts = 0
    last_error = ""
    while True:
        attempts += 1
        try:
            status = await asyncio.wait_for(
                asyncio.to_thread(head, host, timeout=timeout), timeout=timeout + 1.0
            )
        except (
            OSError,
            ssl.SSLError,
            socket.timeout,
            asyncio.TimeoutError,
            http.client.HTTPException,
            ValueError,
        ) as exc:
            last_error = f"{type(exc).__name__}: {exc}"[:200]
            if attempts <= retries:
                await asyncio.sleep(retry_delay)
                continue
            state = CHECKING if (retries == 0 and attempts == 1) else DOWN
            return {
                "state": state,
                "http_status": None,
                "attempts": attempts,
                "detail": (
                    "no response from the public edge yet (certificate "
                    "may still be issuing)" if state == CHECKING
                    else f"public edge unreachable ({last_error})"
                ),
            }
        state, detail = classify_public_status(status)
        return {
            "state": state,
            "http_status": status,
            "attempts": attempts,
            "detail": detail,
        }


# --- the stage runner -----------------------------------------------------


class _Stage:
    __slots__ = ("name", "ok", "detail")

    def __init__(self, name: str, ok: bool | None, detail: str = "") -> None:
        self.name = name
        self.ok = ok
        self.detail = detail

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


def _control_call(org: str, op: str, args: dict, control=None) -> dict:
    if control is None:
        from tools.dashboard.link_serving_supervisor import control as _control

        return _control(org, op, args, timeout=CONTROL_TIMEOUT_S)
    return control(org, op, args)


def _serve_cert_ok(org: str, serve_cert_state=None) -> tuple[bool, str]:
    if serve_cert_state is None:
        from tools.dashboard.link_serving_supervisor import serve_cert_state as _state

        serve_cert_state = _state
    try:
        state = serve_cert_state(org)
    except Exception as exc:
        return False, f"serve certificate state unreadable ({exc})"
    status = str(state.get("status") or "missing")
    if status == "ok":
        return True, "serving certificate current"
    return False, f"serving certificate {status}" + (
        f": {state['error']}" if state.get("error") else ""
    )


def _connector_serving(org: str, control=None) -> tuple[bool, str, dict]:
    try:
        status = _control_call(org, "connector-status", {}, control)
    except Exception as exc:
        return False, f"serving connector not running ({exc})", {}
    if not isinstance(status, dict) or status.get("ok") is not True:
        return False, "serving connector refused status", {}
    if status.get("serving") is not True:
        return False, "serving connector has no live tunnel", status
    return True, "connector tunnel established", status


def _serving_machine(status: dict) -> str | None:
    slot = status.get("serving_slot") if isinstance(status, dict) else None
    machine = slot.get("machine") if isinstance(slot, dict) else None
    return machine if isinstance(machine, str) and _MACHINE_HEX_RE.match(machine) else None


def _lease_held(org: str, reservation_id: str, host: str, control=None) -> tuple[bool, str]:
    """Read-only: does the connector hold an unexpired host lease for this
    reservation under this hostname?"""
    try:
        reply = _control_call(org, "host-leases", {}, control)
    except Exception as exc:
        return False, f"host leases unreadable ({exc})"
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        return False, "serving connector refused host-leases"
    leases = reply.get("leases") if isinstance(reply.get("leases"), dict) else {}
    entry = leases.get(reservation_id)
    if not isinstance(entry, dict):
        return False, "no host lease registered for this reservation"
    leased_host = entry.get("host")
    if isinstance(leased_host, str) and leased_host and leased_host != host:
        return False, f"host lease names {leased_host}, not {host}"
    expires_at = entry.get("expires_at")
    if isinstance(expires_at, (int, float)) and expires_at <= time.time():
        return False, "host lease expired"
    return True, "host lease held"


def _declare_host(
    org: str, reservation_id: str, host: str, machine: str | None, control=None
) -> tuple[bool, str]:
    """Mutating: (re-)declare the serve host, pinned to *machine*. Mirrors
    the gateway reconciler: on host-owned-elsewhere release then re-declare,
    which the relay refuses while the other machine's lease is live so a
    running service is never displaced."""
    args = {"reservation": reservation_id, "host": host}
    if machine is not None:
        args["machine"] = machine
    try:
        reply = _control_call(org, "serve-host", args, control)
        if (
            isinstance(reply, dict)
            and reply.get("ok") is not True
            and reply.get("error") == "host-owned-elsewhere"
            and machine is not None
        ):
            released = _control_call(
                org, "release-host", {"reservation": reservation_id}, control
            )
            if isinstance(released, dict) and released.get("ok") is True:
                reply = _control_call(org, "serve-host", args, control)
    except Exception as exc:
        return False, f"serve-host failed ({exc})"
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        error = (reply or {}).get("error") if isinstance(reply, dict) else None
        return False, f"relay refused the host lease ({error or 'refused'})"
    pinned = "pinned to this machine" if machine is not None else "unpinned"
    return True, f"host lease declared, {pinned}"


def _gateway_advertises(reservation_id: str, gateway_status=None) -> tuple[bool, str]:
    if gateway_status is None:
        from tools.dashboard import web_gateway_supervisor

        gateway_status = web_gateway_supervisor.status
    try:
        status = gateway_status()
    except Exception as exc:
        return False, f"gateway status unreadable ({exc})"
    routes = status.get("advertised_routes") if isinstance(status, dict) else None
    if isinstance(routes, (list, tuple, set)) and reservation_id in routes:
        return True, "local gateway advertises the route"
    state = (status or {}).get("state") if isinstance(status, dict) else None
    reason = (status or {}).get("reason") if isinstance(status, dict) else None
    return False, f"local gateway does not advertise the route ({state or 'unknown'}: {reason or 'no reason'})"


def _row_context(org: str, reservation_id: str) -> tuple[object, dict, str, str | None, bool]:
    """(reservation member, target payload or {}, host, serving_machine,
    remote). Raises ServicePublicationError for unknown / invalid ids."""
    member = service_publication._reservation_for_target(org, reservation_id, serving=False)
    host = service_publication.reservation_hostname_from_payload(member.payload)
    target = service_publication._target_member_by_key(org, reservation_id)
    target_payload = target.payload if target is not None else {}
    serving_machine = target_payload.get("machine_id")
    if not (isinstance(serving_machine, str) and _MACHINE_HEX_RE.match(serving_machine)):
        serving_machine = None
    local = service_publication._read_local_machine_id()
    remote = serving_machine is not None and serving_machine != local
    return member, target_payload, host, serving_machine, remote


def _result(
    reservation_id: str,
    host: str,
    state: str,
    *,
    remote: bool,
    serving_machine: str | None,
    stages: list[_Stage],
    public: dict | None,
    refreshed: bool = False,
) -> dict:
    failed = next((s.name for s in stages if s.ok is False), None)
    result = {
        "reservation_id": reservation_id,
        "origin": f"https://{host}",
        "state": state,
        "remote": remote,
        "serving_machine": serving_machine,
        "failed_stage": failed,
        "detail": next((s.detail for s in stages if s.name == failed), "") if failed else "",
        "stages": [s.as_dict() for s in stages],
        "public": public,
        "checked_at": _utc_now(),
    }
    if refreshed:
        result["refreshed"] = True
    return result


async def service_status(
    org: str,
    reservation_id: str,
    *,
    control=None,
    serve_cert_state=None,
    gateway_status=None,
    head=None,
    retries: int = 1,
) -> dict:
    """Read-only. Never mutates connector, relay, or Settings state.

    Local link: every stage is checked and the public HEAD decides; the
    first failed stage is named so a Down row says WHERE. Remote link: the
    public HEAD alone, and no internal stage detail (the stages live on
    another machine). Paused: no probe at all."""
    member, target, host, serving_machine, remote = _row_context(org, reservation_id)
    stages: list[_Stage] = []
    if member.payload.get("state") == "paused":
        stages.append(_Stage("reservation", None, "paused by the operator"))
        return _result(
            reservation_id, host, PAUSED, remote=remote,
            serving_machine=serving_machine, stages=stages, public=None,
        )
    stages.append(_Stage("reservation", True, "active"))

    if remote:
        public = await probe_public(host, head=head, retries=retries)
        stages.append(_Stage("public", public["state"] == LIVE, public["detail"]))
        # Stage detail for a remote link is the HEAD alone: the other six
        # stages run on the machine that serves it, which this one cannot
        # inspect. The verdict is still real because the HEAD crossed the
        # public path that any visitor would.
        return _result(
            reservation_id, host, public["state"], remote=True,
            serving_machine=serving_machine, stages=stages, public=public,
        )

    if not target:
        stages.append(_Stage("target", False, "no target bound"))
    else:
        try:
            await service_publication.resolve_service_target(org, reservation_id)
            stages.append(_Stage("target", True, f"{target.get('session_id')}:{target.get('port')} reachable"))
        except ServicePublicationError as exc:
            stages.append(_Stage("target", False, exc.code.replace("_", " ")))

    ok, detail = _serve_cert_ok(org, serve_cert_state)
    stages.append(_Stage("certificate", ok, detail))

    ok, detail, status = _connector_serving(org, control)
    stages.append(_Stage("connector", ok, detail))
    if ok:
        ok, detail = _lease_held(org, reservation_id, host, control)
        stages.append(_Stage("lease", ok, detail))
    else:
        stages.append(_Stage("lease", None, "not checked: connector down"))

    ok, detail = _gateway_advertises(reservation_id, gateway_status)
    stages.append(_Stage("gateway", ok, detail))

    public = await probe_public(host, head=head, retries=retries)
    stages.append(_Stage("public", public["state"] == LIVE, public["detail"]))
    return _result(
        reservation_id, host, public["state"], remote=False,
        serving_machine=serving_machine, stages=stages, public=public,
    )


async def refresh_service(
    org: str,
    reservation_id: str,
    *,
    ensure=None,
    control=None,
    serve_cert_state=None,
    gateway_status=None,
    head=None,
) -> dict:
    """Mutating. Re-establish and verify the whole path for one local link
    and RE-DECLARE its machine pin, reporting each stage.

    Refused for a paused reservation (resume it first) and for a link whose
    target is bound to another machine (``target_remote``): the connector
    that must hold the lease runs there, not here."""
    member, target, host, serving_machine, remote = _row_context(org, reservation_id)
    if member.payload.get("state") == "paused":
        raise ServicePublicationError("reservation_paused", 409)
    if remote:
        raise ServicePublicationError(
            "target_remote", 409,
            "this link is served by another machine; refresh it from there",
        )
    stages: list[_Stage] = [_Stage("reservation", True, "active")]

    # 2. target -- the container is live and the port answers, on THIS machine.
    if not target:
        stages.append(_Stage("target", False, "no target bound"))
    else:
        try:
            await service_publication.resolve_service_target(org, reservation_id)
            stages.append(_Stage("target", True, f"{target.get('session_id')}:{target.get('port')} reachable"))
        except ServicePublicationError as exc:
            stages.append(_Stage("target", False, exc.code.replace("_", " ")))

    # 3. certificate -- and bring the connector up on it.
    ok, detail = _serve_cert_ok(org, serve_cert_state)
    stages.append(_Stage("certificate", ok, detail))
    if ensure is None:
        from tools.dashboard.link_serving_supervisor import get_supervisor

        ensure = get_supervisor().ensure
    try:
        ensured = await asyncio.to_thread(ensure, org)
    except Exception as exc:
        ensured = {"running": False, "reason": f"ensure failed ({exc})"}

    # 4. connector -- the supervisor's verdict, then the process's own word.
    ok, detail, status = _connector_serving(org, control)
    if not ok and isinstance(ensured, dict) and ensured.get("reason"):
        detail = f"{detail}; supervisor: {ensured['reason']}"
    stages.append(_Stage("connector", ok, detail))

    # 5. lease -- RE-DECLARE with the serving machine (auto-nh1po). This is
    # the step that pins a link published before the machine pin existed.
    declared_machine = _serving_machine(status) if ok else None
    if ok:
        ok, detail = _declare_host(org, reservation_id, host, declared_machine, control)
        stages.append(_Stage("lease", ok, detail))
    else:
        stages.append(_Stage("lease", None, "not declared: connector down"))

    # 6. gateway.
    ok, detail = _gateway_advertises(reservation_id, gateway_status)
    stages.append(_Stage("gateway", ok, detail))

    # 7. public -- end to end, through the pin just declared.
    public = await probe_public(host, head=head, retries=1)
    stages.append(_Stage("public", public["state"] == LIVE, public["detail"]))
    result = _result(
        reservation_id, host, public["state"], remote=False,
        serving_machine=serving_machine, stages=stages, public=public,
        refreshed=True,
    )
    result["declared_machine"] = declared_machine
    return result
