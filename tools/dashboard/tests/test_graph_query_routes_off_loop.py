"""2026-09-30: /api/graph/search and /api/graph/attention ran their SQLite
queries on the event-loop thread, and each agent `graph search` or
`graph attention` froze every dashboard request for up to 17.5 s. Both
queries now run on a worker thread."""

import asyncio
import threading

from starlette.requests import Request

from tools.dashboard import api_auth, server


def _request(path: str, query: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": path,
                    "query_string": query.encode(), "headers": []})


def _run_off_loop(monkeypatch, name: str, handler, path: str, query: str):
    seen = {}

    def query_fn(*args, **kwargs):
        seen["thread"] = threading.current_thread()
        return [] if name == "search" else [{"text": "hi"}]

    monkeypatch.setattr(server.graph_ops, name, query_fn)
    monkeypatch.setattr(api_auth, "organization_scope_from_request", lambda request: "personal")

    async def call():
        loop_thread = threading.current_thread()
        response = await handler(_request(path, query))
        return loop_thread, response

    loop_thread, response = asyncio.run(call())
    assert response.status_code == 200
    assert seen["thread"] is not loop_thread
    return response


def test_search_runs_off_the_event_loop(monkeypatch):
    _run_off_loop(monkeypatch, "search", server.api_graph_search,
                  "/api/graph/search", "q=thread+pool")


def test_attention_runs_off_the_event_loop(monkeypatch):
    response = _run_off_loop(monkeypatch, "list_attention", server.api_graph_attention,
                             "/api/graph/attention", "last=15&search=pool")
    assert b'"rows":[{"text":"hi"}]' in response.body
