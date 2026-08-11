"""The mission viewer bridge (auto-t2lz1).

A mission's page is authored against the dashboard's own origin. Over the
relay it runs in a sandboxed srcdoc iframe on the relay's origin, where
every ``/api/...`` path is a 404 and every pillar link fails silently --
reproduced on the live relay before this was built.

These tests run the real shim and the real bridge under Node with a stub
channel, so the routing rules are pinned without a browser: which URLs
become which channel ops, and that the bridge never trusts a message from
anywhere but its own frame.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

AUTONET_JS = Path(__file__).resolve().parents[1] / "bootloader" / "autonet.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not on PATH",
)

#: A DOM/window stub just rich enough to load autonet.js and drive the
#: shim's fetch router. Deliberately not jsdom: the surface under test is
#: small and explicit, and a real dependency would hide what it provides.
HARNESS = r"""
global.window = global;
global.self = global;
global.document = {
  // "loading" so the file's own tail registers a DOMContentLoaded
  // listener instead of calling boot() -- these tests exercise the
  // mission bridge, not the whole connect-and-render path.
  readyState: "loading", baseURI: "https://relay.auto.network/l/tok",
  addEventListener() {}, removeEventListener() {},
  getElementById() { return { style: {}, classList: { add() {}, remove() {} },
                              setAttribute() {}, removeAttribute() {},
                              appendChild() {}, contentWindow: {} }; },
  querySelectorAll() { return []; }, createElement() { return { style: {} }; },
  open() {}, write() {}, close() {},
};
global.location = { href: "https://relay.auto.network/l/tok", protocol: "https:" };
global.navigator = { userAgent: "node" };
global.addEventListener = () => {};
global.removeEventListener = () => {};
global.crypto = require("crypto").webcrypto;
global.TextEncoder = require("util").TextEncoder;
global.TextDecoder = require("util").TextDecoder;
global.WebSocket = function () {};
global.fetch = () => Promise.reject(new Error("no network in this harness"));
global.Response = class {
  constructor(body, init) { this._body = body; this.status = (init || {}).status || 200; }
  json() { return Promise.resolve(JSON.parse(this._body)); }
};
"""


def run_js(script: str):
    """Load autonet.js in Node, run *script*, return its JSON result."""
    program = (
        HARNESS
        + "\nconst autonet = (function () { const module = {};\n"
        + AUTONET_JS.read_text()
        + "\nreturn autonet; })();\n"
        + script
    )
    result = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"node failed:\n{result.stderr}")
    return json.loads(result.stdout.strip().splitlines()[-1])


# ── the shim is present and prepended, not substituted ──────────────────


def test_shim_is_prepended_leaving_the_coordinators_html_intact():
    out = run_js("""
      const html = "<html><body>coordinator bytes</body></html>";
      const shimmed = autonet.missionShimmed(html);
      console.log(JSON.stringify({
        ends_with_original: shimmed.endsWith(html),
        has_shim: shimmed.indexOf("mc-request") !== -1,
        original_untouched: shimmed.indexOf(html) === shimmed.length - html.length,
      }));
    """)
    assert out == {"ends_with_original": True, "has_shim": True, "original_untouched": True}


# ── fetch routing: which URL becomes which channel op ───────────────────

ROUTER = r"""
  // Run the shim's body in this context with a captured postMessage, then
  // exercise window.fetch and report what the shim asked the parent for.
  const asked = [];
  global.parent = { postMessage(msg) {
    asked.push({ mcOp: msg.mcOp, body: msg.body });
    // Answer immediately so the promise chain settles.
    handler({ source: global.parent, data: {
      v: 1, op: "mc-response", id: msg.id, result: { status: "ok" } } });
  } };
  let handler = () => {};
  global.window.addEventListener = (name, fn) => { if (name === "message") handler = fn; };
  global.document.addEventListener = () => {};
  const shim = autonet.MISSION_SHIM
    .replace(/^<script>/, "").replace(/<\/script>$/, "").replace("<\\/script>", "");
  eval(shim);
  (async () => {
    __CALLS__
    console.log(JSON.stringify(asked));
  })();
