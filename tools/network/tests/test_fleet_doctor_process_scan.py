"""auto-kqpfw: a process scan that cannot look must not report an absence.

The node image carries no `ps` binary, so shelling it returned nothing on
every containerized node — which is every node — and a failed scan was
indistinguishable from "no connectors are running". On home that inverted the
verdict while five connectors were live (host-0906-222509, 2026-09-08).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tools.network import fleet_doctor
from tools.network.fleet_doctor import ProcessScanUnavailable, _running_connectors


def _fake_proc(tmp_path: Path, procs: dict[int, list[str]]) -> Path:
    root = tmp_path / "proc"
    root.mkdir()
    for pid, argv in procs.items():
        entry = root / str(pid)
        entry.mkdir()
        (entry / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
    (root / "not-a-pid").mkdir()
    return root


def test_finds_connectors_without_a_ps_binary(tmp_path, monkeypatch):
    root = _fake_proc(tmp_path, {
        11: ["python3", "-m", "tools.dashboard.link_serving",
             "--org", "org-a", "--graph-org", "anchore"],
        12: ["python3", "-m", "tools.dashboard.link_serving", "--org", "org-b"],
        13: ["python3", "-m", "something.else", "--org", "org-c"],
        14: ["bash"],
    })
    monkeypatch.setattr(fleet_doctor, "Path", lambda p="/proc": root if p == "/proc" else Path(p))
    found = _running_connectors()
    assert set(found) == {"org-a", "org-b"}
    assert found["org-a"] == {"pid": 11, "graph_org": "anchore"}
    assert found["org-b"]["graph_org"] is None


def test_an_unreadable_process_table_raises_rather_than_reporting_none(tmp_path, monkeypatch):
    empty = tmp_path / "proc"
    empty.mkdir()
    monkeypatch.setattr(fleet_doctor, "Path", lambda p="/proc": empty if p == "/proc" else Path(p))
    with pytest.raises(ProcessScanUnavailable):
        _running_connectors()

    missing = tmp_path / "absent"
    monkeypatch.setattr(fleet_doctor, "Path", lambda p="/proc": missing if p == "/proc" else Path(p))
    with pytest.raises(ProcessScanUnavailable):
        _running_connectors()


def test_check_connectors_marks_the_report_when_it_could_not_look(monkeypatch, capsys):
    def blind():
        raise ProcessScanUnavailable("no readable process entries under /proc")

    monkeypatch.setattr(fleet_doctor, "_running_connectors", blind)
    report: dict = {}
    fleet_doctor.check_connectors(report)
    out = capsys.readouterr().out
    assert report["running_connectors"] == {}
    assert report["process_scan_unavailable"]
    assert "UNKNOWN" in out and "not evidence of absence" in out
    assert "NONE running" not in out


def test_a_successful_scan_clears_the_unavailable_marker(monkeypatch, capsys):
    monkeypatch.setattr(
        fleet_doctor, "_running_connectors",
        lambda: {"org-a": {"pid": 5, "graph_org": None}},
    )
    report = {"process_scan_unavailable": "stale"}
    fleet_doctor.check_connectors(report)
    assert "process_scan_unavailable" not in report
    assert report["running_connectors"]["org-a"]["pid"] == 5
