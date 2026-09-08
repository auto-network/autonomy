"""Behavioral gate for the production Ops document and its network adapter."""
import subprocess
from pathlib import Path


class TestOpsBehavior:
    def test_production_document_controls(self):
        script = Path(__file__).parent / "jsdom" / "mission_ops.cjs"
        result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=40)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PASS Ops" in result.stdout

    def test_chromium_desktop_and_phone(self):
        script = Path(__file__).parent / "jsdom" / "mission_ops_browser.cjs"
        result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=150)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PASS Ops Chromium" in result.stdout
