"""POST /api/network/serve-cert must not run blocking work on the event loop.

A founding on the Windows test node (2026-09-26, auto-lcsqr) logged
``SLOW-REQUEST(HANG) POST /api/network/serve-cert 200 10651ms``: after storing
the credential the route called the link-serving supervisor's ensure()
directly, which reads the sealed serving key and starts the connector. Every
blocking call in the route goes through asyncio.to_thread.
"""

import ast
import inspect
import textwrap

from tools.dashboard import network_routes


def _calls(fn):
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _name(call):
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def test_supervisor_ensure_is_never_called_on_the_loop():
    for call in _calls(network_routes._post_serve_cert_v3):
        assert _name(call) != "ensure", (
            "get_supervisor().ensure must be passed to asyncio.to_thread, not called"
        )


def test_serve_cert_row_read_is_off_the_loop():
    names = [_name(c) for c in _calls(network_routes._post_serve_cert_v3)]
    assert "machine_serve_cert_row" not in names
