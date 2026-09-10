"""auto-nh1po: the connector forwards the dashboard's declared serving
machine on every host-register for that reservation, including the
per-connection re-registration after a reconnect."""

from __future__ import annotations

import asyncio

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.relaykit.connector import TunnelConnector

ORG = "11111111-1111-4111-8111-111111111111"
MACHINE = "ef" * 32


def _connector():
    root, serve_key = KeyPair.generate(), KeyPair.generate()
    cert = issue_cert(
        root, serve_key.public_hex, scope=("tunnel:serve",), org=ORG,
        subject=Subject("persona", "ab" * 32), not_before=0, not_after=2**31,
    )
    return TunnelConnector(
        "ws://relay.test", ORG, serve_key, cert, machine_key=KeyPair.generate(),
    )


def test_serve_host_forwards_the_declaration_and_reregisters_with_it():
    connector = _connector()
    sent = []

    async def control(op, args, timeout=10.0):
        sent.append((op, dict(args)))
        return {"ok": True, "lease": {"generation": 1, "expires_at": 0}}

    connector.control = control

    async def run():
        assert (await connector.serve_host("res-1", "app.example", machine=MACHINE))["ok"]
        assert (await connector.serve_host("res-2", "docs.example"))["ok"]
        # A reconnect re-registers desired hosts: the declaration rides again.
        await connector._register_host("res-1", "app.example")
        await connector._register_host("res-2", "docs.example")
        # Release forgets it; a later undeclared serve_host stays undeclared.
        await connector.release_host("res-1")
        await connector.serve_host("res-1", "app.example")

    asyncio.run(run())

    assert sent == [
        ("host-register", {"reservation": "res-1", "host": "app.example", "machine": MACHINE}),
        ("host-register", {"reservation": "res-2", "host": "docs.example"}),
        ("host-register", {"reservation": "res-1", "host": "app.example", "machine": MACHINE}),
        ("host-register", {"reservation": "res-2", "host": "docs.example"}),
        ("host-release", {"reservation": "res-1"}),
        ("host-register", {"reservation": "res-1", "host": "app.example"}),
    ]
