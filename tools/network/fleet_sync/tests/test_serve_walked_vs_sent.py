"""What a serve walks versus what it sends (auto-hb67a).

Home, 2026-09-29 21:39-21:55Z: every ~20 s the autonomy serve logged
"done after 13.7k transaction(s), 229k frame(s)" while the pulls it answered
delivered tens of frames, and the connector ran at 170% of a core. This
harness drives the serve's own pagers against a store with a known shape
and counts, per phase, the transaction heads walked and the operation
frames that would be sent, so the ratio names the source before anything
is changed. Three request shapes:

- a puller whose watermark map covers every origin the server holds:
  walked == the delta, sent == its surviving rows (the healthy case);
- a puller whose map lacks an origin the server holds and has pulled: the
  serve replays that origin from 0 ("last-writer-wins makes that inert"),
  walking and sending every retained transaction of it on EVERY pull;
- a follower whose cursor does not advance: the serve replays everything
  in (cursor, frontier] across all origins on every pull.

The counts are the acceptance for the fix: a pull whose delta is N
transactions must walk O(N) heads and send O(N) frames.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from tools.graph.db import GraphDB
from tools.network.fleet_sync.catalog import (
    MutationCatalog,
    ensure_origin_cursor_schema,
)
from tools.network.fleet_sync.projection import Projection
from tools.network.fleet_sync_scheduler import (
    SERVE_GROUP_OPERATIONS,
    SQLiteFleetSyncStore,
    _FollowPager,
    _OriginPager,
)
from tools.network.idkit import KeyPair

BASE = 1_790_000_000_000_000_000
SET_ID = "dashboard.example"


def _store(tmp_path: Path):
    """A fleet-activated store whose own origin is ``me``; returns
    (db, catalog, own_incarnation)."""
    key = KeyPair.generate()
    db = GraphDB(tmp_path / "serve.db")
    db.activate_fleet_sync_writers(key.public_hex)
    catalog = MutationCatalog(db.conn, key.public_hex)
    catalog.install()
    ensure_origin_cursor_schema(db.conn)
    return db, catalog, key.public_hex


def _write_local(db, catalog, ts: int, transaction_id: str, rows: int = 1) -> None:
    """One of this store's own transactions: *rows* settings rows."""
    with catalog.transaction(ts, transaction_id):
        for i in range(rows):
            db.conn.execute(
                "INSERT INTO settings (id,set_id,schema_revision,key,payload,publication_state)"
                " VALUES (?,?,1,?,?,'raw')",
                (str(uuid.uuid4()), SET_ID, f"{transaction_id}:{i}", json.dumps({"v": i})),
            )


def _remote_origin(db, tmp_path: Path, name: str, count: int, start_ts: int) -> str:
    """*count* retained transactions of another origin installed into *db*
    the way sync installs them (a second store writes them, the server
    store applies them), so each has a live catalog row for the group
    builder and the server's cursor for that origin sits at the newest.
    Returns the origin's incarnation."""
    key = KeyPair.generate()
    other = GraphDB(tmp_path / f"{name}.db")
    other.activate_fleet_sync_writers(key.public_hex)
    other_catalog = MutationCatalog(other.conn, key.public_hex)
    try:
        for i in range(count):
            with other_catalog.transaction(start_ts + i * 1_000, f"local:{i:032x}"):
                other.conn.execute(
                    "INSERT INTO settings (id,set_id,schema_revision,key,payload,publication_state)"
                    " VALUES (?,?,1,?,?,'raw')",
                    (str(uuid.uuid4()), SET_ID, f"{name}:{i}", json.dumps({"v": i})),
                )
        server_catalog = db.conn._fleet_sync_functions_owner
        position: tuple[int, str | None] = (0, None)
        while True:
            page = other_catalog.next_transactions_for_origin(key.public_hex, position[0], position[1], limit=200)
            if not page:
                break
            for _ref, timestamp, transaction_id, items in page:
                server_catalog.apply_remote_batch(items)
                position = (timestamp, transaction_id)
    finally:
        other.close()
    return key.public_hex


