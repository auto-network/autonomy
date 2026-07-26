"""The AUTONOMY_INVITE codec: round-trip, fail-closed, bearer containment."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from tools.network.invitation import (
    INVITE_VERSION,
    Invitation,
    InvitationError,
    decode_invitation,
    encode_invitation,
)

ORG = "018f6b2a-7c4d-7e11-8a3b-9d5c1e2f4a6b"
ROOT_PUB = "ab" * 32
INVITE_REF = "cd" * 32
TOKEN = "ef" * 32


def sample() -> Invitation:
    return Invitation(org=ORG, root_pub=ROOT_PUB, invite_ref=INVITE_REF, token=TOKEN)


def _recode(**overrides) -> str:
    body = {
        "v": INVITE_VERSION, "org": ORG, "root_pub": ROOT_PUB,
        "invite_ref": INVITE_REF, "token": TOKEN,
    }
    body.update(overrides)
    for drop in [k for k, v in overrides.items() if v is _MISSING]:
        del body[drop]
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    payload = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{payload}.{hashlib.sha256(raw).hexdigest()[:8]}"


_MISSING = object()


def test_round_trip():
    decoded = decode_invitation(encode_invitation(sample()))
    assert decoded == sample()
    assert decoded.token == TOKEN
    assert decoded.token_hash == hashlib.sha256(TOKEN.encode()).hexdigest()


def test_token_hash_matches_the_invite_commitment():
    """The fold verifies sha256(token) == invite.token_hash; the codec's
    view of that must be byte-identical or a valid code reads as a bad one."""
    from tools.network.idkit import generate_token

    token = generate_token()
    inv = Invitation(org=ORG, root_pub=ROOT_PUB, invite_ref=INVITE_REF, token=token)
    assert inv.token_hash == hashlib.sha256(token.encode("utf-8")).hexdigest()


# -- bearer containment -------------------------------------------------------------


def test_bearer_is_redacted_from_debug_surfaces():
    """This value arrives as an env var — visible to docker inspect and to
    anything that logs a config object. Recovering it must take asking."""
    inv = sample()
    for rendering in (repr(inv), str(inv), f"{inv}", "{}".format(inv)):
        assert TOKEN not in rendering
        assert "redacted" in rendering
    # The org-identifying fields stay legible: redaction is not obscurity.
    assert ORG in repr(inv)
    # And the token is still reachable when asked for by name.
    assert inv.token == TOKEN


def test_dataclass_repr_of_a_container_does_not_leak():
    """A bare dataclass would leak through any structure holding it."""
    assert TOKEN not in repr({"invite": sample()})
    assert TOKEN not in repr([sample()])


# -- fail closed ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "", "   ", "not-an-invitation", "noseparator",
        "!!!!.abcdefgh",  # undecodable payload
    ],
)
def test_malformed_codes_rejected(code):
    with pytest.raises(InvitationError):
        decode_invitation(code)


def test_truncation_is_caught_by_the_checksum():
    code = encode_invitation(sample())
    payload, _, checksum = code.rpartition(".")
    truncated = f"{payload[:-4]}.{checksum}"
    with pytest.raises(InvitationError, match="truncated or edited"):
        decode_invitation(truncated)


def test_edited_body_is_caught_by_the_checksum():
    """Swapping the org for another one must not silently redirect a join."""
    code = encode_invitation(sample())
    payload, _, checksum = code.rpartition(".")
    other = _recode(org="018f6b2a-7c4d-7e11-8a3b-000000000000").rpartition(".")[0]
    with pytest.raises(InvitationError, match="truncated or edited"):
        decode_invitation(f"{other}.{checksum}")


@pytest.mark.parametrize(
    "overrides",
    [
        {"v": 2},                                  # unsupported version
        {"org": "not-a-uuid"},
        {"root_pub": "zz" * 32},                   # not hex
        {"root_pub": "AB" * 32},                   # uppercase
        {"invite_ref": "ab" * 31},                 # wrong length
        {"token": ""},
        {"token": 1234},
        {"extra": "field"},                        # unknown field
        {"token": _MISSING},                       # missing field
    ],
)
def test_field_validation_is_fail_closed(overrides):
    with pytest.raises(InvitationError):
        decode_invitation(_recode(**overrides))


def test_no_network_shape_escapes_a_bad_code():
    """Decode either returns a fully-validated invitation or raises; there
    is no partial object for a caller to act on."""
    with pytest.raises(InvitationError):
        decode_invitation(_recode(root_pub="zz" * 32))
