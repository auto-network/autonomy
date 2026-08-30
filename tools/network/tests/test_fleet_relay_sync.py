from __future__ import annotations

import json
import time

import pytest

from tools.network import fleet_relay_sync, fleet_roster
from tools.network.fleet_sync_channel import FleetAuthenticator
from tools.network.fleet_sync_scheduler import decode_done, encode_done
from tools.network.idkit import KeyPair, Subject, issue_cert


@pytest.mark.asyncio
async def test_connector_stream_requires_fleet_machine_hello_and_chunks_checkpoint(
    tmp_path, monkeypatch
):
    root = KeyPair.from_private_hex("10" * 32)
    server_machine = KeyPair.from_private_hex("20" * 32)
    client_machine = KeyPair.from_private_hex("30" * 32)
    server_id = "40" * 32
    client_id = "50" * 32
    entries = (
        fleet_roster.enroll(
            root, machine_id=server_id, machine_pub=server_machine.public_hex
        ),
        fleet_roster.enroll(
            root, machine_id=client_id, machine_pub=client_machine.public_hex
        ),
    )
    now = int(time.time())

    def runtime(machine, machine_id, process_seed):
        process = KeyPair.from_private_hex(process_seed)
        cert = issue_cert(
            machine,
            process.public_hex,
            scope=["fleet:sync"],
            org=f"personal:{root.public_hex}",
            subject=Subject(kind="machine", id=machine_id),
            not_before=now - 30,
            not_after=now + 300,
        )
        return process, cert, {
            "machine_id": machine_id,
            "machine_pub": machine.public_hex,
            "process_private_seed": process.private_hex,
            "delegation_cert": cert.to_dict(),
        }

    server_process, _server_cert, server_payload = runtime(
        server_machine, server_id, "60" * 32
    )
    client_process, client_cert, _client_payload = runtime(
        client_machine, client_id, "70" * 32
    )
    personal = tmp_path / "personal.db"
    personal.touch()
    monkeypatch.setattr(
        "tools.network.fleet_tunnel_server._personal_root_pub",
        lambda: root.public_hex,
    )
    monkeypatch.setattr(
        fleet_relay_sync.fleet_roster,
        "load_entries",
        lambda *, org: list(entries),
    )
    monkeypatch.setattr(fleet_relay_sync, "_org_db_path", lambda _org: personal)

    class FakeAlpha:
        def __init__(self, path, origin):
            assert path == personal
            assert origin == server_machine.public_hex

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def checkpoint(self, directory, **_kwargs):
            directory.mkdir()
            (directory / "alpha-manifest.json").write_bytes(b"manifest")
            chunks = directory / "base"
            chunks.mkdir()
            (chunks / "00000000-test.base").write_bytes(b"base-data")

    monkeypatch.setattr(fleet_relay_sync, "FleetSyncAlpha", FakeAlpha)
    server = fleet_relay_sync.ConnectorFleetRuntime()
    assert server.configure(server_payload) == {
        "ok": True, "machine_id": server_id,
    }
    assert server.scheduler.authenticator.machine_key.public_hex \
        == server_process.public_hex

    async def fake_handle(_token, _message, _peer_pub, **_telemetry):
        async def response():
            yield encode_done(
                epoch="ef" * 32,
                count=0,
                digest=__import__("hashlib").sha256().hexdigest(),
            )
        return response()

    server.scheduler._handle = fake_handle

    client_auth = FleetAuthenticator(
        client_process,
        root_pub=root.public_hex,
        roster_entries=lambda: entries,
        roster_machine_pub=client_machine.public_hex,
        delegation_cert=client_cert,
        require_delegation=True,
    )
    token = "ab" * 16
    private, hello = client_auth.build_client_hello(token)
    stream = await server.handle(token, {
        "v": 1,
        "op": "fleet.sync.pull",
        "roster_epoch": "cd" * 32,
        "checkpoint": True,
        "resume": [],
        "hello": json.loads(hello),
    })
    frames = [frame async for frame in stream]
    first = json.loads(frames[0])
    client_auth.verify_server(
        fleet_relay_sync.canonical_json(first["hello"]),
        session=token,
        client_eph=private.public_key().public_bytes_raw().hex(),
        expected_machine_pub=server_machine.public_hex,
    )
    assert json.loads(frames[1])["kind"] == "checkpoint.begin"
    assert [fleet_relay_sync._decode_file(frame) for frame in frames[2:-2]] == [
        ("alpha-manifest.json", b"manifest"),
        ("base/00000000-test.base", b"base-data"),
    ]
    assert json.loads(frames[-2]) == {
        "v": 1,
        "kind": "checkpoint.end",
        "file_count": 2,
        "total_bytes": 17,
    }
    assert decode_done(frames[-1])[1:] == (
        0, __import__("hashlib").sha256().hexdigest(), 0, None,
    )


def test_checkpoint_file_frame_refuses_traversal_and_digest_tamper():
    frame = fleet_relay_sync._encode_file("../escape", b"body")
    with pytest.raises(fleet_relay_sync.FleetRelaySyncError):
        fleet_relay_sync._decode_file(frame)
    valid = fleet_relay_sync._encode_file("base/one", b"body")
    with pytest.raises(fleet_relay_sync.FleetRelaySyncError):
        fleet_relay_sync._decode_file(valid[:-1] + b"x")


def test_publish_connector_runtime_org_none_is_the_scopeless_target_not_unspecified(
    monkeypatch,
):
    from tools.dashboard import link_serving_supervisor
    from tools.graph.schemas import dashboard_shell

    seen = []
    monkeypatch.setattr(
        link_serving_supervisor,
        "control",
        lambda org, op, args: seen.append(org) or {"ok": True},
    )
    monkeypatch.setattr(dashboard_shell, "shell_default_org", lambda: "anchore")

    fleet_relay_sync.publish_connector_runtime({"x": 1}, org=None)
    assert seen == [None], (
        "org=None must reach control() as the scopeless target -- "
        "'org or shell_default_org()' would silently replace it with the "
        "cosmetic default org instead"
    )

    seen.clear()
    fleet_relay_sync.publish_connector_runtime({"x": 1})
    assert seen == ["anchore"], "an omitted org must still fall back to shell_default_org()"
