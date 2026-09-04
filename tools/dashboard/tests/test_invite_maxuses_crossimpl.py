"""A browser-built multi-use invite event verifies under the Python validator.

Multi-use bearer invitations (auto-2o30f) add an optional ``max_uses`` to the
invite event. The browser mints the event (invitation.js buildInviteBody) and
the server validates it through the same event validator; this drives the real
JS and asserts the Python ``validate_payload`` accepts the max_uses body,
rejects the absent-vs-present shapes consistently, and refuses invite_pub +
max_uses on BOTH sides.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger.events import SchemaError, validate_payload


REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_DRIVER = (
    REPO_ROOT
    / "tools/dashboard/static/js/ceremony/tests/invite-maxuses-vector.mjs"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_browser_built_max_uses_invite_verifies_server_side():
    sponsor = KeyPair.generate().public_hex
    fixture = {
        "role": "member",
        "expiry": 1_900_000_000_000,
        "sponsor": sponsor,
        "token_hash": hashlib.sha256(b"a-bearer-token").hexdigest(),
        "max_uses": 3,
    }
    result = subprocess.run(
        ["node", str(NODE_DRIVER), json.dumps(fixture)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    out = json.loads(result.stdout)

    # The browser-built multi-use body carries max_uses and passes the server
    # validator unchanged.
    assert out["multi"]["max_uses"] == 3
    assert validate_payload(out["multi"]) == "invite"

    # Absent means single-use: the field is simply omitted (byte-identical to a
    # legacy invite), and still valid.
    assert "max_uses" not in out["single"]
    assert validate_payload(out["single"]) == "invite"

    # invite_pub + max_uses is refused in the browser AND by the validator.
    assert out["pub_plus_maxuses_error"] and "token" in out["pub_plus_maxuses_error"]
    with pytest.raises(SchemaError):
        validate_payload({
            "type": "invite",
            "granted_role": "member",
            "expiry": fixture["expiry"],
            "sponsor": sponsor,
            "invite_pub": sponsor,
            "max_uses": 3,
        })
