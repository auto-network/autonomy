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
