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
from types import SimpleNamespace

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


# ── The doctor must not contradict its own connector list (auto-clune.7) ──


def _serving_readiness_output(monkeypatch, capsys) -> str:
    """Run just the serving-readiness section with its I/O stubbed out."""
    from tools.network import fleet_doctor
    from tools.dashboard import link_serving_supervisor as sup
    from tools.graph import org_ops

    monkeypatch.setattr(org_ops, "list_orgs", lambda: [])
    monkeypatch.setattr(
        sup, "serve_cert_state", lambda org, now=None: {"status": "ok"})
    monkeypatch.setattr(sup, "_has_live_grant", lambda org, now: True)
    capsys.readouterr()
    fleet_doctor.check_serving_readiness({})
    return capsys.readouterr().out


def test_a_non_designated_machine_that_may_serve_is_not_called_by_design(
    monkeypatch, capsys,
):
    """THE ONE THAT MATTERS, witnessed on sjc-2 minutes after the rollout: the
    doctor listed THREE running connectors and, two lines below, said "not
    serving by design: this machine is not the selected tunnel server, so it
    correctly runs no connector".

    The gate read `state().allowed`, which was the serving rule when it was
    written and is not any more. A check that outlives its premise becomes the
    same defect aimed the other way — it was added to stop the doctor
    fabricating a cause, and it began fabricating the opposite one.

    So: not designated, but permitted, must NOT produce the by-design line.
    """
    from tools.network import fleet_doctor, fleet_tunnel_server

    monkeypatch.setattr(
        fleet_tunnel_server, "state",
        lambda: SimpleNamespace(allowed=False, reason="not-designated",
                                managed=True, selected_machine_id="other"))
    monkeypatch.setattr(
        fleet_tunnel_server, "tunnel_serving_permitted",
        lambda: (True, "designation-not-required"))

    out = _serving_readiness_output(monkeypatch, capsys)

    assert "not serving by design" not in out, (
        "a machine that MAY serve was reported as correctly running no "
        "connector — the exact contradiction observed on sjc-2")


def test_a_machine_refused_for_safety_is_still_called_by_design(
    monkeypatch, capsys,
):
    """NEGATIVE CONTROL. The gate's original purpose survives: a machine that
    genuinely may not serve must still be reported as designed rather than as
    a fault, or the doctor goes back to fabricating a cause for the system
    working correctly."""
    from tools.network import fleet_doctor, fleet_tunnel_server

    monkeypatch.setattr(
        fleet_tunnel_server, "state",
        lambda: SimpleNamespace(allowed=False, reason="machine-not-rostered",
                                managed=True, selected_machine_id=None))
    monkeypatch.setattr(
        fleet_tunnel_server, "tunnel_serving_permitted",
        lambda: (False, "machine-not-rostered"))

    out = _serving_readiness_output(monkeypatch, capsys)

    assert "not serving by design" in out
