from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from tools.network.registry import turn_credentials
from tools.network.registry.turn_credentials import (
    TURN_CREDENTIAL_TTL_SECONDS,
    TurnCredentialError,
    TurnCredentialIssuer,
    load_turn_secrets,
)
from tools.network.relaykit.ice_signaling import STUN_URL, TURN_URLS


SECRET_A = "a" * 64
SECRET_B = "b" * 64


def test_secret_file_accepts_rotation_pair_and_rejects_ambiguous_shapes(tmp_path):
    path = tmp_path / "turn-rest-secrets"
    path.write_text(f"{SECRET_A}\n{SECRET_B}\n", encoding="ascii")
    assert load_turn_secrets(path) == (SECRET_A, SECRET_B)

    invalid_values = (
        "",
        "A" * 64,
        f"{SECRET_A}\n{SECRET_A}\n",
        f"{SECRET_A}\n{SECRET_B}\n{'c' * 64}\n",
    )
    for invalid in invalid_values:
        path.write_text(invalid, encoding="ascii")
        with pytest.raises(TurnCredentialError):
            load_turn_secrets(path)


def test_issuer_uses_last_rotation_secret_and_emits_frozen_opaque_shape():
    issuer = TurnCredentialIssuer(
        (SECRET_A, SECRET_B),
        clock=lambda: 1_800_000_000,
        token_hex=lambda size: "c" * (size * 2),
    )
    configuration = issuer.issue("org-name-that-must-not-reach-the-username")
    stun, turn = configuration.ice_servers

    assert configuration.expires_at == 1_800_000_000 + TURN_CREDENTIAL_TTL_SECONDS
    assert stun == {"urls": [STUN_URL]}
    assert turn["urls"] == list(TURN_URLS)
    assert turn["credentialType"] == "password"
    assert turn["username"] == f"{configuration.expires_at}:{'c' * 32}"
    assert "org-name" not in turn["username"]
    expected = hmac.new(
        SECRET_B.encode("ascii"),
        turn["username"].encode("ascii"),
        hashlib.sha1,
    ).digest()
    assert turn["credential"] == base64.b64encode(expected).decode("ascii")


def test_issuer_refuses_only_after_the_absurd_abuse_tripwire(monkeypatch):
    monkeypatch.setattr(turn_credentials, "TURN_ISSUANCE_PER_ORG", 2)
    issuer = TurnCredentialIssuer(
        (SECRET_A,), clock=lambda: 1000, token_hex=lambda _size: "d" * 32
    )
    issuer.issue("org-a")
    issuer.issue("org-a")
    with pytest.raises(TurnCredentialError, match="temporarily unavailable"):
        issuer.issue("org-a")
    issuer.issue("org-b")
