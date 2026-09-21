"""relaykit.close_codes: the one table of application close codes, the
coded close-frame payload, and the words a person reads."""
from __future__ import annotations

import pytest

from tools.network.relaykit import close_codes as cc


def test_payload_round_trips_and_bounds_the_reason():
    payload = cc.encode_close_payload(cc.CLOSE_KEY_RESOLUTION_REFUSED, "x" * 500)
    code, reason = cc.decode_close_payload(payload)
    assert code == 4502 and reason == "x" * cc.MAX_REASON_BYTES


def test_an_empty_or_foreign_payload_is_a_plain_1000():
    # A connector that predates coded closes sends no payload.
    assert cc.decode_close_payload(b"") == (1000, "")
    assert cc.decode_close_payload(b"\x01") == (1000, "")
    # Only the connector's own family is ever forwarded to a viewer.
    assert cc.decode_close_payload((4404).to_bytes(2, "big") + b"spoof") == (1000, "")
    with pytest.raises(ValueError):
        cc.encode_close_payload(4404, "not the connector's to send")


@pytest.mark.parametrize("exc, code", [
    (PermissionError("connector credential unavailable"), cc.CLOSE_CONNECTOR_UNARMED),
    (PermissionError("link key resolution refused"), cc.CLOSE_KEY_RESOLUTION_REFUSED),
    (PermissionError("channel authorization unavailable"), cc.CLOSE_AUTHORIZATION_UNAVAILABLE),
    (PermissionError("channel authorization refused"), cc.CLOSE_KEY_RESOLUTION_REFUSED),
    (type("HandshakeError", (Exception,), {})("bad record"), cc.CLOSE_VIEWER_HANDSHAKE_FAILED),
    (RuntimeError("boom"), cc.CLOSE_CONNECTOR_ERROR),
    # The two follow member-local refusals map from their own typed exceptions,
    # the way the unarmed PermissionError maps to CLOSE_CONNECTOR_UNARMED.
    (cc.FollowNoFrontier("no covered persona write floor"), cc.CLOSE_FOLLOW_NO_FRONTIER),
    (cc.FollowBehind("cursor 900 above frontier 500"), cc.CLOSE_FOLLOW_BEHIND),
])
def test_classification(exc, code):
    got, reason = cc.classify_connector_error(exc)
    assert got == code
    assert str(exc) in reason


def test_follow_close_codes_round_trip_the_close_frame():
    # Both are connector-authored (4500-range), so a coded close frame carries
    # them verbatim to the viewer and the relay.
    for code in (cc.CLOSE_FOLLOW_NO_FRONTIER, cc.CLOSE_FOLLOW_BEHIND):
        got, reason = cc.decode_close_payload(cc.encode_close_payload(code, "why"))
        assert got == code and reason == "why"


def test_every_code_has_a_name_a_meaning_and_a_remedy():
    for code, meaning in cc.MEANINGS.items():
        assert meaning.name and meaning.meaning and meaning.remedy, code


def test_describe_reads_as_an_instruction():
    text = cc.describe(cc.CLOSE_CONNECTOR_UNARMED, "connector credential unavailable")
    assert text.startswith("close 4501 (connector unarmed); the far side said: "
                           "connector credential unavailable; ")
    assert "remedy: unlock the dashboard" in text
    assert cc.describe(None) == "no close code (the connection did not close cleanly)"
    assert cc.describe(4242) == "close 4242"
