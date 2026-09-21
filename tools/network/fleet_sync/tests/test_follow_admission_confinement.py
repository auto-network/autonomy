"""Follow-admission confinement, the absent write path, and the empty blob
response (bead auto-zltlg; design of record graph://5f2f5a49-00d §10.3).

A follow admission is confined to exactly the organization its genesis id
names — never the personal scope, never another organization's — and the
refusal names the check. A follow serves reads only: a blob request returns an
empty response (bodies are deferred to a later version), no dispatch op on a
follow reaches a write, and 100 follow pulls leave the per-follower state
tables untouched (the follower is stateless to the server).
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from tools.graph.db import GraphDB
from tools.graph.schemas.fleet_sync_peer_scope import (
    FLEET_SYNC_PEER_SCOPE_SET_ID,
)
from tools.network.fleet_roster import enroll
from tools.network.fleet_sync import write_floors
from tools.network.fleet_sync.blob_transport import done_frame, encode_blob_request
from tools.network.fleet_sync.catalog import MutationCatalog
from tools.network.fleet_sync_channel import Admission
from tools.network import fleet_sync_scheduler as fss
from tools.network.fleet_sync_scheduler import (
    FleetSyncProtocolError,
    SQLiteFleetSyncStore,
    encode_pull_request,
    roster_epoch,
)
from tools.network.idkit import KeyPair, Subject, issue_cert

ORG = "cd" * 32          # the organization's ledger genesis id (64 hex)
OTHER_ORG = "ef" * 32    # a different organization's genesis id
ORG_SCOPE = "myorg"      # the scope _org_scopes_for maps ORG to


def _insert_source(conn, identity: str, state: str = "published") -> None:
    conn.execute(
        "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at,"
        "publication_state) VALUES(?,?,?,?,?,?,?)",
        (identity, "note", identity, "{}", "2026-09-20T00:00:00Z",
         "2026-09-20T00:00:00Z", state),
    )


def _persona_cert(persona: KeyPair, machine: KeyPair):
    now = int(time.time())
    return issue_cert(
        persona, machine.public_hex, scope=["fleet:sync"], org=ORG,
        subject=Subject("persona", persona.public_hex),
        not_before=now - 60, not_after=now + 3600,
    )


def _build(tmp_path: Path, *, seal_floor_at: int | None = 200):
    """A scheduler whose one org scope (ORG_SCOPE) is backed by ``personal.db``,
    with three published sources and (optionally) a covered persona write floor
    so the org frontier F equals ``seal_floor_at``."""
    root, machine, peer = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
    personal = tmp_path / "personal.db"
    db = GraphDB(personal)
    catalog = MutationCatalog(db.conn, machine.public_hex)
    catalog.install()
    for ts, name in ((100, "s1"), (200, "s2"), (300, "s3")):
        with catalog.transaction(ts, f"tx-{name}"):
            _insert_source(db.conn, name)
    if seal_floor_at is not None:
        persona = KeyPair.generate()
        record = write_floors.seal_persona_write_floor(
            db.conn, signer=machine, persona_cert=_persona_cert(persona, machine),
            org=ORG, roster_machines={machine.public_hex},
            positions={machine.public_hex: seal_floor_at},
        )
        assert record is not None
        db.conn.commit()
    db.close()
    entries = (
        enroll(root, machine_pub=machine.public_hex),
        enroll(root, machine_pub=peer.public_hex, seq=1),
    )
    scheduler = fss.FleetSyncScheduler(fss.FleetSyncRuntimeConfig(
        machine_key=machine, personal_root_pub=root.public_hex,
        roster_entries=lambda: entries, peer_addresses=lambda: {},
        personal_db_path=personal, poll_interval=60.0,
    ))
    scheduler._roster_snapshot = entries
    # ORG's one scope is ORG_SCOPE, backed here by the personal store; no other
    # org has a scope. This is the seam _confine_scope and follow_frontier use.
    scheduler._org_scopes_for = lambda org: [ORG_SCOPE] if org == ORG else []
    scheduler._scope_paths = lambda: {"personal": personal, ORG_SCOPE: personal}
    return scheduler, personal


def _run(scheduler, request, *, admission):
    async def go():
        out = []
        reply = await scheduler._handle("t" * 32, request, "", admission=admission)
        if reply is None or isinstance(reply, (bytes, bytearray)):
            return reply
        async for frame in reply:
            out.append(bytes(frame))
        return out

    return asyncio.run(go())


def _pull(scheduler, personal, *, cursor):
    store = SQLiteFleetSyncStore(personal)
    return encode_pull_request(
        roster_epoch(scheduler._roster_snapshot, scheduler.config.personal_root_pub),
        compat=store.compatibility_digest(), bootstrap=False,
        scope=ORG_SCOPE, watermarks={ORG: cursor},
    )


# ── confinement (design §10.3) ───────────────────────────────────


def test_follow_confined_to_the_named_org_scope(tmp_path):
    scheduler, _ = _build(tmp_path)
    # The organization's own scope is allowed.
    scheduler._confine_scope(ORG_SCOPE, ORG, follow=True)


def test_follow_refuses_the_personal_scope_naming_the_check(tmp_path):
    scheduler, _ = _build(tmp_path)
    try:
        scheduler._confine_scope("personal", ORG, follow=True)
    except FleetSyncProtocolError as exc:
        assert "follow scope confinement" in str(exc)
    else:
        raise AssertionError("a follow requesting personal must be refused")


def test_follow_refuses_another_orgs_scope_naming_the_check(tmp_path):
    scheduler, _ = _build(tmp_path)
    try:
        scheduler._confine_scope("someotherorg", ORG, follow=True)
    except FleetSyncProtocolError as exc:
        assert "follow scope confinement" in str(exc)
    else:
        raise AssertionError("a follow requesting another org must be refused")


def test_a_pull_naming_the_personal_scope_is_refused_end_to_end(tmp_path):
    scheduler, personal = _build(tmp_path)
    store = SQLiteFleetSyncStore(personal)
    request = encode_pull_request(
        roster_epoch(scheduler._roster_snapshot, scheduler.config.personal_root_pub),
        compat=store.compatibility_digest(), bootstrap=False,
        scope="personal", watermarks={ORG: 0},
    )
    try:
        _run(scheduler, request, admission=Admission(kind="follow", org=ORG))
    except FleetSyncProtocolError as exc:
        assert "follow scope confinement" in str(exc)
    else:
        raise AssertionError("a follow pull on the personal scope must be refused")


# ── follow_frontier: the value the link server refuses on ─────────


def test_follow_frontier_is_the_min_over_covered_persona_floors(tmp_path):
    scheduler, _ = _build(tmp_path, seal_floor_at=200)
    assert scheduler.follow_frontier(ORG) == 200


def test_follow_frontier_is_none_without_a_covered_floor(tmp_path):
    scheduler, _ = _build(tmp_path, seal_floor_at=None)
    assert scheduler.follow_frontier(ORG) is None


# ── the absent write path (design §10.3) ─────────────────────────


def test_a_blob_request_on_a_follow_returns_an_empty_response(tmp_path):
    scheduler, _ = _build(tmp_path)
    digests = ["ab" * 32, "cd" * 32]
    frames = _run(
        scheduler, encode_blob_request(digests),
        admission=Admission(kind="follow", org=ORG),
    )
    # One blob.done frame, every requested digest missing, no chunk served.
    assert frames == [done_frame([], digests)]


def test_no_follow_dispatch_op_reaches_a_write(tmp_path):
    """Enumerate the follow admission's dispatch table (blob → empty response,
    pull → read-only serve) and prove neither writes: after every op the
    per-follower state tables are still empty."""
    scheduler, personal = _build(tmp_path)
    admission = Admission(kind="follow", org=ORG)
    # blob op
    _run(scheduler, encode_blob_request(["ab" * 32]), admission=admission)
    # pull op (a delta and a bootstrap sweep)
    _run(scheduler, _pull(scheduler, personal, cursor=100), admission=admission)
    store = SQLiteFleetSyncStore(personal)
    boot = encode_pull_request(
        roster_epoch(scheduler._roster_snapshot, scheduler.config.personal_root_pub),
        compat=store.compatibility_digest(), bootstrap=True,
        scope=ORG_SCOPE, watermarks={ORG: 0},
    )
    _run(scheduler, boot, admission=admission)
    assert _served_acks(personal) == 0
    assert _peer_scope_rows(personal) == 0


def test_100_follow_pulls_leave_the_per_follower_tables_unchanged(tmp_path):
    scheduler, personal = _build(tmp_path)
    admission = Admission(kind="follow", org=ORG)
    for _ in range(100):
        frames = _run(scheduler, _pull(scheduler, personal, cursor=100),
                      admission=admission)
        assert frames  # a real reply each time
    assert _served_acks(personal) == 0
    assert _peer_scope_rows(personal) == 0


def _served_acks(personal: Path) -> int:
    db = GraphDB(personal)
    try:
        return db.conn.execute(
            "SELECT count(*) FROM fleet_sync_served_acks"
        ).fetchone()[0]
    except Exception:
        return 0
    finally:
        db.close()


def _peer_scope_rows(personal: Path) -> int:
    db = GraphDB(personal)
    try:
        return db.conn.execute(
            "SELECT count(*) FROM settings WHERE set_id=?",
            (FLEET_SYNC_PEER_SCOPE_SET_ID,),
        ).fetchone()[0]
    except Exception:
        return 0
    finally:
        db.close()
