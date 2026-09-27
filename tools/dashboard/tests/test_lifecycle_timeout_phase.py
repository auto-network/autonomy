"""A lifecycle TimeoutError with no message must still record a failure.

Windows test node, 2026-09-27: the host terminal's registration future timed
out with a bare ``TimeoutError()``; ``str(exc).split()[0]`` raised IndexError
inside the handler, the failure was never recorded, and /terminal showed
"connecting..." for good. Every handler falls back to the phase in progress.
"""

import re
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "server.py"


def test_no_handler_indexes_an_empty_timeout_message():
    src = SERVER.read_text()
    bare = re.findall(r"str\(exc\)\.split\(\)\[0\]", src)
    assert bare == [], "use (str(exc).split() or [phase])[0]"


def test_registration_waits_out_an_event_loop_stall():
    from tools.dashboard import server
    assert server._LIFECYCLE_REGISTER_TIMEOUT_S >= 20
