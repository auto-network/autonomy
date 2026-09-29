"""auto-kd6tl / auto-gy1ir: a ledger open that waits on the org database (a
sync apply's write lock, or the checkpoint when its connection closes) must
not stop the dashboard answering other requests."""

from __future__ import annotations

import asyncio
import time

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard import network_routes


def test_a_slow_ledger_open_does_not_block_other_requests(monkeypatch, tmp_path):
    import tools.network.ledger as ledger

    store_path = tmp_path / "acme.db"
    store_path.write_bytes(b"")
    monkeypatch.setattr(network_routes, "_mock_mode", lambda: False)
    monkeypatch.setattr(network_routes, "resolve_scoped_org",
                        lambda org, request=None: ("acme", None))
    monkeypatch.setattr(ledger, "org_ledger_db_path", lambda org: store_path)

    class SlowStore:
        """Stands in for an open waiting on the org database lock."""

        def __init__(self, path):
            time.sleep(1.0)
            raise ledger.LedgerError("database is locked")

    monkeypatch.setattr(ledger, "LedgerStore", SlowStore)

    async def probe(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[
        Route("/api/network/ledger/heads", network_routes.get_ledger_heads),
        Route("/probe", probe),
    ])

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            done: dict[str, float] = {}

            async def timed(name, url):
                response = await client.get(url)
                done[name] = time.monotonic()
                return response

            heads = asyncio.create_task(timed("heads", "/api/network/ledger/heads?org=acme"))
            await asyncio.sleep(0)          # the heads request starts first
            probe = asyncio.create_task(timed("probe", "/probe"))
            return await heads, await probe, done

    heads, answer, done = asyncio.run(scenario())
    assert answer.status_code == 200
    # Answered while the ledger open was still waiting, not after it.
    assert done["heads"] - done["probe"] > 0.5, done
    assert heads.status_code == 500 and "could not read authority ledger" in heads.json()["error"]
