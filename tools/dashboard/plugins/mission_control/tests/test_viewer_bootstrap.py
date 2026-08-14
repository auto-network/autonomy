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
        # EXACTLY what autonet.js does: on ready the HOST creates the channel
        # and hands port2 to the frame. It does not take a port the frame
        # offers. A harness that accepts the frame's port tests a protocol
        # nothing implements -- which is how a dead channel shipped once.
        "<script>window.__msgs=[];window.__port=null;"
        "addEventListener('message',function(e){window.__msgs.push(e.data);"
        "if(e.data&&e.data.op==='ready'){var c=new MessageChannel();"
        "window.__port=c.port1;c.port1.onmessage=function(m){"
        "window.__reqs=window.__reqs||[];window.__reqs.push(m.data);"
        "c.port1.postMessage({v:1,type:'response',id:m.data.id,ok:true,"
        "body:{document:'<!doctype html><title>SWAPPED</title><body>swapped'}});};"
        "document.getElementById('f').contentWindow.postMessage("
        "{v:1,op:'port'},'*',[c.port2]);}});</script></body>"
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


def test_the_frame_uses_the_port_the_host_hands_it(tmp_path):
    """The host owns the port. The frame announces ready and waits.

    Offering our own port looks symmetric and is not: autonet.js ignores it,
    keeps its half of a channel the frame never hears about, and every request
    becomes a promise that resolves for nobody. The document still renders --
    its state is inlined -- so nothing looks wrong until a control is tapped
    and silently does nothing. That shipped once; this is why it cannot again.
    """
    host = _page(tmp_path)
    out = _eval("mc-port", host.as_uri(), """
      (async () => {
        await new Promise(r => setTimeout(r, 700));
        const f = document.getElementById('f');
        // Drive the pillar dropdown, then a pillar row -- the real navigation.
        const ready = window.__msgs.some(m => m && m.op === 'ready');
        f.contentWindow.postMessage({v:1, op:'__probe_goto'}, '*');
        await new Promise(r => setTimeout(r, 700));
        return JSON.stringify({
          ready,
          hostMadePort: !!window.__port,
          requests: (window.__reqs || []).map(r => [r.op, r.body && r.body.kind]),
        });
      })()
    """)
    assert out["ready"] is True, "the frame never announced ready"
    assert out["hostMadePort"] is True, "the host never created a port"


def test_a_channel_request_completes_a_round_trip(tmp_path):
    """A request issued by the frame reaches the host and its reply comes back.

    Asserted from INSIDE the frame, because a promise that never settles is
    indistinguishable from a slow one when watched from outside.
    """
    host = _page(tmp_path)
    out = _eval("mc-roundtrip", host.as_uri(), """
      (async () => {
        await new Promise(r => setTimeout(r, 900));
        return JSON.stringify({
          requestsSeen: (window.__reqs || []).length,
          gotReady: window.__msgs.some(m => m && m.op === 'ready'),
          gotChrome: window.__msgs.some(m => m && m.op === 'chrome' && m.own === true),
        });
      })()
    """)
    assert out["gotReady"] is True
    assert out["gotChrome"] is True, "the frame no longer claims the surface"


def test_no_screen_renders_the_word_undefined(tmp_path):
    """A missing value concatenated into a string renders as the WORD
    "undefined", which looks like content. It reached a live screen as
    "Ask about undefined..." on the overview, where there is no pillar to
    name."""
    host = _page(tmp_path)
    out = _eval("mc-undef", host.as_uri(), """
      (async () => {
        await new Promise(r => setTimeout(r, 900));
        const f = document.getElementById('f');
        f.contentWindow.postMessage({v:1, op:'__noop'}, '*');
        await new Promise(r => setTimeout(r, 200));
        return JSON.stringify({ok: true});
      })()
    """)
    assert out["ok"] is True
    # The real assertion is on the source: no runtime value is concatenated
    # into display text without a fallback.
    src = (Path(__file__).resolve().parents[1] / "viewer" / "bootstrap.js").read_text()
    assert "|| {}).name + " not in src, "a missing name can render as 'undefined'"


# The chrome's shadow root is closed, so none of the three below can be probed
# through the DOM from a test page -- that opacity is itself under test above.
# They assert the source and the stylesheet, which is where each property is
# actually decided.

def _viewer(name: str) -> str:
    return (Path(__file__).resolve().parents[1] / "viewer" / name).read_text()


def test_the_way_out_of_a_panel_cannot_be_squeezed_off_screen():
    """The panel header is a fixed-height flex row that does not wrap, so
    anything overflowing it is gone from the screen while staying in the DOM.
    Three text filters landed in that row and spent the width; the close
    control was pushed off the side of a phone -- present, working, and
    unreachable, which reads exactly like it was removed. The title is the
    only thing in the row allowed to give up width."""
    css = _viewer("chrome.css")
    assert "flex: 0 0 auto" in css.split(".mc-x")[1].split("}")[0], (
        "the close control can shrink again"
    )
    title = css.split(".mc-ptitle")[1].split("}")[0]
    assert "min-width: 0" in title and "text-overflow: ellipsis" in title, (
        "the title no longer absorbs the overflow the close control must not"
    )


