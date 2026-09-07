"""Exact-route authority for the legacy voice WebSocket commit command."""
from starlette.responses import JSONResponse
from starlette.routing import Route
from tools.dashboard import api_auth

PATH = "/api/internal/voice-commit"


async def commit(request):
    principal = api_auth.principal_from_request(request)
    if (principal.kind is not api_auth.ApiPrincipalKind.EXTERNAL_SERVICE
            or principal.application_scope != "voice-sidecar"
            or principal.resource_audience != "dashboard-local"
            or not principal.allows_api("POST", PATH)):
        return JSONResponse({"error": "voice service credential required"}, status_code=403)
    try:
        data = await request.json()
        bind, text = data["bind"], data["text"]
        if not isinstance(bind, str) or not bind or not isinstance(text, str) or not text or len(text) > 100000:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"error": "invalid voice commit"}, status_code=400)
    from tools.dashboard.tmux_send import tmux_send_awaited
    try:
        await tmux_send_awaited(bind, text)
    except Exception:
        return JSONResponse({"error": "delivery not confirmed"}, status_code=503)
    return JSONResponse({"ok": True})


ROUTES = [Route(PATH, commit, methods=["POST"])]
