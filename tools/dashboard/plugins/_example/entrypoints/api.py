"""Authenticated, organization-scoped backend example for dashboard plugins."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard.api_auth import (
    organization_scope_from_request,
    require_authenticated_api_caller,
)
from tools.dashboard.plugins._example.entrypoints.schemas import (
    EXAMPLE_RECORD_SET_ID,
)
from tools.graph import settings_ops


async def get_example_record(request: Request) -> JSONResponse:
    """Return this caller's organization-local example record.

    Response: ``{organization, record}``, where ``record`` is either null or
    ``{key, message, updated_at}``. The route intentionally accepts no org in
    its path, query, or body: identity middleware supplies the trusted scope.
    """

    refusal = require_authenticated_api_caller(request)
    if refusal is not None:
        return refusal

    organization = organization_scope_from_request(request)
    if organization is None:
        return JSONResponse(
            {"error": "organization scope required"},
            status_code=400,
        )

    # `peers=[]` is deliberate. Organization scope chooses the owning DB;
    # disabling peers prevents published rows from another org composing into
    # this application-private response.
    row = settings_ops.read_set_key(
        EXAMPLE_RECORD_SET_ID,
        "current",
        org=organization,
        peers=[],
    )
    record = None
    if row is not None:
        payload = row["payload"]
        record = {
            "key": row["key"],
            "message": payload["message"],
            "updated_at": payload["updated_at"],
        }
    return JSONResponse({
        "organization": organization,
        "record": record,
    })


# The plugin loader mounts these Route objects exactly as declared. It does
# not add a prefix, so keep every route inside the plugin's namespace.
routes: list[Route] = [
    Route(
        "/api/plugins/example/record",
        get_example_record,
        methods=["GET"],
    ),
]
