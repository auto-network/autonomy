"""The invite bridge's local-origin half (auto-1ihgz) — shell contract.

Display/consent ONLY, under the I1 hard constraints from MC's ingress
audit and relay's endpoint trace: no input fields, no password anywhere,
no invocation of the headless join endpoint, no ceremony code. The accept
action leads to an honest held state until auto-9rw91 rules the
acceptance mechanics.
"""

from __future__ import annotations

import re
from pathlib import Path

from starlette.testclient import TestClient

DASHBOARD = Path(__file__).resolve().parents[1]
# The page is an SPA fragment inside the dashboard shell: /network/join
# serves the shell, /pages/network-join serves this markup.
TEMPLATE = (DASHBOARD / "templates" / "pages" / "network-join.html").read_text(
    encoding="utf-8"
)
PAGE_JS = (DASHBOARD / "static" / "js" / "network-join.js").read_text(
    encoding="utf-8"
)
RELAYKIT_CORE = DASHBOARD / "static" / "js" / "lib" / "relaykit-core.js"
CONTROLLER_JS = (
    DASHBOARD / "static" / "js" / "join" / "accept-controller.js"
).read_text(encoding="utf-8")


def _client():
    from tools.dashboard.server import app

    return TestClient(app)


class TestRoute:
    def test_serves_one_static_shell_regardless_of_query(self):
        client = _client()
        bare = client.get("/network/join")
        full = client.get(
            "/network/join?org=11111111-1111-4111-8111-111111111111"
            "&invite_ref=" + "e" * 64
        )
        assert bare.status_code == full.status_code == 200
        assert bare.text == full.text  # neutrality: query never interpolated
        # The dashboard shell is served; the SPA router injects the page.
        assert 'id="content"' in bare.text
        assert "network-join.js" in bare.text
        # The dashboard's global cache middleware rewrites Cache-Control;
        # the property that matters is that the shell is never cached.
        assert "no-store" in bare.headers["cache-control"] \
            or "no-cache" in bare.headers["cache-control"]
        assert bare.headers["referrer-policy"] == "no-referrer"
        assert bare.headers["x-content-type-options"] == "nosniff"

    def test_fragment_is_the_static_page_markup(self):
        client = _client()
        bare = client.get("/pages/network-join")
        full = client.get("/pages/network-join?org=x&invite_ref=y")
        assert bare.status_code == full.status_code == 200
        assert bare.text == full.text  # the server reads neither query nor fragment
        assert bare.text.strip() == TEMPLATE.strip()
        assert "no-store" in bare.headers["cache-control"] \
            or "no-cache" in bare.headers["cache-control"]
        assert bare.headers["referrer-policy"] == "no-referrer"

    def test_serves_the_shared_relaykit_core_own_origin(self):
        response = _client().get("/static/js/lib/relaykit-core.js")
        assert response.status_code == 200
        assert response.content == RELAYKIT_CORE.read_bytes()