"""


def route(calls: str):
    return run_js(ROUTER.replace("__CALLS__", calls))


def test_listing_mission_questions_becomes_a_read():
    assert route(
        'await window.fetch("/api/missions/m1/questions");'
    ) == [{"mcOp": "read", "body": {"kind": "questions"}}]


def test_listing_pillar_questions_carries_the_pillar_id():
    assert route(
        'await window.fetch("/api/pillars/p1/questions");'
    ) == [{"mcOp": "read", "body": {"kind": "questions", "pillar_id": "p1"}}]


def test_asking_a_question_becomes_a_write():
    assert route(
        'await window.fetch("/api/missions/m1/questions", {method:"POST",'
        ' body: JSON.stringify({question:"why?", anchor:"table:x"})});'
    ) == [{"mcOp": "write", "body": {
        "kind": "question", "question": "why?", "anchor": "table:x"}}]


def test_asking_on_a_pillar_carries_the_pillar_id():
    out = route(
        'await window.fetch("/api/pillars/p9/questions", {method:"POST",'
        ' body: JSON.stringify({question:"q"})});'
    )
    assert out[0]["mcOp"] == "write"
    assert out[0]["body"]["pillar_id"] == "p9"


def test_reopening_becomes_a_write():
    assert route(
        'await window.fetch("/api/missions/m1/questions/e7/reopen", {method:"POST",'
        ' body: JSON.stringify({followup:"not quite"})});'
    ) == [{"mcOp": "write", "body": {
        "kind": "reopen", "entry_id": "e7", "followup": "not quite"}}]


def test_listing_pillars_becomes_a_read():
    assert route(
        'await window.fetch("/api/missions/m1/pillars");'
    ) == [{"mcOp": "read", "body": {"kind": "pillars"}}]


def test_presence_becomes_a_read():
    assert route(
        'await window.fetch("/api/graph/settings/dashboard.surface.presence");'
    ) == [{"mcOp": "read", "body": {"kind": "presence"}}]


def test_an_unrelated_url_is_left_to_the_real_fetch():
    """The shim must not swallow requests it does not own -- an artifact
    fetching its own inline asset still goes to the browser."""
    assert route(
        'try { await window.fetch("https://example.com/thing.json"); } catch (e) {}'
    ) == []


# ── the bridge only trusts its own frame ────────────────────────────────


BRIDGE = r"""
  const sent = [];
  const channel = {
    sendMessage(bytes) { sent.push(new TextDecoder().decode(bytes)); return Promise.resolve(); },
    recvMessage() {
      return Promise.resolve(new TextEncoder().encode(
        JSON.stringify({ v: 1, status: "ok", pillars: [] }) + "\n"));
    },
  };
  const posted = [];
  const childWindow = { postMessage(msg) { posted.push(msg); } };
  let handler = null;
  global.window.addEventListener = (name, fn) => { if (name === "message") handler = fn; };
  global.window.removeEventListener = () => {};
  const bridge = new autonet.MissionBridge({
    frame: { contentWindow: childWindow }, channel,
  });
  __BODY__
