"""auto-1cs87: the note viewer renders the attachment manifest as clickable
download controls and speaks the frozen semantic-intent bridge.

Two harnesses, both real Chrome via agent-browser (skipped when absent):

* ``_run`` loads the viewer as a top-level document and drives/reads its DOM
  directly. A production viewer runs in a sandbox-opaque iframe whose DOM no
  host (or agent-browser) can introspect cross-origin, so this in-isolation
  harness is the only way to assert the rendered controls, intents, state
  matrix, and input validation.
* ``_run_host`` embeds the real served viewer in an iframe with the
  production sandbox attributes (``allow-scripts``, opaque origin) and proves
  the postMessage bridge across that boundary: content posted from the host
  reaches the child and the child's messages return to the host.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tools.dashboard import link_serving

pytestmark = pytest.mark.skipif(
    shutil.which("agent-browser") is None,
    reason="agent-browser (headless Chrome) not on PATH",
)

_HOST_HTML = """<!doctype html><meta charset="utf-8"><body>
<iframe id="v" src="note-viewer.html" sandbox="allow-scripts"></iframe>
<script>
window.__fromChild = [];
addEventListener('message', (e) => {
  if (e.source === document.getElementById('v').contentWindow) window.__fromChild.push(e.data);
});
window.__post = (msg) => document.getElementById('v').contentWindow.postMessage(msg, '*');
</script></body>
"""


def _eval(session: str, js: str):
    result = subprocess.run(
        ["agent-browser", "--session", session, "eval", "--stdin"],
        input=js, capture_output=True, text=True, timeout=60, check=True,
    )
    parsed = json.loads(result.stdout.strip())
    return json.loads(parsed) if isinstance(parsed, str) else parsed


def _open(session: str, url: str):
    subprocess.run(
        ["agent-browser", "--session", session, "open", url],
        capture_output=True, text=True, timeout=60, check=True,
    )


def _close(session: str):
    subprocess.run(
        ["agent-browser", "--session", session, "close"],
        capture_output=True, text=True, timeout=30,
    )


def _run(tmp_path, session: str, js: str):
    """Isolated: load the served viewer top-level and run one eval scenario."""
    viewer = tmp_path / "note-viewer.html"
    viewer.write_bytes(link_serving._note_viewer_bytes())
    try:
        _open(session, f"file://{viewer}")
        return _eval(session, js)
    finally:
        _close(session)


def _run_host(tmp_path, session: str, js: str):
    """Sandboxed: embed the served viewer in an opaque iframe; run scenario."""
    (tmp_path / "note-viewer.html").write_bytes(link_serving._note_viewer_bytes())
    host = tmp_path / "host.html"
    host.write_text(_HOST_HTML, encoding="utf-8")
    try:
        _open(session, f"file://{host}")
        return _eval(session, js)
    finally:
        _close(session)


# Isolated harness preamble: capture child->parent intents (not our injected
# attachment.state), post content as the parent would, list controls.
_HARNESS = """
window.__intents = [];
window.addEventListener('message', (e) => {
  const d = e.data;
  if (d && typeof d.type === 'string'
      && d.type.startsWith('attachment.') && d.type !== 'attachment.state') {
    window.__intents.push(d);
  }
});
function post(msg) { window.postMessage(msg, '*'); }
function content(attachments) {
  post({v: 1, op: 'content', title: 'T', markdown: '# T\\n\\nbody',
        parts: [], attachments});
}
function controls() { return [...document.querySelectorAll('.attachment')]; }
function only() { return controls()[0]; }
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const ENTRY = (over) => Object.assign({ref:'a1', name:'f.bin',
  mime:'application/octet-stream', raw_sha256:'a'.repeat(64),
  total_size:2097152, oversize:false}, over || {});
"""


def test_renders_controls_click_and_state(tmp_path):
    js = _HARNESS + """
(async () => {
  content([
    ENTRY({ref:'a1', name:'report.pdf', mime:'application/pdf', total_size:2097152}),
    ENTRY({ref:'a2', name:'notes.txt', mime:'text/plain', total_size:500}),
    ENTRY({ref:'a3', name:'huge.bin', total_size:900, oversize:true}),
  ]);
  await wait(50);
  const cs = controls();
  cs[0].click();
  post({v:1, type:'attachment.state', ref:'a1', state:'downloading', received:1048576, total:2097152});
  await wait(30);
  return JSON.stringify({
    hidden: document.getElementById('attachments').hidden,
    count: cs.length,
    names: cs.map(c => c.querySelector('.attachment-name').textContent),
    sizes: cs.map(c => c.querySelector('.attachment-meta').textContent),
    types: cs.map(c => c.querySelector('.attachment-type').textContent),
    oversizeDisabled: cs[2].disabled,
    status0: cs[0].querySelector('.attachment-status').textContent,
    intents: window.__intents,
  });
})();
"""
    r = _run(tmp_path, "cs87-render", js)
    assert r["hidden"] is False
    assert r["count"] == 3
    assert r["names"] == ["report.pdf", "notes.txt", "huge.bin"]
    assert r["sizes"] == ["2.0 MB", "500 B", "900 B"]
    assert r["types"] == ["PDF", "TXT", "BIN"]
    assert r["oversizeDisabled"] is True
    assert r["intents"] == [{"v": 1, "type": "attachment.select", "ref": "a1"}]
    assert r["status0"] == "Downloading 50%"


def test_empty_manifest_renders_no_section(tmp_path):
    js = _HARNESS + """