def serve_counts(store: SQLiteFleetSyncStore, pager) -> dict[str, int]:
    """Drive *pager* the way the serve loop does and count what it walks
    and what it would send."""
    walked = sent_frames = groups = 0
    while True:
        page = pager.next()
        if page is None:
            break
        ref, (origin, transaction_id, _ts) = page
        walked += 1
        offset, more = 0, True
        while more:
            items, more = store.transaction_group(
                ref, origin, transaction_id, offset=offset,
                limit=SERVE_GROUP_OPERATIONS, projection=Projection.FULL,
            )
            offset += SERVE_GROUP_OPERATIONS
            groups += 1
            sent_frames += len(items)
    return {"walked": walked, "sent_frames": sent_frames, "groups": groups}


def test_a_covering_watermark_map_walks_only_the_delta(tmp_path):
    db, catalog, me = _store(tmp_path)
    try:
        for i in range(50):
            _write_local(db, catalog, BASE + i * 1_000, f"local:{i:032x}")
        store = SQLiteFleetSyncStore(tmp_path / "serve.db")
        bounds, _floors = store.serve_snapshot()
        # The puller holds everything but the newest five of my transactions.
        watermarks = {me: BASE + 44 * 1_000}
        counts = serve_counts(store, _OriginPager(store, watermarks, None, bounds=bounds))
        assert counts == {"walked": 5, "sent_frames": 5, "groups": 5}
    finally:
        db.close()


def test_an_origin_missing_from_the_map_is_replayed_whole_on_every_pull(tmp_path):
    """The serve replays an origin the puller sends no watermark for from 0,
    bounded only by this store's own cursor for it. Every retained
    transaction of that origin is walked AND sent, every pull, however
    small the delta the puller actually needs."""
    db, catalog, me = _store(tmp_path)
    try:
        for i in range(5):
            _write_local(db, catalog, BASE + 10**9 + i * 1_000, f"local:{i:032x}")
        # 450 spans three SERVE_PAGE_TRANSACTIONS (200) pages; the count is
        # exact, so a larger origin proves nothing more and costs ~3 ms per
        # transaction group served.
        third = _remote_origin(db, tmp_path, "third", 450, BASE)
        store = SQLiteFleetSyncStore(tmp_path / "serve.db")
        bounds, _floors = store.serve_snapshot()
        watermarks = {me: BASE + 10**9 + 2 * 1_000}     # the puller needs 2 of mine; it never names `third`
        first = serve_counts(store, _OriginPager(store, watermarks, None, bounds=bounds))
        again = serve_counts(store, _OriginPager(store, watermarks, None, bounds=bounds))
        assert first["walked"] == 2 + 450 and first["sent_frames"] == 2 + 450
        assert again == first, "the same pull costs the same again: nothing the serve does advances it"
    finally:
        db.close()


def test_a_follower_whose_cursor_does_not_advance_is_re_served_everything(tmp_path):
    """A follow delta serves (cursor, frontier] across every origin. A
    follower presenting the same cursor each pull is re-served the whole
    span each time."""
    db, catalog, me = _store(tmp_path)
    try:
        _remote_origin(db, tmp_path, "fourth", 600, BASE)    # > 2 pages above the cursor
        for i in range(100):
            _write_local(db, catalog, BASE + 5 * 10**6 + i * 1_000, f"local:{i:032x}")
        store = SQLiteFleetSyncStore(tmp_path / "serve.db")
        bounds, _floors = store.serve_snapshot()
        frontier = BASE + 5 * 10**6 + 99 * 1_000
        stale_cursor = BASE + 300 * 1_000          # three hundred transactions in
        counts = serve_counts(store, _FollowPager(store, stale_cursor, frontier, bounds=bounds))
        assert counts["walked"] == 299 + 100 and counts["sent_frames"] == 299 + 100   # strictly above the cursor
        again = serve_counts(store, _FollowPager(store, stale_cursor, frontier, bounds=bounds))
        assert again == counts
    finally:
        db.close()
