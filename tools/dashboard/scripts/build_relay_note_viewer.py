#!/usr/bin/env python3
"""Generate the content-free relay note viewer from pinned dashboard vendors."""

from pathlib import Path


DASHBOARD = Path(__file__).resolve().parents[1]
VENDOR = DASHBOARD / "static" / "vendor"
VIEWER_DIR = DASHBOARD / "relay_viewer"
TEMPLATE = VIEWER_DIR / "note-viewer.template.html"
OUTPUT = VIEWER_DIR / "note-viewer.html"


def build() -> str:
    text = TEMPLATE.read_text()
    replacements = {
        "/*__MARKED_JS__*/": (VENDOR / "marked.min.js").read_text(),
        "/*__PURIFY_JS__*/": (VENDOR / "purify.min.js").read_text(),
        "/*__HIGHLIGHT_JS__*/": (
            VENDOR / "highlightjs" / "highlight.min.js"
        ).read_text(),
        "/*__HIGHLIGHT_CSS__*/": (
            VENDOR / "highlightjs" / "github-dark.min.css"
        ).read_text(),
    }
    for marker, value in replacements.items():
        if text.count(marker) != 1:
            raise RuntimeError(f"expected exactly one {marker}")
        text = text.replace(marker, value.replace("</script", "<\\/script"))
    return text


def main() -> None:
    OUTPUT.write_text(build())


if __name__ == "__main__":
    main()