class TestI1Constraints:
    """The properties the three-pillar convergence demands."""

    def test_exactly_one_input_and_it_is_the_link_field(self):
        # The paste step owns the flow's single input — a visible url
        # field on its own screen. Nothing password-shaped can ever
        # appear here.
        lowered = TEMPLATE.lower()
        assert lowered.count("<input") == 1
        assert 'type="url"' in lowered
        for forbidden in ("<form", 'type="password"', "password"):
            assert forbidden not in lowered, forbidden

    def test_page_resolves_public_routing_on_own_origin(self):
        # Exactly four same-origin calls: the routing envelope for a pasted
        # link, this person's own Personal profile for the Joining-as tile,
        # the install of the organization on THIS machine once the org's
        # ledger has admitted it, and the one-shot registry re-adoption when
        # the bundle's record did not fold. Nothing else leaves the page.
        lowered = PAGE_JS.lower()
        assert lowered.count("fetch(") == 4
        assert 'fetch("/api/network/invite/resolve"' in lowered
        assert 'fetch("/api/identity/profile"' in lowered
        assert 'fetch("/api/network/join/outcome"' in lowered
        assert 'fetch("/api/network/membership-checkpoint/adopt"' in lowered
        assert "/envelope" not in lowered
        assert "root_pub" not in lowered
        for forbidden in ("xmlhttprequest", "websocket",
                          "navigator.sendbeacon"):
            assert forbidden not in lowered, forbidden

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
        resolve_fn = PAGE_JS[PAGE_JS.index("function fetchEnvelope"):
                             PAGE_JS.index("function showPasteStep")]
        assert "heldBearer" not in resolve_fn
        assert "bearer" not in resolve_fn.lower()

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
        assert 'fragment.get("k")' in PAGE_JS
        assert 'fragment.get("t")' in PAGE_JS
        assert 'query.get("channel_token")' not in PAGE_JS
        assert 'query.get("t")' not in PAGE_JS

    def test_failure_classes_all_keep_the_action_disabled(self):
        # Incomplete links never enter the org step. Envelope/transport and
        # authenticated ledger closure flow through reportTerminal, which
        # hides the action; transcript failure remains a distinct controller
        # state so the page cannot dress it up as ledger truth.
        assert '$("step-broken").classList.remove("hidden")' in PAGE_JS
        terminal = PAGE_JS[PAGE_JS.index("function reportTerminal"):
                           PAGE_JS.index("function wireAccept")]
        assert 'show("accept-block", false)' in terminal
        assert 'button.classList.add("hidden")' in terminal
        assert "state: 'security'" in CONTROLLER_JS
        assert "state: 'link-lost'" in CONTROLLER_JS
        assert "state: 'closed'" in CONTROLLER_JS

    def test_envelope_never_supplies_human_presentation(self):
        envelope = PAGE_JS[PAGE_JS.index("function fetchEnvelope"):
                           PAGE_JS.index("function showPasteStep")]
        for field in ("org_name", "org_description", "org_icon", "org_color",
                      "sponsor_name", "sponsor_avatar"):
            assert field not in envelope
        # Rendering is fed only by the authenticated JoinSession result.
        assert "session.brand.orgName" in PAGE_JS

    def test_nothing_is_promised_that_does_not_work(self):
        # Operator product rule: UI shows what works. auto-9rw91 added the
        # accept control AND its function together, which is what this test
        # now holds: the organization step carries exactly one control, and
        # the page wires it to a real join session rather than rendering a
        # button that does nothing. The previous form of this test asserted
        # the step had NO controls, which was correct only while accepting
        # was unimplemented — it is superseded, not loosened.
        org_step = TEMPLATE[TEMPLATE.index('id="step-org"'):
                            TEMPLATE.index('id="step-broken"')]
        assert org_step.lower().count("<button") == 1
        assert 'id="accept"' in org_step
        # The control's function: a session is connected, the action is
        # bound, and the invitee signs ONCE; under an approval role the
        # approver's admission event carries that signed claim, so there is
        # no second submit on this page.
        assert "connectSession(" in PAGE_JS
        assert 'button.addEventListener("click"' in PAGE_JS
        assert "finalize" not in PAGE_JS
        # The control appears only once the organization has answered.
        assert 'show("accept-block", true)' in PAGE_JS
        for leaked in ("under review", "still being finished", "ceremony",
                       "passphrase", "held", "pending ruling"):
            assert leaked not in TEMPLATE.lower(), leaked


def test_install_outcome_forwards_seed_rows_and_the_bundled_checkpoint():
    """network-join.js installAdmitted POSTs /api/network/join/outcome with the
    sponsor's reachability_rows (the install seed's addresses; keyreg defect
    install_seed_addresses) and the bundled checkpoint (rule bundle_adopt).
    The first real join (2026-09-25) forwarded neither."""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "static/js/network-join.js").read_text()
    start = js.index('fetch("/api/network/join/outcome"')
    body = js[start:js.index("}).then", start)]
    assert "reachability_rows: material.reachability_rows" in body
    assert "checkpoint: material.checkpoint" in body


def test_install_finishes_the_machine_set_up_in_the_same_opening():
    """C6: after the install, the page runs the sign-in's own three phases
    (fetchPreparation, prepareSignon, submitSignon) with the root it held
    from the claim, or opens the root once more after a delayed admission;
    the held seed is zeroed the moment the claim goes pending and after the
    set-up either way; the outcome is said on its own line."""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "static/js/network-join.js").read_text()
    assert 'import("/static/js/ceremony/signon-phases.js")' in js
    for call in ("phases.fetchPreparation(", "phases.prepareSignon(", "phases.submitSignon("):
        assert call in js, call
    assert "heldSeed = new Uint8Array(opened.seed)" in js
    assert js.count("zeroHeld()") >= 5
    pending = js.index('if (result.state === "pending")')
    assert "zeroHeld()" in js[pending:pending + 60]
    assert 'say("setup-line"' in js
    assert 'id="setup-line"' in TEMPLATE


def test_recovery_adoption_fires_only_when_install_did_not_adopt_and_once():
    """The one recovery call to /api/network/membership-checkpoint/adopt is
    guarded by installed.checkpoint.ok === false, appears exactly once, and
    its failure is swallowed (the model's liveness does not rest on it)."""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "static/js/network-join.js").read_text()
    assert js.count('fetch("/api/network/membership-checkpoint/adopt"') == 1
    call = js.index('fetch("/api/network/membership-checkpoint/adopt"')
    guard = js.rindex("if (", 0, call)
    assert "installed.checkpoint.ok === false" in js[guard:call]
    assert ".catch(function () { return null; })" in js[call:call + 400]
