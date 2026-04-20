"""Project-root pytest configuration — worker-aware fixtures for pytest-xdist.

Tests under both ``tools/dashboard/tests`` and ``agents/tests`` load this
conftest. Fixtures defined here provide per-worker port bases and per-worker
browser sessions so ``pytest -n auto`` workers don't collide.
"""
import os

import pytest

from tools.dashboard.tests._xdist import worker_index


@pytest.fixture(scope="session")
def worker_port_base() -> int:
    """Port base per xdist worker. Workers get 8100, 8200, 8300, ..."""
    return 8100 + worker_index() * 100


@pytest.fixture(scope="session", autouse=True)
def _isolate_browser_session():
    """Each xdist worker gets its own agent-browser session (independent Chromium)."""
    os.environ.setdefault("AGENT_BROWSER_SESSION", f"pytest-{worker_index()}")
    yield
