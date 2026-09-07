from __future__ import annotations

import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from tools.graph.db import GraphDB
from tools.graph.models import Source
from tools.network import fleet_relay_sync, fleet_roster
from tools.network.fleet_sync_channel import FleetAuthenticator
from tools.network.fleet_sync_scheduler import (
    decode_done,
    decode_pull_request,
    encode_done,
)
from tools.network.idkit import KeyPair, Subject, issue_cert


def _two_machine_fleet():
    """Root + two enrolled machines with per-machine runtime payloads."""
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

    return SimpleNamespace(
        root=root,
        server_machine=server_machine,
        client_machine=client_machine,
        server_id=server_id,
        client_id=client_id,
        entries=entries,
        runtime=runtime,
    )


def _prepare_org_db(path, origin_pub: str) -> None:
    db = GraphDB(path)
    try:
        db.activate_fleet_sync_writers(origin_pub)
    finally:
        db.close()


def _insert_note(path, source_id: str, title: str) -> None:
    db = GraphDB(path)
    try:
        db.insert_source(Source(id=source_id, type="note", title=title))
    finally:
        db.close()


def _has_note(path, source_id: str) -> bool:
    if not path.exists():
        return False
    try:
        with sqlite3.connect(
            f"file:{path}?mode=ro&immutable=1", uri=True
        ) as conn:
            return conn.execute(
                "SELECT 1 FROM sources WHERE id=?", (source_id,)
            ).fetchone() is not None
    except sqlite3.Error:
        return False


def _configure_relay_server(fleet, personal_path, monkeypatch):
    monkeypatch.setattr(
        "tools.network.fleet_tunnel_server._personal_root_pub",
        lambda: fleet.root.public_hex,
    )
    monkeypatch.setattr(
        fleet_relay_sync.fleet_roster,
        "load_entries",
        lambda *, org: list(fleet.entries),
    )
    monkeypatch.setattr(
        fleet_relay_sync, "_org_db_path", lambda _org: personal_path
    )
    _server_process, _cert, payload = fleet.runtime(
        fleet.server_machine, fleet.server_id, "60" * 32
    )
    server = fleet_relay_sync.ConnectorFleetRuntime()
    assert server.configure(payload) == {
        "ok": True, "machine_id": fleet.server_id,
    }
    return server


def _client_hello(fleet, token: str):
    process, cert, _payload = fleet.runtime(
        fleet.client_machine, fleet.client_id, "70" * 32
    )
    auth = FleetAuthenticator(
        process,
        root_pub=fleet.root.public_hex,
        roster_entries=lambda: fleet.entries,
        roster_machine_pub=fleet.client_machine.public_hex,
        delegation_cert=cert,
        require_delegation=True,
    )
    private, hello = auth.build_client_hello(token)
    return auth, private, hello


@pytest.mark.asyncio
async def test_scoped_pull_serves_checkpoint_from_the_scope_database(
    tmp_path, monkeypatch
):
    fleet = _two_machine_fleet()
    personal = tmp_path / "personal.db"
    personal.touch()
    alpha = tmp_path / "alpha.db"
    _prepare_org_db(alpha, fleet.server_machine.public_hex)
    _insert_note(alpha, "a-1", "org content")
    server = _configure_relay_server(fleet, personal, monkeypatch)
    monkeypatch.setattr(
        server.scheduler, "_scope_paths",
        lambda: {"personal": personal, "alpha": alpha},
    )

    class ScopeAlpha:
        def __init__(self, path, origin):
            assert path == alpha, "checkpoint must build from the scope DB"
            assert origin == fleet.server_machine.public_hex

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def checkpoint(self, directory, **_kwargs):
            directory.mkdir()
            (directory / "alpha-manifest.json").write_bytes(b"manifest")

    monkeypatch.setattr(fleet_relay_sync, "FleetSyncAlpha", ScopeAlpha)
    delegated = []

    async def fake_handle(_token, message, _peer_pub, **_telemetry):
        delegated.append(message)

        async def response():
            yield encode_done(
                epoch="ef" * 32,
                count=0,
                digest=__import__("hashlib").sha256().hexdigest(),
            )
        return response()

    server.scheduler._handle = fake_handle

    token = "ab" * 16
    auth, private, hello = _client_hello(fleet, token)
    from tools.network.fleet_sync_scheduler import SQLiteFleetSyncStore

    stream = await server.handle(token, {
        "v": 1,
        "op": "fleet.sync.pull",
        "roster_epoch": "cd" * 32,
        "checkpoint": True,
        "compat": SQLiteFleetSyncStore(alpha).compatibility_digest(),
        "resume": [],
        "hello": json.loads(hello),
        "scope": "alpha",
    })
    frames = [frame async for frame in stream]
    auth.verify_server(
        fleet_relay_sync.canonical_json(json.loads(frames[0])["hello"]),
        session=token,
        client_eph=private.public_key().public_bytes_raw().hex(),
        expected_machine_pub=fleet.server_machine.public_hex,
    )
    assert json.loads(frames[1])["kind"] == "checkpoint.begin"
    assert json.loads(frames[-2])["kind"] == "checkpoint.end"
    # The delegated delta phase carries the scope through to the scheduler.
    assert len(delegated) == 1
    assert decode_pull_request(delegated[0])[3] == "alpha"