(async () => {
  content([]);
  await wait(50);
  return JSON.stringify({
    hidden: document.getElementById('attachments').hidden,
    count: controls().length,
  });
})();
"""
    r = _run(tmp_path, "cs87-empty", js)
    assert r["hidden"] is True
    assert r["count"] == 0


def test_full_state_matrix_status_action_and_unknown_ignored(tmp_path):
    js = _HARNESS + """
(async () => {
  content([ENTRY()]);
  await wait(50);
  const c = only();
  const rows = [];
  async function step(msg) {
    post(Object.assign({v:1, type:'attachment.state', ref:'a1'}, msg));
    await wait(15);
    const before = window.__intents.length;
    c.click();
    await wait(10);
    const emitted = window.__intents.slice(before).map(i => i.type);
    rows.push({state: c.dataset.state, status: c.querySelector('.attachment-status').textContent,
               disabled: c.disabled, emitted});
  }
  await step({state:'idle'});
  await step({state:'queued'});
  await step({state:'downloading', received:0, total:2097152});
  await step({state:'ready'});
  await step({state:'exporting'});
  await step({state:'complete'});
  await step({state:'cancelled'});
  await step({state:'error', error_code:'unavailable'});
  // Unknown state is ignored: control keeps its prior (error) state.
  post({v:1, type:'attachment.state', ref:'a1', state:'frobnicate'});
  await wait(15);
  const afterUnknown = c.dataset.state;
  return JSON.stringify({rows, afterUnknown});
})();
"""
    r = _run(tmp_path, "cs87-matrix", js)
    rows = {row["state"]: row for row in r["rows"]}
    assert rows["idle"]["status"] == "Download" and rows["idle"]["emitted"] == ["attachment.select"]
    assert rows["queued"]["status"] == "Queued…" and rows["queued"]["emitted"] == ["attachment.cancel"]
    assert rows["downloading"]["status"] == "Downloading 0%" and rows["downloading"]["emitted"] == ["attachment.cancel"]
    assert rows["ready"]["status"] == "Save" and rows["ready"]["emitted"] == ["attachment.export"]
    assert rows["exporting"]["status"] == "Saving…" and rows["exporting"]["disabled"] is True and rows["exporting"]["emitted"] == []
    assert rows["complete"]["status"] == "Saved" and rows["complete"]["disabled"] is True and rows["complete"]["emitted"] == []
    assert rows["cancelled"]["status"] == "Cancelled" and rows["cancelled"]["emitted"] == ["attachment.select"]
    assert rows["error"]["status"] == "Error: unavailable" and rows["error"]["emitted"] == ["attachment.select"]
    assert r["afterUnknown"] == "error"  # unknown state left the control unchanged


def test_rejects_malformed_state_envelopes(tmp_path):
    js = _HARNESS + """
(async () => {
  content([ENTRY()]);
  await wait(50);
  const c = only();
  const bad = [
    {v:999, type:'attachment.state', ref:'a1', state:'downloading', received:1, total:2},
    {v:1, type:'attachment.state', ref:'a1', state:'downloading', received:250.5, total:100},
    {v:1, type:'attachment.state', ref:'a1', state:'downloading', received:-5, total:100},
    {v:1, type:'attachment.state', ref:'a1', state:'downloading', received:200, total:100},
    {v:1, type:'attachment.state', ref:'a1', state:'downloading', received:1, total:2, path:'/tmp/evil'},
    {v:1, type:'attachment.state', ref:'a1', state:'bogus'},
    {v:1, type:'attachment.state', ref:'a1', state:'downloading', error_code:5, received:1, total:2},
    {v:1, type:'attachment.state', ref:'a1', state:'downloading', received:1},   // half-present pair
    {v:1, type:'attachment.state', ref:'a1', state:'downloading', total:2},      // half-present pair
  ];
  for (const m of bad) { post(m); await wait(8); }
  const afterBad = {state: c.dataset.state, status: c.querySelector('.attachment-status').textContent};
  // A well-formed envelope still applies.
  post({v:1, type:'attachment.state', ref:'a1', state:'downloading', received:1048576, total:2097152});
  await wait(15);
  return JSON.stringify({afterBad, afterGood: c.querySelector('.attachment-status').textContent});
})();
"""
    r = _run(tmp_path, "cs87-badstate", js)
    # Every malformed envelope left the control at its initial idle state.
    assert r["afterBad"] == {"state": "idle", "status": "Download"}
    assert r["afterGood"] == "Downloading 50%"


def test_rejects_malformed_manifest_entries(tmp_path):
    js = _HARNESS + """
