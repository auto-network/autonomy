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
        "active_sessions": [
            {
                "session_id": "auto-present-owner",
                "label": "Present owner",
                "type": "container",
                "is_live": True,
                "active": True,
                "age_seconds": 3,
            }
        ],
        "beads": [],
        "experiments": [
            {
                **make_experiment(
                    TEST_EXPERIMENT_ID,
                    title="Roadmap Deck",
                    html="<section>One</section><section>Two</section>",
                ),
                "creator_session_id": "auto-present-owner",
                "creator_session_label": "Present owner",
            }
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
    owner_presence = deck_response.json()["owner_presence"]
    assert deck["design_id"] == TEST_EXPERIMENT_ID
    assert deck["latest_revision_id"] == TEST_EXPERIMENT_ID
    assert deck["name"] == "Roadmap Deck"
    assert deck["slide_count"] == 2
    assert deck["slide_ids"] == ["slide-1", "slide-2"]
    assert owner_presence["participant_id"] == "auto-present-owner"
    assert owner_presence["participant_label"] == "Present owner"
    assert owner_presence["is_owner"] is True
    assert owner_presence["is_live"] is True
    assert owner_presence["is_active"] is True

    assert shown_response.status_code == 200
    assert shown_response.json()["deck"]["last_shown_at"].endswith("Z")

    assert library_response.status_code == 200
    decks = library_response.json()["decks"]
    assert [d["design_id"] for d in decks] == [TEST_EXPERIMENT_ID]

    persisted = json.loads(fixture_path.read_text())["settings"][PRESENTATION_DECK_SET_ID]
    assert persisted["_all"][0]["key"] == TEST_EXPERIMENT_ID


def test_presentations_api_returns_offline_owner_presence_when_unowned(tmp_path, monkeypatch):
    fixture_path = tmp_path / "fixture.json"
    fixture = {
        "active_sessions": [],
        "beads": [],
        "experiments": [
            make_experiment(
                TEST_EXPERIMENT_ID,
                title="Legacy Deck",
                html="<section>One</section>",
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
        response = client.get(f"/api/presentations/deck/{TEST_EXPERIMENT_ID}")

    assert response.status_code == 200
    owner_presence = response.json()["owner_presence"]
    assert owner_presence["participant_label"] == "No owner session"
    assert owner_presence["display_initial"] == "?"
    assert owner_presence["is_owner"] is True
    assert owner_presence["is_live"] is False
    assert owner_presence["accepts_pings"] is False


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
  helpers.parsePresentPath('/presentations/deck-1/3'),
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
const fullDoc = helpers.iframeDocument({{
  variants: [{{ html: '<!doctype html><html><head><style>.x{{color:red}}</style></head><body><section>Full</section></body></html>' }}],
}}, 0);
assert(fullDoc.includes('<style>.x{{color:red}}</style>'));
assert(fullDoc.includes('<main id="present-scroll-root"><section>Full</section></main>'));
assert(!fullDoc.includes('<main id="present-scroll-root"><!doctype html>'));
assert.equal(helpers.presentSurfaceId('/presentations/deck-1/3'), 'presentations:deck-1');
const topbar = helpers.topbarHtml(
  {{ name: 'Deck', subtitle: 'Sub' }},
  1,
  3,
  [{{ participant_id: 'listener', participant_label: 'Listener', state: 'present' }}],
  {{ participant_id: 'owner', participant_label: 'Owner', is_owner: true, is_live: true, is_active: true, intent: 'listening' }},
);
assert(topbar.includes('present-topbar-presence'));
assert(topbar.includes('present-topbar-owner is-live'));
assert(topbar.includes('Owner'));
assert(topbar.includes('2 / 3'));
"""
    subprocess.run([node, "-e", script], check=True)


def test_present_runtime_reveals_second_slide_after_scroll():
    """Regression for iPhone blank slide 2.

    Design Studio decks commonly hide slide text until the slide gets an
    ``.in`` reveal class. On iOS, the deck's own IntersectionObserver can
    miss updates inside Present's iframe scroller; Present must reveal the
    active slide when its own scroll runtime observes slide 2.
    """
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
const doc = helpers.iframeDocument({{
  variants: [{{ html: `
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<style>
  html, body {{ margin: 0; height: 100%; background: #020617; color: white; }}
  .slide {{ min-height: 100svh; display: grid; place-items: center; }}
  .slide:not(.in) .r {{ opacity: 0; }}
  .slide.in .r {{ opacity: 1; }}
</style>
</head>
<body>
  <section class="slide in"><h1 class="r">First visible slide</h1></section>
  <section class="slide"><h1 class="r">Second must reveal</h1></section>
</body>
</html>
` }}],
}}, 1);
assert(doc.includes('Second must reveal'));
assert(
  doc.includes('function reveal(index)'),
  'Present runtime must define reveal(index) so slide 2 content is not left opacity-hidden',
);
assert(
  doc.includes('reveal(index);post("present:active"'),
  'scroll reporting must reveal the active slide before updating the topbar/page indicator',
);
assert(
  doc.includes('reveal(index);var el=slides[index]'),
  'programmatic navigation to slide 2 must reveal the target slide before scrolling',
);
"""
    subprocess.run([node, "-e", script], check=True)
