"""Initial-load bottom pin survives a poisoned Alpine $refs proxy.

Regression contract for the open-at-top bug (graph note efb90d5d-f5b):
Alpine's $refs magic caches an empty proxy per evaluation context if it is
first read before any x-ref directive has initialized. When that happened,
_scrollToBottom's ref lookup returned undefined and the pin silently
no-oped, so clicking into a session rendered at scrollTop=0.

Locks in the two defenses:
  1. _entriesEl() falls back to $root.querySelector('.sv-entries') when
     $refs comes back empty.
  2. _scrollToBottom retries across frames while the container is still
     rendering, then pins and records the programmatic baseline.
"""
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
VIEWER_JS = ROOT / "tools/dashboard/static/js/pages/session-viewer.js"
RENDERER_JS = ROOT / "tools/dashboard/static/js/lib/session-renderer.js"

HARNESS = r"""
const fs = require('fs');
let factory;
global.window = {};
global.document = {addEventListener: (_name, callback) => callback()};
global.Alpine = {
  data: (_name, callback) => { factory = callback; },
  store: () => ({test: {entries: [{}]}}),
};
global.requestAnimationFrame = (cb) => cb();  // synchronous frames
const warnings = [];
console.warn = (...args) => warnings.push(args.join(' '));
eval(fs.readFileSync(process.argv[1], 'utf8'));  // real SessionRenderer
eval(fs.readFileSync(process.argv[2], 'utf8'));  // viewer component

function makeViewer() {
  const viewer = factory();
  viewer.sessionKey = 'test';
  viewer._mode = 'page';
  viewer.showTerminal = false;
  viewer.state = 'ready';
  viewer.$nextTick = (fn) => fn();
  return viewer;
}

const out = {};

// 1. Poisoned $refs (empty proxy) → _entriesEl falls back to $root.
{
  const viewer = makeViewer();
  const scroller = {scrollHeight: 5000, scrollTop: 0, clientHeight: 1000};
  viewer.$refs = {};  // what a poisoned cached proxy looks like to readers
  viewer.$root = {querySelector: (sel) => (sel === '.sv-entries' ? scroller : null)};
  out.fallbackResolves = viewer._entriesEl() === scroller;
  viewer.showJumpToBottom = true;
  viewer._scrollToBottom();
  out.pinnedViaFallback = scroller.scrollTop === 5000;
  out.baselineRecorded = viewer._lastScrollTop === 5000;
  out.jumpHiddenAfterPin = viewer.showJumpToBottom === false;
}

// 2. Container appears a few frames late → the pin retries and lands.
{
  const viewer = makeViewer();
  const scroller = {scrollHeight: 3000, scrollTop: 0, clientHeight: 1000};
  let frames = 0;
  viewer.$refs = {};
  viewer.$root = {querySelector: () => (++frames >= 4 ? scroller : null)};
  viewer._scrollToBottom();
  out.pinnedAfterRetry = scroller.scrollTop === 3000;
}

// 3. Container never appears → bounded retry, loud warning, no throw.
{
  const viewer = makeViewer();
  viewer.$refs = {};
  viewer.$root = {querySelector: () => null};
  warnings.length = 0;
  viewer._scrollToBottom();
  out.warnedWhenMissing = warnings.some((w) => w.includes('scroll-to-bottom'));
}

// 4. Renderer latch functions also survive a poisoned $refs.
{
  const r = window.SessionRenderer;
  const scroller = {scrollHeight: 1000, scrollTop: 795, clientHeight: 200};
  const ctx = {
    autoScroll: false,
    _lastScrollTop: 300,
    $refs: {},
    $root: {querySelector: (sel) => (sel === '.sv-entries' ? scroller : null)},
  };
  r.onScroll.call(ctx);
  out.latchRelatchesViaFallback = ctx.autoScroll === true;
  ctx.autoScroll = false;
  r.resumeScroll.call(ctx);
  out.resumeScrollsViaFallback = scroller.scrollTop === 1000 && ctx.autoScroll === true;
}

process.stdout.write(JSON.stringify(out));
"""


def test_initial_scroll_pin_survives_poisoned_refs():
    result = subprocess.run(
        ["node", "-e", HARNESS, str(RENDERER_JS), str(VIEWER_JS)],
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == {
        "fallbackResolves": True,
        "pinnedViaFallback": True,
        "baselineRecorded": True,
        "jumpHiddenAfterPin": True,
        "pinnedAfterRetry": True,
        "warnedWhenMissing": True,
        "latchRelatchesViaFallback": True,
        "resumeScrollsViaFallback": True,
    }
