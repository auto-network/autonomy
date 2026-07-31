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
    page_html = (PLUGIN_DIR / "page.html").read_text()
    assert '@pointerdown.prevent="startProgressScrub($event)"' in page_html
    assert '@pointermove.window="moveProgressScrub($event)"' in page_html
    assert 'aria-label="Slide navigation"' in page_html


def test_presentation_deck_schema_validates_payload():
    payload = {
        "design_id": "deck-1",
        "latest_revision_id": "rev-2",
        "name": "Quarterly Review",
        "slide_count": 2,
        "slide_ids": ["slide-1", "slide-2"],
    }

    PresentationDeckV1.validate(payload)
    PresentationDeckV1.validate({k: v for k, v in payload.items() if k != "latest_revision_id"})

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
    assert "latest_revision_id" not in persisted["_all"][0]["payload"]


def test_presentations_library_hydrates_latest_design_revision(tmp_path, monkeypatch):
    fixture_path = tmp_path / "fixture.json"
    design_id = "roadmap-deck"
    fixture = {
        "active_sessions": [],
        "beads": [],
        "experiments": [
            {
                **make_experiment(
                    "rev-old",
                    title="Old Roadmap",
                    html="<section>Old</section>",
                ),
                "design_id": design_id,
                "revision_seq": 1,
                "revisions": ["rev-old", "rev-new"],
                "created_at": "2026-06-08T10:00:00Z",
            },
            {
                **make_experiment(
                    "rev-new",
                    title="New Roadmap",
                    html="<section>One</section><section>Two</section><section>Three</section>",
                ),
                "design_id": design_id,
                "revision_seq": 2,
                "revisions": ["rev-old", "rev-new"],
                "created_at": "2026-06-08T11:00:00Z",
            },
        ],
        "settings": {
            PRESENTATION_DECK_SET_ID: {
                "_all": [
                    {
                        "key": design_id,
                        "payload": {
                            "design_id": design_id,
                            "latest_revision_id": "rev-old",
                            "name": "Old Roadmap",
                            "last_shown_at": "2026-06-08T20:00:00Z",
                            "slide_count": 1,
                            "slide_ids": ["slide-1"],
                        },
                    }
                ]
            }
        },
    }
    write_fixture(fixture, fixture_path)
    monkeypatch.setenv("DASHBOARD_MOCK", str(fixture_path))

    from tools.dashboard.dao import mock as dao_mock

    monkeypatch.setattr(dao_mock, "FIXTURE_PATH", fixture_path)

    app = Starlette(routes=present_api.routes)
    with TestClient(app) as client:
        response = client.get("/api/presentations/decks")

    assert response.status_code == 200
    decks = response.json()["decks"]
    assert len(decks) == 1
    deck = decks[0]
    assert deck["design_id"] == design_id
    assert deck["latest_revision_id"] == "rev-new"
    assert deck["name"] == "New Roadmap"
    assert deck["modified_at"] == "2026-06-08T11:00:00Z"
    assert deck["updated_at"] == "2026-06-08T11:00:00Z"
    assert deck["slide_count"] == 3
    assert deck["slide_ids"] == ["slide-1", "slide-2", "slide-3"]
    assert deck["last_shown_at"] == "2026-06-08T20:00:00Z"


def test_presentations_library_sorts_by_modified_not_last_opened(tmp_path, monkeypatch):
    fixture_path = tmp_path / "fixture.json"
    fixture = {
        "active_sessions": [],
        "beads": [],
        "experiments": [
            {
                **make_experiment("rev-a", title="Older Modified", html="<section>A</section>"),
                "design_id": "deck-a",
                "revision_seq": 1,
                "revisions": ["rev-a"],
                "created_at": "2026-06-08T09:00:00Z",
            },
            {
                **make_experiment("rev-b", title="Newer Modified", html="<section>B</section>"),
                "design_id": "deck-b",
                "revision_seq": 1,
                "revisions": ["rev-b"],
                "created_at": "2026-06-08T12:00:00Z",
            },
        ],
        "settings": {
            PRESENTATION_DECK_SET_ID: {
                "_all": [
                    {
                        "key": "deck-a",
                        "payload": {
                            "design_id": "deck-a",
                            "name": "Older Modified",
                            "last_shown_at": "2026-06-08T22:00:00Z",
                        },
                    },
                    {
                        "key": "deck-b",
                        "payload": {
                            "design_id": "deck-b",
                            "name": "Newer Modified",
                            "last_shown_at": "2026-06-08T13:00:00Z",
                        },
                    },
                ]
            }
        },
    }
    write_fixture(fixture, fixture_path)
    monkeypatch.setenv("DASHBOARD_MOCK", str(fixture_path))

    from tools.dashboard.dao import mock as dao_mock

    monkeypatch.setattr(dao_mock, "FIXTURE_PATH", fixture_path)

    app = Starlette(routes=present_api.routes)
    with TestClient(app) as client:
        response = client.get("/api/presentations/decks")

    assert response.status_code == 200
    decks = response.json()["decks"]
    assert [deck["design_id"] for deck in decks] == ["deck-b", "deck-a"]
    assert decks[0]["modified_at"] == "2026-06-08T12:00:00Z"
    assert decks[1]["last_shown_at"] == "2026-06-08T22:00:00Z"


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
assert(
  !fs.readFileSync({str(PLUGIN_DIR / 'page.js')!r}, 'utf8').includes('/shown'),
  'Opening a deck should not write presentation metadata; publish/show state is explicit.',
);
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
assert(doc.includes('applySnapMode'), 'runtime must relax snapping for slides taller than the viewport');
assert(doc.includes('y proximity'), 'tall-slide decks must downgrade mandatory snapping to proximity');
assert(doc.includes('present-slide-gap'), 'adjacent slides must be separated by a visible gap');
assert(doc.includes('window.FIXTURE_STATES'));
const fullDoc = helpers.iframeDocument({{
  variants: [{{ html: '<!doctype html><html><head><style>.x{{color:red}}</style></head><body><section>Full</section></body></html>' }}],
}}, 0);
assert(fullDoc.includes('<style>.x{{color:red}}</style>'));
assert(
  fullDoc.indexOf('color:#e5e7eb') < fullDoc.indexOf('.x{{color:red}}'),
  'viewer cosmetic defaults must precede deck styles so deck CSS wins the cascade',
);
assert(
  fullDoc.indexOf('.x{{color:red}}') < fullDoc.indexOf('#present-scroll-root{{'),
  'structural pager rules must come after deck styles',
);
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
assert.equal(helpers.progressIndexFromPosition(0, {{ left: 0, width: 100 }}, 11), 0);
assert.equal(helpers.progressIndexFromPosition(49, {{ left: 0, width: 100 }}, 11), 5);
assert.equal(helpers.progressIndexFromPosition(100, {{ left: 0, width: 100 }}, 11), 10);
assert.equal(helpers.progressIndexFromPosition(-50, {{ left: 0, width: 100 }}, 11), 0);
assert.equal(helpers.progressIndexFromPosition(150, {{ left: 0, width: 100 }}, 11), 10);
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
