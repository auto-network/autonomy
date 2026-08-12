"""Navigation, clicked in a real browser, on BOTH surfaces.

Every other browser test here builds its own srcdoc page and never clicks a
pillar, so two navigation faults shipped and were found by the operator on his
phone instead:

  * the dashboard served <base href="about:srcdoc"> at a real URL, so every
    relative link in a coordinator's content resolved against about:srcdoc and
    the browser blocked it;
  * the top bar asked a MessagePort that only exists inside the relay frame, so
    on the dashboard every navigation control silently did nothing.

Neither is visible to a unit test. Both are one click away from obvious.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "agent-browser"], capture_output=True).returncode != 0,
    reason="agent-browser not on PATH",
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


SERVER = """
import sys, os, json
sys.path.insert(0, {repo!r})
os.environ["MISSION_CONTROL_DB"] = {db!r}
from tools.dashboard.dao import mission_control_db as db
from pathlib import Path
db.DB_PATH = Path({db!r})
db.init_db(db.DB_PATH)
m = db.create_mission("Nav Test")
mid = m["mission_id"]
db.push_site_revision(mid, "<body><h1>OVERVIEW SCREEN</h1></body>", "r1")
pid = db.create_pillar(mid, "Second Pillar", "sess", "#4ade80")["pillar_id"]
db.set_pillar_last_done(pid, "Did a thing that finished.")
db.push_pillar_site_revision(pid, "<body><h1>PILLAR SCREEN</h1></body>", "r1")
Path({ids!r}).write_text(json.dumps({{"mission_id": mid, "pillar_id": pid}}))

