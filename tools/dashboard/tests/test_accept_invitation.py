"""The accept-invitation paste flow (auto-a1xq3) — the ruled ingress.

MC's paste-security constraints, pinned structurally: the pasted link is
never sent to any server, never persisted, validated by shape only, and
routed by pure navigation. The parse logic lives in its own file exactly
so these absences are file-pinnable (the b91a8770 pattern, third use).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

DASHBOARD = Path(__file__).resolve().parents[1]
ACCEPT_JS = (DASHBOARD / "static" / "js" / "accept-invitation.js").read_text(
    encoding="utf-8"
)
INDICATOR_JS = (
    DASHBOARD / "static" / "js" / "identity-indicator.js"
).read_text(encoding="utf-8")
JOIN_JS = (DASHBOARD / "static" / "js" / "network-join.js").read_text(
    encoding="utf-8"
)
JOIN_HTML = (DASHBOARD / "templates" / "network-join.html").read_text(
    encoding="utf-8"
)
BASE_HTML = (DASHBOARD / "templates" / "base.html").read_text(encoding="utf-8")

TOKEN = "7f" * 16
SHARE = f"https://auto.network/l/{TOKEN}#t=bearer%20secret"
HANDOFF = (
    "https://registry.auto.network/network/join"
    "?org=11111111-1111-4111-8111-111111111111&root_pub=" + "a" * 64
    + "&invite_ref=" + "e" * 64 + "#channel_token=" + TOKEN + "&t=bearer"
)


class TestStructuralAbsences:
    def test_no_network_primitives(self):
        for forbidden in ("fetch(", "xmlhttprequest", "websocket",
                          "sendbeacon", "/api/"):
            assert forbidden not in ACCEPT_JS.lower(), forbidden

    def test_no_persistence(self):
        # A credential-bearing string that survives the tab is a credential
        # at rest in a place nobody audits (MC review).
        for forbidden in ("localstorage", "sessionstorage", "indexeddb",
                          "document.cookie"):
            assert forbidden not in ACCEPT_JS.lower(), forbidden

    def test_no_input_of_any_kind_in_the_chrome(self):
        # The chrome may DISPLAY Gate-1 state (the 'Password' method
        # label); it creates no fields at all — the paste screen owns
        # the flow's one input.
        assert 'type="password"' not in INDICATOR_JS.lower()
        assert "input.type" not in INDICATOR_JS


class TestBearerNeverEgresses:
    """auto-yw5gz: the page holds the bearer and resolves on its own origin
    with transport credentials only. These grep-level checks pin that the
    bearer is never placed in the one request the page makes."""

    def test_resolve_body_is_transport_credentials_only(self):
        # The single fetch body carries relay_host + channel_token (and, for a
        # handoff, the public org/root_pub/invite_ref). It must never carry the
        # bearer under any name.
        i = JOIN_JS.index("JSON.stringify(body)")
        # The request body object is assembled just above the fetch.
        assembly = JOIN_JS[JOIN_JS.index("var body = {"):i]
        for forbidden in ("bearer", "heldBearer", "parsed.bearer", '"t"', "'t'"):
            assert forbidden not in assembly, forbidden

    def test_the_bearer_is_held_never_stored(self):
        # Held in a closure for the ceremony; never persisted anywhere audited.
        assert "heldBearer" in JOIN_JS
        for forbidden in ("localstorage", "sessionstorage", "indexeddb",
                          "document.cookie"):
            assert forbidden not in JOIN_JS.lower(), forbidden

    def test_the_one_endpoint_is_the_local_resolve(self):
        # Same-origin path; no relay/registry origin is ever fetched by the page.
        assert "/api/network/invite/resolve" in JOIN_JS
        assert "auto.network" not in JOIN_JS  # never a cross-origin fetch

    def test_org_step_stays_button_free(self):
        # No Accept until the ceremony lands (auto-9rw91).
        org = JOIN_HTML[JOIN_HTML.index('id="step-org"'):
                        JOIN_HTML.index('id="step-broken"')]
        assert "<button" not in org.lower()


class TestMount:
    def test_panel_action_opens_the_full_page_flow(self):
        # The chrome holds no paste UI (operator: a cramped dropdown box is
        # not a workflow) — the action navigates to the paste SCREEN, which
        # owns the input and everything after.
        assert "location.assign('/network/join')" in INDICATOR_JS
        assert "acceptPastedLink" not in INDICATOR_JS
        assert "identity-panel-invite" not in INDICATOR_JS

    def test_join_page_owns_the_parse_module(self):
        template = (DASHBOARD / "templates" / "network-join.html").read_text(
            encoding="utf-8"
        )
        accept = template.index("accept-invitation.js")
        page = template.index("network-join.js")
        assert accept < page
        assert "accept-invitation.js" not in BASE_HTML


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
class TestParseBehavior:
    def _run(self, script: str) -> dict:
        module = str(DASHBOARD / "static" / "js" / "accept-invitation.js")
        result = subprocess.run(
            ["node", "-e",
             f"const api = require({json.dumps(module)});" + script],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def test_share_link_parses_to_transport_credentials_for_local_resolve(self):
        # auto-yw5gz: a /l/ share link no longer navigates to the relay
        # bridge. It parses to the transport credentials (relay origin +
        # channel token) plus the bearer, which the caller HOLDS — the
        # dashboard resolves the link on its own origin. No destination: the
        # navigation-to-relay is gone.
        out = self._run(
            "process.stdout.write(JSON.stringify("
            f"api.parseInvitationLink({json.dumps(SHARE)})))"
        )
        assert out == {
            "kind": "bridge",
            "relayHost": "https://auto.network",
            "channelToken": TOKEN,
            "bearer": "bearer secret",
        }
        assert "destination" not in out  # the relay bounce is gone

    def test_handoff_routes_locally_with_query_and_fragment_preserved(self):
        out = self._run(
            "process.stdout.write(JSON.stringify("
            f"api.parseInvitationLink({json.dumps(HANDOFF)})))"
        )
        assert out["kind"] == "local"
        assert out["destination"].startswith("/network/join?org=")
        assert "#channel_token=" + TOKEN in out["destination"]
        assert "registry.auto.network" not in out["destination"]

    def test_shape_errors_are_honest_and_navigation_free(self):
        cases = {
            "": "Paste an invitation link.",
            "not a url": "does not look like a link",
            "javascript:alert(1)": "does not look like a link",
            f"https://auto.network/l/{TOKEN}": "missing its secret part",
            "https://auto.network/network/join?org=x#t=y":
                "missing its invitation details",
            "https://example.com/other": "not an Autonomy invitation",
        }
        for pasted, expected in cases.items():
            out = self._run(
                "const calls=[];"
                f"const r=api.acceptPastedLink({json.dumps(pasted)},"
                "(d)=>calls.push(d));"
                "process.stdout.write(JSON.stringify({r, calls}))"
            )
            assert out["r"]["kind"] == "error", pasted
            assert expected in out["r"]["reason"], pasted
            assert out["calls"] == [], f"navigated on invalid paste: {pasted}"

    def test_handoff_navigates_but_a_share_link_resolves_in_place(self):
        # A handoff link navigates (fragment survives the hop). A share link
        # never navigates — it is handed to the resolve handler with its
        # transport credentials, so the bearer stays in the browser.
        out = self._run(
            "const nav=[],res=[];"
            f"api.acceptPastedLink({json.dumps(HANDOFF)},"
            "{navigate:(d)=>nav.push(d),resolve:(r)=>res.push(r)});"
            f"api.acceptPastedLink({json.dumps(SHARE)},"
            "{navigate:(d)=>nav.push(d),resolve:(r)=>res.push(r)});"
            "process.stdout.write(JSON.stringify({nav, res}))"
        )
        assert len(out["nav"]) == 1
        assert out["nav"][0].startswith("/network/join?org=")
        assert out["res"] == [{
            "kind": "bridge",
            "relayHost": "https://auto.network",
            "channelToken": TOKEN,
            "bearer": "bearer secret",
        }]

    def test_a_share_link_without_a_resolve_handler_stays_put(self):
        # Default dispatch has no resolve handler, so a share link parses but
        # is NOT acted on — the bearer never escapes by accident.
        out = self._run(
            "const nav=[];"
            f"const r=api.acceptPastedLink({json.dumps(SHARE)},(d)=>nav.push(d));"
            "process.stdout.write(JSON.stringify({kind:r.kind, nav}))"
        )
        assert out == {"kind": "bridge", "nav": []}
