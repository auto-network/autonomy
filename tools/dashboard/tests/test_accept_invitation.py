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

    def test_share_link_routes_to_the_pasted_url_verbatim(self):
        out = self._run(
            "process.stdout.write(JSON.stringify("
            f"api.parseInvitationLink({json.dumps(SHARE)})))"
        )
        assert out == {"kind": "bridge", "destination": SHARE}

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

    def test_navigation_fires_only_on_success(self):
        out = self._run(
            "const calls=[];"
            f"api.acceptPastedLink({json.dumps(SHARE)},(d)=>calls.push(d));"
            "process.stdout.write(JSON.stringify(calls))"
        )
        assert out == [SHARE]
