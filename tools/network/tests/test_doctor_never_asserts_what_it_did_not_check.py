"""A diagnostic must never report an absence it did not verify.

Three live incidents on 2026-09-08, all the same shape: a check that could
not look reported that nothing was there, and each one cost hours.
  - the process scan shelled a binary the node image lacks -> "no connectors"
    while five were running;
  - a pull that carried nothing reported "success";
  - the error scan read a file that does not exist in a container -> an empty
    section that read as "no errors" when it had scanned zero lines.
"""
from __future__ import annotations

from pathlib import Path

from tools.network import fleet_doctor


def test_an_unreadable_log_source_says_it_did_not_scan(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fleet_doctor, "DATA_ROOT", tmp_path, raising=False)
    report: dict = {}
    fleet_doctor.check_recent_errors(report)
    out = capsys.readouterr().out
    # It must not imply cleanliness.
    assert "NOT SCANNED" in out
    assert "proves nothing" in out
    assert report.get("unscanned_log_sources")


def test_the_certificate_message_does_not_claim_the_vault_is_locked():
    """It tests a per-process key, not the operator's vault. Saying 'the
    vault is locked' sent a live diagnosis down the wrong path."""
    import inspect

    from tools.dashboard import service_certificate, service_certificate_manager

    for module in (service_certificate, service_certificate_manager):
        source = inspect.getsource(module)
        assert "certificate vault is locked" not in source, module.__name__
    issue_src = inspect.getsource(service_certificate.issue)
    assert "THIS PROCESS" in issue_src
