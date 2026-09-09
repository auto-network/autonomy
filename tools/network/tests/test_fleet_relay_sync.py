from __future__ import annotations

import asyncio
import contextlib
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


def _control_kinds(frames):
    """Every JSON control record in a served stream, in order.

    The bootstrap frame used to sit at a fixed index because the relay emitted
    its own header immediately after the hello. Bootstrap now comes
    from the shared serve, so its position depends on what else that serve
    emits. Position was never the property under test -- presence is.
    """
    kinds = []
    for frame in frames:
        if isinstance(frame, (bytes, bytearray)) and frame.startswith(b"{"):
            import json as _json
            try:
                kinds.append(_json.loads(frame).get("kind"))
            except Exception:
                continue
    return kinds


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
            "bootstrap": False,
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
        "bootstrap": True,
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
async def test_per_origin_watermarks_serve_each_author_once_and_never_echo(
    tmp_path, monkeypatch
):
    """Design of record: a puller sends {origin: max timestamp held}; the
    server streams, per origin, only transactions newer than that, and
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
    # Server-originated transactions at known timestamps, plus a transaction
    # the CLIENT originated that the server imported (must never echo).
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
        assert "sweep.begin" not in kinds
        return [(origin, tx) for origin, tx, _ops in headers]

    server_pub = fleet.server_machine.public_hex
    # Knows nothing about this origin (established elsewhere): receives all
    # three, once, in origin order -- not a snapshot, not the puller's own.
    assert [tx for _o, tx in await served({"ee" * 32: 9_999})] == [
        "tx-s-old", "tx-s-mid", "tx-s-new",
    ]
    # Holds the server's writes through ts=2000: receives only s-new.
    assert await served({server_pub: 2_000}) == [(server_pub, "tx-s-new")]
    # Holds everything: receives nothing -- and that map is a full
    # acknowledgement, so the server may now retire those frames (the
    # existing served-ack floor, fed by implied_ack_ref).
    assert await served({server_pub: 3_000}) == []


@pytest.mark.asyncio
async def test_server_rebuilds_retired_frames_from_its_rows(
    tmp_path, monkeypatch
):
    """A machine that received an origin's early writes as a bootstrap (or
    pruned their journal frames after every peer acknowledged) holds those
    writes only as catalog rows plus live rows. It serves them anyway: the
    frames are rebuilt from the rows, exactly as a swept page is built, so
    a puller below that prefix receives it here and nothing is skipped."""
    from tools.network.fleet_sync_scheduler import (
        _TRANSACTION_MAGIC, _OPERATION_MAGIC, encode_pull_request,
        SQLiteFleetSyncStore, decode_transaction_header,
        decode_operation_frame,
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
        # A fourth transaction overwrites a0's row: tx-a0 has nothing
        # surviving, tx-a3 owns the row now.
        with catalog.transaction(4_000, "tx-a3"):
            db.conn.execute("UPDATE sources SET title='a0-renamed' WHERE id='a0'")
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
        served = [decode_transaction_header(f) for f in frames if f.startswith(_TRANSACTION_MAGIC)]
        ops = [decode_operation_frame(f)[1] for f in frames if f.startswith(_OPERATION_MAGIC)]
        return controls, served, ops

    # Puller knows nothing of this origin: served everything, in order.
    # tx-a0 survives nowhere (overwritten by tx-a3) and is named as empty
    # with its timestamp so the puller's watermark still passes it;
    # tx-a1's frame is rebuilt from the catalog row and the live row.
    controls, served, ops = await pull({"ee" * 32: 5})
    assert [tx for _o, tx, _n in served] == ["tx-a1", "tx-a2", "tx-a3"]
    assert [n for _o, _tx, n in served] == [1, 1, 1]
    empties = [c for c in controls if c.get("kind") == "transaction.empty"]
    assert [(c["origin"], c["transaction_id"], c["timestamp_ns"]) for c in empties] == [
        (server_pub, "tx-a0", 1_000)
    ]
    assert not any(c.get("kind") == "retired" for c in controls)
    rebuilt = ops[0]
    assert rebuilt.table == "sources" and rebuilt.timestamp_ns == 2_000
    assert dict(rebuilt.values)["title"] == "a1"
    assert dict(ops[2].values)["title"] == "a0-renamed"
    # Puller already holds through 2000: only the newer two.
    controls, served, _ops = await pull({server_pub: 2_000})
    assert [tx for _o, tx, _n in served] == ["tx-a2", "tx-a3"]
    assert not any(c.get("kind") == "transaction.empty" for c in controls)


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


@pytest.mark.asyncio
async def test_large_transaction_is_served_in_bounded_groups_and_applies_whole(
    tmp_path, monkeypatch
):
    """A transaction with more surviving rows than one wire group carries is
    served as several groups under one transaction id, the first group
    leaving before the whole transaction is built (the 60 s silence bound),
    and the receiver applies every row (SJC-2 autonomy scope, 2026-09-07)."""
    from tools.network import fleet_sync_scheduler as fss
    from tools.network.fleet_sync_scheduler import (
        _TRANSACTION_MAGIC, encode_pull_request, SQLiteFleetSyncStore,
        decode_transaction_header,
    )
    from tools.network.fleet_sync.catalog import MutationCatalog

    monkeypatch.setattr(fss, "SERVE_GROUP_OPERATIONS", 40)
    fleet = _two_machine_fleet()
    alpha = tmp_path / "alpha.db"
    _prepare_org_db(alpha, fleet.server_machine.public_hex)
    db = GraphDB(alpha)
    try:
        catalog = MutationCatalog(db.conn, fleet.server_machine.public_hex)
        with catalog.transaction(5_000, "tx-bulk"):
            for i in range(105):
                db.conn.execute(
                    "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (f"bulk-{i:03d}", "note", f"t{i}", "{}",
                     "2026-09-07T00:00:00Z", "2026-09-07T00:00:00Z"),
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
    request = encode_pull_request(
        "cd" * 32, compat=store.compatibility_digest(), resume=(),
        scope="alpha", bootstrap=False, watermarks={"ee" * 32: 1},
    )
    stream = await server.scheduler._handle("tok", request, fleet.client_machine.public_hex)
    frames = [f async for f in stream]
    headers = [decode_transaction_header(f) for f in frames if f.startswith(_TRANSACTION_MAGIC)]
    # 105 rows in groups of 40: three groups, one transaction id.
    assert [n for _o, _tx, n in headers] == [40, 40, 25]
    assert {tx for _o, tx, _n in headers} == {"tx-bulk"}

    # The receiver applies every group; all 105 rows land once.
    target = tmp_path / "target.db"
    _prepare_org_db(target, fleet.client_machine.public_hex)
    tdb = GraphDB(target)
    try:
        right = MutationCatalog(tdb.conn, fleet.client_machine.public_hex)
        from tools.network.fleet_sync_scheduler import _OPERATION_MAGIC, decode_operation_frame
        from tools.network.fleet_sync.compaction import AuthoredMutation
        group: list = []
        applied = 0
        current = None
        for f in frames:
            if f.startswith(_TRANSACTION_MAGIC):
                if group:
                    applied += right.apply_remote_batch(group)[0]
                    group = []
                current = decode_transaction_header(f)
            elif f.startswith(_OPERATION_MAGIC):
                op, mutation = decode_operation_frame(f)
                group.append(AuthoredMutation(current[0], current[1], op, mutation))
        if group:
            applied += right.apply_remote_batch(group)[0]
        assert applied == 105
        assert tdb.conn.execute("SELECT COUNT(*) FROM sources WHERE id LIKE 'bulk-%'").fetchone()[0] == 105
        # And the receiver's watermark for the origin passed the transaction.
        assert right.origin_watermarks()[fleet.server_machine.public_hex] == 5_000
    finally:
        tdb.close()


@pytest.mark.asyncio
async def test_serve_ends_right_after_its_done_frame_even_when_the_prune_is_slow(
    tmp_path, monkeypatch
):
    """The channel server marks a record final only when the generator
    ends; the served-ack prune and the telemetry write used to run after
    the done frame inside the generator, holding it back for as long as
    they took (SJC-2 saw every data frame then 60 s of silence, 2026-09-07).
    The generator must end promptly; the prune runs in a task."""
    import asyncio
    import time as _time

    from tools.network import fleet_sync_scheduler as fss
    from tools.network.fleet_sync_scheduler import (
        _DONE_MAGIC, encode_pull_request, SQLiteFleetSyncStore,
    )

    fleet = _two_machine_fleet()
    alpha = tmp_path / "alpha.db"
    _prepare_org_db(alpha, fleet.server_machine.public_hex)
    _insert_note(alpha, "a-1", "content")
    store = SQLiteFleetSyncStore(alpha)
    personal = tmp_path / "personal.db"
    personal.touch()
    server = _configure_relay_server(fleet, personal, monkeypatch)
    monkeypatch.setattr(
        server.scheduler, "_scope_paths",
        lambda: {"personal": personal, "alpha": alpha},
    )
    pruned = asyncio.Event()

    def slow_prune(self, others, epoch):
        _time.sleep(1.5)
        pruned.set()
        return (0, 0)

    monkeypatch.setattr(fss.SQLiteFleetSyncStore, "prune_acknowledged", slow_prune)
    monkeypatch.setattr(fss, "PRUNE_MIN_INTERVAL_S", 0.0)
    request = encode_pull_request(
        "cd" * 32, compat=store.compatibility_digest(), resume=(),
        scope="alpha", bootstrap=False, watermarks={"ee" * 32: 1},
    )
    started = _time.monotonic()
    stream = await server.scheduler._handle("tok", request, fleet.client_machine.public_hex)
    frames = [f async for f in stream]
    elapsed = _time.monotonic() - started
    assert frames and frames[-1].startswith(_DONE_MAGIC)
    assert elapsed < 1.0, f"generator held its done frame for {elapsed:.1f}s"
    # The prune still happens, off the response path.
    await asyncio.wait_for(pruned.wait(), 5.0)


# ── One inbound listener per machine (2026-09-09) ───────────────────────
#
# Outbound tunnels are per-org by design — each authenticates AS that org.
# The INBOUND direct listener is machine-wide and multiplexes every org by
# channel, so exactly one process may bind it. Every connector computed the
# same bind from the same machine-scoped row, so four org connectors raced
# for the port: anchore won and the PERSONAL connector, which actually owns
# fleet sync, could not bind at all.


def _configure_and_capture(runtime, monkeypatch, port):
    """Run the REAL configure() and return the port it resolved to.

    Exercised rather than flag-asserted: the earlier version of these tests
    checked the ownership boolean, which cannot show that a port was not
    taken.
    """
    from tools.network import fleet_direct_config, fleet_relay_sync as frs
    from tools.network import fleet_roster as fr, fleet_runtime, machine_boot

    captured = {}

    monkeypatch.setattr(
        fleet_direct_config, "load",
        lambda *a, **k: fleet_direct_config.FleetDirectConfig(
            "0.0.0.0", port, (), advertise_auto=False, serve_in="connector"))
    # configure() imports fleet_tunnel_server lazily inside the function, so
    # patch the module itself rather than an attribute of fleet_relay_sync.
    from tools.network import fleet_tunnel_server as fts
    monkeypatch.setattr(fts, "_personal_root_pub", lambda: "aa" * 32)
    monkeypatch.setattr(fr, "load_entries", lambda **k: ())
    monkeypatch.setattr(
        fleet_runtime.FleetRuntimeCredential, "from_browser_payload",
        classmethod(lambda cls, payload, **kw: SimpleNamespace(
            process_key=SimpleNamespace(private_hex="11" * 32),
            machine_pub="bb" * 32, delegation_cert=None, machine_id="m-1",
            machine_key=None, serving_machine_key=None,
            reachability_cert=None)))
    monkeypatch.setattr(
        frs, "FleetSyncRuntimeConfig",
        lambda **kw: captured.update(kw) or SimpleNamespace(**kw))
    class _Server:
        """Records whether the listener was actually started."""

        def __init__(self):
            self.running = False
            self.started = 0
            self.host = "0.0.0.0"
            self.port = 0

        async def start(self):
            self.started += 1
            self.running = True
            self.port = port
            return port

        async def stop(self):
            self.running = False

    class _Auth:
        """configure() authorizes this machine on the scheduler before it is
        installed; without it configure raises and the runtime is never armed
        — which is precisely what the removed suppress was hiding."""

        def __init__(self):
            self.authorized = []
            self.machine_pub = "bb" * 32

        def authorize(self, pub):
            self.authorized.append(pub)

    monkeypatch.setattr(
        frs, "FleetSyncScheduler",
        lambda config: SimpleNamespace(
            config=config, server=_Server(), authenticator=_Auth()))
    # NO SUPPRESS. configure() raising must FAIL this test: the config lambda
    # captures the port before the rest of configure() runs, so a swallowed
    # exception left the port assertions passing while the runtime was never
    # actually armed — the scheduler unset and nothing installed. That is the
    # test agreeing with broken code, which is the failure mode these tests
    # exist to catch.
    result = runtime.configure({"machine_id": "m-1"})
    assert result["ok"] is True, result
    assert runtime.scheduler is not None, (
        "configure() returned without installing the scheduler")
    return captured.get("listen_port"), captured.get("listen_host")


def test_an_org_connector_starting_first_does_not_take_the_port(monkeypatch):
    """THE ONE THAT MATTERS, in the order that actually broke: the ORG
    connector configures FIRST and must still resolve to an ephemeral
    loopback port, leaving the machine-wide port free for the personal
    connector that starts later."""
    port = 19410
    org_runtime = fleet_relay_sync.ConnectorFleetRuntime()
    org_runtime.set_owns_inbound_listener(False)

    org_port, org_host = _configure_and_capture(org_runtime, monkeypatch, port)

    # 0 means the bind is DISABLED for this process (ensure_direct_listener
    # stops/skips the server at <= 0), not "pick an ephemeral port".
    assert org_port == 0, "an org connector must not take the machine port"
    assert org_host != "0.0.0.0"

    # And it must not start a server at all.
    listener = asyncio.run(org_runtime.ensure_direct_listener())
    assert listener is None
    assert org_runtime.scheduler.server.started == 0


def test_the_personal_connector_still_gets_the_port(monkeypatch):
    """PRESERVATION, not recovery: direct sync currently works, so the owner
    must still bind exactly the configured address."""
    port = 19410
    personal = fleet_relay_sync.ConnectorFleetRuntime()
    personal.set_owns_inbound_listener(True)

    got_port, got_host = _configure_and_capture(personal, monkeypatch, port)

    assert (got_host, got_port) == ("0.0.0.0", port)

    # The owner genuinely starts it — preservation, not just non-contention.
    listener = asyncio.run(personal.ensure_direct_listener())
    assert listener == ("0.0.0.0", port)
    assert personal.scheduler.server.started == 1


def test_org_first_then_personal_do_not_contend(monkeypatch):
    """Both together, in the failing order. The org connector resolving to 0
    is what leaves the port available for the personal one."""
    port = 19411
    org = fleet_relay_sync.ConnectorFleetRuntime()
    org.set_owns_inbound_listener(False)
    personal = fleet_relay_sync.ConnectorFleetRuntime()
    personal.set_owns_inbound_listener(True)

    org_port, _ = _configure_and_capture(org, monkeypatch, port)
    personal_port, _ = _configure_and_capture(personal, monkeypatch, port)

    assert org_port == 0 and personal_port == port
    assert org_port != personal_port, "they must not want the same port"


def test_ownership_defaults_to_not_binding():
    """Fail closed: a connector that never declares its scope must not take
    the machine's port by accident."""
    assert (
        fleet_relay_sync.ConnectorFleetRuntime()._owns_inbound_listener is False
    )


@pytest.mark.asyncio
async def test_connector_stream_requires_the_fleet_machine_hello(
    monkeypatch, tmp_path
) -> None:
    """A connector stream must not serve anything before the fleet hello.

    This is the surviving half of
    test_connector_stream_requires_fleet_machine_hello_and_chunks_bootstrap.
    That test asserted TWO properties: the hello requirement, and that the
    relay chunked a bootstrap it built itself. The relay builds nothing now --
    bootstrap comes from the shared serve -- so the chunking half is gone with
    the mechanism. The hello half is authentication and is entirely unaffected
    by what is being served, so it is re-established here rather than deleted
    silently along with it.
    """
    fleet = _two_machine_fleet()
    personal = tmp_path / "personal.db"
    personal.touch()
    server = _configure_relay_server(fleet, personal, monkeypatch)
    token = "ab" * 16
    with pytest.raises(fleet_relay_sync.FleetRelaySyncError):
        await server.handle(token, {
            "v": fleet_relay_sync.PROTOCOL_VERSION,
            "op": "fleet.sync.pull",
            "roster_epoch": "ab" * 32,
            # No hello: the server must refuse before serving a single frame.
        })
