"""Settings sets only the operator in person may read in full through the
dashboard's settings routes (auto-26e8a). auto-u0q28 replaces this by never
returning any vaulted value inline, and deletes this module.

``autonomy.org.vault.harness-credential`` holds the secrets of
organization-shared inference accounts: the most portable secret in an
organization vault. Its rows are for members' launches, which the launcher
opens in-process. Through the settings routes, a caller that is not the
operator in person -- the dashboard cookie or a host terminal
(:func:`tools.dashboard.vault_routes.is_operator_terminal`) -- receives each
row with its value redacted. The parts that name an account (alias, email,
organization name) are in the public ``autonomy.org.harness.account`` set
(auto-raepo). Writes are open to agents (operator ruling 2026-10-02).
"""

from __future__ import annotations

import functools
import json

from starlette.responses import JSONResponse

from tools.graph.schemas.harness_account import ORG_HARNESS_CREDENTIAL_SET_ID

#: set_id -> key suffixes whose values are not secret.
RESTRICTED = {ORG_HARNESS_CREDENTIAL_SET_ID: ()}
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
