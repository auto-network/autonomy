"""xdist worker helpers — per-worker ports and browser isolation.

Tests that bind to fixed ports or talk to agent-browser need per-worker
namespacing so parallel workers don't collide. Most tests should use the
``worker_port_base`` fixture in the root conftest; tests that need the
port at module-import time (e.g. declared as a module-level constant)
can call ``worker_test_port(base)`` directly.
"""
import os


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
