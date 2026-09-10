"""Reading this machine's serving slot for the descriptor's relay locator.

auto-e38g4. Contract graph://7ed8a519-356 §5: publish only after the
connector's hello is acknowledged, and "a late acknowledgment for an older
generation cannot publish its descriptor". §6: absent authority reports
nothing and never fabricates a locator.
"""

from __future__ import annotations

import pytest

from tools.dashboard import fleet_enrollment_routes as fer

PERSONA = "ab" * 32
SLOT = "cd" * 32
ORG = "11111111-1111-4111-8111-111111111111"


def _status(**over):
    reply = {
        "ok": True,
        "serving": True,
        "serving_slot": {"persona_pub": PERSONA, "machine": SLOT},
        "relay_base": "wss://auto.network",
        "org_uuid": ORG,
        "accepted_caps": ["fleet-directed-stream/1", "tls-stream/1"],
    }
    reply.update(over)
    return reply


@pytest.fixture
def control(monkeypatch):
    """Replace the supervisor control call; the test decides what it answers."""
    calls = []

    def install(fn):
        def _control(org, op, args, timeout=None):
            calls.append((org, op))
            return fn()
        monkeypatch.setattr(fer.link_serving_supervisor, "control", _control)
        return calls

    return install


def test_a_live_connector_yields_its_actual_slot(control):
    control(_status)

    locator = fer._serving_slot_locator()

    assert locator == {
        "relay_base": "wss://auto.network",
        "org_uuid": ORG,
        "persona_pub": PERSONA,
        "serving_machine_pub": SLOT,
        "capabilities": ["fleet-directed-stream/1", "tls-stream/1"],
    }


def test_a_connector_that_has_not_completed_its_hello_publishes_nothing(control):
    """§5: the slot is only real once the relay has filed it. Publishing before
    the hello would advertise a slot no peer can pair with."""
    control(lambda: _status(serving=False))

    assert fer._serving_slot_locator() is None


def test_a_stale_generation_does_not_publish(control, monkeypatch):
    """A newer activation began while the status round trip was in flight.

    Publishing that reply would mint a NEWER descriptor generation carrying an
    OLDER slot, and peers fence by generation: they would take a slot that may
    already be gone with no way to be corrected until the content changed.
    """
    def bump_then_answer():
        fer._runtime_generation += 1
        return _status()

    monkeypatch.setattr(fer, "_runtime_generation", 7, raising=False)
    control(bump_then_answer)

    assert fer._serving_slot_locator() is None


def test_the_same_generation_does_publish(control, monkeypatch):
    """The fence must not be so eager that it refuses the ordinary case."""
    monkeypatch.setattr(fer, "_runtime_generation", 7, raising=False)
    control(_status)

    assert fer._serving_slot_locator() is not None


def test_no_connector_at_all_is_none_not_an_exception(control):
    def unavailable():
        raise fer.link_serving_supervisor.TunnelUnavailable("no connector")

    control(unavailable)

    assert fer._serving_slot_locator() is None


@pytest.mark.parametrize("missing", ["relay_base", "org_uuid"])
def test_a_reply_without_the_relay_identity_publishes_nothing(control, missing):
    control(lambda: _status(**{missing: None}))

    assert fer._serving_slot_locator() is None


def test_a_slot_without_a_machine_publishes_nothing(control):
    control(lambda: _status(serving_slot={"persona_pub": PERSONA, "machine": None}))

    assert fer._serving_slot_locator() is None


def test_a_relay_base_that_is_not_a_bare_origin_publishes_nothing(control):
    """Canonicalization happens here, at the build input, and refuses rather
    than rewrites: a signed body must be what its signer meant."""
    control(lambda: _status(relay_base="wss://auto.network/t/some-org"))

    assert fer._serving_slot_locator() is None


def test_a_default_port_is_canonicalized_away(control):
    control(lambda: _status(relay_base="wss://auto.network:443"))

    assert fer._serving_slot_locator()["relay_base"] == "wss://auto.network"


def test_capabilities_are_bounded(control):
    from tools.network import fleet_descriptor

    control(lambda: _status(accepted_caps=[f"cap/{i}" for i in range(64)]))

    caps = fer._serving_slot_locator()["capabilities"]
    assert len(caps) == fleet_descriptor.MAX_CAPABILITIES
