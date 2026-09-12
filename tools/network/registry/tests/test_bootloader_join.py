"""The bootloader assembles org-join context without leaking its bearer."""

from __future__ import annotations

import json
import base64
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


AUTONET_JS = Path(__file__).resolve().parents[1] / "bootloader" / "autonet.js"
AUTONET_TEST_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "relaykit" / "tests" / "autonet_test_source.cjs"
)
ORG = "11111111-1111-4111-8111-111111111111"
ROOT_PUB = "ab" * 32
LINK_KEY = base64.urlsafe_b64encode(bytes.fromhex("cd" * 32)).decode().rstrip("=")
INVITE_REF = "bc" * 32
BEARER = "01" * 32
CHANNEL_TOKEN = "7f" * 16  # the /l/<token> path segment (registry-visible)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_join_context_and_fragment_only_delivery() -> None:
    envelope_json = json.dumps(
        {"org": ORG, "root_pub": ROOT_PUB, "invite_ref": INVITE_REF}
    )
    script = (
            f"let src=require({json.dumps(str(AUTONET_TEST_SOURCE))}).loadAutonetTestSource();"
        "src=src.replace(/window\\.autonet = autonet;[\\s\\S]*$/,'return autonet;');"
        "src=src.replace(/^const autonet = \\(\\(\\) => \\{/,'');"
        "const navigations=[];"
        "const location={assign:(url)=>navigations.push(url),"
        f"origin:'https://relay.auto.network',pathname:'/l/'+{json.dumps(CHANNEL_TOKEN)}}};"
        "const factory=new Function("
        "'TextEncoder','crypto','location','URLSearchParams',src);"
        "const A=factory(TextEncoder,{subtle:{}},location,URLSearchParams);"
        f"const envelope={envelope_json};"
        f"const context=A.assembleJoinContext(envelope,{json.dumps(BEARER)},{json.dumps(LINK_KEY)});"
        "const destination=A.deliverJoinContext(context);"
        "let blankRejected=false, longRejected=false, badRefRejected=false;"
        f"try{{A.assembleJoinContext(envelope,'',{json.dumps(LINK_KEY)});}}catch(e){{blankRejected=true;}}"
        f"try{{A.assembleJoinContext(envelope,'x'.repeat(129),{json.dumps(LINK_KEY)});}}"
        "catch(e){longRejected=true;}"
        f"try{{A.assembleJoinContext({{...envelope,invite_ref:'BC'.repeat(32)}},'x',{json.dumps(LINK_KEY)});}}"
        "catch(e){badRefRejected=true;}"
        "process.stdout.write(JSON.stringify({context,destination,navigations,"
        "blankRejected,longRejected,badRefRejected}));"
    )
    result = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)

    assert output["context"] == {
        "org": ORG,
        "inviteRef": INVITE_REF,
        "relayHost": "https://relay.auto.network",
        "token": BEARER,
        "linkKey": LINK_KEY,
    }
    assert output["navigations"] == [output["destination"]]
    destination = urlsplit(output["destination"])
    assert destination.path == "/network/join"
    assert parse_qs(destination.query) == {
        "org": [ORG],
        "invite_ref": [INVITE_REF],
        "relay_host": ["https://relay.auto.network"],
    }
    # BOTH credentials ride the fragment (auto-y7nap, relay review): the
    # channel token is bearer-class — possession opens the registry
    # channel — so it must not create a second server-visible URL surface.
    assert parse_qs(destination.fragment) == {
        "channel_token": [CHANNEL_TOKEN],
        "k": [LINK_KEY],
        "t": [BEARER],
    }
    # Neither credential ever rides the query, under any name.
    assert BEARER not in destination.query
    assert LINK_KEY not in destination.query
    assert CHANNEL_TOKEN not in destination.query
    assert BEARER not in envelope_json
    assert LINK_KEY not in envelope_json
    assert output["blankRejected"] is True
    assert output["longRejected"] is True
    assert output["badRefRejected"] is True
