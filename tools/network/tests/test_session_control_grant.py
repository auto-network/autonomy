"""The session:control grant: a second SCOPE on the one process delegation
(fleet:sync + session:control), verified at runtime handoff and by an
authenticator verifying either scope (graph://7eb29bc8-31a §6.3). Every
refusal carries its own code (fleet_sync_channel.FleetHandshakeRefused)."""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest

from tools.network import fleet_roster, fleet_runtime, session_control
from tools.network.fleet_sync_channel import FleetAuthenticator, FleetHandshakeRefused
from tools.network.idkit import KeyPair, Subject, issue_cert


NOW = 1_777_000_100
ROOT = KeyPair.from_private_hex("12" * 32)
BOTH = ["fleet:sync", "session:control"]
SYNC = ["fleet:sync"]


def _machine(n: int):
    machine = KeyPair.from_private_hex(f"{0x30 + n:02x}" * 32)
    process = KeyPair.from_private_hex(f"{0x50 + n:02x}" * 32)
    machine_id = f"{0x70 + n:02x}" * 32
    entry = fleet_roster.enroll(
        ROOT, machine_id=machine_id, machine_pub=machine.public_hex
    )
    return machine, process, machine_id, entry


def _cert(machine, process_pub, machine_id, scope, *, now=NOW, ttl=300,
          target_types=None):
    return issue_cert(
        machine,
        process_pub,
        scope=list(scope),
        org=f"personal:{ROOT.public_hex}",
        subject=Subject(kind="machine", id=machine_id),
        not_before=now - 30,
        not_after=now + ttl,
        target_types=target_types,
    )


def _payload(machine, process, machine_id, *, scope=BOTH):
    return {
        "machine_id": machine_id,
        "machine_pub": machine.public_hex,
        "process_private_seed": process.private_hex,
        "delegation_cert": _cert(
            machine, process.public_hex, machine_id, scope
        ).to_dict(),
    }


def _activate(payload, entries):
    return fleet_runtime.FleetRuntimeCredential.from_browser_payload(
        payload,
        personal_root_pub=ROOT.public_hex,
        roster_entries=entries,
        now=NOW,
    )


# ── runtime handoff ──────────────────────────────────────────────────────────


def test_runtime_accepts_one_delegation_carrying_both_scopes():
    machine, process, machine_id, entry = _machine(1)
    credential = _activate(_payload(machine, process, machine_id), [entry])
    assert credential.delegation_cert.scope == ("fleet:sync", "session:control")
    assert credential.has_session_control
    assert credential.delegations() == [
        {"scope": BOTH, "not_after": NOW + 300}
    ]


def test_runtime_armed_before_the_scope_still_syncs_without_it():
    machine, process, machine_id, entry = _machine(1)
    credential = _activate(
        _payload(machine, process, machine_id, scope=SYNC), [entry])
    assert not credential.has_session_control
    assert credential.delegations() == [
        {"scope": SYNC, "not_after": NOW + 300}
    ]


def test_a_cached_credential_with_the_retired_second_cert_still_arms(caplog):
    machine, process, machine_id, entry = _machine(1)
    payload = _payload(machine, process, machine_id, scope=SYNC)
    payload["session_control_cert"] = _cert(
        machine, process.public_hex, machine_id, ["session:control"]).to_dict()
    with caplog.at_level(logging.INFO, logger="tools.network.fleet_runtime"):
        credential = _activate(payload, [entry])
    # The retired certificate grants nothing: only the delegation's scope counts.
    assert not credential.has_session_control
    assert "session_control_cert" in caplog.text


@pytest.mark.parametrize("scope, target_types", [
    (["fleet:sync", "node:announce"], None),       # authority outside the set
    (["session:control"], None),                   # no fleet:sync
    (BOTH, ["design"]),                            # target types
])
def test_runtime_refuses_a_delegation_outside_the_process_scopes(scope, target_types):
    machine, process, machine_id, entry = _machine(1)
    payload = _payload(machine, process, machine_id)
    payload["delegation_cert"] = _cert(
        machine, process.public_hex, machine_id, scope,
        target_types=target_types).to_dict()
    with pytest.raises(fleet_runtime.FleetRuntimeError):
        _activate(payload, [entry])