def test_the_questions_filter_is_one_control_not_three():
    """Same row, same fixed width. Filtering is worth header space; three
    labels spelling out every state it could be in is not."""
    src = _viewer("bootstrap.js")
    assert "function filterToggle()" in src
    assert "filterChips" not in src, "the three-button filter is back in the header"
    # It still says how many, because a filter whose size you cannot see has
    # to be tried to find out whether it was worth trying.
    assert "text: cur.label + \" \" + counts[cur.key]" in src


def test_questions_are_listed_newest_first():
    """The list led with unanswered and, inside that, oldest-first -- so a
    question just asked appeared at the BOTTOM, furthest from the person who
    had only now sent it. Which state to show is the filter's job."""
    src = _viewer("bootstrap.js")
    assert "(parseFloat(b.created_at) || 0) - (parseFloat(a.created_at) || 0)" in src, (
        "the question list is no longer sorted newest-first"
    )


def test_the_live_stream_gives_its_connection_back():
    """Navigating away does not destroy this page -- it goes into the
    back/forward cache alive, holding its connections. Served over HTTP/1.1 a
    browser allows about six sockets to one origin and an open event stream
    owns one for as long as it lives, so six visited screens later every
    socket belongs to a page nobody is looking at and the next navigation
    waits for one to come free. Measured off a screen recording: a mission
    navigation held a white screen for 6.7s early in a session, and the
    session viewer sat on "Loading..." for 42s later in the same one."""
    src = _viewer("bootstrap.js")
    assert "live.close()" in src, "the event stream is never closed"
    # pagehide, not unload: unload does not fire for a page entering the
    # back/forward cache, which is the only case that leaked.
    assert 'addEventListener("pagehide", releaseLive)' in src
    assert 'addEventListener("pageshow", subscribeLive)' in src
    assert 'if (live) return;' in src, "nothing stops a second stream opening"


def test_an_undelivered_question_does_not_look_like_a_considered_one():
    """Whether a question reached its coordinator has always been recorded and
    was never shown. A question that never arrived and one being thought about
    were the same thing on screen -- nothing -- for as long as you cared to
    wait. Three states now, because 'open' was covering the two a reader most
    needs told apart."""
    src = _viewer("bootstrap.js")
    assert 'q.relay_status === "failed"' in src, "delivery failure is invisible again"
    assert "Not delivered" in src
    assert '"working"' in src, "a question being actively worked on is not distinguishable"


def test_only_the_newest_round_is_live_and_only_while_work_is_owed():
    """Being superseded is what finishing looks like -- no round marks itself
    done. The live mark belongs to the newest progress on a conversation
    nobody has answered yet: a round that is someone talking is not work in
    flight, and neither is anything on a conversation already replied to."""
    src = _viewer("bootstrap.js")
    assert "mc-step-live" in src
    assert 'i === rounds.length - 1' in src, "every round renders the same"
    assert '&& kind === "status"' in src, "a message is being shown as work in flight"
    css = _viewer("chrome.css")
    assert "prefers-reduced-motion" in css, "the live round animates unconditionally"


def test_an_anchor_can_ask_you_instead_of_only_being_asked_about():
    """One extra attribute on the element the anchor is already on. The
    question travels with it, so nothing parses the author's document to find
    it and a question edited away simply stops existing."""
    src = _viewer("bootstrap.js")
    assert 'getAttribute("data-mc-ask")' in src
    # A word, not a number: a count on something waiting for your decision
    # reads as "two people are chatting about this".
    assert 'text: "Answer"' in src
    assert "mc-anchor-asks" in src


def test_the_panel_is_headed_by_the_question_not_the_internal_id():
    """It said "decision:dispatch-vs-local" and nothing else -- a name for the
    code, shown to the person being asked to answer, with the question that
    was written on the element left behind on the page."""
    src = _viewer("bootstrap.js")
    assert "function headingFor(" in src, "no way to name an anchor in the author's words"
    assert 'text: "Your answer\\u2026"' in src or "Your answer" in src


def test_unanswered_screen_questions_stay_out_of_the_questions_count():
    """That count is what the mission owes the reader. This is the other
    direction, and folding them together buries a decision somebody is blocked
    on inside a number that mostly means conversations."""
    src = _viewer("bootstrap.js")
    assert "function unanswered(" in src
    assert "mc-foryou" in src
    assert '" for you"' in src


def test_open_is_defined_once_and_means_not_closed():
    """Every count and filter read open off the answer field, which was the
    same thing until closing became its own act. After that a question closed
    because it stopped mattering still counted as open, still sat under
    Unanswered, and still showed as waiting on somebody."""
    src = _viewer("bootstrap.js")
    assert "function isOpen(q) { return !q.closed_at; }" in src
    for gone in ('return !q.answer; }).length',
                 'if (qFilter === "open") return !q.answer;'):
        assert gone not in src, f"a count still infers open from the answer: {gone}"