@pytest.mark.asyncio
async def test_scoped_schema_mismatch_refuses_only_that_scope(
    tmp_path, monkeypatch
):
    fleet = _two_machine_fleet()
    personal = tmp_path / "personal.db"
    personal.touch()
    alpha = tmp_path / "alpha.db"
    _prepare_org_db(alpha, fleet.server_machine.public_hex)
    server = _configure_relay_server(fleet, personal, monkeypatch)
    monkeypatch.setattr(
        server.scheduler, "_scope_paths",
        lambda: {"personal": personal, "alpha": alpha},
    )

    async def fake_handle(_token, _message, _peer_pub, **_telemetry):
        async def response():
            yield encode_done(
                epoch="ef" * 32,
                count=0,
                digest=__import__("hashlib").sha256().hexdigest(),
            )
        return response()

    server.scheduler._handle = fake_handle
    token = "ab" * 16

    def request(scope, compat):
        _auth, _private, hello = _client_hello(fleet, token)
        body = {
            "v": 1,
            "op": "fleet.sync.pull",
            "roster_epoch": "cd" * 32,
            "checkpoint": False,
            "compat": compat,
            "resume": [],
            "hello": json.loads(hello),
        }
        if scope is not None:
            body["scope"] = scope
        return body

    # A mismatched org digest refuses that scope's pull...
    with pytest.raises(fleet_relay_sync.FleetRelaySyncError, match="schema mismatch"):
        await server.handle(token, request("alpha", "ee" * 32))
    # ...an unknown scope refuses with its own error...
    with pytest.raises(fleet_relay_sync.FleetRelaySyncError, match="unknown fleet sync scope"):
        await server.handle(token, request("nope", "ee" * 32))
    # ...and the personal scope still serves afterwards.
    stream = await server.handle(token, request(
        None, server.scheduler.store.compatibility_digest()
    ))
    frames = [frame async for frame in stream]
    assert json.loads(frames[0])["kind"] == "fleet.server-hello"
    assert decode_done(frames[-1])[1] == 0


@pytest.mark.asyncio
async def test_org_write_crosses_the_relay_path_with_isolation(
    tmp_path, monkeypatch
):
    """The acceptance shape of test_org_scope_sync, at the relay layer: an
    org row crosses via a real scoped checkpoint + install while the
    personal database and a second org stay untouched."""
    fleet = _two_machine_fleet()
    server_dir = tmp_path / "server"
    client_dir = tmp_path / "client"
    server_dir.mkdir()
    client_dir.mkdir()
    server_personal = server_dir / "personal.db"
    server_personal.touch()
    server_alpha = server_dir / "alpha.db"
    server_beta = server_dir / "beta.db"
    _prepare_org_db(server_alpha, fleet.server_machine.public_hex)
    _prepare_org_db(server_beta, fleet.server_machine.public_hex)
    _insert_note(server_alpha, "a-note", "alpha crossing")
    _insert_note(server_beta, "b-note", "beta crossing")
    client_personal = client_dir / "personal.db"
    _prepare_org_db(client_personal, fleet.client_machine.public_hex)
    client_alpha = client_dir / "alpha.db"
    client_beta = client_dir / "beta.db"

    server = _configure_relay_server(fleet, server_personal, monkeypatch)
    server_paths = {
        "personal": server_personal,
        "alpha": server_alpha,
        "beta": server_beta,
    }
    monkeypatch.setattr(
        server.scheduler, "_scope_paths", lambda: dict(server_paths)
    )

    # Client side: repoint the module's path resolution at the client's
    # databases. The server's paths were captured at configure time.
    monkeypatch.setattr(
        fleet_relay_sync, "_org_db_path", lambda _org: client_personal
    )
    monkeypatch.setattr(
        fleet_relay_sync,
        "discover_org_sync_scopes",
        lambda: {"alpha": client_alpha, "beta": client_beta},
    )
    token = "ab" * 16
    monkeypatch.setattr(
        fleet_relay_sync, "_route_location",
        lambda _rendezvous: ("https://relay", "wss://relay", token),
    )

    async def fake_envelope(_base, _token):
        return {
            "target_type": "fleet:join",
            "root_pub": fleet.root.public_hex,
            "org": "personal",
        }

    monkeypatch.setattr(fleet_relay_sync, "_fetch_envelope", fake_envelope)

    class LoopbackChannel:
        """Drives server.handle directly — the transport under test is the
        fleet application protocol, not the WebSocket relay beneath it."""

        def __init__(self):
            self._stream = None

        @classmethod
        async def connect(cls, *_args, **_kwargs):
            return cls()

        async def send_message(self, raw):
            message = json.loads(raw)
            try:
                self._stream = await server.handle(token, message)
            except fleet_relay_sync.FleetRelaySyncError as exc:
                async def refused():
                    yield fleet_relay_sync.canonical_json({
                        "kind": "fleet.server-error", "error": str(exc),
                    })
                self._stream = refused()

        async def recv_message_stream(self):
            async for frame in self._stream:
                yield frame, False

        async def close(self):
            pass

    monkeypatch.setattr(fleet_relay_sync, "ViewerChannel", LoopbackChannel)
    from tools.network import fleet_route, fleet_runtime

    _process, _cert, payload = fleet.runtime(
        fleet.client_machine, fleet.client_id, "70" * 32
    )
    credential = fleet_runtime.FleetRuntimeCredential.from_browser_payload(
        payload,
        personal_root_pub=fleet.root.public_hex,
        roster_entries=fleet.entries,
    )
    route = fleet_route.FleetRoute(
        rendezvous="https://relay/l/" + token,
        origin_machine_pub=fleet.server_machine.public_hex,
    )

    await fleet_relay_sync.pull_checkpoint_once(
        credential, route, include_checkpoint=True, scope="alpha",
    )
    assert _has_note(client_alpha, "a-note"), "org write must cross the relay"
    assert not _has_note(client_personal, "a-note")
    assert not _has_note(client_beta, "a-note")
    assert not _has_note(client_beta, "b-note")

    await fleet_relay_sync.pull_checkpoint_once(
        credential, route, include_checkpoint=True, scope="beta",
    )
    assert _has_note(client_beta, "b-note")
    assert not _has_note(client_alpha, "b-note")
    assert not _has_note(client_personal, "b-note")

    # Exactly one checkpoint receipt per scope: the install path records
    # it; the client must not add a second (the 64963898 class).
    with sqlite3.connect(
        f"file:{client_alpha}?mode=ro&immutable=1", uri=True
    ) as conn:
        assert conn.execute(
            "SELECT COALESCE(SUM(checkpoints_received),0) "
            "FROM fleet_sync_peer_state"
        ).fetchone()[0] == 1


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
    # The serve decision now refuses to checkpoint an empty database; this
    # test's store is a bare touched file standing in for real content.
    monkeypatch.setattr(server.scheduler.store, "has_state", lambda: True)
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
        "compat": server.scheduler.store.compatibility_digest(),
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


