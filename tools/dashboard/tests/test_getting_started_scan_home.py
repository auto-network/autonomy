"""The Getting Started sign-in scan reports where it looked.

The scan runs in the dashboard container against /host-home; the Welcome
page tells the operator the host path instead (AUTONOMY_HOST_HOME), so a
failed scan names a folder the operator can check.
"""

from tools.dashboard.plugins.getting_started.entrypoints import api
from tools.graph import credential_import


def _empty_scan(monkeypatch):
    monkeypatch.setattr(credential_import, "operator_home", lambda: "/host-home")
    monkeypatch.setattr(
        credential_import, "run_import",
        lambda home, dry_run=False: credential_import.ImportReport(),
    )


def test_scan_reports_the_host_home(monkeypatch):
    _empty_scan(monkeypatch)
    monkeypatch.setenv("AUTONOMY_HOST_HOME", "/home/tester")
    out = api._scan(False)
    assert out["home"] == "/home/tester"
    assert out["usable"] == []


def test_scan_home_is_none_without_the_mount(monkeypatch):
    _empty_scan(monkeypatch)
    monkeypatch.delenv("AUTONOMY_HOST_HOME", raising=False)
    assert api._scan(True)["home"] is None
