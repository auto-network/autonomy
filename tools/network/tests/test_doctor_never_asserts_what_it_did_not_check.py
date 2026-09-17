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


def test_the_readiness_verdict_never_rests_on_a_probe_that_failed():
    """A connector-status query that RAISED must not print "eligible and
    running".

    The branch fetched connector-status, read one key, and swallowed every
    exception with a bare `pass` -- after which the next statement printed
    the green line unconditionally. A refused socket, a missing .ctl or a
    timeout, each of which a connector with no live tunnel is likely to
    produce, therefore rendered identically to a connector that answered.
    This file's whole premise is that could-not-look and nothing-is-there
    stay distinguishable; _running_connectors raises ProcessScanUnavailable
    for exactly that reason 200 lines below, and this branch was the one
    producing the operator-facing verdict.

    Pinned at the source level on purpose: reaching the branch behaviourally
    means stubbing the tunnel-serving gate, org_ops, serve_cert_state, the
    running-connector map AND the supervisor, and a test that elaborate
    tends to be deleted rather than repaired. The property here is narrow --
    the handler must not fall through to the success line -- and reads
    directly off the source, the same technique this file already uses for
    the certificate message.
    """
    import inspect

    from tools.network import fleet_doctor

    source = inspect.getsource(fleet_doctor.check_serving_readiness)

    # The swallow that caused it, in the exact shape it had.
    assert "pass  # connector-status is itself best-effort here" not in source

    # Could-not-look must say so, and must not be reported as a verdict.
    assert "UNREADABLE" in source
    assert "UNKNOWN, not confirmed" in source

    # And the reply's own liveness field must actually be read: everything
    # else in this section tests whether the PROCESS exists, so without this
    # a connector looping on a refused hello is indistinguishable from one
    # holding a live tunnel. Home spent 00:31-01:50 on 2026-09-10 in that
    # state with three org connectors replaced every 60s.
    assert 'status.get("serving")' in source


def test_refuses_to_run_inside_a_session_container(monkeypatch, capsys):
    """A session container holds no fleet data; a report from it reads as a
    healthy-looking empty node (2026-09-17). Only the host terminal runs it."""
    monkeypatch.setenv("AUTONOMY_SESSION", "auto-0917-094700")
    monkeypatch.setattr("sys.argv", ["fleet_doctor"])
    assert fleet_doctor.main() == 2
    assert "host terminal" in capsys.readouterr().err


def test_host_terminal_session_is_not_refused(monkeypatch):
    monkeypatch.setenv("AUTONOMY_SESSION", "host-0916-103518")
    monkeypatch.setattr("sys.argv", ["fleet_doctor", "--ssh", "x"])
    monkeypatch.setattr(fleet_doctor, "_run_remote", lambda *a, **k: 0)
    assert fleet_doctor.main() == 0
