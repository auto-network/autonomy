"""xdist worker helpers — per-worker ports and browser isolation.

Tests that bind to fixed ports or talk to agent-browser need per-worker
namespacing so parallel workers don't collide. Most tests should use the
``worker_port_base`` fixture in the root conftest; tests that need the
port at module-import time (e.g. declared as a module-level constant)
can call ``worker_test_port(base)`` directly.
"""
import os
import socket


def bind_free_port() -> tuple[socket.socket, int]:
    """Allocate a listening socket on a kernel-assigned free port.

    Returns ``(socket, port)``. Hand ``socket.fileno()`` to uvicorn via
    ``--fd`` (with ``pass_fds``) and close the socket after ``Popen`` — the
    child inherits its own copy of the descriptor.

    This REPLACES :func:`worker_test_port` for server binds. A worker-index
    derived port has no session dimension, so every session on host networking
    computes the same port for a base and a readiness probe can silently answer
    from another session's server (see the port-collision report). A
    kernel-assigned port is owned by this process the instant we bind it and
    cannot collide with anyone.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(128)
    s.set_inheritable(True)
    return s, s.getsockname()[1]


def spawn_mock_uvicorn(*, env, nonce, cwd=None, ready_timeout=45):
    """Boot a DASHBOARD_MOCK uvicorn on a kernel-assigned free port and verify
    identity before returning ``(proc, port)``.

    Binds 127.0.0.1:0 here and hands the descriptor to uvicorn via ``--fd`` (so
    the port cannot collide with another session — see :func:`bind_free_port`),
    then polls ``/api/_mock/harness-nonce`` and refuses to return until the
    served nonce equals *nonce*. Callers must have written the fixture with
    ``{"__harness_nonce__": nonce, ...}`` and set ``env["DASHBOARD_MOCK"]``.
    Raises RuntimeError on timeout or nonce mismatch (kills the process first).
    """
    import json as _json
    import subprocess
    import sys
    import time
    import urllib.request

    sock, port = bind_free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "tools.dashboard.server:app",
         "--fd", str(sock.fileno()), "--log-level", "warning"],
        env=env, pass_fds=(sock.fileno(),), cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    sock.close()
    deadline = time.time() + ready_timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/_mock/harness-nonce", timeout=1,
            ) as r:
                if _json.loads(r.read().decode()).get("nonce") == nonce:
                    return proc, port
        except Exception:
            pass
        time.sleep(0.2)
    proc.kill()
    raise RuntimeError(
        f"mock uvicorn on port {port} failed to start or did not echo this "
        "harness's nonce (port collision / wrong server / boot failure)"
    )


def worker_index() -> int:
    """Return xdist worker index (0 for main or missing xdist, 0..N otherwise)."""
    worker = os.environ.get("PYTEST_XDIST_WORKER", "gw0")
    if worker == "master":
        return 0
    return int(worker.replace("gw", ""))


def worker_test_port(base: int, stride: int = 100) -> int:
    """Offset a port base by this worker's index so parallel binds don't clash.

    Default stride of 100 is comfortably wider than the bases we use today
    (8082, 8083, 8086, 8091, 8092, 8121), so two different base values on
    different workers never land on the same port.
    """
    return base + worker_index() * stride