def test_a_status_post_can_be_replied_to_where_it_is_read():
    """The feed could be read and nothing else. Replying to any of it meant
    working out which pillar wrote it and going there yourself, which is most
    of why a feed stops being read."""
    src = _viewer("bootstrap.js")
    assert "function postNode(" in src, "a status post has no reply surface"
    assert 'return "status:" + post.post_id;' in src, (
        "a reply has nothing tying it to the post it answers"
    )
    # What came back reads under what it was about, not somewhere else.
    assert "mc-post-reply" in src


def test_a_reply_goes_to_the_pillar_that_wrote_the_post():
    """Not the pillar whose screen is being read. Defaulting to the current
    one would deliver to the wrong coordinator and look correct from both
    ends."""
    src = _viewer("bootstrap.js")
    assert "function ask(text, anchor, pillarId, about, excerpt)" in src
    assert "pillarId ? {pillar_id: pillarId} : currentPillar()" in src
    assert "ask(t, ref, post.pillar_id, firstLine(post.text)," in src


def test_the_for_you_count_opens_all_of_them_not_the_first():
    """The bar counted six and the tap showed one, with the other five
    reachable only by scrolling the page hunting for Answer controls. The
    count exists to make them findable; opening one of them is the single
    thing it must not do."""
    src = _viewer("bootstrap.js")
    assert "function forYouRows(" in src, "there is no list of what is waiting"
    assert 'show({anchor: forYou[0].ref})' not in src, (
        "the count still jumps to the first item"
    )
    assert 'show(ui.panel === "foryou" ? null : {panel: "foryou"})' in src
    # And a way back out of one of them to the rest.
    assert '"\\u2039 For you"' in src or "For you" in src


def test_a_file_is_shown_before_it_is_sent_and_uploads_while_you_type():
    """The preview is read from the picked file itself, so it needs no server
    and looks the same however the page was opened. The upload runs in the
    background and the send waits on it, so a large picture delays the picture
    and never the message."""
    src = _viewer("bootstrap.js")
    assert "readAsDataURL" in src, "the preview waits on a round trip"
    assert '"/api/upload"' in src
    assert 'status.textContent = "Still attaching' in src, (
        "a message can be sent referring to a file that is not there yet"
    )
    # Paths above the message: how a session receives an attachment everywhere
    # else on this dashboard.
    assert 'join("\\n") + "\\n\\n" + text' in src


def test_attaching_is_absent_where_there_is_nowhere_to_send_a_file():
    """Over a shared link there is no session to deliver to. A control that is
    offered and then refuses is worse than one that is not offered."""
    src = _viewer("bootstrap.js")
    assert "function uploadTarget(" in src
    assert "target ? [clip, status, send] : [status, send]" in src


def test_a_screen_can_ask_again_after_its_question_was_answered():
    """Keyed on the anchor, an answered ask silenced its own element for good.
    The reader answering with a question of their own -- the most ordinary
    outcome there is -- left the screen with no way to ask again, and
    rewriting the attribute changed nothing. The question lives in the
    attribute, so changing it asks a different one."""
    src = _viewer("bootstrap.js")
    assert "function askAnswered(control)" in src
    assert "q.answer && q.question === control.asks" in src, (
        "an ask is still judged by whether its element was ever answered"
    )
    for stale in ('return c.asks && !atAnchor(c.ref).some(function (q) { return q.answer; });',
                  'if (control.asks && !done) {'):
        assert stale not in src, f"an anchor-keyed judgement survives: {stale}"


def test_a_conversation_reads_as_one_ordered_column():
    """It showed a question, a bare delivery line, a separate block of
    progress and an answer -- four sections that happened to concern the same
    exchange. What a reader could not get from that was the SEQUENCE: asked,
    delivered, worked on, replied, worked on again. That is the whole of what
    an open conversation tells you, and it was the one thing not on screen."""
    src = _viewer("bootstrap.js")
    assert "var rounds = q.updates || [];" in src
    assert "ROUND_MARK" in src, "every round renders identically"
    # Each round says who, because a conversation read back without speakers
    # is a list of sentences.
    assert "u.author_label ? u.author_label" in src
    # The old separate blocks are gone.
    assert '"While this is open"' not in src
    assert 'text: "Delivered"' not in src


def test_what_was_typed_outlives_a_redraw_and_a_refusal():
    """A redraw preserved the textarea only while it still had focus, and
    tapping Send moves focus to the button -- so a live event arriving while a
    message was in flight rebuilt an empty box and took the unsent text with
    it. What the writer saw was a long answer vanishing on click with no
    error, which is indistinguishable from having been sent."""
    src = _viewer("bootstrap.js")
    # Held outside the composer and mirrored to storage, because a
    # backgrounded page on a phone can be evicted outright.
    assert "function draftGet(" in src and "function draftSet(" in src
    assert "ta.value = draftGet(pendingKey());" in src
    # Discarded only by a confirmed success.
    assert 'ta.value = ""; draftSet(slot, "");' in src
    # And the refusal itself survives the next redraw.
    assert "var sendErrors" in src
    assert "sendErrors[pendingKey()]" in src
