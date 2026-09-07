"""Design Studio thumbnails without an LLM.

A design revision's thumbnail is rendered from its own HTML, not captured
from an operator's screen and not delegated to an agent:

1. ``wrap_variant_html`` rebuilds the document exactly the way the viewer's
   iframe does (Tailwind CDN build, Alpine, the parent page's inline styles,
   ``window.FIXTURE`` from the revision's fixture) so the render matches what
   the operator sees at ``/design/<revision>``.
2. ``capture`` renders it headlessly (``agent-browser``) at a desktop viewport
   and a phone viewport and records layout measurements from each.
3. ``classify`` decides the form factor from the pixels: a narrow, centered
   band of ink at desktop width is a phone mockup; horizontal overflow at
   phone width is a desktop-only layout; everything else is responsive.
4. ``compose`` builds the catalog thumbnail for that form factor.  Responsive
   designs get the phone render superimposed on the desktop render (or the
   side-by-side style — the raw captures are kept, so the style is a switch,
   not a re-render).

Artifacts live beside the legacy browser capture in
``DATA_ROOT/experiments/<revision_id>/``:

    desktop.png     raw desktop capture (1280x800)
    mobile.png      raw phone capture (390x844)
    thumbnail.jpg   the composed catalog thumbnail (1280x800)
    thumbnail.json  form factor, style, measurements, renderer, timestamp

``screenshot.png`` (the operator's in-browser capture, also what agents
receive when they ask for one) is never touched.  The catalog prefers
``thumbnail.jpg`` and falls back to ``screenshot.png``.

The queue at the bottom is deliberately small: one worker, one revision at a
time, de-duplicated, fed by revision creation and by a startup backfill of
every design whose latest revision has no thumbnail.  When ``agent-browser``
is not installed on the dashboard host the queue drains without rendering and
``status()`` says so, so the gallery can show *why* a tile is blank.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDOR_DIR = REPO_ROOT / "tools" / "dashboard" / "static" / "vendor"
BASE_TEMPLATE = REPO_ROOT / "tools" / "dashboard" / "templates" / "base.html"

DESKTOP_VIEWPORT = (1280, 800)
MOBILE_VIEWPORT = (390, 844)
THUMBNAIL_SIZE = (1280, 800)

FORM_FACTORS = ("desktop", "mobile", "both")
STYLES = ("overlay", "side", "desktop-only")
DEFAULT_STYLE = "overlay"

#: A phone mockup rendered at desktop width leaves most of the viewport as
#: background: its ink spans well under half the width, centered.
MOBILE_INK_FRACTION = 0.45
MOBILE_CENTER_TOLERANCE = 0.08
#: Pixel channel delta that counts as "not background".
INK_TOLERANCE = 18

AGENT_BROWSER_SESSION = "design-thumbnails"
AGENT_BROWSER_TIMEOUT = 90
SETTLE_MS = 1200

_MEASURE_JS = (
    "(function(){var de=document.documentElement,b=document.body;"
    "var kids=Array.from(b.children).filter(function(e){var r=e.getBoundingClientRect();"
    "return r.width>0&&r.height>0&&e.tagName!=='SCRIPT'&&e.tagName!=='STYLE'});"
    "var left=1e9,right=0;kids.forEach(function(e){var r=e.getBoundingClientRect();"
    "left=Math.min(left,r.left);right=Math.max(right,r.right)});"
    "if(!kids.length){left=0;right=0}"
    "return JSON.stringify({vw:innerWidth,vh:innerHeight,sw:de.scrollWidth,sh:de.scrollHeight,"
    "left:Math.round(left),right:Math.round(right),kids:kids.length})})()"
)


# ── Paths and metadata ────────────────────────────────────────────────


def _safe_revision_id(raw: str) -> str:
    rev_id = str(raw or "").strip()
    if not rev_id or rev_id in {".", ".."} or "/" in rev_id or "\\" in rev_id:
        return ""
    return rev_id


def revision_dir(revision_id: str) -> Path | None:
    rev_id = _safe_revision_id(revision_id)
    if not rev_id:
        return None
    from tools.data_paths import DATA_ROOT

    base = (DATA_ROOT / "experiments").resolve()
    candidate = (base / rev_id).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate


def read_meta(revision_id: str) -> dict | None:
    rev_dir = revision_dir(revision_id)
    if not rev_dir:
        return None
    path = rev_dir / "thumbnail.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def thumbnail_path(revision_id: str) -> Path | None:
    """The best catalog image for a revision: composed thumbnail, else the
    operator's browser capture, else None."""
    rev_dir = revision_dir(revision_id)
    if not rev_dir:
        return None
    for name in ("thumbnail.jpg", "screenshot.png"):
        candidate = rev_dir / name
        if candidate.is_file():
            return candidate
    return None


