from tools.agent_test.cli import _progress_line
from tools.agent_test.worker import _execution_state, _hang_threshold


def test_execution_state_identifies_last_unfinished_node():
    events = [
        {"kind": "collection", "nodes": ["test_ok", "test_stuck"]},
        {"kind": "report", "nodeid": "test_ok", "phase": "setup", "outcome": "passed"},
        {"kind": "report", "nodeid": "test_ok", "phase": "call", "outcome": "passed"},
        {"kind": "report", "nodeid": "test_stuck", "phase": "setup", "outcome": "passed"},
    ]
    state = _execution_state(events)
    assert state["total"] == 2
    assert state["unfinished"] == ["test_stuck"]
    assert state["current_nodeid"] == "test_stuck"


def test_hang_threshold_uses_history_and_has_unknown_fallback():
    threshold, source = _hang_threshold(
        "test_stuck", {"test_stuck": {"median_seconds": 4, "maximum_seconds": 10}},
    )
    assert threshold == 120
    assert source == "retained node history"
    unknown, source = _hang_threshold("test_unknown", {})
    assert unknown == 600
    assert source == "no retained history for the active node"


def test_progress_line_reports_suspect_and_known_remaining_work():
    line = _progress_line({
        "status": "running",
        "progress": {
            "status": "suspect",
            "completed": 7,
            "total": 8,
            "percent": 87,
            "known_remaining_seconds": 12,
            "remaining_nodes": 1,
            "unknown_remaining_nodes": 0,
            "hang": {
                "nodeid": "test_stuck",
                "stalled_seconds": 121,
                "threshold_seconds": 120,
            },
        },
    })
    assert "Progress: 87% (7/8 test nodes)." in line
    assert "Suspect hung: test_stuck" in line
    assert "Known remaining work: ~12.0s" in line
