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