def _pull_message(fleet, alpha_path, hello, scope="alpha"):
    from tools.network.fleet_sync_scheduler import SQLiteFleetSyncStore
    return {
        "v": 1,
        "op": "fleet.sync.pull",
        "roster_epoch": "cd" * 32,
        "checkpoint": True,
        "compat": SQLiteFleetSyncStore(alpha_path).compatibility_digest(),
        "resume": [],
        "hello": json.loads(hello),
        "scope": scope,
    }


def _fake_delta_handle(server):
    async def fake_handle(_token, _message, _peer_pub, **_telemetry):
        async def response():
            yield encode_done(
                epoch="ef" * 32,
                count=0,
                digest=__import__("hashlib").sha256().hexdigest(),
            )
        return response()
    server.scheduler._handle = fake_handle


@pytest.mark.asyncio
async def test_abandoned_pull_aborts_its_checkpoint_build(tmp_path, monkeypatch):
    """When the puller disconnects mid-build, the build thread must observe
    should_abort and exit — not finish a full-database build for nobody
    (the 2026-09-06 100%-CPU wedge)."""
    import asyncio
    import threading
    from tools.network.fleet_sync.sync import CheckpointAborted

    fleet = _two_machine_fleet()
    personal = tmp_path / "personal.db"
    personal.touch()
    alpha = tmp_path / "alpha.db"
    _prepare_org_db(alpha, fleet.server_machine.public_hex)
    _insert_note(alpha, "a-1", "org content")
    server = _configure_relay_server(fleet, personal, monkeypatch)
    monkeypatch.setattr(
        server.scheduler, "_scope_paths",
        lambda: {"personal": personal, "alpha": alpha},
    )
    _fake_delta_handle(server)

    build_started = threading.Event()
    build_finished = threading.Event()
    observed = {}

    class BlockingAlpha:
        def __init__(self, _path, _origin):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def checkpoint(self, _directory, *, should_abort=None, **_kwargs):
            build_started.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if should_abort is not None and should_abort():
                    observed["aborted"] = True
                    build_finished.set()
                    raise CheckpointAborted("test abort")
                time.sleep(0.01)
            build_finished.set()
            raise AssertionError("abort was never observed by the build")

    monkeypatch.setattr(fleet_relay_sync, "FleetSyncAlpha", BlockingAlpha)
    token = "ab" * 16
    _auth, _private, hello = _client_hello(fleet, token)
    stream = await server.handle(token, _pull_message(fleet, alpha, hello))
    await stream.__anext__()  # server-hello arrives before the build
    puller = asyncio.ensure_future(stream.__anext__())
    assert await asyncio.to_thread(build_started.wait, 5)
    puller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await puller
    assert await asyncio.to_thread(build_finished.wait, 5)
    assert observed.get("aborted") is True


