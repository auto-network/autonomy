"""End-to-end render integration: settings -> compose -> jsdom DOM.

Each scenario builds real, schema-validated settings payloads
(scenarios.py), composes the complete document through
``compose.render_screen``, and hands it to the jsdom harness
(jsdom/mission_viewer.cjs) which loads it with scripts enabled and
asserts on every screen: tabs, legends, orderings, full-screen pages,
provenance, the event feed, blockers, charter, chat.

Skips cleanly when node/jsdom is unavailable, same convention as
tools/dashboard/tests/test_jsdom_smoke.py.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tools.dashboard.plugins.mission.tests import scenarios

_HARNESS = Path(__file__).parent / "jsdom" / "mission_viewer.cjs"


def _run_scenario(monkeypatch, tmp_path, name: str, store) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    doc = store.render(monkeypatch)
    path = tmp_path / f"{name}.html"
    path.write_text(doc, encoding="utf-8")
    proc = subprocess.run(
        [node, str(_HARNESS), str(path), name],
        capture_output=True, text=True, timeout=120)
    combined = (proc.stdout or "") + (proc.stderr or "")
    if "MODULE_NOT_FOUND" in combined or "Cannot find module 'jsdom'" in combined:
        pytest.skip("jsdom not resolvable")
    assert proc.returncode == 0, f"{name} failed:\n{combined}"
    assert f"PASS {name}" in proc.stdout, combined


def test_full_mission_renders_every_screen(monkeypatch, tmp_path):
    _run_scenario(monkeypatch, tmp_path, "full", scenarios.full())


def test_empty_mission_degrades_gracefully(monkeypatch, tmp_path):
    _run_scenario(monkeypatch, tmp_path, "empty", scenarios.empty())
