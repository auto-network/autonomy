"""The fleet-machine invite link (auto-ei8w0).

Design ``graph://0c655045-ee4``. Each test maps to one acceptance bullet:
the anchor is the PERSONAL root and not an organization root, a differing
root is rejected by name, tamper/truncation fail without crashing, and the
link carries nothing that lets its holder act as the operator.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair
from tools.network import fleet_invite
from tools.network.fleet_invite import FleetInvite, FleetInviteError


RENDEZVOUS = "https://primary.example.net/fleet/rv/abc123"
INVITE_ID = "ab" * 32


def _mint(root: KeyPair, **over) -> FleetInvite:
    kw = dict(rendezvous=RENDEZVOUS, invite_id=INVITE_ID, expires_at=0)
    kw.update(over)
    return fleet_invite.mint(root, **kw)


def test_a_fresh_machine_accepts_a_link_and_adopts_its_anchor():
    root = KeyPair.generate()
    invite = _mint(root)
    code = fleet_invite.encode(invite)
    decoded = fleet_invite.decode(code)
    # A fresh machine (no prior root) accepts any self-consistent offer.
    fleet_invite.verify(decoded)  # does not raise
    assert decoded.personal_root_pub == root.public_hex
    assert decoded.rendezvous == RENDEZVOUS


def test_a_machine_with_a_different_personal_root_is_rejected_by_anchor():
    operator = KeyPair.generate()
    invite = fleet_invite.decode(fleet_invite.encode(_mint(operator)))
    other_machines_root = KeyPair.generate().public_hex
    with pytest.raises(FleetInviteError, match="not this machine's personal root"):
        fleet_invite.verify(invite, expected_root_pub=other_machines_root)
    # The operator's own machine (matching anchor) accepts.
    fleet_invite.verify(invite, expected_root_pub=operator.public_hex)


def test_an_organization_root_signature_does_not_verify_as_a_fleet_offer():
    """The anchor is the personal root and is not incidentally satisfied by
    an org root: an offer whose carried anchor is an org root but whose
    signature was made by that org root still must be a PERSONAL-root offer
    to this path — and more sharply, a signature made under one key does not
    verify when the offer claims a different anchor."""
    personal = KeyPair.generate()
    org_root = KeyPair.generate()
    good = _mint(personal)
    # Swap the carried anchor to the org root but keep the personal-root
    # signature: verification now checks the personal-root signature against
    # the org-root key and fails.
    forged = FleetInvite(
        personal_root_pub=org_root.public_hex,
        rendezvous=good.rendezvous, invite_id=good.invite_id,
        expires_at=good.expires_at, signature=good.signature,
    )
    with pytest.raises(FleetInviteError, match="does not verify"):
        fleet_invite.verify(forged)
    # And an offer genuinely minted by the org root verifies only against the
    # org root — proving the check binds the signer, whatever kind of key it
    # is; the fleet flow supplies the personal root, never an org root.
    org_signed = _mint(org_root)
    fleet_invite.verify(org_signed)  # self-consistent under org_root...
    # ...but on the operator's machine, whose anchor is the personal root,
    # an org-root-anchored offer is refused by anchor before its signature is
    # even checked.
    with pytest.raises(FleetInviteError, match="not this machine's personal root"):
        fleet_invite.verify(org_signed, expected_root_pub=personal.public_hex)


def test_an_org_root_offer_fails_by_anchor_before_signature_on_the_operators_machine():
    """The pillar's sharp check: on the operator's machine (which knows its
    personal root), a fleet invite signed by an ORG root fails by ANCHOR, not
    merely by signature mismatch — and the anchor check fires FIRST, so even
    a perfectly-valid org-root signature is refused for being the wrong kind
    of key, not for being a bad signature. A VALID org-signed offer (which
    passes its own signature check) is used precisely so a signature-only
    gate would let it through."""
    personal = KeyPair.generate()
    org_root = KeyPair.generate()
    org_signed = fleet_invite.decode(fleet_invite.encode(_mint(org_root)))
    # Self-consistent: its signature verifies against its carried anchor.
    fleet_invite.verify(org_signed)
    # But on the operator's machine it is refused by ANCHOR, before signature.
    with pytest.raises(FleetInviteError, match="not this machine's personal root"):
        fleet_invite.verify(org_signed, expected_root_pub=personal.public_hex)
    # Symmetrically, the operator's own personal-root offer FAILS to verify
    # against an organization root as the expected anchor — proving the anchor
    # is not incidentally satisfied by both kinds.
    personal_offer = fleet_invite.decode(fleet_invite.encode(_mint(personal)))
    with pytest.raises(FleetInviteError, match="not this machine's personal root"):
        fleet_invite.verify(personal_offer, expected_root_pub=org_root.public_hex)


def test_decode_performs_no_signature_check():
    """decode is purely structural — it must refuse an org invite (kind) and
    a truncated one (checksum) WITHOUT any crypto, so a caller can triage a
    pasted code before touching a key. An offer with a syntactically valid
    but cryptographically bogus signature decodes fine; only verify rejects
    it."""
    root = KeyPair.generate()
    invite = _mint(root)
    bogus = FleetInvite(
        personal_root_pub=invite.personal_root_pub, rendezvous=invite.rendezvous,
        invite_id=invite.invite_id, expires_at=invite.expires_at,
        signature="00" * 64,  # well-formed 128-hex, does not verify
    )
    code = fleet_invite.encode(bogus)
    decoded = fleet_invite.decode(code)  # no raise — structure is valid
    assert decoded.signature == "00" * 64
    with pytest.raises(FleetInviteError, match="does not verify"):
        fleet_invite.verify(decoded)


def test_a_tampered_offer_fails_and_does_not_crash():
    root = KeyPair.generate()
    invite = _mint(root)
    # Tamper the rendezvous after signing: the signature no longer covers it.
    tampered = FleetInvite(
        personal_root_pub=invite.personal_root_pub,
        rendezvous="https://attacker.example.net/rv/steal",
        invite_id=invite.invite_id, expires_at=invite.expires_at,
        signature=invite.signature,
    )
    with pytest.raises(FleetInviteError, match="does not verify"):
        fleet_invite.verify(tampered)


def test_a_truncated_link_fails_cleanly_before_any_crypto():
    root = KeyPair.generate()
    code = fleet_invite.encode(_mint(root))
    for broken in (code[:-1], code[:-3], code[: len(code) // 2], code + "x"):
        with pytest.raises(FleetInviteError):
            fleet_invite.decode(broken)


def _encode_raw(body: dict) -> str:
    import base64 as _b64
    import hashlib as _h
    import json as _j
    raw = _j.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    payload = _b64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{payload}.{_h.sha256(raw).hexdigest()[:8]}"


def test_a_non_fleet_code_is_refused_before_crypto():
    """An organization invitation (or any other code) fed to this path is
    refused before any signature check — never misread as a fleet offer.
    A real org invite (v2, no kind) is refused on version; a code that
    collides on version but not kind is refused on kind. Both refusals land
    before crypto."""
    org_invite = _encode_raw({"v": 2, "org": "u", "root_pub": "aa" * 32})
    with pytest.raises(FleetInviteError, match="unsupported fleet invite version"):
        fleet_invite.decode(org_invite)

    wrong_kind = _encode_raw({
        "v": fleet_invite.FLEET_INVITE_VERSION, "kind": "organization",
        "personal_root_pub": "aa" * 32, "rendezvous": RENDEZVOUS,
        "invite_id": "bb" * 32, "expires_at": 0, "sig": "cc" * 64,
    })
    with pytest.raises(FleetInviteError, match="not a fleet-machine invite"):
        fleet_invite.decode(wrong_kind)


def test_the_link_carries_nothing_that_can_act_as_the_operator():
    """Assert the absence positively. The carried anchor is the operator's
    PUBLIC key — treating it as a private seed derives a DIFFERENT, unrelated
    key, so the link yields no signer that speaks for the operator. And a
    holder of only the link cannot forge a fresh offer for the same anchor:
    changing any signed field breaks the signature, because minting one needs
    the personal root PRIVATE key, which the link does not carry."""
    root = KeyPair.generate()
    invite = fleet_invite.decode(fleet_invite.encode(_mint(root)))

    # The carried value is the PUBLIC key. Misusing it as a private seed
    # produces an unrelated key — never the operator's root — so nothing in
    # the link reconstructs a signer that acts as the operator.
    assert invite.personal_root_pub == root.public_hex
    impostor = KeyPair.from_private_hex(invite.personal_root_pub)
    assert impostor.public_hex != root.public_hex

    # A holder cannot forge a valid offer for the SAME anchor: re-signing a
    # changed correlation id with the only signer they can build (the
    # impostor) does not verify against the operator's anchor.
    forged_body_sig = impostor.sign_hex(
        fleet_invite.signing_input(
            personal_root_pub=root.public_hex, rendezvous=invite.rendezvous,
            invite_id="cd" * 32, expires_at=invite.expires_at,
        )
    )
    forged = FleetInvite(
        personal_root_pub=root.public_hex, rendezvous=invite.rendezvous,
        invite_id="cd" * 32, expires_at=invite.expires_at,
        signature=forged_body_sig,
    )
    with pytest.raises(FleetInviteError, match="does not verify"):
        fleet_invite.verify(forged)


def test_roundtrip_is_byte_stable_and_encode_carries_the_signature():
    root = KeyPair.generate()
    invite = _mint(root, expires_at=1_900_000_000_000)
    code = fleet_invite.encode(invite)
    back = fleet_invite.decode(code)
    assert back == invite
    fleet_invite.verify(back)
    # Re-encoding the decoded offer is identical — canonical, stable bytes.
    assert fleet_invite.encode(back) == code


def test_rendezvous_must_be_https():
    root = KeyPair.generate()
    with pytest.raises(FleetInviteError, match="https"):
        _mint(root, rendezvous="http://insecure.example.net/rv")
    with pytest.raises(FleetInviteError, match="rendezvous"):
        _mint(root, rendezvous="")
