"""Runner offers from the dashboard (bead auto-a51qv).

The org profile's "Allow organization members to remotely launch Workspaces
on this machine" toggle, and ``graph runner offer|withdraw|list``, drive:

* ``offer`` -- write this machine's ``autonomy.org.session-runner`` row in the
  organization, signed by its per-organization serving key under the member
  persona's certificate (both held by the org sync channel);
* ``withdraw`` -- deprecate that row (NEW launches are refused; running
  sessions are untouched, operator ruling 2026-09-29);
* ``runners`` -- every offer whose own evidence verifies and whose persona is
  still a member, ``live`` when its machine is among the organization's live
  relay serving slots.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.graph import settings_ops
from tools.graph.schemas.org_session_runner import (
    HARNESSES,
    ORG_SESSION_RUNNER_REVISION as REVISION,
    ORG_SESSION_RUNNER_SET_ID as SET_ID,
)

logger = logging.getLogger(__name__)

NOT_SERVING = "runner-org-not-served"   # no org sync channel on this machine


def _channel(slug: str):
    from tools.dashboard import org_sync_channels

    return org_sync_channels.provider()().get(slug)


def _label() -> str:
    from tools.dashboard import fleet_machines, session_presence

    local = session_presence.local_machine()
    return fleet_machines.label_for(local.machine_pub) if local else "this machine"


def _default_capacity() -> int:
    from tools.dashboard import server

    return int(server._resolved_dispatch_limits().get("agentic_max_concurrent", 0))


def local_images(slug: str) -> list[str]:
    """The organization's workspace images present on this machine."""
    try:
        out = subprocess.run(
            ["docker", "images", "--format", "{{.Repository}}"],
            capture_output=True, text=True, timeout=20, check=True).stdout
    except Exception:
        logger.warning("runner offer: could not list docker images", exc_info=True)
        return []
    prefix = f"{slug}/"
    return sorted({line.strip() for line in out.splitlines() if line.startswith(prefix)})


def offer(slug: str, capacity: int | None = None) -> dict:
    """Write (or refresh) this machine's offer in *slug*."""
    from tools.network.org_session_runner import build_row

    channel = _channel(slug)
    if channel is None:
        return {"ok": False, "error": NOT_SERVING}
    row = build_row(
        channel.machine_key, channel.persona_cert, label=_label(),
        capacity=_default_capacity() if capacity is None else int(capacity),
        harnesses=list(HARNESSES), images=local_images(slug))
    settings_ops.upsert_by_key(SET_ID, REVISION, channel.machine_pub, row,
                               org=slug, state="published")
    return {"ok": True, "machine_pub": channel.machine_pub, "offer": row}


def withdraw(slug: str) -> dict:
    """Deprecate this machine's offer in *slug*, if it has one."""
    channel = _channel(slug)
    if channel is None:
        return {"ok": False, "error": NOT_SERVING}
    withdrawn = 0
    for member in settings_ops.read_owned_set(SET_ID, org=slug).members:
        if member.key == channel.machine_pub and not member.deprecated:
            settings_ops.deprecate_setting(member.id, org=slug)
            withdrawn += 1
    return {"ok": True, "machine_pub": channel.machine_pub, "withdrawn": withdrawn}


def runners(slug: str) -> dict:
    """The organization's verified offers, each marked live or not."""
    from tools.dashboard import org_sync_channels
    from tools.network.org_session_runner import verify_row

    channel = _channel(slug)
    if channel is None:
        return {"ok": False, "error": NOT_SERVING}
    slots = org_sync_channels.relay_slots_provider()().get(slug) or []
    live = {s.get("machine") for s in slots if isinstance(s, dict)}
    out = []
    for member in settings_ops.read_owned_set(SET_ID, org=slug).members:
        offer_row = verify_row(member.key, member.payload, org=channel.org,
                               is_member=channel.is_member)
        if offer_row is None:
            continue
        out.append({
            "machine_pub": member.key, "persona_pub": offer_row["persona_pub"],
            "label": offer_row["label"], "capacity": offer_row["capacity"],
            "harnesses": offer_row["harnesses"], "images": offer_row["images"],
            "updated_at": offer_row["updated_at"], "live": member.key in live,
            "this_machine": member.key == channel.machine_pub,
        })
    return {"ok": True, "runners": sorted(out, key=lambda r: r["label"])}


def offered_here(slug: str) -> bool:
    """Whether this machine currently offers itself in *slug*."""
    channel = _channel(slug)
    if channel is None:
        return False
    return any(m.key == channel.machine_pub and not m.deprecated
               for m in settings_ops.read_owned_set(SET_ID, org=slug).members)


# ── routes (global operator authority) ──────────────────────────────────────


async def get_runners(request: Request) -> JSONResponse:
    from tools.dashboard import api_auth

    refused = api_auth.require_global_api_authority(request)
    if refused is not None:
        return refused
    slug = request.path_params["slug"]
    reply = await asyncio.to_thread(runners, slug)
    reply["offered_here"] = await asyncio.to_thread(offered_here, slug)
    return JSONResponse(reply, status_code=200 if reply.get("ok") else 409)


async def put_runner(request: Request) -> JSONResponse:
    """``{"offered": bool, "capacity"?: int}`` for this machine in the org."""
    from tools.dashboard import api_auth

    refused = api_auth.require_global_api_authority(request)
    if refused is not None:
        return refused
    slug = request.path_params["slug"]
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("offered"), bool):
        return JSONResponse({"error": "offered (true or false) is required"}, status_code=400)
    capacity = body.get("capacity")
    if capacity is not None and (isinstance(capacity, bool) or not isinstance(capacity, int)):
        return JSONResponse({"error": "capacity must be an integer"}, status_code=400)
    try:
        reply = await asyncio.to_thread(offer, slug, capacity) if body["offered"] \
            else await asyncio.to_thread(withdraw, slug)
    except Exception as exc:
        logger.warning("runner offer for %s failed", slug, exc_info=True)
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=400)
    return JSONResponse(reply, status_code=200 if reply.get("ok") else 409)


ROUTES = [
    Route("/api/orgs/{slug}/runners", get_runners, methods=["GET"]),
    Route("/api/orgs/{slug}/runner", put_runner, methods=["PUT"]),
]
