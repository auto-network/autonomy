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

    def test_page_network_is_exactly_the_one_ruled_resolve_call(self):
        # DELIBERATE CHANGE (2026-08-14): the operator ruled the
        # paste-into-own-dashboard design — the page resolves invitations on
        # its OWN origin (auto-yw5gz). The former no-network pin therefore
        # narrows, consciously, to: exactly one fetch, to exactly the resolve
        # endpoint, whose body carries transport credentials only. The bearer
        # still never leaves the browser.
        lowered = PAGE_JS.lower()
        assert lowered.count("fetch(") == 1
        assert 'fetch("/api/network/invite/resolve"' in PAGE_JS
        assert lowered.count("/api/") == 1
        for forbidden in ("xmlhttprequest", "websocket",
                          "navigator.sendbeacon"):
            assert forbidden not in lowered, forbidden
        # The SENT body shape is an allowlist, not just the declaration
        # (adversarial review 2026-08-14: pinning the declaration alone let a
        # later `body.t = heldBearer` mutation ship green). Every write to the
        # body object, anywhere in the script, must stay inside the allowed
        # public-field set.
        import re

        assert ("var body = { relay_host: relayHost, "
                "channel_token: channelToken };") in PAGE_JS
        mutations = set(re.findall(r"body\.(\w+)\s*=", PAGE_JS))
        assert mutations <= {"org", "root_pub", "invite_ref"}, mutations
        assert not re.search(r"body\s*\[", PAGE_JS)  # no dynamic-key writes

    def test_the_bearer_never_reaches_any_request(self):
        # heldBearer exists to be HELD for the future accept ceremony. It may
        # be assigned from parsed link input; it may never flow toward the
        # network: not into the resolve body, not into fetch, not serialized.
        import re

        uses = [m.start() for m in re.finditer(r"heldBearer", PAGE_JS)]
        assert uses, "the held bearer disappeared — reassess this guard"
        for idx in uses:
            line_start = PAGE_JS.rfind("\n", 0, idx) + 1
            line = PAGE_JS[line_start:PAGE_JS.index("\n", idx)]
            ok = re.match(r"\s*(var\s+)?heldBearer\s*=", line)
            assert ok, f"heldBearer used outside plain assignment: {line.strip()}"
        resolve_fn = PAGE_JS[PAGE_JS.index("function resolveOnOrigin"):
                             PAGE_JS.index("function showPasteStep")]
        assert "heldBearer" not in resolve_fn
        assert "bearer" not in resolve_fn.lower().replace(
            "// the bearer is deliberately absent.", "")

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
