"""mail-send keeps waiting through dashboard errors (agents/capabilities/mailbox/tools/mail-send).

The request lives in the dashboard: if the tool quit on one failed wait, a later
approval would send an email the agent had already reported as failed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOOL = Path(__file__).resolve().parents[3] / "agents" / "capabilities" / "mailbox" / "tools" / "mail-send"


def _run(tmp_path, replies):
    replies = list(replies)
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, code, payload):
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            seen.append("POST")
            self._reply(200, {"id": "central-test"})

        def do_GET(self):
            seen.append("GET")
            code, payload = replies.pop(0)
            self._reply(code, payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        env = {**os.environ, "AUTONOMY_DASHBOARD": f"http://127.0.0.1:{server.server_port}",
               "CROSSTALK_TOKEN": "t"}
        # The first retry sleeps 5 s; shorten it so the test stays fast.
        shutil.copy(TOOL.parent / "_mail_lib.py", tmp_path)
        tool = tmp_path / "mail-send"
        tool.write_text(TOOL.read_text().replace("delay = 5\n", "delay = 0\n"))
        proc = subprocess.run([sys.executable, str(tool), "--to", "a@example.com",
                               "--subject", "Hi"], input="Hello\n", capture_output=True,
                              text=True, env=env, timeout=30)
    finally:
        server.shutdown()
    return proc, seen


def test_a_dashboard_error_while_waiting_does_not_end_the_wait(tmp_path):
    sent = {"approved": True, "execution": {"ok": True, "message_id": "<m@x>"}}
    proc, seen = _run(tmp_path, [(500, {"error": "Internal Server Error"}), (503, {"error": "unavailable"}),
                       (200, {"result": None}), (200, {"result": sent})])
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["message_id"] == "<m@x>"
    assert "still waiting on request central-test" in proc.stderr
    assert seen == ["POST", "GET", "GET", "GET", "GET"]


def test_expiry_is_reported_as_nothing_sent(tmp_path):
    proc, _ = _run(tmp_path, [(200, {"result": {"approved": False, "outcome": "expired"}})])
    assert proc.returncode == 1
    assert "expired; nothing was sent" in proc.stderr


def test_a_client_error_still_fails_fast(tmp_path):
    proc, seen = _run(tmp_path, [(404, {"error": "not found"})])
    assert proc.returncode == 1 and "HTTP 404" in proc.stderr
    assert seen == ["POST", "GET"]
