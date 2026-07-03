"""Design Studio librarian helpers.

This module is intentionally plugin-local.  The generic plugin substrate owns
installing the graph Setting that advertises the librarian action; this code is
the Design-specific worker that action can run.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "svg"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "svg"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = " ".join(str(data or "").split())
        if text:
            self.parts.append(text)


@dataclass(frozen=True)
class LibrarianResult:
    revision_id: str
    screenshot_path: str
    screenshot_written: bool
    description_before: str
    description_after: str
    description_updated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "screenshot_path": self.screenshot_path,
            "screenshot_written": self.screenshot_written,
            "description_before": self.description_before,
            "description_after": self.description_after,
            "description_updated": self.description_updated,
        }


def _repo_root() -> Path:
    override = os.environ.get("DESIGN_STUDIO_REPO_ROOT")
    if override:
        return Path(override).resolve()
    from agents.design_db import REPO_ROOT

    return REPO_ROOT


def _screenshot_path(revision_id: str) -> Path:
    return _repo_root() / "data" / "experiments" / revision_id / "screenshot.png"


def _select_variant(design: dict[str, Any]) -> dict[str, Any] | None:
    variants = list(design.get("variants") or [])
    if not variants:
        return None
    selected = [
        v for v in variants
        if v.get("selected") and v.get("rank") is not None
    ]
    if selected:
        return sorted(selected, key=lambda v: int(v.get("rank") or 999))[0]
    selected_any = [v for v in variants if v.get("selected")]
    if selected_any:
        return selected_any[0]
    return variants[-1]


def _fixture_head(fixture_raw: str | None) -> tuple[str, str]:
    fixture_raw = fixture_raw or "{}"
    try:
        fixture = json.loads(fixture_raw)
    except Exception:
        fixture = {}
    if (
        isinstance(fixture, dict)
        and isinstance(fixture.get("states"), dict)
        and fixture["states"]
    ):
        keys = list(fixture["states"].keys())
        first = fixture["states"][keys[0]]
        return (
            "<script>"
            f"window.FIXTURE = {json.dumps(first)};"
            f"window.FIXTURE_STATES = {json.dumps(fixture['states'])};"
            "</script>",
            "States: " + ", ".join(str(k) for k in keys[:4]),
        )
    return f"<script>window.FIXTURE = {fixture_raw};</script>", ""


def render_html_document(design: dict[str, Any]) -> str:
    """Return a standalone HTML document for the latest/selected design variant."""
    variant = _select_variant(design)
    if not variant:
        raise ValueError(f"design {design.get('id') or '<unknown>'} has no variants")
    fixture_head, _ = _fixture_head(design.get("fixture"))
    body = str(variant.get("html") or "")
    title = html.escape(str(design.get("title") or "Design Studio preview"))
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">"
        f"<title>{title}</title>"
        "<script src=\"https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4\"></script>"
        "<script defer src=\"https://cdn.jsdelivr.net/npm/alpinejs@3/dist/cdn.min.js\"></script>"
        "<style>"
        "html,body{margin:0;min-height:100%;font-family:-apple-system,BlinkMacSystemFont,"
        "\"Segoe UI\",Roboto,sans-serif;background:#111827;color:#e5e7eb;overflow:auto;}"
        "body{width:100%;}"
        "</style>"
        f"{fixture_head}"
        "</head><body>"
        f"{body}"
        "</body></html>"
    )


def summarize_design(design: dict[str, Any], *, max_length: int = 180) -> str:
    variant = _select_variant(design)
    raw_html = str((variant or {}).get("html") or "")
    parser = _TextExtractor()
    parser.feed(raw_html)
    text = " ".join(parser.parts)
    text = re.sub(r"\s+", " ", text).strip()
    _, fixture_hint = _fixture_head(design.get("fixture"))
    title = str(design.get("title") or "Untitled design").strip()
    if fixture_hint and text:
        summary = f"{fixture_hint}. {text}"
    elif text:
        summary = text
    else:
        summary = f"Design preview for {title}."
    summary = summary.strip(" .")
    if len(summary) > max_length:
        summary = summary[: max_length - 1].rsplit(" ", 1)[0].rstrip(" ,.;:") + "."
    return summary


def _description_is_stale(value: str | None) -> bool:
    text = (value or "").strip()
    if len(text) < 8:
        return True
    return text.lower() in {
        "todo",
        "tbd",
        "placeholder",
        "no description",
        "none",
    }


def _update_description(revision_id: str, description: str, *, force: bool = False) -> tuple[str, str, bool]:
    from agents.design_db import _get_conn

    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT description FROM designs WHERE id = ?",
            (revision_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"design revision not found: {revision_id}")
        before = str(row["description"] or "")
        if not force and not _description_is_stale(before):
            return before, before, False
        conn.execute(
            "UPDATE designs SET description = ? WHERE id = ?",
            (description, revision_id),
        )
        conn.commit()
        return before, description, before != description
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def _capture_with_agent_browser(html_path: Path, screenshot_path: Path, *, viewport: str) -> None:
    width_raw, _, height_raw = viewport.partition("x")
    width = int(width_raw or "960")
    height = int(height_raw or "720")
    session = f"design-librarian-{int(time.time() * 1000)}"
    url = html_path.resolve().as_uri()
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "agent-browser",
            "--session",
            session,
            "--allow-file-access",
            "batch",
            "--bail",
            f"set viewport {width} {height}",
            f"open {url}",
            "wait 1500",
            f"screenshot {screenshot_path}",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )
    subprocess.run(
        ["agent-browser", "--session", session, "close"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def run_librarian(
    revision_id: str,
    *,
    force_description: bool = False,
    viewport: str = "960x720",
    capture: bool = True,
) -> LibrarianResult:
    from agents.design_db import get_design

    design = get_design(revision_id)
    if not design:
        raise ValueError(f"design revision not found: {revision_id}")

    screenshot_path = _screenshot_path(revision_id)
    screenshot_written = False
    if capture:
        document = render_html_document(design)
        with tempfile.TemporaryDirectory(prefix="design-librarian-") as tmp:
            html_path = Path(tmp) / "preview.html"
            html_path.write_text(document, encoding="utf-8")
            _capture_with_agent_browser(html_path, screenshot_path, viewport=viewport)
        screenshot_written = screenshot_path.is_file() and screenshot_path.stat().st_size > 0

    summary = summarize_design(design)
    before, after, updated = _update_description(
        revision_id,
        summary,
        force=force_description,
    )
    return LibrarianResult(
        revision_id=revision_id,
        screenshot_path=str(screenshot_path),
        screenshot_written=screenshot_written,
        description_before=before,
        description_after=after,
        description_updated=updated,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh Design Studio thumbnail and metadata")
    parser.add_argument("revision_id", help="Design revision id to refresh")
    parser.add_argument("--force-description", action="store_true")
    parser.add_argument("--viewport", default="960x720", help="Screenshot viewport, e.g. 960x720")
    parser.add_argument("--no-capture", action="store_true", help="Only refresh metadata")
    args = parser.parse_args(argv)
    result = run_librarian(
        args.revision_id,
        force_description=args.force_description,
        viewport=args.viewport,
        capture=not args.no_capture,
    )
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
