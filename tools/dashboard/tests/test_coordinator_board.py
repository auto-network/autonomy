"""L1 unit tests for the coordinator-board plugin.

Covers manifest validation, schema payload validation, frontend helper
purity (via Node), and plugin discovery. Behavioral sweep
(browser-driven) tests live in
``test_behavioral_sweep.TestCoordinatorBoard`` /
``TestCoordinatorBoardSettingsWiring``.

Bead history:
* auto-1runm — initial L1 fixtures (canvas + operator-message schemas,
  ``api.py`` facade routes).
* auto-lffg5 — facade retired; three new Setting schemas
  (``CoordinatorTileV1``, ``CoordinatorThreadV1``,
  ``CoordinatorDecisionV1``) replace the facade's payload synthesis.
* auto-y98f2 — explicit ``dashboard.coordinator`` binding replaces
  coordinator-role discovery for board-level routing.
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
    # Bead auto-lffg5 retired the api.py facade — no api routes.
    assert manifest.entrypoints.api is None
    # All six schemas declared on the manifest.
    assert manifest.entrypoints.schemas == [
        "tools.dashboard.plugins.coordinator_board.entrypoints.schemas:CoordinatorV1",
        "tools.dashboard.plugins.coordinator_board.entrypoints.schemas:CoordinatorCanvasV1",
        "tools.dashboard.plugins.coordinator_board.entrypoints.schemas:OperatorMessageToCoordinatorV1",
        "tools.dashboard.plugins.coordinator_board.entrypoints.schemas:CoordinatorTileV1",
        "tools.dashboard.plugins.coordinator_board.entrypoints.schemas:CoordinatorThreadV1",
        "tools.dashboard.plugins.coordinator_board.entrypoints.schemas:CoordinatorDecisionV1",
    ]
    assert manifest.entrypoints.actions == [
        "tools.dashboard.plugins.coordinator_board.entrypoints.actions",
    ]


def test_plugin_discovers_with_real_substrate():
    """The shipped substrate finds the plugin directory."""
    discovered = loader.discover()
    by_id = {d.manifest.id: d for d in discovered}
    assert "coordinator-board" in by_id, (
        "loader.discover() did not pick up the coordinator-board plugin"
    )
    assert by_id["coordinator-board"].plugin_dir == PLUGIN_DIR


def test_load_all_resolves_no_routes_and_six_schemas():
    """``load_all`` resolves the plugin with no api routes + 6 schemas."""
    loaded = loader.load_all()
    by_id = {p.id: p for p in loaded}
    plugin = by_id.get("coordinator-board")
    assert plugin is not None, (
        "load_all() did not include coordinator-board — entrypoint import failed?"
    )
    # api.py is gone — no routes contributed by the plugin.
    assert plugin.routes == []
    # Six schemas: coordinator + canvas + operator-message + tile + thread + decision.
    assert len(plugin.schemas) == 6
    schema_ids = {s.set_id for s in plugin.schemas}
    assert schema_ids == {
        "dashboard.coordinator",
        "dashboard.coordinator-canvas",
        "dashboard.operator-message-to-coordinator",
        "dashboard.coordinator-tile",
        "dashboard.coordinator-thread",
        "dashboard.coordinator-decision",
    }


def test_static_files_present():
    """Substrate-required assets exist alongside the manifest. ``api.py``
    is intentionally absent — bead auto-lffg5 retired it."""
    for fname in ("plugin.yaml", "page.html", "page.js"):
        assert (PLUGIN_DIR / fname).is_file(), f"missing plugin asset: {fname}"
    assert not (PLUGIN_DIR / "entrypoints" / "api.py").exists(), (
        "api.py facade should be deleted by bead auto-lffg5"
    )


class TestCoordinatorPresenceIntegration:
    """Presence.alpine wiring + template contracts for coordinator-board."""

    def test_page_js_wraps_state_with_presence_alpine(self):
        js = (PLUGIN_DIR / "page.js").read_text()
        assert "_coordPresenceRuntime.alpine" in js
        assert "_SURFACE_ID = 'coordinator-board'" in js
        assert "_syncPresencePosition" in js
        assert "participantColor" in js
        assert "participantLabel" in js

    def test_page_html_renders_presence_panel_and_attribution(self):
        html = (PLUGIN_DIR / "page.html").read_text()
        assert 'data-testid="coord-presence-panel"' in html
        assert 'data-testid="coord-presence-stack"' in html
        assert "coord-presence-avatar-" in html
        assert "participantColor(p.participant_id)" in html
        assert "participantLabel(t.session, t.label)" in html
        assert "participantInitialFor(t.session, t.label)" in html


# ── Setting schemas ──────────────────────────────────────────────────


class TestCoordinatorSchema:
    def _validate(self, payload):
        coord_schemas.CoordinatorV1.validate(payload)

    def test_minimum_payload_passes(self):
        self._validate({"session_id": "auto-0503-230356"})

    def test_missing_session_id_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate({})

    def test_blank_session_id_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"session_id": "   "})

    def test_unknown_field_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate({"session_id": "auto-1", "extra": True})


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


class TestCoordinatorTileSchema:
    def _validate(self, payload):
        coord_schemas.CoordinatorTileV1.validate(payload)

    def test_minimum_payload_passes(self):
        self._validate({
            "label": "Foo", "role": "implementer",
            "thing": "doing it", "asks": "fyi",
        })

    def test_full_payload_passes(self):
        self._validate({
            "label": "Foo", "role": "implementer",
            "thing": "doing it", "asks": "yes_no",
            "ageMin": 5, "updateKind": "discovery",
            "detail": "found a thing",
        })

    def test_missing_required_rejected(self):
        for missing in ("label", "role", "thing", "asks"):
            payload = {
                "label": "L", "role": "r", "thing": "t", "asks": "fyi",
            }
            del payload[missing]
            with pytest.raises(SchemaValidationError):
                self._validate(payload)

    def test_invalid_asks_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate({
                "label": "L", "role": "r", "thing": "t", "asks": "explode",
            })

    def test_invalid_update_kind_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate({
                "label": "L", "role": "r", "thing": "t", "asks": "fyi",
                "updateKind": "wat",
            })

    def test_unknown_field_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate({
                "label": "L", "role": "r", "thing": "t", "asks": "fyi",
                "extra": True,
            })


class TestCoordinatorThreadSchema:
    def _validate(self, payload):
        coord_schemas.CoordinatorThreadV1.validate(payload)

    def test_minimum_payload_passes(self):
        self._validate({
            "label": "L", "role": "pair",
            "status": "shipping", "lead": "ships",
        })

    def test_full_payload_passes(self):
        self._validate({
            "label": "L", "role": "pair",
            "status": "blocked", "lead": "stuck",
            "bullets": ["one", "two"],
            "ageMin": 12, "totalTurns": 200,
            "needs": "Make a call",
        })

    def test_invalid_status_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate({
                "label": "L", "role": "p", "status": "explode", "lead": "x",
            })

    def test_bullets_must_be_list_of_strings(self):
        with pytest.raises(SchemaValidationError):
            self._validate({
                "label": "L", "role": "p", "status": "shipping",
                "lead": "x", "bullets": "not a list",
            })
        with pytest.raises(SchemaValidationError):
            self._validate({
                "label": "L", "role": "p", "status": "shipping",
                "lead": "x", "bullets": [1, 2],
            })

    def test_unknown_field_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate({
                "label": "L", "role": "p", "status": "shipping",
                "lead": "x", "extra": True,
            })


class TestCoordinatorDecisionSchema:
    def _validate(self, payload):
        coord_schemas.CoordinatorDecisionV1.validate(payload)

    def test_thumb_yes_minimal(self):
        self._validate({
            "tile_id": "t1", "kind": "thumb_yes", "target_session": "s1",
        })

    def test_choice_requires_text(self):
        with pytest.raises(SchemaValidationError):
            self._validate({
                "tile_id": "t1", "kind": "choice", "target_session": "s1",
            })
        # With non-empty choice it's fine.
        self._validate({
            "tile_id": "t1", "kind": "choice", "choice": "ship",
            "target_session": "s1",
        })

    def test_custom_requires_text(self):
        with pytest.raises(SchemaValidationError):
            self._validate({
                "tile_id": "t1", "kind": "custom", "target_session": "s1",
            })
        self._validate({
            "tile_id": "t1", "kind": "custom",
            "choice": "hold off until Friday", "target_session": "s1",
        })

    def test_invalid_kind_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate({
                "tile_id": "t1", "kind": "explode", "target_session": "s1",
            })

    def test_missing_required_rejected(self):
        for missing in ("tile_id", "kind", "target_session"):
            payload = {
                "tile_id": "t1", "kind": "thumb_yes", "target_session": "s1",
            }
            del payload[missing]
            with pytest.raises(SchemaValidationError):
                self._validate(payload)

    def test_unknown_field_rejected(self):
        with pytest.raises(SchemaValidationError):
            self._validate({
                "tile_id": "t1", "kind": "thumb_yes",
                "target_session": "s1", "extra": True,
            })

    def test_sitrep_and_refresh_request_skip_choice(self):
        self._validate({
            "tile_id": "t1", "kind": "sitrep_request", "target_session": "s1",
        })
        self._validate({
            "tile_id": "t1", "kind": "refresh_request", "target_session": "s1",
        })


def test_schema_synopsis_published():
    """Module-level SYNOPSIS surfaces through ``set find`` lookups."""
    assert isinstance(coord_schemas.SYNOPSIS, dict)
    assert "summary" in coord_schemas.SYNOPSIS
    assert "nouns" in coord_schemas.SYNOPSIS
    assert "coordinator decision" in coord_schemas.SYNOPSIS["nouns"]


def test_schema_field_metadata_populated():
    """Each new schema declares ``_field_metadata`` so ``set schema`` works."""
    for cls in (
        coord_schemas.CoordinatorV1,
        coord_schemas.CoordinatorTileV1,
        coord_schemas.CoordinatorThreadV1,
        coord_schemas.CoordinatorDecisionV1,
    ):
        meta = cls._field_metadata
        assert isinstance(meta, dict) and meta, (
            f"{cls.__name__} missing _field_metadata"
        )
        # ``export_json_schema`` synthesises a draft-07-flavoured
        # schema dict — required iff metadata flagged required.
        js = cls.export_json_schema()
        assert js["type"] == "object"
        assert isinstance(js["properties"], dict) and js["properties"]


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
            "hasOnTileThumbYes: typeof c.onTileThumbYes === 'function', "
            "hasOnTileThumbNo: typeof c.onTileThumbNo === 'function', "
            "hasOnTileSitrep: typeof c.onTileSitrep === 'function', "
            "hasOnTileRefresh: typeof c.onTileRefresh === 'function', "
            "hasSwitchTab: typeof c.switchTab === 'function', "
            "hasRenderInlineLinks: typeof c.renderInlineLinks === 'function', "
            "hasStatusBadge: typeof c.statusBadge === 'function', "
            "hasParticipantLabel: typeof c.participantLabel === 'function', "
            "hasParticipantInitialFor: typeof c.participantInitialFor === 'function', "
            "hasAgeStr: typeof c.ageStr === 'function' };"
        )
        assert out["tab"] == "primary"
        for k in (
            "hasInit", "hasLoadBoard", "hasOnOperatorMessage",
            "hasOnTileThumbYes", "hasOnTileThumbNo",
            "hasOnTileSitrep", "hasOnTileRefresh", "hasSwitchTab",
            "hasRenderInlineLinks", "hasStatusBadge",
            "hasParticipantLabel", "hasParticipantInitialFor",
            "hasAgeStr",
        ):
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
        assert "slate" in out["unknown"]

    def test_normalize_tile_extracts_session_from_key(self):
        """Tile ``session`` comes from the segment after the colon in
        the Setting key (``<coord>:<tile-session>``)."""
        out = self._eval(
            "return c._normalizeTile({"
            " key: 'coord-A:auto-foo', "
            " payload: { label: 'Foo', role: 'pair', "
            "            thing: 'do it', asks: 'yes_no', ageMin: 3 } "
            "});"
        )
        assert out["session"] == "auto-foo"
        assert out["label"] == "Foo"
        assert out["asks"] == "yes_no"

    def test_normalize_thread_extracts_session_from_key(self):
        out = self._eval(
            "return c._normalizeThread({"
            " key: 'coord-A:auto-bar', "
            " payload: { label: 'B', role: 'pair', "
            "            status: 'blocked', lead: 'l' } "
            "});"
        )
        assert out["session"] == "auto-bar"
        assert out["status"] == "blocked"

    def test_decision_label(self):
        """``decisionLabel`` synthesises the 'you said: …' text."""
        out = self._eval(
            "return { yes:    c.decisionLabel({ kind: 'thumb_yes' }), "
            "         no:     c.decisionLabel({ kind: 'thumb_no' }), "
            "         pick:   c.decisionLabel({ kind: 'choice', choice: 'ship it' }), "
            "         custom: c.decisionLabel({ kind: 'custom', choice: 'hold off' }), "
            "         sitrep: c.decisionLabel({ kind: 'sitrep_request' }), "
            "         refresh:c.decisionLabel({ kind: 'refresh_request' }), "
            "         empty:  c.decisionLabel(null) };"
        )
        assert out["yes"] == "thumb yes"
        assert out["no"] == "thumb no"
        assert out["pick"] == "ship it"
        assert out["custom"] == "hold off"
        assert out["sitrep"] == "requested sitrep"
        assert out["refresh"] == "requested refresh"
        assert out["empty"] == ""

    def test_surface_participants_sort_and_overflow(self):
        out = self._eval(
            "c.participants = ["
            "  { participant_id: 'agent-b', participant_kind: 'agent', participant_label: 'Bravo' },"
            "  { participant_id: 'operator-1', participant_kind: 'operator', participant_label: 'Operator' },"
            "  { participant_id: 'agent-a', participant_kind: 'agent', participant_label: 'Alpha' },"
            "  { participant_id: 'agent-c', participant_kind: 'agent', participant_label: 'Charlie' },"
            "  { participant_id: 'agent-d', participant_kind: 'agent', participant_label: 'Delta' },"
            "  { participant_id: 'agent-e', participant_kind: 'agent', participant_label: 'Echo' }"
            "];"
            "return { "
            "  ordered: c.surfaceParticipants.map(p => p.participant_id), "
            "  visible: c.visibleParticipants.map(p => p.participant_id), "
            "  overflow: c.overflowParticipantCount "
            "};"
        )
        assert out["ordered"] == [
            "operator-1",
            "agent-a",
            "agent-b",
            "agent-c",
            "agent-d",
            "agent-e",
        ]
        assert out["visible"] == [
            "operator-1",
            "agent-a",
            "agent-b",
            "agent-c",
            "agent-d",
        ]
        assert out["overflow"] == 1

    def test_participant_label_and_title_prefer_live_row(self):
        out = self._eval(
            "c.participants = ["
            "  { participant_id: 'auto-live', participant_kind: 'agent', "
            "    participant_label: 'Live Agent', state: 'working', "
            "    intent: 'Reviewing tracking' }"
            "];"
            "return { "
            "  liveLabel: c.participantLabel('auto-live', 'Fallback'), "
            "  missingLabel: c.participantLabel('auto-missing', 'Fallback'), "
            "  liveTitle: c.participantTitle('auto-live', 'Fallback'), "
            "  missingTitle: c.participantTitle('auto-missing', 'Fallback') "
            "};"
        )
        assert out["liveLabel"] == "Live Agent"
        assert out["missingLabel"] == "Fallback"
        assert out["liveTitle"] == "Live Agent — Reviewing tracking"
        assert out["missingTitle"] == "Last update from Fallback"

    def test_offline_writer_fallback_keeps_deterministic_color(self):
        out = self._eval(
            "return { "
            "  label: c.participantLabel('auto-offline', 'Offline Writer'), "
            "  initial: c.participantInitialFor('auto-offline', 'Offline Writer'), "
            "  color: c.participantColor('auto-offline') "
            "};"
        )
        assert out["label"] == "Offline Writer"
        assert out["initial"] == "O"
        assert out["color"].startswith("hsl(")
        assert out["color"].endswith("70% 60%)")

    def test_switch_tab_updates_active_zone(self):
        out = self._eval(
            "c.switchTab('tracking'); return { tab: c.tab, label: c.tabLabel(c.tab) };"
        )
        assert out["tab"] == "tracking"
        assert out["label"] == "Tracking"

    def test_win_detection_logic(self):
        """Verbatim quick-reply detection (drives celebrateWin)."""
        out = self._eval(
            "c.data.canvas = { question: 'q', context: '', "
            "                  quickReplies: ['Yes, do it', 'No, hold'] }; "
            "const replies = (c.data.canvas.quickReplies || []).map(r => r.trim()); "
            "return { verbatim: replies.includes('Yes, do it'), "
            "         edited:   replies.includes('Yes, do it now') };"
        )
        assert out["verbatim"] is True
        assert out["edited"] is False
