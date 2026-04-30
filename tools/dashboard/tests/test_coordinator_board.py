"""L1 unit tests for the coordinator-board plugin.

Covers manifest validation, schema payload validation, frontend helper
purity (via Node), and plugin discovery — see bead auto-1runm. The
behavioral sweep (browser-driven) tests live in
``test_behavioral_sweep.TestCoordinatorBoard``.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tools.dashboard.plugin_api import loader
from tools.dashboard.plugin_api.manifest import PluginManifest
from tools.dashboard.plugins.coordinator_board.entrypoints import api as coord_api
from tools.dashboard.plugins.coordinator_board.entrypoints import schemas as coord_schemas
from tools.graph.schemas.registry import SchemaValidationError


PLUGIN_DIR = (
    Path(__file__).resolve().parents[1] / "plugins" / "coordinator_board"
)


# ── Manifest ─────────────────────────────────────────────────────────


def test_manifest_yaml_parses_and_validates():
    """plugin.yaml round-trips through the substrate's PluginManifest."""
    manifest_path = PLUGIN_DIR / "plugin.yaml"
    raw = yaml.safe_load(manifest_path.read_text())
    manifest = PluginManifest.model_validate(raw)
    assert manifest.id == "coordinator-board"
    assert manifest.api_version == 1
    assert manifest.paths == ["/coordinator"]
    assert manifest.assets.template == "page.html"
    assert manifest.assets.script == "page.js"
    assert manifest.nav.label == "Coordinator"
    assert manifest.frontend.alpine_root == "coordinatorBoard"
    # Backend entrypoints declared so the substrate registers /api/coordinator/*
    assert manifest.entrypoints.api == (
        "tools.dashboard.plugins.coordinator_board.entrypoints.api:routes"
    )
    assert manifest.entrypoints.schemas == [
        "tools.dashboard.plugins.coordinator_board.entrypoints.schemas:CoordinatorCanvasV1",
        "tools.dashboard.plugins.coordinator_board.entrypoints.schemas:OperatorMessageToCoordinatorV1",
    ]


def test_plugin_discovers_with_real_substrate():
    """The shipped substrate finds the plugin directory."""
    discovered = loader.discover()
    by_id = {d.manifest.id: d for d in discovered}
    assert "coordinator-board" in by_id, (
        "loader.discover() did not pick up the coordinator-board plugin"
    )
    assert by_id["coordinator-board"].plugin_dir == PLUGIN_DIR


def test_load_all_resolves_routes_and_schemas():
    """``load_all`` resolves both the api routes list and the schema list."""
    loaded = loader.load_all()
    by_id = {p.id: p for p in loaded}
    plugin = by_id.get("coordinator-board")
    assert plugin is not None, (
        "load_all() did not include coordinator-board — entrypoint import failed?"
    )
    # /api/coordinator/board + /api/coordinator/message
    assert len(plugin.routes) == 2
    paths = sorted(r.path for r in plugin.routes)
    assert paths == ["/api/coordinator/board", "/api/coordinator/message"]
    # Both schemas surface in the resolved Setting registry
    assert len(plugin.schemas) == 2


def test_static_files_present():
    """Substrate-required assets exist alongside the manifest."""
    for fname in ("plugin.yaml", "page.html", "page.js"):
        assert (PLUGIN_DIR / fname).is_file(), f"missing plugin asset: {fname}"


# ── Setting schemas ──────────────────────────────────────────────────


class TestCoordinatorCanvasSchema:
    def _validate(self, payload):
        coord_schemas.CoordinatorCanvasV1.validate(payload)

    def test_minimum_payload_passes(self):
        self._validate({"question": "What now?"})

    def test_full_payload_passes(self):
        self._validate({
            "ageMin": 5,
            "question": "Direct-GitHub or capability-layer?",
            "context": "[auto-3nill](/bead/auto-3nill) wires direct…",
            "quickReplies": ["Sequence them", "Capability supersedes"],
        })

    def test_missing_question_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"ageMin": 1})

    def test_blank_question_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"question": "   "})

    def test_quick_replies_must_be_list_of_strings(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"question": "x", "quickReplies": "not a list"})
        with pytest.raises(SchemaValidationError):
            self._validate({"question": "x", "quickReplies": [1, 2, 3]})

    def test_unknown_field_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"question": "x", "unexpected": True})

    def test_age_min_must_be_number(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"question": "x", "ageMin": "huge"})


class TestOperatorMessageSchema:
    def _validate(self, payload):
        coord_schemas.OperatorMessageToCoordinatorV1.validate(payload)

    def test_minimum_payload_passes(self):
        self._validate({"text": "ok"})

    def test_full_payload_passes(self):
        self._validate({"text": "go for it", "sentAt": "2026-04-30T07:00:00Z"})

    def test_missing_text_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate({})

    def test_text_must_be_string(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"text": 42})

    def test_sent_at_must_be_string_or_null(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"text": "hi", "sentAt": 1234567890})

    def test_unknown_field_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"text": "hi", "extra": True})


# ── api.py defaults ──────────────────────────────────────────────────


