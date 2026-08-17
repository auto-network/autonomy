"""The bootloader's JS canonical-JSON must match idkit byte-for-byte.

The viewer verifies the SERVER_HELLO's delegation chain in
``autonet.js``, reconstructing each cert's signed bytes with a JS
``canonicalJson``. If that ever drifts from ``idkit.canonical_json``, a
genuine chain would fail to verify (viewers break) or — worse — the
transcript hash would diverge and channels would silently fail to
establish. This pins the two encoders together.

Skips cleanly where Node.js is unavailable; the encoder is exercised
live in the browser harness regardless.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.network.idkit import canonical_json

AUTONET_JS = Path(__file__).resolve().parents[1] / "bootloader" / "autonet.js"
AUTONET_TEST_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "relaykit" / "tests" / "autonet_test_source.cjs"
)

CASES = [
    {"v": 1, "eph_pub": "ab" * 32},
    {"z": 1, "a": 2, "m": [3, 2, 1]},
    {"scope": ["link:publish", "tunnel:serve"], "org": "11111111-1111-4111-8111-111111111111"},
    {"s": 'tab\tnewline\nquote"backslash\\end'},
    {"unicode": "café é ☃ end", "emoji": "\U0001f600"},
    {"nested": {"b": {"c": [1, {"d": True, "e": None}]}, "a": 0}},
    {"ctrl": "\x00\x01\x1f\x7f"},
    {"big": 9007199254740992, "neg": -42, "zero": 0},
    # A realistic delegation-cert payload shape.
    {"v": 1, "child_pub": "cd" * 32, "scope": ["link:publish"], "org": "o",
     "subject": {"kind": "agent", "id": "sess-1"}, "not_before": 100, "not_after": 200},
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_js_canonical_json_matches_idkit():
    cases_json = json.dumps(CASES)
    script = (
        f"let src=require({json.dumps(str(AUTONET_TEST_SOURCE))}).loadAutonetTestSource();"
        "src=src.replace(/window\\.autonet = autonet;[\\s\\S]*$/,'return autonet;');"
        "src=src.replace(/^const autonet = \\(\\(\\) => \\{/,'');"
        "const factory=new Function('TextEncoder','crypto',src);"
        "const A=factory(TextEncoder,{subtle:{}});"
        "const cases=JSON.parse(process.argv[1]);"
        "process.stdout.write(JSON.stringify(cases.map(c=>A.canonicalJson(c))));"
    )
    result = subprocess.run(
        ["node", "-e", script, cases_json],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    js_encoded = json.loads(result.stdout)
    py_encoded = [canonical_json(c).decode("ascii") for c in CASES]
    assert js_encoded == py_encoded