def renderer_available() -> bool:
    return shutil.which("agent-browser") is not None


def _renderer_version() -> str:
    try:
        out = subprocess.run(
            ["agent-browser", "--version"], capture_output=True, text=True, timeout=15,
        )
        return (out.stdout or out.stderr or "").strip()[:80]
    except (OSError, subprocess.SubprocessError):
        return ""


# ── 1. Wrap the variant the way the viewer does ───────────────────────


def _parent_css() -> str:
    try:
        base = BASE_TEMPLATE.read_text()
    except OSError:
        return ""
    match = re.search(r"<style>(.*?)</style>", base, re.S)
    return match.group(1) if match else ""


def _variant_for_thumbnail(design: dict) -> dict | None:
    """The viewer renders the LAST variant; mirror that so the thumbnail
    shows what the operator sees when they open the design."""
    variants = design.get("variants") or []
    return variants[-1] if variants else None


def wrap_variant_html(design: dict, vendor_href: str | None = None) -> str:
    """Rebuild the iframe document ``_injectIframe`` writes in page.js."""
    variant = _variant_for_thumbnail(design) or {}
    body = variant.get("html") or ""
    vendor = (vendor_href or VENDOR_DIR.as_uri()).rstrip("/")
    fixture_raw = design.get("fixture") or "{}"
    try:
        fixture = json.loads(fixture_raw)
    except (TypeError, ValueError):
        fixture = None
    if (
        isinstance(fixture, dict)
        and isinstance(fixture.get("states"), dict)
        and fixture["states"]
    ):
        states = fixture["states"]
        first = states[next(iter(states))]
        fixture_script = (
            "<script>window.FIXTURE=" + json.dumps(first)
            + ";window.FIXTURE_STATES=" + json.dumps(states) + ";</script>"
        )
    else:
        fixture_script = "<script>window.FIXTURE=" + fixture_raw + ";</script>"
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">"
        f"<script src=\"{vendor}/tailwind-browser-4.3.3.min.js\"></script>"
        f"<style>{_parent_css()}</style>"
        "<style>html,body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"
        "\"Segoe UI\",Roboto,sans-serif;background:#111827;color:#e5e7eb;"
        "overflow:auto !important;}</style>"
        + fixture_script
        + f"<script defer src=\"{vendor}/alpine-3.15.12.min.js\"></script>"
        "</head><body>" + body + "</body></html>"
    )


# ── 2. Headless capture ───────────────────────────────────────────────


def _agent_browser(*args: str, timeout: int = AGENT_BROWSER_TIMEOUT) -> str:
    cmd = ["agent-browser", "--session", AGENT_BROWSER_SESSION, *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(
            f"agent-browser {' '.join(args[:2])} failed: "
            f"{(proc.stderr or proc.stdout).strip()[:300]}"
        )
    return proc.stdout


def _parse_measure(raw: str) -> dict:
    text = (raw or "").strip()
    # eval prints the JSON string, sometimes quoted; take the outermost object.
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise RuntimeError(f"unreadable measurement: {text[:120]!r}")
    data = json.loads(text[start:end + 1].replace("\\\"", "\""))
    return {k: int(data.get(k) or 0) for k in ("vw", "vh", "sw", "sh", "left", "right", "kids")}


def capture(revision_id: str, design: dict, out_dir: Path) -> dict:
    """Render the wrapped variant at both viewports into ``out_dir`` and
    return the measurements.  Raises when the browser is unavailable."""
    if not renderer_available():
        raise RuntimeError("agent-browser is not installed on this host")
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="design-thumb-") as tmp:
        page = Path(tmp) / f"{revision_id}.html"
        page.write_text(wrap_variant_html(design), encoding="utf-8")
        _agent_browser("open", page.as_uri())
        measurements = {}
        for label, (width, height) in (("desktop", DESKTOP_VIEWPORT), ("mobile", MOBILE_VIEWPORT)):
            _agent_browser("set", "viewport", str(width), str(height))
            _agent_browser("wait", str(SETTLE_MS))
            measurements[label] = _parse_measure(_agent_browser("eval", _MEASURE_JS))
            _agent_browser("screenshot", str(out_dir / f"{label}.png"))
    return measurements


