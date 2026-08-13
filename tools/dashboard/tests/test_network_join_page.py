"""The invite bridge's local-origin half (auto-1ihgz) — shell contract.

Display/consent ONLY, under the I1 hard constraints from MC's ingress
audit and relay's endpoint trace: no input fields, no password anywhere,
no invocation of the headless join endpoint, no ceremony code. The accept
action leads to an honest held state until auto-9rw91 rules the
acceptance mechanics.
"""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

DASHBOARD = Path(__file__).resolve().parents[1]
TEMPLATE = (DASHBOARD / "templates" / "network-join.html").read_text(
    encoding="utf-8"
)
PAGE_JS = (DASHBOARD / "static" / "js" / "network-join.js").read_text(
    encoding="utf-8"
)


def _client():
    from tools.dashboard.server import app

    return TestClient(app)


class TestRoute:
    def test_serves_one_static_shell_regardless_of_query(self):
        client = _client()
        bare = client.get("/network/join")
        full = client.get(
            "/network/join?org=11111111-1111-4111-8111-111111111111"
            "&root_pub=" + "a" * 64 + "&invite_ref=" + "e" * 64
        )
        assert bare.status_code == full.status_code == 200
        assert bare.text == full.text  # neutrality: query never interpolated
        assert bare.text.strip() == TEMPLATE.strip()
        # The dashboard's global cache middleware rewrites Cache-Control;
        # the property that matters is that the shell is never cached.
        assert "no-store" in bare.headers["cache-control"] \
            or "no-cache" in bare.headers["cache-control"]
        assert bare.headers["referrer-policy"] == "no-referrer"
        assert bare.headers["x-content-type-options"] == "nosniff"


class TestI1Constraints:
    """The properties the three-pillar convergence demands."""

    def test_exactly_one_input_and_it_is_the_link_field(self):
        # The paste step owns the flow's single input — a visible url
        # field on its own screen. Nothing password-shaped can ever
        # appear here.
        lowered = TEMPLATE.lower()
        assert lowered.count("<input") == 1
        assert 'type="url"' in lowered
        body = lowered[lowered.index("<body"):]
        for forbidden in ("<form", 'type="password"', "password"):
            assert forbidden not in body, forbidden

    def test_no_network_calls_in_page_script(self):
        for forbidden in ("fetch(", "xmlhttprequest", "websocket",
                          "navigator.sendbeacon", "/api/"):
            assert forbidden not in PAGE_JS.lower(), forbidden

    def test_no_ceremony_code(self):
        # TO THE IMPLEMENTER OF ACCEPTANCE MECHANICS (auto-9rw91): when the
        # ruling lands, the correct implementation is browser-side
        # decrypt-and-sign — which needs exactly the WebCrypto calls this
        # list forbids. CHANGE THIS TEST DELIBERATELY and say so in your
        # commit message: narrow it to "ceremony code only via the audited
        # ceremony/ modules, passphrase still never in any request", rather
        # than quietly relaxing the list. A test edited to ship the right
        # thing is fine; a test loosened without anyone noticing is how the
        # property dies. (MC review, 2026-08-13.)
        for forbidden in ("subtle", "crypto.", "decrypt", "derivepersona",
                          "signevent", "keypair", "armor"):
            assert forbidden not in PAGE_JS.lower(), forbidden

    def test_credentials_read_from_fragment_only(self):
        assert 'fragment.get("channel_token")' in PAGE_JS
        assert 'fragment.get("t")' in PAGE_JS
        assert 'query.get("channel_token")' not in PAGE_JS
        assert 'query.get("t")' not in PAGE_JS

    def test_nothing_is_promised_that_does_not_work(self):
        # Operator product rule: UI shows what works. The paste step's
        # Next button is fully functional; the ORGANIZATION step carries
        # no controls until the ceremony makes accepting real (auto-9rw91
        # adds control and function together). No internal narration in
        # user copy.
        org_step = TEMPLATE[TEMPLATE.index('id="step-org"'):
                            TEMPLATE.index('id="step-broken"')]
        assert "<button" not in org_step.lower()
        for leaked in ("under review", "still being finished", "ceremony",
                       "passphrase", "held", "pending ruling"):
            assert leaked not in TEMPLATE.lower(), leaked