@pytest.mark.asyncio
async def test_concurrent_pulls_hold_one_build_slot_per_scope(
    tmp_path, monkeypatch
):
    """Two pulls for the same scope must serialize their checkpoint builds:
    stacked concurrent builds starve each other so none finishes inside the
    client's patience."""
    import asyncio
    import threading

    fleet = _two_machine_fleet()
    personal = tmp_path / "personal.db"
    personal.touch()
    alpha = tmp_path / "alpha.db"
    _prepare_org_db(alpha, fleet.server_machine.public_hex)
    _insert_note(alpha, "a-1", "org content")
    server = _configure_relay_server(fleet, personal, monkeypatch)
    monkeypatch.setattr(
        server.scheduler, "_scope_paths",
        lambda: {"personal": personal, "alpha": alpha},
    )
    _fake_delta_handle(server)

    starts: list[float] = []
    release = threading.Event()

    class SlowAlpha:
        def __init__(self, _path, _origin):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def checkpoint(self, directory, *, should_abort=None, **_kwargs):
            starts.append(time.monotonic())
            assert release.wait(5), "first build was never released"
            directory.mkdir()
            (directory / "alpha-manifest.json").write_bytes(b"manifest")

    monkeypatch.setattr(fleet_relay_sync, "FleetSyncAlpha", SlowAlpha)

    async def run_pull(token_hex):
        _auth, _private, hello = _client_hello(fleet, token_hex)
        stream = await server.handle(
            token_hex, _pull_message(fleet, alpha, hello)
        )
        return [frame async for frame in stream]

    first = asyncio.ensure_future(run_pull("ab" * 16))
    second = asyncio.ensure_future(run_pull("ba" * 16))
    deadline = time.monotonic() + 5
    while not starts and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert len(starts) == 1, "one build must start promptly"
    await asyncio.sleep(0.3)
    assert len(starts) == 1, "second build must queue, not stack"
    release.set()
    frames_first = await first
    frames_second = await second
    assert len(starts) == 2
    for frames in (frames_first, frames_second):
        kinds = [
            json.loads(frame).get("kind") for frame in frames
            if frame[:1] in ("{", b"{")  # file frames are binary (FSB1)
        ]
        assert "checkpoint.begin" in kinds and "checkpoint.end" in kinds


@pytest.mark.asyncio
async def test_slow_build_emits_keepalives_before_checkpoint_begin(
    tmp_path, monkeypatch
):
    """A build longer than the keepalive interval must emit keepalive frames
    BEFORE checkpoint.begin, so the client's 60s frame-silence limit never
    trips mid-build (the 2026-09-06 200s-build delivery failure)."""
    import asyncio

    monkeypatch.setattr(fleet_relay_sync, "BUILD_KEEPALIVE_INTERVAL_S", 0.05)
    fleet = _two_machine_fleet()
    personal = tmp_path / "personal.db"
    personal.touch()
    alpha = tmp_path / "alpha.db"
    _prepare_org_db(alpha, fleet.server_machine.public_hex)
    _insert_note(alpha, "a-1", "org content")
    server = _configure_relay_server(fleet, personal, monkeypatch)
    monkeypatch.setattr(
        server.scheduler, "_scope_paths",
        lambda: {"personal": personal, "alpha": alpha},
    )
    _fake_delta_handle(server)

    class SlowAlpha:
        def __init__(self, _path, _origin):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def checkpoint(self, directory, *, should_abort=None, **_kwargs):
            time.sleep(0.25)  # ~5 keepalive intervals
            directory.mkdir()
            (directory / "alpha-manifest.json").write_bytes(b"manifest")

    monkeypatch.setattr(fleet_relay_sync, "FleetSyncAlpha", SlowAlpha)
    token = "ab" * 16
    _auth, _private, hello = _client_hello(fleet, token)
    stream = await server.handle(token, _pull_message(fleet, alpha, hello))

    kinds = []
    async for frame in stream:
        if frame[:1] in ("{", b"{"):
            kinds.append(json.loads(frame).get("kind"))
        else:
            kinds.append("<file>")

    assert "keepalive" in kinds, "a slow build must emit keepalives"
    # Every keepalive precedes checkpoint.begin (build is before the begin).
    first_begin = kinds.index("checkpoint.begin")
    assert kinds[:first_begin].count("keepalive") >= 1
    assert "checkpoint.end" in kinds, "build still completes and delivers"


