"""Guards for markdown table and document-viewer rules (operator, 2026-09-27).

Phone screenshots showed three failures these pin:
- tables forced to the column's width (`width: 100%`), so the browser
  squeezed every column to its longest word or overflowed anyway;
- a chat message's break-anywhere rule reaching table cells, so short
  columns collapsed to one character ("H / e / i / g / h / t");
- the attachment viewer drawing two close buttons, one under Save / Share.
"""

import re
from pathlib import Path

DASH = Path(__file__).resolve().parents[1]
BASE = (DASH / "templates" / "base.html").read_text()
CARDS = (DASH / "static" / "css" / "session-cards.css").read_text()
CHIPS = (DASH / "static" / "css" / "session-chips.css").read_text()
LIGHTBOX = (DASH / "templates" / "partials" / "session-lightbox.html").read_text()
MARKDOWN = (DASH / "static" / "js" / "markdown.js").read_text()


def _rule(css: str, selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"no rule for {selector}"
    return m.group(1)


def test_markdown_tables_are_not_forced_to_full_width():
    assert "width: 100%" not in _rule(BASE, ".markdown-body table")
    assert "width: 100%" not in _rule(CHIPS, ".sc-va-lightbox-md table")
    assert "width: auto" in _rule(CARDS, ".md-table .md-table-scroll > table")


def test_table_cells_break_only_between_words():
    cells = _rule(CARDS, ".md-table .md-table-scroll > table th,\n.md-table .md-table-scroll > table td")
    assert "overflow-wrap: normal" in cells and "word-break: normal" in cells
    assert "vertical-align: top" in cells


def test_renderer_sizes_every_table():
    assert "window.TableFit.attach(t)" in MARKDOWN
    assert BASE.index("js/lib/table-fit.js") < BASE.index("js/markdown.js")


def test_document_viewer_has_one_close_control():
    assert "lightbox-md-close" not in LIGHTBOX
    assert "sc-va-lightbox-md-close" not in CHIPS
    assert 'data-testid="lightbox-close"' in LIGHTBOX


def test_document_viewer_reserves_the_bar_row_and_safe_area():
    doc = _rule(CHIPS, ".sc-va-lightbox.sc-va-lightbox--doc")
    assert "env(safe-area-inset-top)" in doc and "48px" in doc
    assert "sc-va-lightbox--doc" in LIGHTBOX


def test_document_lists_show_markers():
    assert "list-style: disc" in _rule(CHIPS, ".sc-va-lightbox-md ul")