# ── handshake ────────────────────────────────────────────────────────────────


def _authenticator(n, entries, *, scope, cert_scope, now=None, ttl=300):
    machine, process, machine_id, _entry = _machine(n)
    now = int(time.time()) if now is None else now
    cert = (
        None if cert_scope is None
        else _cert(machine, process.public_hex, machine_id, cert_scope,
                   now=now, ttl=ttl)
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


def _refused(code, fn, *args):
    with pytest.raises(FleetHandshakeRefused) as info:
        fn(*args)
    assert info.value.refusal == code, info.value
    return info.value


@pytest.mark.parametrize("scope", ["session:control", "fleet:sync"])
def test_one_delegation_serves_both_scopes_between_two_machines(scope):
    entries = [_machine(1)[3], _machine(2)[3]]
    client = _authenticator(1, entries, scope=scope, cert_scope=BOTH)
    server = _authenticator(2, entries, scope=scope, cert_scope=BOTH)
    client_pub, *_rest = _handshake(client, server)
    assert client_pub == _machine(1)[0].public_hex


def test_a_sync_only_peer_is_refused_session_control_by_name():
    entries = [_machine(1)[3], _machine(2)[3]]
    # The client's own check runs first: it cannot sign a session hello.
    client = _authenticator(1, entries, scope="session:control", cert_scope=SYNC)
    _refused("own-scope-missing", client.build_client_hello, "pair-1")
    # The server refuses the same material when it arrives in a hello.
    sync_client = _authenticator(1, entries, scope="fleet:sync", cert_scope=SYNC)
    server = _authenticator(2, entries, scope="session:control", cert_scope=BOTH)
    _refused("peer-scope-missing", _handshake, sync_client, server)


def test_a_sync_only_delegation_still_syncs_with_a_two_scope_peer():
    entries = [_machine(1)[3], _machine(2)[3]]
    client = _authenticator(1, entries, scope="fleet:sync", cert_scope=SYNC)
    server = _authenticator(2, entries, scope="fleet:sync", cert_scope=BOTH)
    client_pub, *_rest = _handshake(client, server)
    assert client_pub == _machine(1)[0].public_hex


def test_no_delegation_at_all_is_its_own_code():
    entries = [_machine(1)[3]]
    client = _authenticator(1, entries, scope="session:control", cert_scope=None)
    _refused("own-scope-undelegated", client.build_client_hello, "pair-1")


def test_excess_scope_is_its_own_code_on_either_side():
    entries = [_machine(1)[3], _machine(2)[3]]
    foreign = ["fleet:sync", "node:announce"]
    client = _authenticator(1, entries, scope="fleet:sync", cert_scope=foreign)
    # The sender's own check refuses to sign with it...
    _refused("own-scope-excess", client.build_client_hello, "pair-1")
    # ...and a receiver refuses it in a hello whose sender skipped that check.
    client._authorize_local_signer = lambda _delegate: None
    server = _authenticator(2, entries, scope="fleet:sync", cert_scope=BOTH)
    _refused("peer-scope-excess", _handshake, client, server)


def test_an_expired_peer_delegation_is_its_own_code(monkeypatch):
    entries = [_machine(1)[3], _machine(2)[3]]
    client = _authenticator(1, entries, scope="session:control", cert_scope=BOTH,
                            ttl=300)
    server = _authenticator(2, entries, scope="session:control", cert_scope=BOTH,
                            ttl=20_000)
    _private, hello = client.build_client_hello("pair-1")
    later = time.time() + 10_000
    monkeypatch.setattr(time, "time", lambda: later)
    with pytest.raises(FleetHandshakeRefused) as info:
        server.accept_client(hello, session="pair-1")
    assert info.value.refusal == "peer-delegation-expired"


def test_a_machine_not_in_the_roster_is_its_own_code():
    client = _authenticator(1, [_machine(1)[3]], scope="session:control",
                            cert_scope=BOTH)
    server = _authenticator(2, [_machine(2)[3]], scope="session:control",
                            cert_scope=BOTH)
    _refused("peer-not-in-roster", _handshake, client, server)


# ── the session-control authenticator of an armed runtime ────────────────────


def _runtime(cert_scope):
    auth = _authenticator(1, [_machine(1)[3]], scope="fleet:sync",
                          cert_scope=cert_scope)
    return SimpleNamespace(scheduler=SimpleNamespace(authenticator=auth))


def test_session_authenticator_uses_the_one_delegation():
    auth = session_control.session_authenticator(_runtime(BOTH))
    assert auth.scope == "session:control"
    assert auth.delegation_cert.scope == ("fleet:sync", "session:control")


def test_session_authenticator_names_a_runtime_without_the_scope():
    with pytest.raises(session_control.SessionControlError) as info:
        session_control.session_authenticator(_runtime(SYNC))
    assert info.value.refusal == session_control.NOT_GRANTED


def test_session_authenticator_names_an_unarmed_process():
    with pytest.raises(session_control.SessionControlError) as info:
        session_control.session_authenticator(SimpleNamespace(scheduler=None))
    assert info.value.refusal == session_control.UNARMED


# ── the unauthenticated pre-handshake refusal record ─────────────────────────


def test_every_handshake_code_raised_is_one_a_peer_may_report():
    """PEER_REFUSAL_CODES bounds what an unauthenticated refusal record can
    say; a raise site with a code outside it would be silently genericised."""
    import inspect
    import re

    from tools.network import fleet_sync_channel

    source = inspect.getsource(fleet_sync_channel)
    raised = set()
    for literal in re.findall(r'FleetHandshakeRefused\(\s*f?"([^"]+)"', source):
        if "{suffix}" in literal:
            continue  # _chain_refusal's template; expanded from _CHAIN_REFUSALS below
        if literal.startswith("{side}-"):
            raised |= {literal.replace("{side}", s) for s in ("own", "peer")}
        else:
            raised.add(literal)
    raised |= {f"{s}-delegation-{suffix}" for s in ("own", "peer")
               for _kind, suffix in fleet_sync_channel._CHAIN_REFUSALS}
    raised |= {"own-delegation-invalid", "peer-delegation-invalid"}
    assert raised, "no codes found: the scan is broken"
    assert raised <= fleet_sync_channel.HANDSHAKE_REFUSALS, (
        raised - fleet_sync_channel.HANDSHAKE_REFUSALS)
    assert fleet_sync_channel.HANDSHAKE_REFUSALS <= session_control.PEER_REFUSAL_CODES


@pytest.mark.parametrize("record, expected", [
    ({"refusal": "session-control-not-granted", "detail": "x" * 1000},
     ("session-control-not-granted", 300)),
    ({"refusal": "peer-scope-missing"}, ("peer-scope-missing", 0)),
    # A forged code cannot choose the UI state.
    ({"refusal": "session-control-not-negotiated"}, ("peer-refused", None)),
    ({"refusal": "anything <b>at all</b>" * 20}, ("peer-refused", None)),
])
def test_a_peer_refusal_record_is_bounded(record, expected):
    import asyncio
    import json as _json

    from tools.network.relaykit.frames import VIEWER_KIND_RECORD, tag_viewer_message

    raw = tag_viewer_message(VIEWER_KIND_RECORD, _json.dumps(
        {"v": 1, "ok": False, **record}).encode())

    class Endpoint:
        session = "s"

        async def recv(self):
            return raw

    transport = session_control._RefusalAwareTransport(Endpoint())
    with pytest.raises(session_control._PeerRefusal) as info:
        asyncio.run(transport.recv())
    code, detail_len = expected
    assert info.value.refusal == code
    if detail_len is not None:
        assert len(info.value.detail) == detail_len
    else:
        assert len(info.value.detail) <= 100
