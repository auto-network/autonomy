"""Unit tests for the LLM-free Design Studio thumbnail renderer.

Everything here runs without a browser: the capture step is the only part
that needs ``agent-browser`` and it is exercised by the smoke path, not by
these tests.  Classification and composition are tested on synthetic PNGs.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from tools.dashboard import design_thumbnails as dt


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    from tools import data_paths

    monkeypatch.setattr(data_paths, "DATA_ROOT", tmp_path)
    (tmp_path / "experiments").mkdir()
    return tmp_path


def _png(path: Path, size: tuple[int, int], ink: tuple[int, int, int, int] | None,
         bg=(17, 24, 39), fg=(226, 232, 240)) -> Path:
    im = Image.new("RGB", size, bg)
    if ink:
        ImageDraw.Draw(im).rectangle(list(ink), fill=fg)
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)
    return path


# ── wrap_variant_html ─────────────────────────────────────────────────


def test_wrap_mirrors_the_viewer_iframe_document():
    design = {
        "fixture": json.dumps({"states": {"Empty": {"count": 0}, "Busy": {"count": 9}}}),
        "variants": [
            {"id": "a", "html": "<main>first</main>"},
            {"id": "b", "html": "<main data-testid='last'>last</main>"},
        ],
    }
    html = dt.wrap_variant_html(design, vendor_href="file:///vendor")
    assert "file:///vendor/tailwind-browser-4.3.3.min.js" in html
    assert "file:///vendor/alpine-3.15.12.min.js" in html
    # The viewer renders the LAST variant and seeds FIXTURE with the first state.
    assert "data-testid='last'" in html and "<main>first</main>" not in html
    assert 'window.FIXTURE={"count": 0}' in html
    assert "window.FIXTURE_STATES=" in html


def test_wrap_without_states_passes_fixture_through():
    html = dt.wrap_variant_html({"fixture": '{"title": "x"}', "variants": [{"html": "<p>hi</p>"}]})
    assert 'window.FIXTURE={"title": "x"};' in html
    assert "FIXTURE_STATES" not in html
    assert dt.VENDOR_DIR.as_uri() in html


# ── classify ──────────────────────────────────────────────────────────


def test_classify_full_width_content_that_fits_a_phone_is_both(tmp_path):
    desktop = _png(tmp_path / "desktop.png", dt.DESKTOP_VIEWPORT, (0, 0, 1279, 400))
    form, evidence = dt.classify(desktop, {"vw": 390, "sw": 390, "right": 390})
    assert form == "both"
    assert evidence["desktop_ink_fraction"] == 1.0
    assert evidence["mobile_overflow"] is False


def test_classify_narrow_centered_ink_is_a_phone_mockup(tmp_path):
    desktop = _png(tmp_path / "desktop.png", dt.DESKTOP_VIEWPORT, (445, 40, 834, 760))
    form, evidence = dt.classify(desktop, {"vw": 390, "sw": 390, "right": 390})
    assert form == "mobile"
    assert evidence["desktop_ink_centered"] is True
    assert evidence["desktop_ink_fraction"] < dt.MOBILE_INK_FRACTION


def test_classify_phone_overflow_is_desktop_only(tmp_path):
    desktop = _png(tmp_path / "desktop.png", dt.DESKTOP_VIEWPORT, (40, 0, 1239, 700))
    form, evidence = dt.classify(desktop, {"vw": 390, "sw": 390, "right": 961})
    assert form == "desktop"
    assert evidence["mobile_overflow"] is True


def test_classify_narrow_but_off_center_ink_is_not_a_phone(tmp_path):
    # A sidebar-only render: narrow ink hugging the left edge is a desktop shell.
    desktop = _png(tmp_path / "desktop.png", dt.DESKTOP_VIEWPORT, (16, 0, 567, 800))
    form, _ = dt.classify(desktop, {"vw": 390, "sw": 390, "right": 390})
    assert form == "both"


# ── compose ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("form_factor", dt.FORM_FACTORS)
@pytest.mark.parametrize("style", dt.STYLES)
def test_compose_always_yields_a_thumbnail_sized_image(tmp_path, form_factor, style):
    desktop = _png(tmp_path / "desktop.png", dt.DESKTOP_VIEWPORT, (0, 0, 1279, 400))
    mobile = _png(tmp_path / "mobile.png", dt.MOBILE_VIEWPORT, (0, 0, 389, 300), fg=(99, 102, 241))
    image = dt.compose_image(desktop, mobile, form_factor, style)
    assert image.size == dt.THUMBNAIL_SIZE
    assert image.mode == "RGB"


def test_overlay_places_the_phone_render_bottom_right(tmp_path):
    desktop = _png(tmp_path / "desktop.png", dt.DESKTOP_VIEWPORT, None)
    mobile = _png(tmp_path / "mobile.png", dt.MOBILE_VIEWPORT, (0, 0, 389, 843), fg=(255, 0, 0))
    image = dt.compose_image(desktop, mobile, "both", "overlay")
    assert image.getpixel((1150, 700))[0] > 200          # phone (red) bottom-right
    assert image.getpixel((100, 100)) == (17, 24, 39)     # desktop background untouched


def test_desktop_only_style_ignores_the_phone_render(tmp_path):
    desktop = _png(tmp_path / "desktop.png", dt.DESKTOP_VIEWPORT, None)
    mobile = _png(tmp_path / "mobile.png", dt.MOBILE_VIEWPORT, (0, 0, 389, 843), fg=(255, 0, 0))
    image = dt.compose_image(desktop, mobile, "both", "desktop-only")
    assert image.getpixel((1150, 700)) == (17, 24, 39)


def test_compose_and_recompose_write_beside_the_raw_captures(data_root):
    rev = "rev-1"
    rev_dir = data_root / "experiments" / rev
    _png(rev_dir / "desktop.png", dt.DESKTOP_VIEWPORT, (0, 0, 1279, 400))
    _png(rev_dir / "mobile.png", dt.MOBILE_VIEWPORT, (0, 0, 389, 300))
    out = dt.compose(rev, "both", "overlay")
    assert out == rev_dir / "thumbnail.jpg" and out.is_file()
    assert dt.thumbnail_path(rev) == out
    assert dt.read_meta(rev) is None
    assert dt.recompose_revision(rev, "side") is None   # never rendered: no meta

    (rev_dir / "thumbnail.json").write_text(json.dumps({"form_factor": "both", "style": "overlay"}))
    meta = dt.recompose_revision(rev, "side")
    assert meta["style"] == "side"
    assert json.loads((rev_dir / "thumbnail.json").read_text())["style"] == "side"


def test_thumbnail_path_falls_back_to_the_browser_capture(data_root):
    rev_dir = data_root / "experiments" / "rev-2"
    _png(rev_dir / "screenshot.png", (10, 10), None)
    assert dt.thumbnail_path("rev-2") == rev_dir / "screenshot.png"
    assert dt.thumbnail_path("../rev-2") is None
    assert dt.thumbnail_path("") is None


# ── backfill selection ────────────────────────────────────────────────


def test_missing_revisions_picks_each_design_latest_revision(data_root, monkeypatch):
    from agents import design_db

    monkeypatch.setattr(design_db, "DB_PATH", data_root / "experiments.db")
    monkeypatch.setattr(design_db, "_initialized", False)
    first = design_db.create_design(title="A", description="", fixture=None,
                                    variants=[{"id": "v", "html": "<p>a1</p>"}])
    second = design_db.create_design(title="A", description="", fixture=None,
                                     variants=[{"id": "v", "html": "<p>a2</p>"}], design_id=first)
    other = design_db.create_design(title="B", description="", fixture=None,
                                    variants=[{"id": "v", "html": "<p>b</p>"}])
    assert set(dt.missing_revisions()) == {second, other}
    rev_dir = data_root / "experiments" / other
    rev_dir.mkdir(parents=True)
    (rev_dir / "thumbnail.json").write_text("{}")
    assert dt.missing_revisions() == [second]


# ── queue ─────────────────────────────────────────────────────────────


def test_queue_dedupes_and_reports_an_unavailable_renderer(monkeypatch):
    monkeypatch.setattr(dt, "renderer_available", lambda: False)

    async def scenario():
        q = dt.ThumbnailQueue()
        assert q.enqueue("rev-x") is False          # not started yet
        q.start()
        assert q.enqueue("rev-x") is True
        assert q.enqueue("rev-x") is False          # already pending
        assert q.enqueue("../nope") is False
        await q._queue.join()
        status = q.status()
        await q.stop()
        return status

    status = asyncio.run(scenario())
    assert status["available"] is False
    assert status["failed"] == 1 and status["rendered"] == 0
    assert "not installed" in status["last_error"]


def test_queue_renders_and_notifies(monkeypatch):
    monkeypatch.setattr(dt, "renderer_available", lambda: True)
    rendered = []
    monkeypatch.setattr(dt, "render_revision", lambda rev_id: {"revision_id": rev_id, "form_factor": "both"})

    async def scenario():
        q = dt.ThumbnailQueue()
        q.start()

        async def on_rendered(meta):
            rendered.append(meta["revision_id"])

        q.on_rendered = on_rendered
        q.enqueue("rev-a")
        q.enqueue("rev-b")
        await q._queue.join()
        status = q.status()
        await q.stop()
        return status

    status = asyncio.run(scenario())
    assert rendered == ["rev-a", "rev-b"]
    assert status["rendered"] == 2 and status["failed"] == 0 and status["pending"] == 0


# ── artifacts rendered elsewhere ──────────────────────────────────────


def _jpeg_bytes() -> bytes:
    import io

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (1, 2, 3)).save(buf, "JPEG")
    return buf.getvalue()


def _png_bytes() -> bytes:
    import io

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (1, 2, 3)).save(buf, "PNG")
    return buf.getvalue()


def test_write_artifacts_stores_images_and_meta(data_root):
    stored = dt.write_artifacts(
        "rev-remote",
        {"thumbnail.jpg": _jpeg_bytes(), "desktop.png": _png_bytes(), "mobile.png": _png_bytes()},
        {"form_factor": "mobile", "design_id": "design-r", "style": "overlay", "renderer": "agent-browser 0.27"},
    )
    rev_dir = data_root / "experiments" / "rev-remote"
    assert (rev_dir / "thumbnail.jpg").read_bytes() == _jpeg_bytes()
    assert (rev_dir / "desktop.png").is_file() and (rev_dir / "mobile.png").is_file()
    meta = dt.read_meta("rev-remote")
    assert meta["form_factor"] == "mobile" and meta["design_id"] == "design-r" and meta["uploaded"] is True
    assert stored["renderer"] == "agent-browser 0.27"
    assert dt.thumbnail_path("rev-remote") == rev_dir / "thumbnail.jpg"


@pytest.mark.parametrize("files, meta, message", [
    ({"desktop.png": b"\x89PNG..."}, {"form_factor": "both"}, "thumbnail.jpg is required"),
    ({"thumbnail.jpg": b"\xff\xd8\xff.."}, {"form_factor": "tablet"}, "form_factor"),
    ({"thumbnail.jpg": b"\xff\xd8\xff..", "evil.sh": b"#!"}, {"form_factor": "both"}, "unknown artifact"),
    ({"thumbnail.jpg": b"not a jpeg"}, {"form_factor": "both"}, "not the expected image type"),
])
def test_write_artifacts_rejects_bad_input(data_root, files, meta, message):
    with pytest.raises(ValueError, match=message):
        dt.write_artifacts("rev-bad", files, meta)
    assert not (data_root / "experiments" / "rev-bad").exists()


def test_write_artifacts_refuses_path_traversal(data_root):
    with pytest.raises(ValueError):
        dt.write_artifacts("../escape", {"thumbnail.jpg": _jpeg_bytes()}, {"form_factor": "both"})
