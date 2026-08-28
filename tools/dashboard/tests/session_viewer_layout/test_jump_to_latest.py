import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
VIEWER_JS = ROOT / "tools/dashboard/static/js/pages/session-viewer.js"
RENDERER_JS = ROOT / "tools/dashboard/static/js/lib/session-renderer.js"
PAGE_HTML = ROOT / "tools/dashboard/templates/pages/session-view.html"
ENTRIES_HTML = ROOT / "tools/dashboard/templates/partials/session-entries.html"
BASE_HTML = ROOT / "tools/dashboard/templates/base.html"


def test_jump_to_latest_threshold_scope_and_action():
    script = r"""
const fs = require('fs');
let factory;
global.window = {};
global.document = {addEventListener: (_name, callback) => callback()};
global.Alpine = {
  data: (_name, callback) => { factory = callback; },
  store: () => ({test: {entries: [{}]}}),
};
eval(fs.readFileSync(process.argv[1], 'utf8'));  // real SessionRenderer
eval(fs.readFileSync(process.argv[2], 'utf8'));  // viewer component
const viewer = factory();
const scroller = {scrollHeight: 5000, scrollTop: 1000, clientHeight: 1000};
viewer.sessionKey = 'test';
viewer._mode = 'page';
viewer.showTerminal = false;

// Pre-ready: _updateJumpToBottom must bail BEFORE touching $refs — an
// early read poisons Alpine's cached $refs proxy for the whole component
// (the open-at-top regression, graph note efb90d5d-f5b).
let refsTouched = false;
Object.defineProperty(viewer, '$refs', {
  configurable: true,
  get() { refsTouched = true; return {entriesContainer: scroller}; },
});
viewer.state = 'loading';
viewer.showJumpToBottom = true;
viewer._updateJumpToBottom();
const preReadyHidden = !viewer.showJumpToBottom;
const preReadyRefsUntouched = !refsTouched;

viewer.state = 'ready';
viewer._updateJumpToBottom();
const exactThree = viewer.showJumpToBottom;
scroller.scrollTop = 999;
viewer._updateJumpToBottom();
const beyondThree = viewer.showJumpToBottom;
viewer.resumeScroll = function () {
  this.autoScroll = true;
  scroller.scrollTop = scroller.scrollHeight;
};
viewer.jumpToBottom();
process.stdout.write(JSON.stringify({
  preReadyHidden, preReadyRefsUntouched,
  exactThree, beyondThree,
  hiddenAfterJump: viewer.showJumpToBottom,
  autoScroll: viewer.autoScroll,
  scrollTop: scroller.scrollTop,
}));
"""
    result = subprocess.run(
        ["node", "-e", script, str(RENDERER_JS), str(VIEWER_JS)],
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == {
        "preReadyHidden": True,
        "preReadyRefsUntouched": True,
        "exactThree": False,
        "beyondThree": True,
        "hiddenAfterJump": False,
        "autoScroll": True,
        "scrollTop": 5000,
    }

    page = PAGE_HTML.read_text()
    entries = ENTRIES_HTML.read_text()
    css = BASE_HTML.read_text()
    assert 'data-testid="session-jump-to-bottom"' in page
    assert 'x-show="showJumpToBottom && !showTerminal"' in page
    assert '<div class="sv-transcript" x-show="!showTerminal">' in page
    assert "_mode !== 'page' && !autoScroll" in entries
    jump_css = css.split(".sv-jump-to-bottom {", 1)[1].split("}", 1)[0]
    assert "position: absolute" in jump_css
    assert "right: max(14px" in jump_css and "bottom: 14px" in jump_css
    assert "width: 44px" in jump_css and "height: 44px" in jump_css
    assert "border-radius: 9999px" in jump_css
    assert "opacity: 0.86" in jump_css
