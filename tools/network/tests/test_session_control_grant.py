"""The session:control grant: a separate process delegation beside fleet:sync,
verified at runtime handoff and by an authenticator verifying that scope
(graph://7eb29bc8-31a §6.3, bead auto-1jlpf)."""

from __future__ import annotations

import time

import pytest

from tools.network import fleet_roster, fleet_runtime
from tools.network.fleet_sync_channel import (
    SESSION_CAP_MISSING,
    FleetAuthenticator,
)
from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.relaykit.channel import HandshakeError


NOW = 1_777_000_100
ROOT = KeyPair.from_private_hex("12" * 32)


def _machine(n: int):
    machine = KeyPair.from_private_hex(f"{0x30 + n:02x}" * 32)
    process = KeyPair.from_private_hex(f"{0x50 + n:02x}" * 32)
    machine_id = f"{0x70 + n:02x}" * 32
    entry = fleet_roster.enroll(
        ROOT, machine_id=machine_id, machine_pub=machine.public_hex
    )
    return machine, process, machine_id, entry


def _cert(machine, process_pub, machine_id, scope, *, now=NOW, ttl=300):
    return issue_cert(
        machine,
        process_pub,
        scope=[scope],
        org=f"personal:{ROOT.public_hex}",
        subject=Subject(kind="machine", id=machine_id),
        not_before=now - 30,
        not_after=now + ttl,
    )


def _payload(machine, process, machine_id, *, session_cert=True):
    payload = {
        "machine_id": machine_id,
        "machine_pub": machine.public_hex,
        "process_private_seed": process.private_hex,
        "delegation_cert": _cert(
            machine, process.public_hex, machine_id, "fleet:sync"
        ).to_dict(),
    }
    if session_cert:
        payload["session_control_cert"] = _cert(
            machine, process.public_hex, machine_id, "session:control"
        ).to_dict()
    return payload


def _activate(payload, entries):
    return fleet_runtime.FleetRuntimeCredential.from_browser_payload(
        payload,
        personal_root_pub=ROOT.public_hex,
        roster_entries=entries,
        now=NOW,
    )


def test_runtime_accepts_session_control_beside_fleet_sync():
    machine, process, machine_id, entry = _machine(1)
    credential = _activate(_payload(machine, process, machine_id), [entry])
    assert credential.session_control_cert.scope == ("session:control",)
    assert credential.delegation_cert.scope == ("fleet:sync",)
    assert [d["scope"] for d in credential.delegations()] == [
        ["fleet:sync"], ["session:control"],
    ]


def test_runtime_without_session_control_is_unchanged():
    machine, process, machine_id, entry = _machine(1)
    credential = _activate(
        _payload(machine, process, machine_id, session_cert=False), [entry]
    )
    assert credential.session_control_cert is None
    assert credential.delegations() == [
        {"scope": ["fleet:sync"], "not_after": NOW + 300}
    ]


@pytest.mark.parametrize("mutate", [
    # a second scope on the session cert
    lambda m, p, mid: _cert(m, p.public_hex, mid, "session:control").to_dict()
    | {"scope": ["session:control", "fleet:sync"]},
    # a fleet:sync cert offered as the session cert
    lambda m, p, mid: _cert(m, p.public_hex, mid, "fleet:sync").to_dict(),
    # to another process key
    lambda m, p, mid: _cert(m, "ab" * 32, mid, "session:control").to_dict(),
    # naming another machine
    lambda m, p, mid: _cert(m, p.public_hex, "cd" * 32, "session:control").to_dict(),
    # expired
    lambda m, p, mid: _cert(m, p.public_hex, mid, "session:control",
                            now=NOW - 10_000).to_dict(),
])
def test_runtime_refuses_a_bad_session_control_cert(mutate):
    machine, process, machine_id, entry = _machine(1)
    payload = _payload(machine, process, machine_id)
    payload["session_control_cert"] = mutate(machine, process, machine_id)
    with pytest.raises(fleet_runtime.FleetRuntimeError):
        _activate(payload, [entry])


def _authenticator(n, entries, *, scope, cert_scope):
    machine, process, machine_id, _entry = _machine(n)
    now = int(time.time())
    cert = (
        None if cert_scope is None
        else _cert(machine, process.public_hex, machine_id, cert_scope, now=now)
    )
    return FleetAuthenticator(
        process,
        root_pub=ROOT.public_hex,
        roster_entries=lambda: entries,
        roster_machine_pub=machine.public_hex,
        delegation_cert=cert,
        require_delegation=True,
        scope=scope,
    )


def _handshake(client, server):
    _private, hello = client.build_client_hello("pair-1")
    return server.accept_client(hello, session="pair-1")


def test_session_control_hello_verifies_between_two_machines():
    entries = [_machine(1)[3], _machine(2)[3]]
    client = _authenticator(1, entries, scope="session:control",
                            cert_scope="session:control")
    server = _authenticator(2, entries, scope="session:control",
                            cert_scope="session:control")
    client_pub, *_rest = _handshake(client, server)
    assert client_pub == _machine(1)[0].public_hex


def test_session_authenticator_refuses_a_fleet_sync_delegation():
    entries = [_machine(1)[3], _machine(2)[3]]
    client = _authenticator(1, entries, scope="fleet:sync",
                            cert_scope="fleet:sync")
    server = _authenticator(2, entries, scope="session:control",
                            cert_scope="session:control")
    with pytest.raises(HandshakeError, match=SESSION_CAP_MISSING):
        _handshake(client, server)


def test_sync_authenticator_refuses_a_session_control_delegation():
    entries = [_machine(1)[3], _machine(2)[3]]
    client = _authenticator(1, entries, scope="session:control",
                            cert_scope="session:control")
    server = _authenticator(2, entries, scope="fleet:sync",
                            cert_scope="fleet:sync")
    with pytest.raises(HandshakeError):
        _handshake(client, server)


def test_runtime_without_session_control_cannot_open_a_session_hello():
    entries = [_machine(1)[3]]
    client = _authenticator(1, entries, scope="session:control",
                            cert_scope=None)
    with pytest.raises(HandshakeError, match=SESSION_CAP_MISSING):
        client.build_client_hello("pair-1")


def test_session_hello_from_a_machine_not_in_the_roster_is_refused():
    entries = [_machine(2)[3]]
    client = _authenticator(1, [_machine(1)[3]], scope="session:control",
                            cert_scope="session:control")
    server = _authenticator(2, entries, scope="session:control",
                            cert_scope="session:control")
    with pytest.raises(HandshakeError, match="not active"):
        _handshake(client, server)
