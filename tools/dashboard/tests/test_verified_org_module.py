"""The verified-org brand module (packaging-owned, controller-imported).

One owner for the org-identity display guards: the module renders into a
caller-passed container and applies exactly the bridge's safety posture.
The join-page controller (crypto, Model B) imports it; these tests are the
pin that lets that import trust it blind.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[1]
MODULE = DASHBOARD / "static" / "js" / "verified-org.js"
SOURCE = MODULE.read_text(encoding="utf-8")


def _node(script: str) -> str:
    if shutil.which("node") is None:
        pytest.skip("node not on PATH")
    result = subprocess.run(["node", "-e", script],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout


class TestGuards:
    def test_icon_and_color_guards_match_the_bridge(self):
        script = (
            f"const api = require({json.dumps(str(MODULE))});"
            "const out = {"
            "  icon_good: api.safeIcon('data:image/png;base64,iVBORw0KGgo='),"
            "  icon_remote: api.safeIcon('https://evil.example/i.png'),"
            "  icon_html: api.safeIcon('data:text/html;base64,PHNjcmlwdD4='),"
            "  color_good: api.safeColor('#5b3aa6'),"
            "  color_css: api.safeColor('red; background:url(//evil)'),"
            "};"
            "process.stdout.write(JSON.stringify(out));"
        )
        out = json.loads(_node(script))
        assert out["icon_good"] == "data:image/png;base64,iVBORw0KGgo="
        assert out["icon_remote"] is None
        assert out["icon_html"] is None
        assert out["color_good"] == "#5b3aa6"
        assert out["color_css"] is None

    def test_clamps_bound_text(self):
        script = (
            f"const api = require({json.dumps(str(MODULE))});"
            "process.stdout.write(JSON.stringify({"
            "  name: api.clampName('x'.repeat(500)).length,"
            "  byline: api.clampByline('y'.repeat(500)).length,"
            "  nonstring: api.clampName(42),"
            "}));"
        )
        out = json.loads(_node(script))
        assert out["name"] == 120
        assert out["byline"] == 300
        assert out["nonstring"] == ""


class TestContract:
    def test_renders_into_the_passed_container_only(self):
        # The whole point of the module boundary: the controller owns the
        # page DOM; this module touches nothing it wasn't handed.
        assert "getElementById" not in SOURCE
        assert "querySelector" not in SOURCE
        assert "containerEl.ownerDocument" in SOURCE

    def test_never_networks_and_never_sees_credentials(self):
        lowered = SOURCE.lower()
        for forbidden in ("fetch(", "xmlhttprequest", "websocket",
                          "sendbeacon", "bearer", "token",
                          "localstorage", "sessionstorage"):
            assert forbidden not in lowered, forbidden

    def test_unusable_reply_leaves_the_container_untouched(self):
        script = (
            f"const api = require({json.dumps(str(MODULE))});"
            "const calls = [];"
            "const el = { textContent: 'KEEP', style: {},"
            "  ownerDocument: { createElement: () => { calls.push(1);"
            "    return { style: {}, appendChild: () => {} }; } },"
            "  appendChild: () => { calls.push('append'); } };"
            "const a = api.renderVerifiedOrg(el, {});"
            "const b = api.renderVerifiedOrg(el, { org_name: 42 });"
            "const c = api.renderVerifiedOrg(null, { org_name: 'X' });"
            "process.stdout.write(JSON.stringify({a, b, c,"
            "  kept: el.textContent, calls: calls.length}));"
        )
        out = json.loads(_node(script))
        assert out["a"] is False and out["b"] is False and out["c"] is False
        assert out["kept"] == "KEEP"
        assert out["calls"] == 0