# ── 3. Form factor from the pixels ────────────────────────────────────


def ink_span(png: Path) -> tuple[int, int, int, int]:
    """(first_ink_column, last_ink_column, width, height) of a capture, where
    ink is any column whose pixels differ from the edge background colour."""
    from PIL import Image

    im = Image.open(png).convert("RGB")
    width, height = im.size
    px = im.load()
    edge = (
        [px[x, 0] for x in range(0, width, 4)]
        + [px[x, height - 1] for x in range(0, width, 4)]
        + [px[0, y] for y in range(0, height, 4)]
        + [px[width - 1, y] for y in range(0, height, 4)]
    )
    bg = Counter(edge).most_common(1)[0][0]

    def differs(c: tuple) -> bool:
        return max(abs(c[i] - bg[i]) for i in range(3)) > INK_TOLERANCE

    ink_cols = [
        x for x in range(width)
        if sum(1 for y in range(0, height, 3) if differs(px[x, y])) > 2
    ]
    if not ink_cols:
        return 0, width - 1, width, height
    return ink_cols[0], ink_cols[-1], width, height


def classify(desktop_png: Path, mobile_measure: dict) -> tuple[str, dict]:
    """Return (form_factor, evidence)."""
    left, right, width, _ = ink_span(desktop_png)
    fraction = (right - left + 1) / max(width, 1)
    centered = abs((left + right) / 2 - width / 2) < width * MOBILE_CENTER_TOLERANCE
    overflow = (
        int(mobile_measure.get("sw") or 0) > int(mobile_measure.get("vw") or 0) + 2
        or int(mobile_measure.get("right") or 0) > int(mobile_measure.get("vw") or 0) + 2
    )
    evidence = {
        "desktop_ink": [left, right],
        "desktop_ink_fraction": round(fraction, 3),
        "desktop_ink_centered": centered,
        "mobile_overflow": overflow,
    }
    if fraction < MOBILE_INK_FRACTION and centered:
        return "mobile", evidence
    if overflow:
        return "desktop", evidence
    return "both", evidence


# ── 4. Compose the catalog thumbnail ──────────────────────────────────


def _rounded(im, radius: int):
    from PIL import Image, ImageDraw

    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, im.size[0] - 1, im.size[1] - 1], radius, fill=255,
    )
    im = im.convert("RGBA")
    im.putalpha(mask)
    return im


def _phone_frame(mobile_png: Path, height: int):
    from PIL import Image, ImageDraw

    phone = Image.open(mobile_png).convert("RGB")
    phone = phone.resize(
        (round(MOBILE_VIEWPORT[0] * height / MOBILE_VIEWPORT[1]), height), Image.LANCZOS,
    )
    bezel, radius = 10, 34
    frame = Image.new("RGBA", (phone.width + 2 * bezel, phone.height + 2 * bezel), (0, 0, 0, 0))
    ImageDraw.Draw(frame).rounded_rectangle(
        [0, 0, frame.width - 1, frame.height - 1], radius + bezel,
        fill=(24, 26, 32, 255), outline=(70, 74, 86, 255), width=2,
    )
    rounded = _rounded(phone, radius)
    frame.paste(rounded, (bezel, bezel), rounded)
    return frame


