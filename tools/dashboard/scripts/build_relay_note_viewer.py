#!/usr/bin/env python3
"""Generate the content-free relay note viewer from pinned dashboard vendors."""

from pathlib import Path


DASHBOARD = Path(__file__).resolve().parents[1]
VENDOR = DASHBOARD / "static" / "vendor"
VIEWER_DIR = DASHBOARD / "relay_viewer"
TEMPLATE = VIEWER_DIR / "note-viewer.template.html"
OUTPUT = VIEWER_DIR / ".build" / "note-viewer.html"


#: Marker -> vendored asset. The viewer travels inside the artifact bytes
#: over the channel, where it has no origin and can fetch nothing, so the
#: parser, sanitizer and highlighter must physically be in the one file.
VENDORS = {
    "/*__MARKED_JS__*/": VENDOR / "marked-15.0.12.min.js",
    "/*__PURIFY_JS__*/": VENDOR / "purify-3.4.12.min.js",
    "/*__HIGHLIGHT_JS__*/": VENDOR / "highlightjs" / "highlight-11.11.1.min.js",
    "/*__HIGHLIGHT_CSS__*/": VENDOR / "highlightjs" / "github-dark-11.11.1.min.css",
}


def vendor_files() -> list[Path]:
    return list(VENDORS.values())


def build() -> str:
    text = TEMPLATE.read_text()
    replacements = {
        marker: path.read_text() for marker, path in VENDORS.items()
    }
    for marker, value in replacements.items():
        if text.count(marker) != 1:
            raise RuntimeError(f"expected exactly one {marker}")
        text = text.replace(marker, value.replace("</script", "<\\/script"))
    return text


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(build())


if __name__ == "__main__":
    main()
