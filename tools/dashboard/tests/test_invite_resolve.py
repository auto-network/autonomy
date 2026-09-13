"""Local routing endpoint contract; not end-to-end browser verification."""
import httpx
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard import network_routes as routes


def test_local_resolve_returns_only_routing_and_refuses_fragment_values(monkeypatch):
    org, ref, token = "11111111-1111-4111-8111-111111111111", "a" * 64, "b" * 32

    def registry(request):
        assert request.url.path == f"/v1/links/{token}/envelope"
        return httpx.Response(200, json={
            "target_type": "org:join", "org": org, "invite_ref": ref,
            "root_pub": "c" * 64, "meta": {"org_name": "Untrusted"},
        })

    monkeypatch.setattr(routes, "_relay_client", lambda base: httpx.AsyncClient(
        base_url=base, transport=httpx.MockTransport(registry)))
    route = next(r for r in routes.ROUTES if r.path == "/api/network/invite/resolve")
    with TestClient(Starlette(routes=[route])) as client:
        body = {"relay_host": "https://relay.example", "channel_token": token}
        response = client.post(route.path, json=body)
        assert response.status_code == 200
        assert response.json() == {"ok": True, "org": org, "invite_ref": ref}
        for key in ("k", "t", "bearer", "root_pub"):
            assert client.post(route.path, json={**body, key: "c" * 64}).status_code == 400