def _with_shadow(base, layer, pos: tuple[int, int], blur: int = 28,
                 offset: tuple[int, int] = (0, 14), alpha: int = 170):
    from PIL import Image, ImageFilter

    shadow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    shadow.paste((0, 0, 0, alpha), (pos[0] + offset[0], pos[1] + offset[1]), layer.split()[3])
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    base = Image.alpha_composite(base, shadow)
    base.paste(layer, pos, layer)
    return base


def _stage(desktop_png: Path):
    from PIL import Image

    return Image.open(desktop_png).convert("RGBA").resize(THUMBNAIL_SIZE, Image.LANCZOS)


def compose_image(desktop_png: Path, mobile_png: Path, form_factor: str, style: str):
    """Return a PIL RGB image of THUMBNAIL_SIZE for the given inputs."""
    from PIL import Image, ImageFilter

    width, height = THUMBNAIL_SIZE
    if style not in STYLES:
        style = DEFAULT_STYLE
    if form_factor == "desktop" or style == "desktop-only" or not mobile_png.is_file():
        return _stage(desktop_png).convert("RGB")
    if form_factor == "mobile":
        backdrop = _stage(desktop_png).filter(ImageFilter.GaussianBlur(18))
        backdrop = Image.blend(backdrop, Image.new("RGBA", backdrop.size, (10, 12, 18, 255)), 0.55)
        phone = _phone_frame(mobile_png, 720)
        return _with_shadow(
            backdrop, phone, ((width - phone.width) // 2, (height - phone.height) // 2),
        ).convert("RGB")
    if style == "side":
        base = Image.new("RGBA", (width, height), (14, 16, 22, 255))
        desk = Image.open(desktop_png).convert("RGBA").resize((900, 562), Image.LANCZOS)
        base = _with_shadow(base, _rounded(desk, 14), (40, (height - 562) // 2))
        phone = _phone_frame(mobile_png, 600)
        return _with_shadow(base, phone, (width - phone.width - 56, (height - phone.height) // 2)).convert("RGB")
    # overlay: the phone bleeds off the bottom-right of the desktop render
    phone = _phone_frame(mobile_png, 520)
    return _with_shadow(
        _stage(desktop_png), phone, (width - phone.width - 36, height - phone.height + 40),
    ).convert("RGB")


def compose(revision_id: str, form_factor: str, style: str = DEFAULT_STYLE) -> Path:
    rev_dir = revision_dir(revision_id)
    if not rev_dir:
        raise ValueError("invalid revision id")
    image = compose_image(rev_dir / "desktop.png", rev_dir / "mobile.png", form_factor, style)
    out = rev_dir / "thumbnail.jpg"
    tmp = rev_dir / "thumbnail.jpg.tmp"
    image.save(tmp, "JPEG", quality=88, optimize=True)
    os.replace(tmp, out)
    return out


# ── Orchestration ─────────────────────────────────────────────────────


def render_revision(revision_id: str, *, style: str | None = None) -> dict:
    """Capture, classify, compose, and record ``thumbnail.json``.  Returns the
    metadata written.  Raises on any failure; the caller decides what to log."""
    from agents.design_db import get_design

    rev_id = _safe_revision_id(revision_id)
    rev_dir = revision_dir(rev_id)
    if not rev_id or not rev_dir:
        raise ValueError("invalid revision id")
    design = get_design(rev_id)
    if not design or not _variant_for_thumbnail(design):
        raise LookupError(f"revision {rev_id} has no renderable variant")
    style = style if style in STYLES else DEFAULT_STYLE
    started = time.monotonic()
    measurements = capture(rev_id, design, rev_dir)
    form_factor, evidence = classify(rev_dir / "desktop.png", measurements["mobile"])
    compose(rev_id, form_factor, style)
    meta = {
        "revision_id": rev_id,
        "design_id": design.get("design_id") or rev_id,
        "form_factor": form_factor,
        "style": style,
        "measurements": measurements,
        "evidence": evidence,
        "renderer": _renderer_version(),
        "rendered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_seconds": round(time.monotonic() - started, 1),
    }
    tmp = rev_dir / "thumbnail.json.tmp"
    tmp.write_text(json.dumps(meta, indent=2))
    os.replace(tmp, rev_dir / "thumbnail.json")
    return meta


def recompose_revision(revision_id: str, style: str) -> dict | None:
    """Switch the composite style without re-rendering.  None when the raw
    captures are missing (the revision was never rendered)."""
    meta = read_meta(revision_id)
    rev_dir = revision_dir(revision_id)
    if not meta or not rev_dir or not (rev_dir / "desktop.png").is_file():
        return None
    style = style if style in STYLES else DEFAULT_STYLE
    compose(revision_id, meta.get("form_factor") or "both", style)
    meta["style"] = style
    (rev_dir / "thumbnail.json").write_text(json.dumps(meta, indent=2))
    return meta


def latest_revisions() -> list[dict]:
    """One row per design series: its latest revision, newest series first."""
    from agents.design_db import _get_conn

    conn = _get_conn()
    try:
        rows = conn.execute("""\
            SELECT id, COALESCE(design_id, id) AS design_id,
                   COALESCE(revision_seq, 1) AS revision_seq, created_at, status
            FROM designs
            ORDER BY created_at DESC, revision_seq DESC
        """).fetchall()
    finally:
        conn.close()
    seen: set[str] = set()
    latest: list[dict] = []
    for row in rows:
        item = {k: row[k] for k in row.keys()}
        if item["design_id"] in seen:
            continue
        seen.add(item["design_id"])
        latest.append(item)
    return latest


def missing_revisions() -> list[str]:
    """Latest revisions with no composed thumbnail, newest design first."""
    return [
        row["id"] for row in latest_revisions()
        if read_meta(row["id"]) is None
    ]


# ── Queue: one worker, de-duplicated, fed by creation and backfill ────


class ThumbnailQueue:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] | None = None
        self._pending: set[str] = set()
        self._task: asyncio.Task | None = None
        self._current: str | None = None
        self._rendered = 0
        self._failed = 0
        self._last_error = ""
        self._unavailable_logged = False
        self.on_rendered = None  # async callback(meta) — set by the server

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._queue = asyncio.Queue()
        self._task = asyncio.create_task(self._run(), name="design-thumbnails")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    def enqueue(self, revision_id: str) -> bool:
        rev_id = _safe_revision_id(revision_id)
        if not rev_id or self._queue is None or rev_id in self._pending or rev_id == self._current:
            return False
        self._pending.add(rev_id)
        self._queue.put_nowait(rev_id)
        return True

    def enqueue_missing(self) -> int:
        try:
            missing = missing_revisions()
        except Exception:
            logger.exception("design-thumbnails: could not list revisions for backfill")
            return 0
        return sum(1 for rev_id in missing if self.enqueue(rev_id))

    def status(self) -> dict:
        return {
            "available": renderer_available(),
            "running": self._task is not None and not self._task.done(),
            "current": self._current,
            "pending": len(self._pending),
            "rendered": self._rendered,
            "failed": self._failed,
            "last_error": self._last_error,
        }

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            rev_id = await self._queue.get()
            self._pending.discard(rev_id)
            self._current = rev_id
            try:
                if not renderer_available():
                    if not self._unavailable_logged:
                        logger.warning(
                            "design-thumbnails: agent-browser is not installed on this "
                            "host; thumbnails will not render until it is"
                        )
                        self._unavailable_logged = True
                    self._failed += 1
                    self._last_error = "agent-browser is not installed on this host"
                    continue
                meta = await asyncio.to_thread(render_revision, rev_id)
                self._rendered += 1
                self._last_error = ""
                if self.on_rendered is not None:
                    try:
                        await self.on_rendered(meta)
                    except Exception:
                        logger.exception("design-thumbnails: on_rendered callback failed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._failed += 1
                self._last_error = f"{rev_id[:8]}: {exc}"[:300]
                logger.warning("design-thumbnails: render failed for %s: %s", rev_id, exc)
            finally:
                self._current = None
                self._queue.task_done()


queue = ThumbnailQueue()
