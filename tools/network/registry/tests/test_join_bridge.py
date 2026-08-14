"""The org:join bridge page (auto-y7nap) — server-side contract.

The bridge fills the previously-dead redirect target: the bootloader sends
org:join links to a relative /network/join that no service served, so every
browser-opened invitation 404'd. These tests pin the server half — a fixed
neutral shell, strict headers, client-side-only assembly — and the
bootloader's channel_token pass-through. The client behavior (probe as
progressive enhancement, blurb assembly, local handoff) lives in join.js
and is asserted here at the source-contract level: the trust-critical
properties are structural (what the code CAN'T do), not behavioral.
"""

from __future__ import annotations

from pathlib import Path

BOOTLOADER_DIR = Path(__file__).resolve().parents[1] / "bootloader"
JOIN_JS = (BOOTLOADER_DIR / "join.js").read_text(encoding="utf-8")
AUTONET_JS = (BOOTLOADER_DIR / "autonet.js").read_text(encoding="utf-8")


class TestJoinShell:
    def test_one_byte_sequence_regardless_of_query(self, client):
        bare = client.get("/network/join")
        full = client.get(
            "/network/join?org=11111111-1111-4111-8111-111111111111"
            "&root_pub=" + "a" * 64 + "&invite_ref=" + "e" * 64
        )
        assert bare.status_code == full.status_code == 200
        assert bare.content == full.content

    def test_shell_carries_no_invitation_data(self, client):
        body = client.get(
            "/network/join?org=22222222-2222-4222-8222-222222222222"
        ).text
        assert "22222222" not in body

    def test_headers_and_csp(self, client):
        response = client.get("/network/join")
        csp = response.headers["content-security-policy"]
        assert "default-src 'none'" in csp
        # DELIBERATE CHANGE (auto-r7kk4, operator-directed): the page now
        # performs exactly ONE network interaction — the root-pinned E2E
        # join channel over which the ORG self-describes. connect-src
        # admits it; localhost is deliberately NOT a connect target (the
        # no-auto-detection ruling stands — this is the org connection,
        # not a probe). Icons arrive as bounded data URIs only.
        assert "connect-src 'self';" in csp
        # Scheme-wide sources would permit ANY host (relay review): the
        # channel is same-origin and 'self' is the whole allowance.
        assert "wss:" not in csp and "ws:" not in csp
        assert "localhost" not in csp
        assert "img-src data:" in csp
        assert "frame-ancestors 'none'" in csp
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_join_js_served(self, client):
        response = client.get("/l-assets/join.js")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/javascript")
        assert response.text == JOIN_JS


class TestClientSourceContract:
    """Structural assertions on the client code's trust properties."""

    def test_only_network_path_is_the_authenticated_channel(self):
        # DELIBERATE CHANGE (auto-r7kk4): the page's one network
        # interaction is the E2E join channel, reached ONLY through the
        # audited autonet primitives (openSocket/performHandshake) — no
        # raw fetch/XHR/WebSocket/beacon in this file, ever. The BEARER
        # is read from location.hash and appears only in URL construction
        # (blurb, local handoff); the channel setup uses the CHANNEL
        # token and the root pin, never the bearer.
        assert "location.hash" in JOIN_JS
        for forbidden in ("fetch(", "xmlhttprequest", "new websocket",
                          "sendbeacon"):
            assert forbidden not in JOIN_JS.lower(), forbidden
        assert "openSocket" in JOIN_JS and "performHandshake" in JOIN_JS
        handshake = JOIN_JS[JOIN_JS.index("performHandshake(ws"):][:220]
        assert "bearer" not in handshake
        assert 'op: "context"' in JOIN_JS
        for claim_op in ('"submit"', '"status"'):
            assert claim_op not in JOIN_JS, claim_op

    def test_no_ceremony_code(self):
        # The July trust ruling: a relay-served page must not run the join
        # ceremony. No crypto, no claim submission, no envelope handling.
        for forbidden in ("subtle", "crypto.", "member.claim", "submit(",
                          ".sign(", "decrypt", "Ed25519", "keypair"):
            assert forbidden.lower() not in JOIN_JS.lower(), forbidden

    def test_blurb_is_origin_aware(self):
        # Both blurb URLs derive from location.origin so the copied prompt
        # works on the interim registry host and self-upgrades on the apex
        # (same principle as the /install CTA).
        assert 'location.origin + "/install' in JOIN_JS
        assert 'location.origin + "/l/"' in JOIN_JS
        assert "auto.network" not in JOIN_JS

    def test_both_affordances_are_static(self):
        # No probe, no emphasis swap: both ways in always render and the
        # user picks (the ruled paste-and-explicit-action model).
        assert "probe" not in JOIN_JS.lower()
        assert "PROBE_TIMEOUT_MS" not in JOIN_JS