(async () => {
  content([
    {ref:'m1', name:'a', mime:'text/plain', total_size:1, oversize:false},               // missing raw_sha256
    ENTRY({ref:'m2', mime:''}),                                                            // empty mime
    Object.assign(ENTRY({ref:'m3'}), {token:'bearer-secret'}),                             // extra field
    ENTRY({ref:'m4', name:'x'.repeat(256)}),                                               // name too long
    ENTRY({ref:'m5', raw_sha256:'Z'.repeat(64)}),                                          // non-hex hash
    ENTRY({ref:'ok', name:'good.bin'}),                                                    // valid
    ENTRY({ref:'ok', name:'dup.bin'}),                                                     // duplicate ref
  ]);
  await wait(50);
  const cs = controls();
  return JSON.stringify({
    refs: cs.map(c => c.dataset.ref),
    names: cs.map(c => c.querySelector('.attachment-name').textContent),
  });
})();
"""
    r = _run(tmp_path, "cs87-badmanifest", js)
    assert r["refs"] == ["ok"]          # only the valid, first-seen entry
    assert r["names"] == ["good.bin"]


def test_attachment_name_is_text_not_markup(tmp_path):
    js = _HARNESS + """
(async () => {
  content([ENTRY({ref:'x', name:'<img src=x onerror=alert(1)>.png', mime:'image/png', total_size:10})]);
  await wait(50);
  const c = only();
  return JSON.stringify({
    injectedImg: c.querySelectorAll('img').length,
    name: c.querySelector('.attachment-name').textContent,
  });
})();
"""
    r = _run(tmp_path, "cs87-xss", js)
    assert r["injectedImg"] == 0
    assert r["name"] == "<img src=x onerror=alert(1)>.png"


def test_ignores_content_from_a_non_parent_source(tmp_path):
    # A message whose source is not the viewer's parent must be ignored.
    js = _HARNESS + """
(async () => {
  // A child frame posts content to the viewer window (its parent). The
  // viewer's parent is the top window, so event.source != parent -> ignored.
  const f = document.createElement('iframe');
  f.srcdoc = "<scr" + "ipt>parent.postMessage({v:1,op:'content',title:'EVIL',markdown:'evil',parts:[],attachments:[{ref:'e',name:'e',mime:'text/plain',raw_sha256:'a'.repeat(64),total_size:1,oversize:false}]},'*')</scr" + "ipt>";
  document.body.appendChild(f);
  await wait(120);
  const ignored = {title: document.getElementById('note-title').textContent,
                   count: controls().length};
  // A message from the real parent is accepted.
  content([ENTRY({ref:'ok', name:'ok.bin'})]);
  await wait(50);
  return JSON.stringify({ignored, acceptedCount: controls().length, acceptedTitle: document.getElementById('note-title').textContent});
})();
"""
    r = _run(tmp_path, "cs87-source", js)
    assert r["ignored"] == {"title": "", "count": 0}   # foreign content ignored
    assert r["acceptedCount"] == 1 and r["acceptedTitle"] == "T"


def test_sandboxed_bridge_delivers_content_across_opaque_boundary(tmp_path):
    # The production sandbox is opaque (allow-scripts, no allow-same-origin),
    # so the host cannot read the child DOM; it proves the postMessage bridge:
    # content posted from the host reaches the child, which renders and reports
    # back (ready, then title/height once content is applied).
    js = """
(async () => {
  await new Promise(r => setTimeout(r, 300));
  const readyBefore = window.__fromChild.map(m => m.op);
  window.__post({v:1, op:'content', title:'Sandboxed', markdown:'# Sandboxed\\n\\nbody',
    parts:[], attachments:[{ref:'a1', name:'x.pdf', mime:'application/pdf',
      raw_sha256:'a'.repeat(64), total_size:1024, oversize:false}]});
  await new Promise(r => setTimeout(r, 300));
  return JSON.stringify({ops: window.__fromChild.map(m => m.op)});
})();
"""
    r = _run_host(tmp_path, "cs87-sandbox", js)
    # ready arrives on load; title + height arrive only after the host's
    # content crosses the opaque boundary and the child renders it.
    assert r["ops"][0] == "ready"
    assert "title" in r["ops"] and "height" in r["ops"]