@pytest.mark.asyncio
async def test_first_contact_delta_starts_at_the_checkpoint_floor(
    tmp_path, monkeypatch
):
    """After serving a checkpoint the delta phase must NOT replay the journal
    the checkpoint already carries: zero mutation frames follow the
    checkpoint for a quiet store (live 2026-09-06 it replayed ~700k)."""
    from tools.network.fleet_sync_scheduler import (
        _DONE_MAGIC, _MUTATION_MAGIC, _OPERATION_MAGIC,
    )

    fleet = _two_machine_fleet()
    personal = tmp_path / "personal.db"
    personal.touch()
    alpha = tmp_path / "alpha.db"
    _prepare_org_db(alpha, fleet.server_machine.public_hex)
    for i in range(25):
        _insert_note(alpha, f"a-{i}", f"org content {i}")
    server = _configure_relay_server(fleet, personal, monkeypatch)
    monkeypatch.setattr(
        server.scheduler, "_scope_paths",
        lambda: {"personal": personal, "alpha": alpha},
    )
    # REAL scheduler._handle and REAL FleetSyncAlpha: this is the composition
    # the perf suite never exercised.
    token = "ab" * 16
    _auth, _private, hello = _client_hello(fleet, token)
    stream = await server.handle(token, _pull_message(fleet, alpha, hello))
    frames = [frame async for frame in stream]

    kinds = [
        json.loads(f).get("kind") for f in frames if f[:1] in ("{", b"{")
    ]
    assert "checkpoint.begin" in kinds and "checkpoint.end" in kinds
    replayed = [
        f for f in frames
        if f.startswith(_OPERATION_MAGIC) or f.startswith(_MUTATION_MAGIC)
    ]
    assert replayed == [], (
        f"{len(replayed)} journal operations replayed after the checkpoint"
    )
    assert any(f.startswith(_DONE_MAGIC) for f in frames), "delta must close"


@pytest.mark.asyncio
async def test_founded_origin_gets_the_retained_journal_not_a_checkpoint(
    tmp_path, monkeypatch
):
    """A puller that refuses checkpoints (founded ledger) is served the
    retained journal from position 0 through the REAL scheduler._handle,
    even when the server's decision would otherwise be a checkpoint."""
    from tools.network.fleet_sync_scheduler import (
        _DONE_MAGIC, _MUTATION_MAGIC, _OPERATION_MAGIC, encode_pull_request,
        SQLiteFleetSyncStore,
    )

    fleet = _two_machine_fleet()
    alpha = tmp_path / "alpha.db"
    _prepare_org_db(alpha, fleet.server_machine.public_hex)
    for i in range(7):
        _insert_note(alpha, f"a-{i}", f"org content {i}")
    personal = tmp_path / "personal.db"
    personal.touch()
    server = _configure_relay_server(fleet, personal, monkeypatch)
    monkeypatch.setattr(
        server.scheduler, "_scope_paths",
        lambda: {"personal": personal, "alpha": alpha},
    )
    compat = SQLiteFleetSyncStore(alpha).compatibility_digest()

    async def frames_for(accept: bool):
        request = encode_pull_request(
            "cd" * 32, compat=compat, resume=(), scope="alpha",
            bootstrap=True, accept_checkpoint=accept,
        )
        stream = await server.scheduler._handle(
            "tok", request, fleet.client_machine.public_hex,
        )
        return [frame async for frame in stream]

    kinds = lambda frames: [  # noqa: E731
        json.loads(f).get("kind") for f in frames if f[:1] in ("{", b"{")
    ]
    plain = await frames_for(True)
    assert "checkpoint.begin" in kinds(plain)        # the ordinary bootstrap

    origin = await frames_for(False)
    assert "checkpoint.begin" not in kinds(origin)
    replayed = [
        f for f in origin
        if f.startswith(_OPERATION_MAGIC) or f.startswith(_MUTATION_MAGIC)
    ]
    assert replayed, "the retained journal must be replayed instead"
    assert any(f.startswith(_DONE_MAGIC) for f in origin)


