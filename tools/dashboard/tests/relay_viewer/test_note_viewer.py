"""Relay note viewer artifact generation and content-free contract."""

from pathlib import Path

from tools.dashboard.scripts import build_relay_note_viewer


VIEWER = Path(__file__).resolve().parents[2] / "relay_viewer" / "note-viewer.html"


def test_generated_viewer_is_current():
    assert VIEWER.read_text() == build_relay_note_viewer.build()


def test_viewer_is_self_contained_and_content_free():
    text = VIEWER.read_text()
    assert "/*__MARKED_JS__*/" not in text
    assert "/*__PURIFY_JS__*/" not in text
    assert "/*__HIGHLIGHT_JS__*/" not in text
    assert '<script src=' not in text
    assert '<link rel="stylesheet"' not in text
    assert "marked.parse" in text
    assert "DOMPurify.sanitize" in text
    assert "hljs.highlightElement" in text
    assert "event.source === parent" in text
    assert "navigateTo" not in text
    assert "/api/resolve" not in text


def test_viewer_sanitizes_before_highlighting_and_uses_blob_parts():
    text = VIEWER.read_text()
    sanitize_at = text.index("DOMPurify.sanitize(marked.parse")
    highlight_at = text.index("hljs.highlightElement", sanitize_at)
    assert sanitize_at < highlight_at
    assert "img.getAttribute('src')" in text
    assert "new Blob([part.bytes], {type: part.mime})" in text
    assert "Image unavailable" in text
    assert "noopener noreferrer" in text
    assert "anchor.removeAttribute('href')" in text
