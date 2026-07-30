"""auto-1cs87: the sandboxed note viewer renders the attachment manifest as
clickable download controls and speaks the frozen semantic-intent bridge.

The viewer (relay_viewer/note-viewer.html) does its DOM rendering inside a
sandboxed iframe, so it is exercised in a real engine via agent-browser
(headless Chrome). Each scenario loads the actual served viewer bytes, posts
a content message as the same-origin parent would, and inspects the rendered
DOM, the emitted intents, and the state reflection.
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


def _run(tmp_path, session: str, js: str):
    """Load the served viewer and run one eval scenario; return parsed JSON."""
    viewer = tmp_path / "note-viewer.html"
    viewer.write_bytes(link_serving._note_viewer_bytes())
    url = f"file://{viewer}"
    try:
        subprocess.run(
            ["agent-browser", "--session", session, "open", url],
            capture_output=True, text=True, timeout=60, check=True,
        )
        result = subprocess.run(
            ["agent-browser", "--session", session, "eval", "--stdin"],
            input=js, capture_output=True, text=True, timeout=60, check=True,
        )
    finally:
        subprocess.run(
            ["agent-browser", "--session", session, "close"],
            capture_output=True, text=True, timeout=30,
        )
    out = result.stdout.strip()
    parsed = json.loads(out)
    if isinstance(parsed, str):  # agent-browser wraps a returned string
        parsed = json.loads(parsed)
    return parsed


# Capture only child->parent intents (select/cancel/export), never our own
# injected attachment.state, and post the content as the parent would.
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
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
"""


def test_renders_controls_click_and_state(tmp_path):
    js = _HARNESS + """
(async () => {
  content([
    {ref:'a1', name:'report.pdf', mime:'application/pdf',
     raw_sha256:'a'.repeat(64), total_size:2097152, oversize:false},
    {ref:'a2', name:'notes.txt', mime:'text/plain',
     raw_sha256:'b'.repeat(64), total_size:500, oversize:false},
    {ref:'a3', name:'huge.bin', mime:'application/octet-stream',
     raw_sha256:'c'.repeat(64), total_size:900, oversize:true},
  ]);
  await wait(50);
  const cs = controls();
  cs[0].click();                         // idle -> attachment.select
  post({v:1, type:'attachment.state', ref:'a1',
        state:'downloading', received:1048576, total:2097152});
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
    # Clicking an idle control emits exactly one attachment.select for its ref.
    assert r["intents"] == [{"v": 1, "type": "attachment.select", "ref": "a1"}]
    # The parent's state message drives the control's displayed state.
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


def test_state_matrix_actions_and_cancel_export_intents(tmp_path):
    js = _HARNESS + """
(async () => {
  content([{ref:'a1', name:'f.bin', mime:'application/octet-stream',
            raw_sha256:'a'.repeat(64), total_size:2097152, oversize:false}]);
  await wait(50);
  const c = controls()[0];
  const steps = [];
  // downloading -> click emits cancel
  post({v:1, type:'attachment.state', ref:'a1', state:'downloading',
        received:0, total:2097152});
  await wait(20); c.click(); await wait(10);
  steps.push(['downloading', c.querySelector('.attachment-status').textContent]);
  // ready -> click emits export
  post({v:1, type:'attachment.state', ref:'a1', state:'ready'});
  await wait(20); c.click(); await wait(10);
  steps.push(['ready', c.querySelector('.attachment-status').textContent]);
  // complete -> terminal, disabled, no intent
  post({v:1, type:'attachment.state', ref:'a1', state:'complete'});
  await wait(20); const completeDisabled = c.disabled; c.click(); await wait(10);
  // error -> retry via select
  post({v:1, type:'attachment.state', ref:'a1', state:'error',
        error_code:'unavailable'});
  await wait(20);
  const errText = c.querySelector('.attachment-status').textContent;
  c.click(); await wait(10);
  return JSON.stringify({
    steps, completeDisabled, errText, intents: window.__intents,
  });
})();
"""
    r = _run(tmp_path, "cs87-matrix", js)
    assert r["steps"][0] == ["downloading", "Downloading 0%"]
    assert r["steps"][1] == ["ready", "Save"]
    assert r["completeDisabled"] is True
    assert r["errText"] == "Error: unavailable"
    # downloading->cancel, ready->export, complete->(none), error->select
    assert r["intents"] == [
        {"v": 1, "type": "attachment.cancel", "ref": "a1"},
        {"v": 1, "type": "attachment.export", "ref": "a1"},
        {"v": 1, "type": "attachment.select", "ref": "a1"},
    ]


def test_attachment_name_is_text_not_markup(tmp_path):
    js = _HARNESS + """
(async () => {
  content([{ref:'x', name:'<img src=x onerror=alert(1)>.png',
            mime:'image/png', raw_sha256:'a'.repeat(64),
            total_size:10, oversize:false}]);
  await wait(50);
  const c = controls()[0];
  return JSON.stringify({
    // No injected element: the name is textContent, so no child <img> exists.
    injectedImg: c.querySelectorAll('img').length,
    name: c.querySelector('.attachment-name').textContent,
  });
})();
"""
    r = _run(tmp_path, "cs87-xss", js)
    assert r["injectedImg"] == 0
    assert r["name"] == "<img src=x onerror=alert(1)>.png"
