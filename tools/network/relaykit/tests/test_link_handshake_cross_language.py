"""Python↔JS byte compatibility for the per-link handshake surfaces.

The per-link mode (graph://807b4e11-3e9) adds exactly two byte surfaces the
two implementations must agree on: the signed payload (unchanged shape) and
the transcript preimage, which now binds ``link_pub`` instead of ``cert``.
Key derivation is HKDF over the transcript, so a preimage mismatch means the
two ends derive different channel keys and interop silently dies. This test
renders both surfaces in Python and in the shipped relaykit-core.js under
node and asserts byte equality. Skipped when node is absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.network.idkit import canonical_json

RELAYKIT_CORE = (
    Path(__file__).resolve().parents[3]
    / "dashboard" / "static" / "js" / "lib" / "relaykit-core.js"
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")

ORG = "00000000-0000-4000-8000-0000000000dd"
TOKEN = "d1" * 16
CLIENT_EPH = "aa" * 32
SERVER_EPH = "bb" * 32
LINK_PUB = "cc" * 32

_JS = r"""
import { pathToFileURL } from 'node:url';
const A = await import(pathToFileURL(process.argv[2]).href);
const [org, token, clientEph, serverEph, linkPub] = process.argv.slice(3);
const out = {
  signed: A.canonicalJson({
    v: 1, org, token, client_eph: clientEph, server_eph: serverEph,
  }),
  transcript_preimage: A.canonicalJson({
    v: 1, org, token, client_eph: clientEph, server_eph: serverEph,
    link_pub: linkPub,
  }),
};
process.stdout.write(JSON.stringify(out));
"""


def test_signed_payload_and_link_transcript_preimage_match(tmp_path):
    script = tmp_path / "surface.mjs"
    script.write_text(_JS)
    result = subprocess.run(
        ["node", str(script), str(RELAYKIT_CORE),
         ORG, TOKEN, CLIENT_EPH, SERVER_EPH, LINK_PUB],
        capture_output=True, text=True, timeout=60, check=True,
    )
    js = json.loads(result.stdout)

    py_signed = canonical_json({
        "v": 1, "org": ORG, "token": TOKEN,
        "client_eph": CLIENT_EPH, "server_eph": SERVER_EPH,
    }).decode()
    py_transcript = canonical_json({
        "v": 1, "org": ORG, "token": TOKEN,
        "client_eph": CLIENT_EPH, "server_eph": SERVER_EPH,
        "link_pub": LINK_PUB,
    }).decode()

    assert js["signed"] == py_signed
    assert js["transcript_preimage"] == py_transcript