@pytest.mark.asyncio
async def test_established_puller_with_unknown_position_gets_the_journal_not_a_snapshot(
    tmp_path, monkeypatch
):
    """The rule of record: a puller that has sync state (bootstrap=False)
    but whose trail resolves nowhere is served the retained journal even
    when the server's journal has retired history -- never a snapshot.
    Before 2026-09-07 this exact case re-based live databases."""
    from tools.network.fleet_sync_scheduler import (
        _DONE_MAGIC, _MUTATION_MAGIC, _OPERATION_MAGIC, encode_pull_request,
        SQLiteFleetSyncStore,
    )

    fleet = _two_machine_fleet()
    alpha = tmp_path / "alpha.db"
    _prepare_org_db(alpha, fleet.server_machine.public_hex)
    for i in range(6):
        _insert_note(alpha, f"a-{i}", f"org content {i}")
    personal = tmp_path / "personal.db"
    personal.touch()
    server = _configure_relay_server(fleet, personal, monkeypatch)
    monkeypatch.setattr(
        server.scheduler, "_scope_paths",
        lambda: {"personal": personal, "alpha": alpha},
    )
    store = SQLiteFleetSyncStore(alpha)
    # Retire the first frames the way pruning does, so the journal has a gap.
    with sqlite3.connect(alpha) as conn:
        conn.execute(
            "DELETE FROM fleet_sync_journal WHERE transaction_ref IN "
            "(SELECT id FROM fleet_sync_transactions ORDER BY id LIMIT 2)"
        )
    assert store.journal_gap() is True

    request = encode_pull_request(
        "cd" * 32, compat=store.compatibility_digest(), resume=(),
        scope="alpha", bootstrap=False,
    )
    stream = await server.scheduler._handle(
        "tok", request, fleet.client_machine.public_hex,
    )
    frames = [frame async for frame in stream]
    kinds = [json.loads(f).get("kind") for f in frames if f[:1] in ("{", b"{")]
    assert "checkpoint.begin" not in kinds
    replayed = [
        f for f in frames
        if f.startswith(_OPERATION_MAGIC) or f.startswith(_MUTATION_MAGIC)
    ]
    assert len(replayed) == 4, "the four surviving transactions replay"
    assert any(f.startswith(_DONE_MAGIC) for f in frames)

    # A genuinely empty puller (bootstrap=True) still gets the snapshot.
    request = encode_pull_request(
        "cd" * 32, compat=store.compatibility_digest(), resume=(),
        scope="alpha", bootstrap=True,
    )
    stream = await server.scheduler._handle(
        "tok", request, fleet.client_machine.public_hex,
    )
    frames = [frame async for frame in stream]
    kinds = [json.loads(f).get("kind") for f in frames if f[:1] in ("{", b"{")]
    assert "checkpoint.begin" in kinds


@pytest.mark.asyncio
async def test_per_author_watermarks_serve_each_author_once_and_never_echo(
    tmp_path, monkeypatch
):
    """Design of record: a puller sends {author: max timestamp held}; the
    server streams, per author, only transactions newer than that, and
    never the puller's own writes -- so an established peer with no trail
    on this server receives exactly what it lacks, not the journal."""
    from tools.network.fleet_sync_scheduler import (
        _DONE_MAGIC, _TRANSACTION_MAGIC, encode_pull_request,
        SQLiteFleetSyncStore, decode_transaction_header,
    )
    from tools.network.fleet_sync.catalog import MutationCatalog

    fleet = _two_machine_fleet()
    alpha = tmp_path / "alpha.db"
    _prepare_org_db(alpha, fleet.server_machine.public_hex)
    # Server-authored transactions at known timestamps, plus a transaction
    # the CLIENT authored that the server imported (must never echo).
    db = GraphDB(alpha)
    try:
        catalog = MutationCatalog(db.conn, fleet.server_machine.public_hex)
        for ts, ident in ((1_000, "s-old"), (2_000, "s-mid"), (3_000, "s-new")):
            with catalog.transaction(ts, f"tx-{ident}"):
                db.conn.execute(
                    "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (ident, "note", ident, "{}", "2026-09-07T00:00:00Z", "2026-09-07T00:00:00Z"),
                )
    finally:
        db.close()
    store = SQLiteFleetSyncStore(alpha)
    personal = tmp_path / "personal.db"
    personal.touch()
    server = _configure_relay_server(fleet, personal, monkeypatch)
    monkeypatch.setattr(
        server.scheduler, "_scope_paths",
        lambda: {"personal": personal, "alpha": alpha},
    )

    async def served(watermarks):
        request = encode_pull_request(
            "cd" * 32, compat=store.compatibility_digest(), resume=(),
            scope="alpha", bootstrap=False, watermarks=watermarks,
        )
        stream = await server.scheduler._handle(
            "tok", request, fleet.client_machine.public_hex,
        )
        frames = [f async for f in stream]
        headers = [
            decode_transaction_header(f) for f in frames
            if f.startswith(_TRANSACTION_MAGIC)
        ]
        assert any(f.startswith(_DONE_MAGIC) for f in frames)
        kinds = [json.loads(f).get("kind") for f in frames if f[:1] in ("{", b"{")]
        assert "checkpoint.begin" not in kinds
        return [(origin, tx) for origin, tx, _ops in headers]

    server_pub = fleet.server_machine.public_hex
    # Knows nothing about this author (established elsewhere): receives all
    # three, once, in author order -- not a snapshot, not the puller's own.
    assert [tx for _o, tx in await served({"ee" * 32: 9_999})] == [
        "tx-s-old", "tx-s-mid", "tx-s-new",
    ]
    # Holds the server's writes through ts=2000: receives only s-new.
    assert await served({server_pub: 2_000}) == [(server_pub, "tx-s-new")]
    # Holds everything: receives nothing -- and that map is a full
    # acknowledgement, so the server may now retire those frames (the
    # existing served-ack floor, fed by implied_ack_ref).
    assert await served({server_pub: 3_000}) == []
    assert store.oldest_journal_ref() in (None, 4)


