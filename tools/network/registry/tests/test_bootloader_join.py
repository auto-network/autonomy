"""The bootloader assembles org-join context without leaking its bearer."""

from __future__ import annotations

import json
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
        f"pathname:'/l/'+{json.dumps(CHANNEL_TOKEN)}}};"
        "const factory=new Function("
        "'TextEncoder','crypto','location','URLSearchParams',src);"
        "const A=factory(TextEncoder,{subtle:{}},location,URLSearchParams);"
        f"const envelope={envelope_json};"
        f"const context=A.assembleJoinContext(envelope,{json.dumps(BEARER)});"
        "const destination=A.deliverJoinContext(context);"
        "let blankRejected=false, longRejected=false, badRefRejected=false;"
        "try{A.assembleJoinContext(envelope,'');}catch(e){blankRejected=true;}"
        "try{A.assembleJoinContext(envelope,'x'.repeat(129));}"
        "catch(e){longRejected=true;}"
        "try{A.assembleJoinContext({...envelope,invite_ref:'BC'.repeat(32)},'x');}"
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
        "rootPub": ROOT_PUB,
        "inviteRef": INVITE_REF,
        "token": BEARER,
    }
    assert output["navigations"] == [output["destination"]]
    destination = urlsplit(output["destination"])
    assert destination.path == "/network/join"
    assert parse_qs(destination.query) == {
        "org": [ORG],
        "root_pub": [ROOT_PUB],
        "invite_ref": [INVITE_REF],
    }
    # BOTH credentials ride the fragment (auto-y7nap, relay review): the
    # channel token is bearer-class — possession opens the registry
    # channel — so it must not create a second server-visible URL surface.
    assert parse_qs(destination.fragment) == {
        "channel_token": [CHANNEL_TOKEN],
        "t": [BEARER],
    }
    # Neither credential ever rides the query, under any name.
    assert BEARER not in destination.query
    assert CHANNEL_TOKEN not in destination.query
    assert output["blankRejected"] is True
    assert output["longRejected"] is True
    assert output["badRefRejected"] is True
