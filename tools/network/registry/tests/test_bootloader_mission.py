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


# ── in-page navigation: the srcdoc base-URL trap ────────────────────────

NAV = r"""
  const asked = [];
  const scrolled = [];
  const written = [];
  let handler = () => {};
  let clickHandler = () => {};
  global.parent = { postMessage(msg) {
    asked.push({ mcOp: msg.mcOp, body: msg.body });
    handler({ source: global.parent, data: {
      v: 1, op: "mc-response", id: msg.id,
      result: { status: "ok", html: "<html>pillar page</html>" } } });
  } };
  global.window.addEventListener = (name, fn) => { if (name === "message") handler = fn; };
  let domReady = () => {};
  // The shim registers MORE THAN ONE click listener (anchors, then
  // onclick-carrying rows). Keeping only the last silently disabled the
  // first and made the anchor tests fail against working code -- collect
  // them all and dispatch in order, like a real event target.
  const clickHandlers = [];
  global.document.addEventListener = (name, fn) => {
    if (name === "click") clickHandlers.push(fn);
    if (name === "DOMContentLoaded") domReady = fn;
  };
  global.document.documentElement = { outerHTML: "<html>the mission</html>" };
  global.document.currentScript = { textContent: "/*self*/" };
  global.document.getElementById = (id) => ({
    id, scrollIntoView: () => scrolled.push(id),
  });
  global.document.getElementsByName = () => [];
  global.document.open = () => {};
  global.document.write = (h) => written.push(h);
  global.document.close = () => {};
  global.window.scrollTo = () => scrolled.push("__top__");
  global.location = {
    assign: (v) => {}, replace: (v) => {}, href: "about:srcdoc",
  };
  global.window.location = global.location;
  const shim = autonet.MISSION_SHIM
    .replace(/^<script>/, "").replace(/<\/script>$/, "").replace("<\\/script>", "");
  eval(shim);

  function dispatch(target) {
    let prevented = false;
    let stopped = false;
    const event = {
      target,
      preventDefault: () => { prevented = true; },
      stopImmediatePropagation: () => { stopped = true; },
    };
    for (const fn of clickHandlers) {
      fn(event);
      if (stopped) break;
    }
    return prevented;
  }

  function clickHref(href) {
    return dispatch({
      tagName: "A",
      getAttribute: (k) => (k === "href" ? href : null),
      parentElement: null,
    });
  }

  function clickRow(onclickSource, text) {
    return dispatch({
      tagName: "SPAN",
      getAttribute: () => null,
      onclick: onclickSource,
      textContent: text || "",
      parentElement: null,
    });
  }
  __BODY__
"""


def nav(body: str):
    return run_js(NAV.replace("__BODY__", body))


def test_a_fragment_anchor_scrolls_instead_of_navigating():
    """THE BUG FOUND ON THE LIVE RELAY: a srcdoc document has no base URL
    of its own, so "#s5" resolves against the PARENT's URL and navigates
    the frame off the artifact permanently. The OSS Insights binder has 53
    of these -- its entire table of contents -- so this broke far more of
    the page than the pillar links did."""
    out = nav("""
      const prevented = clickHref("#s5");
      console.log(JSON.stringify({ prevented, scrolled, asked }));
    """)
    assert out["prevented"] is True
    assert out["scrolled"] == ["s5"]
    assert out["asked"] == []  # a scroll costs no channel round trip


def test_an_empty_fragment_goes_to_the_top():
    out = nav("""
      const prevented = clickHref("#");
      console.log(JSON.stringify({ prevented, scrolled }));
    """)
    assert out["prevented"] is True
    assert out["scrolled"] == ["__top__"]


def test_a_pillar_link_reads_over_the_channel_and_swaps_in_place():
    out = nav("""
      const prevented = clickHref("/missions/m1/pillars/p7");
      setTimeout(() => console.log(JSON.stringify({
        prevented, asked, wrote: written.length, hasSelf: (written[0]||"").indexOf("/*self*/") !== -1,
      })), 30);
    """)
    assert out["prevented"] is True
    assert out["asked"] == [{"mcOp": "read", "body": {"kind": "pillar_site", "pillar_id": "p7"}}]
    assert out["wrote"] == 1
    # The replacement document carries the shim again, or the pillar page
    # would lose fetch interception and every way back.
    assert out["hasSelf"] is True