@pytest.mark.asyncio
async def test_server_skips_an_author_whose_retired_history_is_above_the_watermark(
    tmp_path, monkeypatch
):
    """A snapshot receiver holds an author's early writes only as installed
    rows (no frames). Serving that author from a low watermark would
    advance the puller past writes it never got. The server must skip the
    author and say so; a puller already past the retired prefix is served."""
    from tools.network.fleet_sync_scheduler import (
        _TRANSACTION_MAGIC, encode_pull_request, SQLiteFleetSyncStore,
        decode_transaction_header,
    )
    from tools.network.fleet_sync.catalog import MutationCatalog

    fleet = _two_machine_fleet()
    alpha = tmp_path / "alpha.db"
    _prepare_org_db(alpha, fleet.server_machine.public_hex)
    db = GraphDB(alpha)
    try:
        catalog = MutationCatalog(db.conn, fleet.server_machine.public_hex)
        for ts, ident in ((1_000, "a0"), (2_000, "a1"), (3_000, "a2")):
            with catalog.transaction(ts, f"tx-{ident}"):
                db.conn.execute(
                    "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (ident, "note", ident, "{}", "2026-09-07T00:00:00Z", "2026-09-07T00:00:00Z"),
                )
        # Install shape: the first transaction's frames are gone.
        db.conn.execute(
            "DELETE FROM fleet_sync_journal WHERE transaction_ref="
            "(SELECT MIN(id) FROM fleet_sync_transactions)"
        )
        db.conn.commit()
    finally:
        db.close()
    store = SQLiteFleetSyncStore(alpha)
    personal = tmp_path / "personal.db"
    personal.touch()
    server = _configure_relay_server(fleet, personal, monkeypatch)
    monkeypatch.setattr(
        server.scheduler, "_scope_paths",
        lambda: {"personal": personal, "alpha": alpha},
    )
    server_pub = fleet.server_machine.public_hex

    async def pull(watermarks):
        request = encode_pull_request(
            "cd" * 32, compat=store.compatibility_digest(), resume=(),
            scope="alpha", bootstrap=False, watermarks=watermarks,
        )
        stream = await server.scheduler._handle("tok", request, fleet.client_machine.public_hex)
        frames = [f async for f in stream]
        controls = [json.loads(f) for f in frames if f[:1] in ("{", b"{")]
        served = [decode_transaction_header(f)[1] for f in frames if f.startswith(_TRANSACTION_MAGIC)]
        return controls, served

    # Puller knows nothing of this author: NOT served, told why.
    controls, served = await pull({"ee" * 32: 5})
    assert served == []
    assert any(c.get("kind") == "retired" and c.get("authors") == [server_pub] for c in controls)
    # Puller already holds the retired prefix (W >= 1000): served the rest.
    controls, served = await pull({server_pub: 1_000})
    assert served == ["tx-a1", "tx-a2"]
    assert not any(c.get("kind") == "retired" for c in controls)


@pytest.mark.asyncio
async def test_fresh_checkpoint_request_right_after_a_delivery_is_refused(
    tmp_path, monkeypatch
):
    """A peer that just received a complete checkpoint and asks again with an
    empty resume trail failed to install it; the server must refuse instead
    of rebuilding (2.27GB per 3.5min live 2026-09-06)."""
    fleet = _two_machine_fleet()
    personal = tmp_path / "personal.db"
    personal.touch()
    alpha = tmp_path / "alpha.db"
    _prepare_org_db(alpha, fleet.server_machine.public_hex)
    _insert_note(alpha, "a-1", "org content")
    server = _configure_relay_server(fleet, personal, monkeypatch)
    monkeypatch.setattr(
        server.scheduler, "_scope_paths",
        lambda: {"personal": personal, "alpha": alpha},
    )
    token = "ab" * 16
    _auth, _private, hello = _client_hello(fleet, token)
    stream = await server.handle(token, _pull_message(fleet, alpha, hello))
    frames = [frame async for frame in stream]
    kinds = [json.loads(f).get("kind") for f in frames if f[:1] in ("{", b"{")]
    assert "checkpoint.end" in kinds, "first delivery completes"

    _auth2, _private2, hello2 = _client_hello(fleet, "cd" * 16)
    with pytest.raises(
        fleet_relay_sync.FleetRelaySyncError, match="failed to keep it"
    ):
        await server.handle("cd" * 16, _pull_message(fleet, alpha, hello2))