def test_default_canvas_has_required_shape():
    canvas = coord_api._DEFAULT_CANVAS
    assert "question" in canvas and isinstance(canvas["question"], str)
    assert "context" in canvas and isinstance(canvas["context"], str)
    assert "quickReplies" in canvas and isinstance(canvas["quickReplies"], list)
    # Default must also pass validation — defends against drift between
    # the page's render-safe shape and the schema contract.
    coord_schemas.CoordinatorCanvasV1.validate(canvas)


def test_routes_export_two_methods():
    paths = {r.path: list(r.methods) for r in coord_api.routes if hasattr(r, "methods")}
    assert "/api/coordinator/board" in paths
    assert "GET" in (paths.get("/api/coordinator/board") or [])
    assert "/api/coordinator/message" in paths
    assert "POST" in (paths.get("/api/coordinator/message") or [])


# ── Frontend helpers (Node-driven purity check) ──────────────────────


def _node_available() -> bool:
    return shutil.which("node") is not None


@pytest.mark.skipif(not _node_available(), reason="node not installed")
class TestPageJsHelpers:
    """Smoke-test the pure helpers in page.js by importing them in Node.

    The page.js file is browser-shaped (uses ``window``), but the trailing
    CommonJS shim exports the ``coordinatorBoard`` factory so the helpers
    can be called from Node.
    """

    def _eval(self, snippet: str) -> dict:
        page_js = str(PLUGIN_DIR / "page.js")
        wrapper = (
            f"const {{ coordinatorBoard }} = require({json.dumps(page_js)}); "
            f"const c = coordinatorBoard(); "
            f"const out = (() => {{ {snippet} }})(); "
            f"process.stdout.write(JSON.stringify(out));"
        )
        proc = subprocess.run(
            ["node", "-e", wrapper],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"node eval failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
            )
        return json.loads(proc.stdout)

    def test_factory_exposes_alpine_component_shape(self):
        out = self._eval(
            "return { tab: c.tab, hasInit: typeof c.init === 'function', "
            "hasLoadBoard: typeof c.loadBoard === 'function', "
            "hasOnOperatorMessage: typeof c.onOperatorMessage === 'function', "
            "hasRenderInlineLinks: typeof c.renderInlineLinks === 'function', "
            "hasStatusBadge: typeof c.statusBadge === 'function', "
            "hasAgeStr: typeof c.ageStr === 'function' };"
        )
        assert out["tab"] == "primary"
        for k in ("hasInit", "hasLoadBoard", "hasOnOperatorMessage",
                  "hasRenderInlineLinks", "hasStatusBadge", "hasAgeStr"):
            assert out[k] is True, f"missing factory method: {k}"

    def test_render_inline_links(self):
        out = self._eval(
            "return { plain: c.renderInlineLinks('hello'), "
            "linked: c.renderInlineLinks('see [auto-x](/bead/auto-x) now'), "
            "escaped: c.renderInlineLinks('<script>alert(1)</script>'), "
            "empty: c.renderInlineLinks('') };"
        )
        assert out["plain"] == "hello"
        assert (
            '<a href="/bead/auto-x" target="_top">auto-x</a>' in out["linked"]
        )
        # XSS-flavour input must be escaped, not rendered as a tag.
        assert "<script>" not in out["escaped"]
        assert "&lt;script&gt;" in out["escaped"]
        assert out["empty"] == ""

    def test_age_str(self):
        out = self._eval(
            "return { zero: c.ageStr(0), tiny: c.ageStr(0.4), small: c.ageStr(7), "
            "hour: c.ageStr(125), day: c.ageStr(60 * 24) };"
        )
        assert out["zero"] == "just now"
        assert out["tiny"] == "just now"
        assert out["small"] == "7m ago"
        assert out["hour"] == "2h 5m ago"
        assert out["day"] == "24h ago"

    def test_status_badge_palette(self):
        out = self._eval(
            "return { shipping: c.statusBadge('shipping'), "
            "blocked: c.statusBadge('blocked'), "
            "designing: c.statusBadge('designing'), "
            "unknown: c.statusBadge('what') };"
        )
        assert "emerald" in out["shipping"]
        assert "amber" in out["blocked"]
        assert "violet" in out["designing"]
        # Unknown statuses fall through to a neutral slate badge.
        assert "slate" in out["unknown"]

    def test_normalize_payload_fills_defaults(self):
        out = self._eval(
            "return c._normalizePayload({});"
        )
        assert out["canvas"]["question"] == ""
        assert out["canvas"]["quickReplies"] == []
        assert out["operatorMessage"]["text"] == ""
        assert out["tiles"] == []

    def test_win_detection_logic(self):
        # Re-create the verbatim-vs-edited check that drives celebrateWin.
        out = self._eval(
            "c.data = c._normalizePayload({"
            " canvas: { question: 'q', context: '', "
            "           quickReplies: ['Yes, do it', 'No, hold'] } "
            "}); "
            "const replies = (c.data.canvas.quickReplies || []).map(r => r.trim()); "
            "return { verbatim: replies.includes('Yes, do it'), "
            "         edited:   replies.includes('Yes, do it now') };"
        )
        assert out["verbatim"] is True
        assert out["edited"] is False