class TestBootloaderPassThrough:
    # The executable proof of the pass-through lives in
    # test_bootloader_join.py::test_join_context_and_fragment_only_delivery,
    # which runs deliverJoinContext under Node and asserts the exact query
    # and fragment shapes (both credentials fragment-only). Source-substring
    # assertions here were review-flagged as satisfiable by unrelated code;
    # only the property a substring CAN carry remains:
    def test_join_page_reads_credentials_from_fragment_only(self):
        # join.js must never look for either credential in the query.
        assert 'query.get("channel_token")' not in JOIN_JS
        assert 'query.get("t")' not in JOIN_JS
        assert 'fragment.get("channel_token")' in JOIN_JS
        assert 'fragment.get("t")' in JOIN_JS


class TestOrgSelfDescription:
    def test_icon_guard_accepts_only_bounded_data_uris(self):
        import json as _json
        import shutil as _shutil
        import subprocess as _subprocess

        if _shutil.which("node") is None:
            import pytest as _pytest
            _pytest.skip("node not on PATH")
        module = str(BOOTLOADER_DIR / "join.js")
        script = (
            f"const api = require({_json.dumps(module)});"
            "const cases = {"
            "  good: api.safeIcon('data:image/png;base64,iVBORw0KGgo='),"
            "  remote: api.safeIcon('https://evil.example/icon.png'),"
            "  local: api.safeIcon('/static/orgs/x.png'),"
            "  script: api.safeIcon('data:text/html;base64,PHNjcmlwdD4='),"
            "  junk: api.safeIcon(12345),"
            "};"
            "process.stdout.write(JSON.stringify(cases));"
        )
        result = _subprocess.run(["node", "-e", script],
                                 capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        out = _json.loads(result.stdout)
        assert out["good"] == "data:image/png;base64,iVBORw0KGgo="
        for bad in ("remote", "local", "script", "junk"):
            assert out[bad] is None, bad

    def test_color_guard_accepts_only_bare_hex(self):
        # org_color themes the header border and nothing else; anything but
        # a six-digit hex literal is discarded (no CSS injection surface).
        import json as _json
        import shutil as _shutil
        import subprocess as _subprocess

        if _shutil.which("node") is None:
            import pytest as _pytest
            _pytest.skip("node not on PATH")
        module = str(BOOTLOADER_DIR / "join.js")
        script = (
            f"const api = require({_json.dumps(module)});"
            "const cases = {"
            "  good: api.safeColor('#5b3aa6'),"
            "  short: api.safeColor('#fff'),"
            "  css: api.safeColor('red; background:url(//evil)'),"
            "  func: api.safeColor('rgb(1,2,3)'),"
            "  junk: api.safeColor(42),"
            "};"
            "process.stdout.write(JSON.stringify(cases));"
        )
        result = _subprocess.run(["node", "-e", script],
                                 capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        out = _json.loads(result.stdout)
        assert out["good"] == "#5b3aa6"
        for bad in ("short", "css", "func", "junk"):
            assert out[bad] is None, bad

    def test_autonet_boots_only_on_share_links(self):
        # The bridge loads autonet.js for its channel primitives; the /l/
        # flow must not auto-boot there (it would render its error state
        # into a page with no bootloader UI).
        assert r'/^\/l\/[0-9a-f]{32}$/.test(location.pathname)' in AUTONET_JS
