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
    invitation_from_join_url,
)

ORG = "018f6b2a-7c4d-7e11-8a3b-9d5c1e2f4a6b"
ROOT_PUB = "ab" * 32
INVITE_REF = "cd" * 32
GRANT_TOKEN = "12" * 16
CLAIM_TOKEN = "ef" * 32


def sample() -> Invitation:
    return Invitation(
        org=ORG,
        root_pub=ROOT_PUB,
        invite_ref=INVITE_REF,
        channel_token=GRANT_TOKEN,
        claim_token=CLAIM_TOKEN,
    )


def _recode(**overrides) -> str:
    body = {
        "v": INVITE_VERSION, "org": ORG, "root_pub": ROOT_PUB,
        "invite_ref": INVITE_REF, "channel_token": GRANT_TOKEN,
        "claim_token": CLAIM_TOKEN,
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
    assert decoded.channel_token == GRANT_TOKEN
    assert decoded.claim_token == CLAIM_TOKEN
    assert decoded.token_hash == hashlib.sha256(CLAIM_TOKEN.encode()).hexdigest()


def test_token_hash_matches_the_invite_commitment():
    """The fold verifies sha256(token) == invite.token_hash; the codec's
    view of that must be byte-identical or a valid code reads as a bad one."""
    from tools.network.idkit import generate_token

    token = generate_token()
    inv = Invitation(
        org=ORG,
        root_pub=ROOT_PUB,
        invite_ref=INVITE_REF,
        channel_token=GRANT_TOKEN,
        claim_token=token,
    )
    assert inv.token_hash == hashlib.sha256(token.encode("utf-8")).hexdigest()


def test_join_url_assembles_the_two_domains_from_path_and_fragment():
    inv = invitation_from_join_url(
        org=ORG,
        root_pub=ROOT_PUB,
        invite_ref=INVITE_REF,
        join_url=f"https://relay.example/l/{GRANT_TOKEN}#t={CLAIM_TOKEN}",
    )
    assert inv == sample()


@pytest.mark.parametrize(
    "url",
    [
        f"https://relay.example/l/{GRANT_TOKEN}",
        f"https://relay.example/l/{GRANT_TOKEN}?t={CLAIM_TOKEN}",
        f"https://relay.example/not-a-link/{GRANT_TOKEN}#t={CLAIM_TOKEN}",
        f"https://relay.example/l/not-hex#t={CLAIM_TOKEN}",
        f"https://relay.example/l/{GRANT_TOKEN}#t={GRANT_TOKEN}",
        f"https://user:pass@relay.example/l/{GRANT_TOKEN}#t={CLAIM_TOKEN}",
    ],
)
def test_join_url_split_is_fail_closed(url):
    with pytest.raises(InvitationError):
        invitation_from_join_url(
            org=ORG,
            root_pub=ROOT_PUB,
            invite_ref=INVITE_REF,
            join_url=url,
        )


# -- bearer containment -------------------------------------------------------------


def test_bearer_is_redacted_from_debug_surfaces():
    """This value arrives as an env var — visible to docker inspect and to
    anything that logs a config object. Recovering it must take asking."""
    inv = sample()
    for rendering in (repr(inv), str(inv), f"{inv}", "{}".format(inv)):
        assert GRANT_TOKEN not in rendering
        assert CLAIM_TOKEN not in rendering
        assert "redacted" in rendering
    # The org-identifying fields stay legible: redaction is not obscurity.
    assert ORG in repr(inv)
    # Both tokens are still reachable when asked for by name.
    assert inv.channel_token == GRANT_TOKEN
    assert inv.claim_token == CLAIM_TOKEN


def test_dataclass_repr_of_a_container_does_not_leak():
    """A bare dataclass would leak through any structure holding it."""
    assert GRANT_TOKEN not in repr({"invite": sample()})
    assert CLAIM_TOKEN not in repr({"invite": sample()})
    assert GRANT_TOKEN not in repr([sample()])
    assert CLAIM_TOKEN not in repr([sample()])


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
        {"v": 3},                                  # unsupported version
        {"org": "not-a-uuid"},
        {"root_pub": "zz" * 32},                   # not hex
        {"root_pub": "AB" * 32},                   # uppercase
        {"invite_ref": "ab" * 31},                 # wrong length
        {"channel_token": ""},
        {"channel_token": "AB" * 16},
        {"claim_token": ""},
        {"claim_token": 1234},
        {"claim_token": GRANT_TOKEN},
        {"extra": "field"},                        # unknown field
        {"claim_token": _MISSING},                 # missing field
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


def test_version_one_is_rejected_with_regeneration_guidance():
    with pytest.raises(InvitationError, match="regenerate"):
        decode_invitation(_recode(
            v=1,
            channel_token=_MISSING,
            claim_token=_MISSING,
            token=CLAIM_TOKEN,
        ))
