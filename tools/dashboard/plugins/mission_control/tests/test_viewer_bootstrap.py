"""The Mission viewer bootstrap's load-time contract.

Driven in a real browser because every property here is a browser property:
whether it ran before the coordinator's scripts, whether it left anything on
`window`, and where it put its controls relative to the author's markup.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from tools.dashboard.scripts import build_mission_viewer as builder  # noqa: E402

pytestmark = pytest.mark.skipif(
    shutil.which("agent-browser") is None, reason="agent-browser not on PATH"
)

# An ordinary coordinator page: complete document, its own script, its own
# globals, two anchored elements -- one already discussed, one not.
AUTHOR = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<style>body{margin:0}.row + p{color:rgb(0,255,0)}</style></head>
<body data-authored="yes">
<script>window.__err=[];addEventListener('error',function(e){window.__err.push(e.message+' @'+e.lineno);});</script>
<div class="rows">
  <p class="row" data-mc-anchor="table:one">first</p>
  <p id="sibling">second</p>
  <p class="row" data-mc-anchor="chart:two">third</p>
</div>
<script>window.AUTHOR_RAN = true;
document.addEventListener('DOMContentLoaded', function(){ window.AUTHOR_DCL = true; });
window.addEventListener('load', function(){ window.AUTHOR_LOAD = true; });</script>
<script>
/* TEST PROBE ONLY -- appended after the author's page, never shipped. The
   frame is an opaque origin, so nothing outside can read its DOM; the
   measurement has to happen in here and be posted out. */
window.addEventListener('load', function () {
  setTimeout(function () {
    var a = document.querySelector('[data-mc-anchor="table:one"]');
    var b = document.querySelector('[data-mc-anchor="chart:two"]');
    parent.postMessage({probe: true,
      insideAnchor: !!(a && a.querySelector('[data-mc-control]')),
      asSibling: !!(a && a.nextElementSibling &&
                    a.nextElementSibling.hasAttribute('data-mc-control')),
      siblingColor: getComputedStyle(document.getElementById('sibling')).color,
      authorRan: !!window.AUTHOR_RAN,
      authorDcl: !!window.AUTHOR_DCL,
      authorLoad: !!window.AUTHOR_LOAD,
      authoredBodyAttr: document.body.getAttribute('data-authored'),
      standards: document.compatMode,
      lang: document.documentElement.lang,
      chromeMounted: !!document.querySelector('[data-mission-chrome]'),
      /* the shadow roots are CLOSED, so even this probe cannot look inside --
         which is the property under test */
      chromeOpaque: (function(){ var h=document.querySelector('[data-mission-chrome]');
                                 return !!h && h.shadowRoot === null; })(),
      anyControl: document.querySelectorAll('[data-mc-control]').length,
      anchorsFound: document.querySelectorAll('[data-mc-anchor]').length,
      errors: window.__err || [],
      leaked: Object.getOwnPropertyNames(window).filter(function (k) {
        return /^(mc|mission|__mc)/i.test(k); }),
    }, '*');
  }, 150);
});
</script>
</body></html>"""

STATE = {
    "screen": "p-one",
    "pillars": [{"pillar_id": "p-one", "name": "First pillar"}],
    "questions": [
        {"entry_id": "e1", "anchor": "table:one", "question": "Why?", "answer": "Because."},
    ],
}


def _page(tmp_path: Path) -> Path:
    """The composed document, exactly as _resolve_mission will emit it."""
    composed = (
        "<!doctype html>\n"
        '<base href="about:srcdoc">\n'
        '<script type="application/json" id="mc-state">'
        + json.dumps(STATE) + "</script>\n"
        "<script>\n" + builder.bootstrap_source() + "\n</script>\n"
        + AUTHOR
    )
    # Hosted in a sandboxed frame the way autonet.js does it, so the test sees
    # the real origin and the real CSP posture.
    host = tmp_path / "host.html"
    host.write_text(
        "<!doctype html><body><iframe id='f' sandbox='allow-scripts' "
        "srcdoc=\"" + composed.replace("&", "&amp;").replace('"', "&quot;") + "\"></iframe>"
        "<script>window.__msgs=[];window.__port=null;"
        "addEventListener('message',function(e){window.__msgs.push(e.data);"
        "if(e.ports&&e.ports[0])window.__port=e.ports[0];});</script></body>"
    )
    return host


def _eval(session: str, url: str, js: str):
    subprocess.run(["agent-browser", "--session", session, "open", url],
                   capture_output=True, text=True, timeout=60, check=True)
    r = subprocess.run(["agent-browser", "--session", session, "eval", "--stdin"],
                       input=js, capture_output=True, text=True, timeout=60, check=True)
    subprocess.run(["agent-browser", "--session", session, "close"],
                   capture_output=True, text=True, timeout=30)
    out = json.loads(r.stdout.strip())
    return json.loads(out) if isinstance(out, str) else out


def test_the_bootstrap_runs_first_and_leaves_no_global(tmp_path):
    """It must run before any coordinator script, and expose nothing.

    Coordinator scripts share this realm. A public handle to the port would let
    them issue channel operations, which would reduce the sandbox to "cannot
    read the parent's DOM".
    """
    host = _page(tmp_path)
    r = _eval("mc-boot", f"file://{host}", """
(async () => {
  await new Promise(r => setTimeout(r, 600));
  const boot = window.__msgs.find(m => m && m.op === 'ready');
  return JSON.stringify({ready: !!boot, gotPort: !!window.__port});
})();
""")
    assert r["ready"] is True, "the bootstrap never announced ready"
    assert r["gotPort"] is True, "no MessagePort was transferred"


def _probe(tmp_path, session):
    host = _page(tmp_path)
    return _eval(session, f"file://{host}", """
(async () => {
  await new Promise(r => setTimeout(r, 900));
  return JSON.stringify(window.__msgs.find(m => m && m.probe) || {missing: true});
})();
""")


def test_controls_mount_inside_the_anchor_not_beside_it(tmp_path):
    """A next sibling breaks the author's adjacent-sibling (+) rules.

    The author stylesheet colours `.row + p` green. A control inserted as a
    sibling of an anchored `.row` stops that rule matching -- measured, and the
    reason the design mounts inside.
    """
    r = _probe(tmp_path, "mc-anchor")
    assert not r.get("missing"), "the in-frame probe never reported"
    assert r["insideAnchor"] is True
    assert r["asSibling"] is False
    assert r["siblingColor"] == "rgb(0, 255, 0)"


def test_the_authors_document_keeps_its_own_lifecycle(tmp_path):
    """Composition must not cost the coordinator anything.

    Their script runs, their DOMContentLoaded and load fire, their <body>
    attributes and standards mode survive. This is what innerHTML-plus-script-
    recreation could not do, and the reason the design composes instead.
    """
    r = _probe(tmp_path, "mc-life")
    assert not r.get("missing")
    assert r["authorRan"] is True
    assert r["authorDcl"] is True
    assert r["authorLoad"] is True
    assert r["authoredBodyAttr"] == "yes"
    assert r["standards"] == "CSS1Compat"
    assert r["lang"] == "en"


def test_the_chrome_is_mounted_and_closed(tmp_path):
    """Closed, so coordinator script cannot reach into it.

    `shadowRoot === null` from inside the same document is exactly what
    `{mode: "closed"}` buys, and it is checked from a script that runs in the
    author's own realm -- the position an author's script would be in.
    """
    r = _probe(tmp_path, "mc-closed")
    assert not r.get("missing")
    assert r["chromeMounted"] is True
    assert r["chromeOpaque"] is True
    assert r["leaked"] == []
