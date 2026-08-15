"""The public landing page (register row 105) — the front door's contract.

Static, self-contained, honest: the real product shown through embedded
real captures, the agent-install CTA, and nothing else — no state, no
network reach, no engineering narration.
"""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from tools.network.registry.app import create_app

BOOTLOADER_DIR = (
    Path(__file__).resolve().parents[1] / "bootloader"
)
LANDING = (BOOTLOADER_DIR / "landing.html").read_text(encoding="utf-8")
# The page carries embedded captures as data URIs; copy/network guards must
# scan the PROSE, not hundreds of KB of base64 (which contains any 3-gram).
import re
LANDING_TEXT = re.sub(r"data:image/[a-z+]+;base64,[A-Za-z0-9+/=]+", "DATAURI", LANDING)


def _client(tmp_path):
    return TestClient(create_app(db_path=tmp_path / "registry.db"))


class TestServing:
    def test_root_serves_the_landing_bytes(self, tmp_path):
        r = _client(tmp_path).get("/")
        assert r.status_code == 200
        assert r.text == LANDING
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["x-content-type-options"] == "nosniff"

    def test_csp_forbids_all_network_reach(self, tmp_path):
        csp = _client(tmp_path).get("/").headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "img-src data:" in csp
        assert "connect-src" not in csp  # nothing may fetch, ever


class TestContent:
    def test_the_cta_hands_the_install_url_to_an_agent(self):
        assert "Please install Autonomy from https://auto.network/install" in LANDING
        assert 'href="/install"' in LANDING

    def test_product_is_shown_with_real_captures(self):
        # Self-contained: embedded data URIs, never external images.
        assert LANDING.count("data:image/png;base64,") >= 3
        assert "src=\"http" not in LANDING

    def test_copy_speaks_interface_not_engineering(self):
        lowered = LANDING_TEXT.lower()
        for leaked in ("registry", "relay", "bearer", "ceremony", "substrate",
                       "endpoint", "backend", "e2e", "csp"):
            assert leaked not in lowered, leaked

    def test_page_reaches_no_network(self):
        lowered = LANDING_TEXT.lower()
        for forbidden in ("fetch(", "xmlhttprequest", "websocket",
                          "sendbeacon", "src=\"//", "https://" ):
            if forbidden == "https://":
                # The ONLY https URL is the install CTA text itself.
                assert lowered.count("https://") == lowered.count(
                    "https://auto.network/install")
                continue
            assert forbidden not in lowered, forbidden