from starlette.applications import Starlette
from tools.dashboard.plugins.mission_control.entrypoints import api as mc
import uvicorn
uvicorn.run(Starlette(routes=mc.routes), host="127.0.0.1", port={port}, log_level="error")
"""


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("nav")
    db, ids = tmp / "mc.db", tmp / "ids.json"
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-c", SERVER.format(repo=str(REPO), db=str(db),
                                             ids=str(ids), port=port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        for _ in range(100):
            if ids.exists():
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
                    break
                except OSError:
                    pass
            time.sleep(0.2)
        else:
            proc.kill()
            pytest.fail(f"server never came up: {proc.stderr.read()[-1200:]!r}")
        yield {"port": port, **json.loads(ids.read_text())}
    finally:
        proc.kill()


def _run(session: str, *args: str, timeout: int = 90) -> str:
    r = subprocess.run(["agent-browser", "--session", session, *args],
                       capture_output=True, text=True, timeout=timeout)
    return r.stdout


def _ref(snapshot: str, label: str) -> str | None:
    """The ref of the first interactive element whose label contains *label*.

    Driven through the accessibility tree on purpose: the chrome lives in a
    CLOSED shadow root, so page script cannot reach it -- el.shadowRoot is
    null, which is exactly what closed means. The tree pierces it, and a
    click through it is the same click a finger makes.
    """
    for line in snapshot.splitlines():
        if label in line and "[ref=" in line:
            return line.split("[ref=")[1].split("]")[0]
    return None


def test_clicking_a_pillar_navigates_on_the_dashboard(server):
    """The surface the operator actually works in, driven by real clicks."""
    url = f"http://127.0.0.1:{server['port']}/missions/{server['mission_id']}"
    session = "mc-nav-dash"
    _run(session, "open", url)
    _run(session, "set", "viewport", "1280", "860")
    time.sleep(2)

    head = json.loads(_run(session, "eval",
        "JSON.stringify({base: !!document.querySelector('base'),"
        " host: !!document.querySelector('[data-mission-chrome]'),"
        " path: location.pathname})").strip())
    if isinstance(head, str):
        head = json.loads(head)
    assert head["host"], "the chrome never mounted on the dashboard"
    assert not head["base"], (
        "a <base> is served at a real URL -- every relative link in the "
        "author's content resolves against it and the browser blocks it"
    )

    bar = _ref(_run(session, "snapshot", "-i"), "\u25be")
    assert bar, "no pillar dropdown in the bar"
    _run(session, "click", "@" + bar)
    time.sleep(1)

    row = _ref(_run(session, "snapshot", "-i"), "Second Pillar")
    assert row, "the pillar chooser did not list the pillar"
    _run(session, "click", "@" + row)
    time.sleep(2)

    after = json.loads(_run(session, "eval",
        "JSON.stringify({path: location.pathname,"
        " body: document.body.innerText.slice(0,60)})").strip())
    if isinstance(after, str):
        after = json.loads(after)
    _run(session, "close", timeout=30)

    assert after["path"] != f"/missions/{server['mission_id']}", (
        f"clicking a pillar did not navigate: still at {after['path']}"
    )
    assert server["pillar_id"] in after["path"], after["path"]
    assert "PILLAR SCREEN" in after["body"], after["body"]


RELAY_HOST = """<!doctype html><meta charset="utf-8"><body style="margin:0">
<iframe id="f" style="width:100%;height:100vh;border:0" sandbox="allow-scripts"></iframe>
<script>
// EXACTLY autonet.js's half of the contract: on `ready` the HOST creates the
// channel and hands port2 to the frame. It never takes a port the frame
// offers -- a harness that did tested a protocol nothing implements, which is
// how a dead channel shipped once.
window.__reads = []; window.__readies = 0;
var SCREENS = __SCREENS__;
var f = document.getElementById("f");
addEventListener("message", function (e) {
  if (!e.data || e.data.v !== 1) return;
  if (e.data.op !== "ready") return;
  window.__readies++;
  var c = new MessageChannel();
  c.port1.onmessage = function (m) {
    var r = m.data;
    if (!r || r.type !== "request") return;
    window.__reads.push((r.body && r.body.kind) || r.op);
    var doc = r.body && r.body.pillar_id ? SCREENS[r.body.pillar_id] : SCREENS.__mission__;
    c.port1.postMessage(doc
      ? {v:1, type:"response", id:r.id, ok:true, body:{document: doc}}
      : {v:1, type:"response", id:r.id, ok:false, error:"no screen"});
  };
  f.contentWindow.postMessage({v:1, op:"port"}, "*", [c.port2]);
});
f.srcdoc = SCREENS.__mission__;
</script></body>"""


def test_clicking_a_pillar_swaps_the_document_over_the_relay(server, tmp_path):
    """The framed surface: no URL to go to, so the screen arrives over the
    channel and replaces the document in place.

    Asserted on the HOST side, because the frame is an opaque origin and
    nothing outside can read its DOM. A second `ready` is the proof the swap
    happened -- the replacement document runs the bootstrap again.
    """
    import html as _html
    os.environ["MISSION_CONTROL_DB"] = str(Path(tempfile.mkdtemp()) / "mc.db")
    sys.path.insert(0, str(REPO))
    from tools.dashboard.dao import mission_control_db as db
    from tools.dashboard.plugins.mission_control import compose

    db.DB_PATH = Path(os.environ["MISSION_CONTROL_DB"])
    db.init_db(db.DB_PATH)
    mid = db.create_mission("Relay Nav")["mission_id"]
    db.push_site_revision(mid, "<body><h1>OVERVIEW</h1></body>", "r1")
    pid = db.create_pillar(mid, "Second Pillar", "s", "#4ade80")["pillar_id"]
    db.set_pillar_last_done(pid, "Finished a thing.")
    db.push_pillar_site_revision(pid, "<body><h1>PILLAR SCREEN</h1></body>", "r1")

    screens = {
        "__mission__": compose.compose_screen(mid, framed=True).decode(),
        pid: compose.compose_screen(mid, pid, framed=True).decode(),
    }
    page = tmp_path / "relay-host.html"
    page.write_text(RELAY_HOST.replace(
        "__SCREENS__", json.dumps(screens).replace("</", "<\\/")))

    session = "mc-nav-relay"
    _run(session, "open", page.as_uri())
    _run(session, "set", "viewport", "1280", "860")
    time.sleep(2)

    bar = _ref(_run(session, "snapshot", "-i"), "▾")
    assert bar, "no pillar dropdown inside the frame"
    _run(session, "click", "@" + bar)
    time.sleep(1)
    row = _ref(_run(session, "snapshot", "-i"), "Second Pillar")
    assert row, "the pillar chooser did not list the pillar"
    _run(session, "click", "@" + row)
    time.sleep(2)

    state = json.loads(_run(session, "eval",
        "JSON.stringify({reads: window.__reads, readies: window.__readies})").strip())
    if isinstance(state, str):
        state = json.loads(state)
    heading = _run(session, "snapshot", "-i")
    _run(session, "close", timeout=30)

    assert "pillar_site" in state["reads"], (
        f"clicking a pillar issued no channel read: {state['reads']}"
    )
    assert state["readies"] >= 2, (
        "the document never announced ready a second time, so the screen was "
        f"fetched but never swapped in (readies={state['readies']})"
    )
    assert "PILLAR SCREEN" in heading, heading[:300]