"""


def bridge(body: str):
    return run_js(BRIDGE.replace("__BODY__", body))


def test_bridge_issues_the_channel_op_and_answers_the_frame():
    out = bridge("""
      handler({ source: childWindow, data: {
        v: 1, op: "mc-request", id: "1", mcOp: "read", body: { kind: "pillars" } } });
      setTimeout(() => console.log(JSON.stringify({
        sent: sent.map(JSON.parse), posted })), 50);
    """)
    assert out["sent"] == [{"v": 1, "op": "read", "body": {"kind": "pillars"}}]
    assert out["posted"][0]["op"] == "mc-response"
    assert out["posted"][0]["result"]["status"] == "ok"


def test_bridge_ignores_a_message_from_another_window():
    """The frame is an opaque origin, so provenance is the source window
    check -- a message from anywhere else must not reach the channel."""
    out = bridge("""
      handler({ source: { impostor: true }, data: {
        v: 1, op: "mc-request", id: "1", mcOp: "read", body: { kind: "pillars" } } });
      setTimeout(() => console.log(JSON.stringify({ sent, posted })), 50);
    """)
    assert out == {"sent": [], "posted": []}


def test_bridge_ignores_an_unknown_op():
    out = bridge("""
      handler({ source: childWindow, data: {
        v: 1, op: "mc-request", id: "1", mcOp: "delete_everything", body: {} } });
      setTimeout(() => console.log(JSON.stringify({ sent, posted })), 50);
    """)
    assert out == {"sent": [], "posted": []}


def test_bridge_answers_null_when_the_channel_fails():
    """A failed exchange must answer, not hang the page waiting forever."""
    out = run_js(BRIDGE.replace("__BODY__", """
      channel.sendMessage = () => Promise.reject(new Error("channel died"));
      handler({ source: childWindow, data: {
        v: 1, op: "mc-request", id: "1", mcOp: "read", body: {} } });
      setTimeout(() => console.log(JSON.stringify({ posted })), 50);
    """))
    assert out["posted"][0]["result"] is None




# ── the rewriter: link shapes taken from the REAL artifact ──────────────
#
# Counted on the live OSS Insights binder rather than imagined:
#   64 fragments · 29 external · 6 pillar links · 5 inline onclick · 1 no-href
# The previous round of this file tested pillar links (6 of 59) because that
# is what was being built, and missed the fragments that dominate the page.
# These cases come from that inventory.


def rewrite(html: str) -> str:
    return run_js(
        "console.log(JSON.stringify(autonet.missionShimmed(%s).slice(autonet.MISSION_SHIM.length)))"
        % json.dumps(html)
    )


def test_a_pillar_href_becomes_a_marker():
    out = rewrite('<a href="/missions/m1/pillars/p7">Collection Pipeline</a>')
    assert 'data-mc-pillar="p7"' in out
    assert "/missions/m1/pillars/p7" not in out


def test_a_mission_home_href_becomes_a_marker():
    out = rewrite('<a href="/missions/m1">Back to mission overview</a>')
    assert 'data-mc-home="1"' in out
    assert 'href="/missions/m1"' not in out


def test_a_fragment_is_marked_and_keeps_its_href():
    """64 of these. They must scroll, not navigate: a srcdoc document
    inherits the PARENT's base URL, so '#s5' resolves to a different
    document and takes the artifact with it."""
    out = rewrite('<a href="#s5">5 · Data Acquisition</a>')
    assert 'data-mc-fragment="s5"' in out
    assert 'href="#s5"' in out


def test_external_links_are_untouched():
    """29 of these. Breaking them would be a worse regression than the bug
    being fixed."""
    html = '<a href="https://github.com/anchore/syft">syft</a>'
    assert rewrite(html) == html


def test_a_static_onclick_naming_a_pillar_becomes_a_marker_and_loses_its_handler():
    out = rewrite(
        '<span onclick="window.location.href=\'/missions/m1/pillars/p9\'">Platform</span>'
    )
    assert 'data-mc-pillar="p9"' in out
    assert "onclick" not in out


def test_an_anchor_with_no_href_is_untouched():
    html = "<a>no href at all</a>"
    assert rewrite(html) == html


def test_relative_and_other_paths_are_left_alone():
    for html in ('<a href="/settings">settings</a>',
                 '<a href="report.pdf">report</a>',
                 '<a href="mailto:x@y.z">mail</a>'):
        assert rewrite(html) == html


def test_the_shim_is_still_prepended_and_the_body_follows():
    out = run_js(
        "const h='<html><body>coordinator bytes</body></html>';"
        "const s=autonet.missionShimmed(h);"
        "console.log(JSON.stringify({has_shim:s.indexOf('mc-request')!==-1,"
        "ends_with_body:s.endsWith('coordinator bytes</body></html>')}))"
    )
    assert out == {"has_shim": True, "ends_with_body": True}


# ── the single runtime click rule ───────────────────────────────────────

CLICK = r"""
  const posted = [];
  const scrolled = [];
  let clickHandlers = [];
  let handler = () => {};
  global.parent = { postMessage(msg) {
    posted.push(msg);
    if (msg.op === "mc-request") {
      handler({ source: global.parent, data: {
        v: 1, op: "mc-response", id: msg.id,
        result: { status: "ok", html: "<html>pillar</html>" } } });
    }
  } };
  global.window.addEventListener = (n, fn) => { if (n === "message") handler = fn; };
  global.document.addEventListener = (n, fn) => { if (n === "click") clickHandlers.push(fn); };
  global.document.getElementById = (id) => ({ id, scrollIntoView: () => scrolled.push(id) });
  global.document.getElementsByName = () => [];
  global.window.scrollTo = () => scrolled.push("__top__");
  const shim = autonet.MISSION_SHIM
    .replace(/^<script>/, "").replace(/<\/script>$/, "").replace("<\\/script>", "");
  eval(shim);

  function click(attrs, opts) {
    opts = opts || {};
    let prevented = false, stopped = false;
    const el = {
      getAttribute: (k) => (k in attrs ? attrs[k] : null),
      onclick: opts.onclick || null,
      textContent: opts.text || "",
      parentElement: null,
    };
    const event = { target: el, preventDefault: () => { prevented = true; },
                    stopImmediatePropagation: () => { stopped = true; } };
    for (const fn of clickHandlers) { fn(event); if (stopped) break; }
    return prevented;
  }
  __BODY__
