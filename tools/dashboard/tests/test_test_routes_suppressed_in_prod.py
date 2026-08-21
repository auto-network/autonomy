"""The /api/test/* dev-prototype routes exist only on a mock/dev dashboard.

They are in-memory debug/toast/version scratch surfaces serving
test_fixtures/input-prototype.html — no database, no production consumer.
Registering them in production would expose ungated mutating scratch state, so
they are gated behind DASHBOARD_MOCK (which the mock server sets for its
temporary-fixture instances) and must not exist in a production route table.
Operator decision 2026-08-21 (auto-1wwpf.6).
"""

from __future__ import annotations

import importlib

import pytest

TEST_PATHS = {"/api/test/debug", "/api/test/version", "/api/test/toast"}


def _registered_api_test_paths(monkeypatch, mock_value):
    if mock_value is None:
        monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    else:
        monkeypatch.setenv("DASHBOARD_MOCK", mock_value)
    server = importlib.reload(importlib.import_module("tools.dashboard.server"))
    try:
        return {
            r.path for r in server.routes
            if getattr(r, "path", "").startswith("/api/test/")
        }
    finally:
        monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
        importlib.reload(server)


def test_absent_in_production(monkeypatch):
    assert _registered_api_test_paths(monkeypatch, None) == set()


def test_present_under_mock(monkeypatch):
    assert _registered_api_test_paths(monkeypatch, "1") == TEST_PATHS
