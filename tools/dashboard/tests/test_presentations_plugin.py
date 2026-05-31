"""Tests for the Present plugin."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from starlette.applications import Starlette
from starlette.testclient import TestClient

from tools.dashboard.plugin_api.manifest import PluginManifest
from tools.dashboard.plugins.presentations.entrypoints import api as present_api
from tools.dashboard.plugins.presentations.entrypoints.schemas import (
    PRESENTATION_DECK_SET_ID,
    PresentationDeckV1,
)
from tools.dashboard.tests.fixtures import (
    TEST_EXPERIMENT_ID,
    make_experiment,
    write_fixture,
)
from tools.graph.schemas.registry import SchemaValidationError


PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "presentations"


def test_presentations_manifest_declares_library_and_deck_paths():
    manifest = PluginManifest.model_validate(
        yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    )

    assert manifest.id == "presentations"
    assert manifest.paths == ["/presentations", "/present"]
    assert manifest.assets.style == "page.css"
    assert (
        "tools.dashboard.plugins.presentations.entrypoints.schemas:PresentationDeckV1"
        in manifest.entrypoints.schemas
    )


def test_presentation_deck_schema_validates_payload():
    payload = {
        "design_id": "deck-1",
        "latest_revision_id": "rev-2",
        "name": "Quarterly Review",
        "slide_count": 2,
        "slide_ids": ["slide-1", "slide-2"],
    }

    PresentationDeckV1.validate(payload)

    with pytest.raises(SchemaValidationError):
        PresentationDeckV1.validate({**payload, "slide_count": 0})

    with pytest.raises(SchemaValidationError):
        PresentationDeckV1.validate({**payload, "extra": True})


def test_presentations_api_reads_design_and_records_shown(tmp_path, monkeypatch):
    fixture_path = tmp_path / "fixture.json"
    fixture = {
        "active_sessions": [],
        "beads": [],
        "experiments": [
            make_experiment(
                TEST_EXPERIMENT_ID,
                title="Roadmap Deck",
                html="<section>One</section><section>Two</section>",
            )
        ],
        "settings": {},
    }
    write_fixture(fixture, fixture_path)
    monkeypatch.setenv("DASHBOARD_MOCK", str(fixture_path))

    from tools.dashboard.dao import mock as dao_mock

    monkeypatch.setattr(dao_mock, "FIXTURE_PATH", fixture_path)

    app = Starlette(routes=present_api.routes)
    with TestClient(app) as client:
        deck_response = client.get(f"/api/presentations/deck/{TEST_EXPERIMENT_ID}")
        shown_response = client.post(f"/api/presentations/deck/{TEST_EXPERIMENT_ID}/shown")
        library_response = client.get("/api/presentations/decks")

    assert deck_response.status_code == 200
    deck = deck_response.json()["deck"]
    assert deck["design_id"] == TEST_EXPERIMENT_ID
    assert deck["latest_revision_id"] == TEST_EXPERIMENT_ID
    assert deck["name"] == "Roadmap Deck"
    assert deck["slide_count"] == 2
    assert deck["slide_ids"] == ["slide-1", "slide-2"]

    assert shown_response.status_code == 200
    assert shown_response.json()["deck"]["last_shown_at"].endswith("Z")

    assert library_response.status_code == 200
    decks = library_response.json()["decks"]
    assert [d["design_id"] for d in decks] == [TEST_EXPERIMENT_ID]

    persisted = json.loads(fixture_path.read_text())["settings"][PRESENTATION_DECK_SET_ID]
    assert persisted["_all"][0]["key"] == TEST_EXPERIMENT_ID


def test_present_routes_keep_plugin_shell_deep_links_available():
    from tools.dashboard import server

    route_paths = {getattr(route, "path", "") for route in server._build_plugin_routes()}

    assert "/presentations" in route_paths
    assert "/presentations/{path:path}" in route_paths
    assert "/present" in route_paths
    assert "/present/{path:path}" in route_paths


def test_present_frontend_helpers():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")

    script = f"""
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
global.window = {{
  Alpine: {{ data(name, factory) {{ window.__factory = factory; }} }},
  addEventListener() {{}},
}};
global.document = {{ addEventListener() {{}} }};
global.history = {{ replaceState() {{}} }};
vm.runInThisContext(fs.readFileSync({str(PLUGIN_DIR / 'page.js')!r}, 'utf8'));
const helpers = window.PresentationsTest;
assert.deepStrictEqual(
  helpers.parsePresentPath('/present/deck-1/slide-3'),
  {{ mode: 'deck', designId: 'deck-1', slideIndex: 2 }}
);
assert.deepStrictEqual(
  helpers.parsePresentPath('/presentations'),
  {{ mode: 'library', designId: '', slideIndex: 0 }}
);
const doc = helpers.iframeDocument({{
  fixture: JSON.stringify({{ states: {{ first: {{ ok: true }} }} }}),
  variants: [
    {{ html: '<section>Wrong</section>' }},
    {{ selected: true, html: '<section>Right</section>' }},
  ],
}}, 0);
assert(doc.includes('<section>Right</section>'));
assert(!doc.includes('<section>Wrong</section>'));
assert(doc.includes('scroll-snap-type:y mandatory'));
assert(doc.includes('window.FIXTURE_STATES'));
"""
    subprocess.run([node, "-e", script], check=True)
