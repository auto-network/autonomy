import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
VIEWER_JS = ROOT / "tools/dashboard/static/js/pages/session-viewer.js"
PAGE_HTML = ROOT / "tools/dashboard/templates/pages/session-view.html"
ENTRIES_HTML = ROOT / "tools/dashboard/templates/partials/session-entries.html"
BASE_HTML = ROOT / "tools/dashboard/templates/base.html"


def test_jump_to_latest_threshold_scope_and_action():
    script = r"""
const fs = require('fs');
let factory;
global.window = {SessionRenderer: {}};
global.document = {addEventListener: (_name, callback) => callback()};
global.Alpine = {
  data: (_name, callback) => { factory = callback; },
  store: () => ({test: {entries: [{}]}}),
};
eval(fs.readFileSync(process.argv[1], 'utf8'));
const viewer = factory();
const scroller = {scrollHeight: 5000, scrollTop: 1000, clientHeight: 1000};
viewer.sessionKey = 'test';
viewer.$refs = {entriesContainer: scroller};
viewer._mode = 'page';
viewer.showTerminal = false;
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
  exactThree, beyondThree,
  hiddenAfterJump: viewer.showJumpToBottom,
  autoScroll: viewer.autoScroll,
  scrollTop: scroller.scrollTop,
}));
"""
    result = subprocess.run(
        ["node", "-e", script, str(VIEWER_JS)],
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == {
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
    assert "_mode !== 'page' && !autoScroll" in entries
    jump_css = css.split(".sv-jump-to-bottom {", 1)[1].split("}", 1)[0]
    assert "grid-row: 2" in jump_css
    assert "width: 44px" in jump_css and "height: 44px" in jump_css
    assert "border-radius: 9999px" in jump_css
