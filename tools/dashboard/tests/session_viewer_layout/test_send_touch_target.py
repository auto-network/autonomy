from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
PAGE_HTML = ROOT / "tools/dashboard/templates/pages/session-view.html"
BASE_HTML = ROOT / "tools/dashboard/templates/base.html"


def _rule(css, selector):
    return css.split(selector + " {", 1)[1].split("}", 1)[0]


def test_send_uses_native_click_with_44px_touch_target():
    page = PAGE_HTML.read_text()
    button = page.split('<button type="button"\n                      class="sv-send"', 1)[1].split("</button>", 1)[0]
    assert '@click="sendMessage()"' in button
    assert "ontouch" not in button
    assert 'aria-label="Send message"' in button

    css = BASE_HTML.read_text()
    assert "overflow: visible" in _rule(css, ".sv-input-bubble")
    send = _rule(css, ".sv-send")
    assert "z-index: 1" in send
    assert "touch-action: manipulation" in send
    target = _rule(css, ".sv-send::before")
    assert "inset: -7px -6px" in target
    assert "pointer-events: auto" in target