@pytest.mark.asyncio
async def test_puller_connects_with_a_bulk_safe_ping_timeout(monkeypatch):
    """The puller's websocket must not let pong latency kill a receiving
    stream: SJC closed a 2.27GB transfer with 'keepalive ping timeout' at
    the library's 20s default while data was still flowing (2026-09-06)."""
    from tools.network.relaykit import viewer as viewer_module

    captured = {}

    async def fake_connect(url, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop here")

    monkeypatch.setattr(viewer_module.websockets, "connect", fake_connect)
    with pytest.raises(RuntimeError, match="stop here"):
        await viewer_module.ViewerChannel.connect(
            "wss://relay.example", "ab" * 16, org="autonomy",
            ping_timeout=fleet_relay_sync.PULL_PING_TIMEOUT_S,
        )
    assert captured["ping_timeout"] == fleet_relay_sync.PULL_PING_TIMEOUT_S
    assert captured["ping_timeout"] > 60.0, "must exceed the 60s frame-silence rule"
    assert captured["ping_interval"] == 20.0, "keep pinging; only the deadline widens"


def test_route_location_accepts_the_wss_hint_spelling():
    token = "ab" * 16
    assert fleet_relay_sync._route_location(f"wss://relay.example/l/{token}") == (
        "https://relay.example", "wss://relay.example", token,
    )
    assert fleet_relay_sync.canonical_rendezvous(
        f"wss://relay.example/l/{token}"
    ) == f"https://relay.example/l/{token}"
    with pytest.raises(fleet_relay_sync.FleetRelaySyncError):
        fleet_relay_sync._route_location("wss://relay.example/v1/links/x/channel")


def test_expired_route_is_classified_distinctly():
    exc = fleet_relay_sync.FleetRelaySyncError("stored Fleet route is unavailable")
    assert fleet_relay_sync._classify_pull_failure(exc) == "route_unavailable"


def test_discovered_standing_route_rotates_the_stored_bootstrap_route(monkeypatch):
    """The stored route is the invitation from enrollment; once the origin
    publishes a standing route, the puller follows it and rotates the row.
    Unknown or malformed discovery leaves the stored route alone."""
    from tools.network import fleet_route

    origin = "11" * 32
    invite = fleet_route.FleetRoute("https://relay.example/l/" + "aa" * 16, origin)
    stored: list = []
    monkeypatch.setattr(fleet_route, "store", lambda route, org="machine": stored.append(route))

    monkeypatch.setattr(fleet_relay_sync, "standing_route_resolver", None)
    assert fleet_relay_sync.rotate_route_if_discovered(invite) is invite

    monkeypatch.setattr(fleet_relay_sync, "standing_route_resolver", lambda pub: None)
    assert fleet_relay_sync.rotate_route_if_discovered(invite) is invite

    monkeypatch.setattr(
        fleet_relay_sync, "standing_route_resolver", lambda pub: "https://relay.example/nope",
    )
    assert fleet_relay_sync.rotate_route_if_discovered(invite) is invite
    assert stored == []

    seen = []

    def resolver(pub):
        seen.append(pub)
        return "wss://relay.example/l/" + "bb" * 16

    monkeypatch.setattr(fleet_relay_sync, "standing_route_resolver", resolver)
    rotated = fleet_relay_sync.rotate_route_if_discovered(invite)
    assert seen == [origin]
    assert rotated == fleet_route.FleetRoute(
        "https://relay.example/l/" + "bb" * 16, origin,
    )
    assert stored == [rotated]

    # Already on the discovered route: no write.
    assert fleet_relay_sync.rotate_route_if_discovered(rotated) is rotated
    assert stored == [rotated]


def test_redelivery_window_escalates_and_caps():
    w = fleet_relay_sync._redelivery_window_s
    base = fleet_relay_sync.REDELIVERY_GUARD_S
    assert [w(0), w(1), w(2), w(3)] == [base, 2 * base, 4 * base, 8 * base]
    assert w(50) == fleet_relay_sync.REDELIVERY_GUARD_MAX_S


@pytest.mark.asyncio
async def test_repeated_unkept_deliveries_widen_the_refusal(tmp_path, monkeypatch):
    """Second unkept delivery → strike 1 → the refusal window doubles; a
    request with a resolvable trail clears the strikes."""
    import asyncio

    monkeypatch.setattr(fleet_relay_sync, "REDELIVERY_GUARD_S", 0.2)
    fleet = _two_machine_fleet()
    personal = tmp_path / "personal.db"
    personal.touch()
    alpha = tmp_path / "alpha.db"
    _prepare_org_db(alpha, fleet.server_machine.public_hex)
    _insert_note(alpha, "a-1", "org content")
    server = _configure_relay_server(fleet, personal, monkeypatch)
    monkeypatch.setattr(
        server.scheduler, "_scope_paths",
        lambda: {"personal": personal, "alpha": alpha},
    )

    async def fresh_pull(token_hex):
        _a, _p, hello = _client_hello(fleet, token_hex)
        stream = await server.handle(token_hex, _pull_message(fleet, alpha, hello))
        return [f async for f in stream]

    await fresh_pull("ab" * 16)                       # delivery #1, strikes 0
    key = next(iter(server._recent_checkpoint_delivery))
    assert server._recent_checkpoint_delivery[key][1] == 0
    await asyncio.sleep(0.25)                         # window(0)=0.2s elapsed
    await fresh_pull("cd" * 16)                       # delivery #2 → strike 1
    assert server._recent_checkpoint_delivery[key][1] == 1
    await asyncio.sleep(0.25)                         # < window(1)=0.4s
    with pytest.raises(fleet_relay_sync.FleetRelaySyncError, match="strike 2"):
        await fresh_pull("ef" * 16)
