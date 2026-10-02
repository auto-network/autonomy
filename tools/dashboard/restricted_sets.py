"""Settings sets only the operator in person may read in full or write
through the dashboard's settings routes (auto-26e8a, declared default
recorded on the bead by the remote-sessions coordinator, 2026-10-02).

``autonomy.org.harness-accounts`` holds organization-shared inference
accounts: the most portable secret in an organization vault. Its rows are
for members' launches, which the launcher opens in-process. Through the
settings routes:

* a caller that is not the operator in person -- the dashboard cookie or a
  host terminal (:func:`tools.dashboard.vault_routes.is_operator_terminal`) --
  receives each row of the set with its value redacted, except the parts
  that name an account (alias, email, organization name);
* adding, replacing, excluding, promoting, deprecating, undeprecating,
  removing or migrating a row of the set needs the operator in person;
  anyone else is refused ``operator-only-set``.

Organization sync carries a member's own writes between machines; that path
does not pass through these routes.
"""

from __future__ import annotations

import functools
import json

from starlette.responses import JSONResponse

from tools.graph.schemas.vault_credential import ORG_HARNESS_ACCOUNTS_SET_ID

#: set_id -> key suffixes whose values are not secret.
RESTRICTED = {ORG_HARNESS_ACCOUNTS_SET_ID: (".alias", ".email", ".org_name")}
REFUSAL = "operator-only-set"
REDACTED = "[redacted]"


def operator_in_person(request) -> bool:
    from tools.dashboard import api_auth
    from tools.dashboard.vault_routes import is_operator_terminal

    return is_operator_terminal(api_auth.principal_from_request(request))


def redact(value):
    """*value* with every restricted row's secret payload replaced."""
    if isinstance(value, list):
        return [redact(v) for v in value]
    if not isinstance(value, dict):
        return value
    out = {k: redact(v) for k, v in value.items()}
    public = RESTRICTED.get(out.get("set_id"))
    if public is not None and "payload" in out and not str(out.get("key", "")).endswith(public):
        out["payload"] = {"value": REDACTED} if isinstance(out["payload"], dict) else REDACTED
    return out


def guard_read(handler):
    """Redact restricted rows in a settings read's JSON reply for anyone but
    the operator in person."""

    @functools.wraps(handler)
    async def guarded(request):
        response = await handler(request)
        if not isinstance(response, JSONResponse) or operator_in_person(request):
            return response
        try:
            data = json.loads(response.body)
        except ValueError:
            return response
        cleaned = redact(data)
        if cleaned == data:
            return response
        return JSONResponse(cleaned, status_code=response.status_code)

    return guarded


async def _set_of_body(request):
    try:
        body = await request.json()
    except Exception:
        return None
    return body.get("set_id") if isinstance(body, dict) else None


async def _set_of_path(request):
    return request.path_params.get("set_id")


async def _set_of_row(request):
    import asyncio

    from tools.dashboard import api_auth
    from tools.graph import ops as graph_ops

    org = api_auth.organization_scope_from_request(request)
    try:
        row = await asyncio.to_thread(graph_ops.get_setting, request.path_params["id"],
                                      org=org or graph_ops.CALLER_ORG)
    except Exception:
        return None
    return getattr(row, "set_id", None)


def guard_write(handler, *, target: str):
    """Refuse a write to a restricted set unless the operator is in person.
    *target* says where the request names its set: ``body``, ``path`` or
    ``row`` (the setting id in the path)."""
    set_of = {"body": _set_of_body, "path": _set_of_path, "row": _set_of_row}[target]

    @functools.wraps(handler)
    async def guarded(request):
        if await set_of(request) in RESTRICTED and not operator_in_person(request):
            return JSONResponse(
                {"error": "only the operator in person may change this set",
                 "refusal": REFUSAL}, status_code=403)
        return await handler(request)

    return guarded