def test_a_link_back_to_the_mission_restores_it_without_a_fetch():
    out = nav("""
      domReady();  // the shim captures the mission document here
      const prevented = clickHref("/missions/m1");
      console.log(JSON.stringify({ prevented, asked, wrote: written.length }));
    """)
    assert out["prevented"] is True
    assert out["asked"] == []  # already held; nothing to fetch
    assert out["wrote"] == 1


def test_any_other_absolute_path_is_refused_rather_than_destroying_the_page():
    """A same-site path would land on the relay's origin as a 404 and take
    the artifact with it. Refusing is strictly better than navigating."""
    out = nav("""
      const prevented = clickHref("/settings");
      console.log(JSON.stringify({ prevented, asked, wrote: written.length }));
    """)
    assert out["prevented"] is True
    assert out["wrote"] == 0


def test_an_external_link_is_left_alone():
    out = nav("""
      const prevented = clickHref("https://example.com/docs");
      console.log(JSON.stringify({ prevented }));
    """)
    assert out["prevented"] is False


def test_a_click_not_on_a_link_is_ignored():
    out = nav("""
      let prevented = false;
      prevented = dispatch({ tagName: "DIV", getAttribute: () => null,
                             parentElement: null, textContent: "" });
      console.log(JSON.stringify({ prevented, asked }));
    """)
    assert out["prevented"] is False


# ── the pillar dropdown: onclick rows, not anchors ──────────────────────


def test_a_dropdown_row_with_a_literal_pillar_path_is_routed():
    """THE MOBILE BUG: the pillar dropdown builds <span> rows carrying
    row.onclick = window.location.href = "/missions/<m>/pillars/<p>".
    They are not anchors, so the anchor walk misses them, and
    window.location is non-configurable in every engine so the old
    redefinition silently no-opped. The dropdown opened and closed but
    nothing could be selected."""
    out = nav("""
      const prevented = clickRow(
        'function () { window.location.href = "/missions/m1/pillars/p7"; }');
      setTimeout(() => console.log(JSON.stringify({ prevented, asked })), 30);
    """)
    assert out["prevented"] is True
    assert out["asked"] == [
        {"mcOp": "read", "body": {"kind": "pillar_site", "pillar_id": "p7"}}
    ]


def test_a_dropdown_row_that_concatenates_its_path_is_matched_by_label():
    """The real site builds the path as "/pillars/" + p.pillar_id, so the
    handler source carries no literal id. Fall back to matching the row's
    own text against the cached pillar roster."""
    out = nav("""
      window.__mcPillars = [
        { pillar_id: "p-collection", name: "Collection Pipeline" },
        { pillar_id: "p-platform", name: "Platform" },
      ];
      const prevented = clickRow(
        'function () { window.location.href = "/missions/" + MISSION_ID + "/pillars/" + p.pillar_id; }',
        "Platform");
      setTimeout(() => console.log(JSON.stringify({ prevented, asked })), 30);
    """)
    assert out["prevented"] is True
    assert out["asked"] == [
        {"mcOp": "read", "body": {"kind": "pillar_site", "pillar_id": "p-platform"}}
    ]


def test_an_unmatched_row_label_is_left_alone():
    out = nav("""
      window.__mcPillars = [{ pillar_id: "p1", name: "Known" }];
      const prevented = clickRow(
        'function () { window.location.href = "/missions/" + M + "/pillars/" + p.id; }',
        "Not In The Roster");
      setTimeout(() => console.log(JSON.stringify({ prevented, asked })), 30);
    """)
    assert out["prevented"] is False
    assert out["asked"] == []


def test_location_assign_is_routed_too():
    out = nav("""
      window.location.assign("/missions/m1/pillars/p9");
      setTimeout(() => console.log(JSON.stringify({ asked })), 30);
    """)
    assert out["asked"] == [
        {"mcOp": "read", "body": {"kind": "pillar_site", "pillar_id": "p9"}}
    ]


def test_the_pillar_roster_is_cached_from_the_pillars_read():
    """The label fallback above depends on this cache being populated as a
    side effect of the menu's own fetch."""
    out = route('await window.fetch("/api/missions/m1/pillars");')
    assert out == [{"mcOp": "read", "body": {"kind": "pillars"}}]