"""


def click_case(body: str):
    return run_js(CLICK.replace("__BODY__", body))


def test_a_pillar_marker_reads_over_the_channel_and_asks_the_parent_to_swap():
    out = click_case("""
      const prevented = click({ "data-mc-pillar": "p7" });
      setTimeout(() => console.log(JSON.stringify({ prevented, posted })), 30);
    """)
    assert out["prevented"] is True
    kinds = [m["op"] for m in out["posted"]]
    assert "mc-request" in kinds and "mc-swap" in kinds
    swap = [m for m in out["posted"] if m["op"] == "mc-swap"][0]
    assert swap["html"] == "<html>pillar</html>"


def test_a_home_marker_asks_the_parent_and_costs_no_channel_traffic():
    """THE BACK-LINK BUG: this used to depend on state captured inside the
    frame, which a pillar swap clobbered with the pillar's own HTML."""
    out = click_case("""
      const prevented = click({ "data-mc-home": "1" });
      console.log(JSON.stringify({ prevented, posted }));
    """)
    assert out["prevented"] is True
    assert [m["op"] for m in out["posted"]] == ["mc-home"]


def test_a_fragment_marker_scrolls_and_costs_no_channel_traffic():
    out = click_case("""
      const prevented = click({ "data-mc-fragment": "s5" });
      console.log(JSON.stringify({ prevented, scrolled, posted }));
    """)
    assert out["prevented"] is True
    assert out["scrolled"] == ["s5"]
    assert out["posted"] == []


def test_an_empty_fragment_marker_goes_to_the_top():
    out = click_case("""
      const prevented = click({ "data-mc-fragment": "" });
      console.log(JSON.stringify({ prevented, scrolled }));
    """)
    assert out["prevented"] is True
    assert out["scrolled"] == ["__top__"]


def test_an_unmarked_click_is_left_alone():
    out = click_case("""
      const prevented = click({});
      console.log(JSON.stringify({ prevented, posted }));
    """)
    assert out["prevented"] is False


def test_the_documented_topbar_snippet_marks_both_destinations():
    """The pillar dropdown IS our own snippet (SKILL.md), copied into
    coordinator sites. Its rows are built in JS, so no rewriter can reach
    them -- marking them at the source is what keeps the viewer's rule
    singular instead of reverse-engineering code we ship. If this drifts,
    the dropdown silently stops working over the relay, which is exactly
    how it shipped broken."""
    from pathlib import Path
    skill = Path(__file__).resolve().parents[3] / "dashboard" / "plugins" / \
        "mission_control" / "SKILL.md"
    text = skill.read_text()
    assert 'top.setAttribute("data-mc-home", "1")' in text
    assert 'row.setAttribute("data-mc-pillar", p.pillar_id)' in text


def test_an_unmarked_interactive_row_is_left_alone():
    """The label-matching fallback is gone. A control the site did not mark
    is not ours to route -- guessing was the whole problem."""
    out = click_case("""
      const prevented = click({}, { onclick: function () {}, text: "Platform" });
      console.log(JSON.stringify({ prevented, posted }));
    """)
    assert out["prevented"] is False
    assert out["posted"] == []


def test_a_mission_takes_the_whole_surface():
    """A mission carries its own top bar, so the relay's header is hidden
    for it -- stacking both left a strip holding one brand letter above the
    real bar."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "bootloader"
    assert "body.mission-surface > header { display: none; }" in \
        (root / "bootloader.html").read_text()
    assert 'classList.toggle("mission-surface", artifact.kind === "mission")' in \
        (root / "autonet.js").read_text()
