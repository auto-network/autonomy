from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

import pytest

from .test_stage1 import _cli, _manifests, _wait_terminal, cli_env, project  # noqa: F401
from tools.agent_test.timing import aggregate_test_durations as aggregate_durations


def test_report_phases_are_aggregated_into_one_duration_per_node():
    observations = aggregate_durations(
        [
            {"kind": "report", "nodeid": "test_a.py::test_ok", "phase": "setup", "outcome": "passed", "duration": 0.1},
            {"kind": "report", "nodeid": "test_a.py::test_ok", "phase": "call", "outcome": "passed", "duration": 0.2},
            {"kind": "report", "nodeid": "test_a.py::test_ok", "phase": "teardown", "outcome": "passed", "duration": 0.3},
            {"kind": "report", "nodeid": "test_a.py::test_error", "phase": "setup", "outcome": "failed", "duration": 0.4},
            {"kind": "report", "nodeid": "test_a.py::test_skip", "phase": "setup", "outcome": "skipped", "duration": 0.5},
        ]
    )

    assert [(item["nodeid"], item["outcome"]) for item in observations] == [
        ("test_a.py::test_error", "error"),
        ("test_a.py::test_ok", "passed"),
        ("test_a.py::test_skip", "skipped"),
    ]
    assert [item["duration_seconds"] for item in observations] == pytest.approx([0.4, 0.6, 0.5])


def test_run_estimates_before_launch_records_timings_and_history_query_is_bounded(
    project: Path, cli_env: dict[str, str],
):
    requests: list[dict] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"path": self.path, **body})
            if self.path == "/api/agent-test/durations" and body["action"] == "estimate":
                response = {
                    "ok": True,
                    "estimated_seconds": 1.25,
                    "serial_seconds": 1.25,
                    "parallelism": body.get("parallelism", 1),
                    "sampled_tests": 1,
                    "sample_count": 10,
                    "unknown_selectors": [],
                }
            elif self.path == "/api/agent-test/durations" and body["action"] == "history":
                response = {
                    "ok": True,
                    "matched_tests": 1,
                    "omitted_tests": 0,
                    "history_limit": 10,
                    "tests": [{
                        "nodeid": "test_timed.py::test_timed",
                        "median_seconds": 1.25,
                        "observations": [
                            {
                                "run_id": f"run-{index}",
                                "duration_seconds": 1.0 + index / 10,
                                "outcome": "passed",
                                "recorded_at": float(index),
                            }
                            for index in range(10)
                        ],
                    }],
                }
            elif self.path == "/api/agent-test/durations" and body["action"] == "record":
                response = {
                    "ok": True,
                    "appended": len(body["observations"]),
                    "duplicates": 0,
                    "pruned": 0,
                    "history_limit": 10,
                }
            else:
                response = {"ok": True}
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format, *_args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = dict(cli_env)
    env["AUTONOMY_SESSION"] = "auto-duration-test"
    env["AGENT_TEST_DASHBOARD"] = f"http://127.0.0.1:{server.server_port}"
    (project / "test_timed.py").write_text("def test_timed():\n    pass\n")
    try:
        started = _cli(project, env, "run", "test_timed.py::test_timed")
        assert started.returncode == 0, started.stderr
        assert started.stdout.index("Estimated test time") < started.stdout.index("Started ")
        assert "~1.2s" in started.stdout
        final = _wait_terminal(env)
        assert final["duration_history"] == {
            "state": "recorded",
            "observations": 1,
            "appended": 1,
            "history_limit": 10,
        }

        before = len(_manifests(env))
        timings = _cli(
            project,
            env,
            "timings",
            "test_timed.py::test_timed",
            "--samples",
            "3",
        )
        assert timings.returncode == 0, timings.stderr
        assert "latest 10 retained per test" in timings.stdout
        assert "10 sample(s)" in timings.stdout
        assert timings.stdout.count(" passed") == 3
        assert len(_manifests(env)) == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
