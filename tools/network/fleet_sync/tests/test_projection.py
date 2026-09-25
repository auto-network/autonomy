"""Public projection over the fleet-sync engine (bead auto-cshno).

Covers the projection predicates, the projected base walk, the projected
sweep, the projected delta with demotion tombstones and satellite cascade, the
demotion round-trip through ``materialize``, the too-old refusal, and the
byte-identity of ``Projection.FULL`` against the unprojected iterators.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3

from tools.graph.cross_org import PEER_VISIBLE_STATES
from tools.graph.db import GraphDB
from tools.network.fleet_sync.authored_sweep import read_live_authored_page
from tools.network.fleet_sync.catalog import MutationCatalog
from tools.network.fleet_sync.materialize import materialize
from tools.network.fleet_sync.projection import (
    Projection,
    SATELLITE_TABLES,
    public_predicate_sql,
    settings_row_is_public,
    source_row_is_public,
)
from tools.network.fleet_sync.streaming import iter_indexed_snapshot_mutations


ORIGIN = "machine-a"


def _insert_source(
    conn: sqlite3.Connection, identity: str, state: str, *, title: str = "t"
) -> None:
    conn.execute(
        "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at,"
        "publication_state) VALUES(?,?,?,?,?,?,?)",
        (identity, "note", title, "{}", "2026-08-19T00:00:00Z",
         "2026-08-19T00:00:00Z", state),
    )


def _insert_thought(conn: sqlite3.Connection, identity: str, source_id: str) -> None:
    conn.execute(
        "INSERT INTO thoughts(id,source_id,content,role,created_at) "
        "VALUES(?,?,?,?,?)",
        (identity, source_id, "c", "user", "2026-08-19T00:00:00Z"),
    )


def _insert_derivation(conn: sqlite3.Connection, identity: str, source_id: str) -> None:
    conn.execute(
        "INSERT INTO derivations(id,source_id,content,created_at) VALUES(?,?,?,?)",
        (identity, source_id, "c", "2026-08-19T00:00:00Z"),
    )


def _insert_attachment(conn: sqlite3.Connection, identity: str, source_id: str) -> None:
    conn.execute(
        "INSERT INTO attachments(id,hash,filename,size_bytes,file_path,source_id,"
        "created_at) VALUES(?,?,?,?,?,?,?)",
        (identity, "h" + identity, "f.png", 1, "/a/" + identity, source_id,
         "2026-08-19T00:00:00Z"),
    )


def _insert_tag(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        "INSERT INTO tags(name,description,created_at,updated_at) VALUES(?,?,?,?)",
        (name, "d", "2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z"),
    )


def _insert_note_comment(conn: sqlite3.Connection, identity: str, source_id: str) -> None:
    conn.execute(
        "INSERT INTO note_comments(id,source_id,content,created_at) VALUES(?,?,?,?)",
        (identity, source_id, "c", "2026-08-19T00:00:00Z"),
    )


def _insert_capture(conn: sqlite3.Connection, identity: str) -> None:
    conn.execute(
        "INSERT INTO captures(id,content,status,created_at) VALUES(?,?,?,?)",
        (identity, "c", "captured", "2026-08-19T00:00:00Z"),
    )


def _insert_setting(
    conn: sqlite3.Connection, *, identity: str, set_id: str, key: str,
    state: str, deprecated: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO settings(id,set_id,schema_revision,key,payload,"
        "publication_state,deprecated,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (identity, set_id, 1, key, "{}", state, deprecated,
         "2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z"),
    )


def _catalog(db: GraphDB) -> MutationCatalog:
    catalog = MutationCatalog(db.conn, ORIGIN)
    catalog.install()
    return catalog


def _seed_mixed(catalog: MutationCatalog) -> None:
    """A store holding raw, curated, published and canonical sources plus
    their satellites, comments, captures, a tag registry row and settings."""
    conn = catalog.conn
    with catalog.transaction(1000, "seed"):
        _insert_source(conn, "s_pub", "published")
        _insert_source(conn, "s_canon", "canonical")
        _insert_source(conn, "s_cur", "curated")
        _insert_source(conn, "s_raw", "raw")
        _insert_thought(conn, "t_pub", "s_pub")
        _insert_derivation(conn, "d_pub", "s_pub")
        _insert_attachment(conn, "a_pub", "s_pub")
        _insert_thought(conn, "t_raw", "s_raw")
        _insert_tag(conn, "topic")
        _insert_note_comment(conn, "nc1", "s_pub")
        _insert_capture(conn, "cap1")
        _insert_setting(conn, identity="set_pub", set_id="autonomy.org",
                        key="k", state="published")
        _insert_setting(conn, identity="set_dep", set_id="autonomy.org.primer",
                        key="k", state="published", deprecated=1)
        _insert_setting(conn, identity="set_id_raw",
                        set_id="autonomy.identity.machine", key="k", state="raw")
        # Published for MEMBERS, never for followers: what the first real
        # follow leaked (2026-09-25) until the set allowlist.
        _insert_setting(conn, identity="set_ledger",
                        set_id="autonomy.org.ledger-event", key="e1",
                        state="published")
        _insert_setting(conn, identity="set_reach",
                        set_id="autonomy.org.fleet-reachability", key="m1",
                        state="published")
        _insert_setting(conn, identity="set_deck",
                        set_id="dashboard.presentation.deck", key="d1",
                        state="canonical")


def _public_tables(items) -> set[str]:
    return {m.table for m in items}


# ── Predicates ────────────────────────────────────────────────


def test_predicates_reuse_peer_visible_states() -> None:
    assert PEER_VISIBLE_STATES == ("published", "canonical")
    for state in ("published", "canonical"):
        assert source_row_is_public({"publication_state": state})
        assert settings_row_is_public(
            {"set_id": "autonomy.org", "publication_state": state, "deprecated": 0}
        )
    for state in ("raw", "curated"):
        assert not source_row_is_public({"publication_state": state})
    # A deprecated but peer-visible setting is not public.
    assert not settings_row_is_public(
        {"set_id": "autonomy.org", "publication_state": "published", "deprecated": 1}
    )


def test_settings_cross_a_follow_only_from_follower_visible_sets() -> None:
    """Members replicate the ledger, member profiles and fleet reachability at
    'published'; that state means members, not the public. Only a set named
    in FOLLOW_VISIBLE_SET_IDS crosses, and every leaked set is outside it."""
    from tools.network.fleet_sync.projection import FOLLOW_VISIBLE_SET_IDS

    for set_id in (
        "autonomy.org.ledger-event", "autonomy.org.fleet-reachability",
        "autonomy.org.member-profile", "dashboard.action-registry-state",
        "dashboard.action-registry-cursor", "dashboard.presentation.deck",
        "autonomy.workspace", "dashboard.feature_flags",
    ):
        assert set_id not in FOLLOW_VISIBLE_SET_IDS
        assert not settings_row_is_public(
            {"set_id": set_id, "publication_state": "published", "deprecated": 0}
        )
    assert {"autonomy.org", "autonomy.org.primer",
            "autonomy.org.capability.primer",
            "autonomy.capability.contract"} <= FOLLOW_VISIBLE_SET_IDS
    # A row with no set_id at all is never public.
    assert not settings_row_is_public(
        {"publication_state": "published", "deprecated": 0}
    )


def test_public_predicate_sql_excludes_and_shapes() -> None:
    assert public_predicate_sql("note_comments") is None
    assert public_predicate_sql("captures") is None
    assert public_predicate_sql("tags") == "0"  # no source_id column
    assert "publication_state IN ('published', 'canonical')" in \
        public_predicate_sql("sources")
    assert "deprecated = 0" in public_predicate_sql("settings")
    assert "set_id IN ('autonomy.capability.contract', 'autonomy.org'" in \
        public_predicate_sql("settings")
    assert public_predicate_sql("thoughts").startswith("source_id IN")


# ── Base walk ─────────────────────────────────────────────────


def test_public_base_walk_filters(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = _catalog(db)
        _seed_mixed(catalog)
        public = list(iter_indexed_snapshot_mutations(
            db.conn, projection=Projection.PUBLIC
        ))
        source_ids = {m.address[0] for m in public if m.table == "sources"}
        assert source_ids == {"s_pub", "s_canon"}
        tables = _public_tables(public)
        # Excluded tables never appear.
        assert "note_comments" not in tables
        assert "captures" not in tables
        assert "tags" not in tables  # no source_id, never admitted
        # Satellites only for the public source.
        assert {m.address[0] for m in public if m.table == "thoughts"} == {"t_pub"}
        assert {m.address[0] for m in public if m.table == "derivations"} == {"d_pub"}
        assert {m.address[0] for m in public if m.table == "attachments"} == {"a_pub"}
        # Settings: follower-visible sets, published, non-deprecated only;
        # identity, deprecated, ledger, reachability and decks gone.
        setting_keys = {m.address[0] for m in public if m.table == "settings"}
        assert setting_keys == {"autonomy.org"}
    finally:
        db.close()


# ── Sweep ─────────────────────────────────────────────────────


def test_public_sweep_filters(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = _catalog(db)
        _seed_mixed(catalog)
        page = read_live_authored_page(
            db.conn, frontier={ORIGIN: 1 << 62},
            max_records=1000, max_bytes=1 << 20,
            projection=Projection.PUBLIC,
        )
        assert page.exhausted
        muts = [item.mutation for item in page.records]
        source_ids = {m.address[0] for m in muts if m.table == "sources"}
        assert source_ids == {"s_pub", "s_canon"}
        tables = _public_tables(muts)
        assert "note_comments" not in tables
        assert "captures" not in tables
        assert "tags" not in tables
        # A completed public sweep has no tombstones -- a live walk has none.
        assert all(not m.tombstone for m in muts)
    finally:
        db.close()


# ── Delta path ────────────────────────────────────────────────


def test_iter_mutations_public_excludes_and_projects(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = _catalog(db)
        _seed_mixed(catalog)
        public = list(catalog.iter_mutations(projection=Projection.PUBLIC))
        # A live sources row is emitted only for a peer-visible source; every
        # other sources entry is a tombstone (the bead's rule: any sources
        # entry whose live row fails the predicate is tombstoned).
        live_sources = {m.mutation.address[0] for m in public
                        if m.mutation.table == "sources"
                        and not m.mutation.tombstone}
        assert live_sources == {"s_pub", "s_canon"}
        dead_sources = {m.mutation.address[0] for m in public
                        if m.mutation.table == "sources"
                        and m.mutation.tombstone}
        assert {"s_cur", "s_raw"} <= dead_sources
        # No live sources row ever carries a non-public value.
        assert not [m for m in public if m.mutation.table == "sources"
                    and not m.mutation.tombstone
                    and m.mutation.address[0] in {"s_cur", "s_raw"}]
        tables = {m.mutation.table for m in public}
        assert "note_comments" not in tables
        assert "captures" not in tables
        # t_raw belongs to a raw source: tombstoned, never admitted live.
        raw_thought = [m for m in public
                       if m.mutation.table == "thoughts"
                       and m.mutation.address[0] == "t_raw"]
        assert raw_thought and all(m.mutation.tombstone for m in raw_thought)
        # The public thought is live and appears once.
        pub_thought = [m for m in public
                       if m.mutation.table == "thoughts"
                       and m.mutation.address[0] == "t_pub"]
        assert len(pub_thought) == 1 and not pub_thought[0].mutation.tombstone
    finally:
        db.close()


def test_demotion_emits_source_and_satellite_tombstones(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = _catalog(db)
        _seed_mixed(catalog)
        # Demote the published source in place (id unchanged).
        with catalog.transaction(2000, "demote"):
            db.conn.execute(
                "UPDATE sources SET publication_state='raw' WHERE id='s_pub'"
            )
        public = list(catalog.iter_mutations(projection=Projection.PUBLIC))
        by_table = {}
        for m in public:
            by_table.setdefault(m.mutation.table, []).append(m.mutation)
        # The source is now a tombstone, not a live row.
        pub_sources = [m for m in by_table.get("sources", [])
                       if m.address[0] == "s_pub"]
        assert len(pub_sources) == 1 and pub_sources[0].tombstone
        # Its satellites are tombstoned via the eager cascade at the source's
        # demotion timestamp (a whole-catalog pass also tombstones each
        # satellite through its own entry, since its source is now private, so
        # a satellite may appear more than once -- all tombstones, harmless
        # idempotent deletes on the follower).
        for table, sid in (("thoughts", "t_pub"), ("derivations", "d_pub"),
                           ("attachments", "a_pub")):
            hits = [m for m in by_table.get(table, []) if m.address[0] == sid]
            assert hits and all(h.tombstone for h in hits)
            assert any(h.timestamp_ns == 2000 for h in hits)
        # s_canon stays live and public.
        canon = [m for m in by_table.get("sources", []) if m.address[0] == "s_canon"]
        assert len(canon) == 1 and not canon[0].tombstone
    finally:
        db.close()


def test_demotion_round_trip_receiver_loses_row_and_satellites(
    tmp_path: Path,
) -> None:
    origin_db = GraphDB(tmp_path / "origin.db")
    follower = GraphDB(tmp_path / "follower.db")
    try:
        catalog = _catalog(origin_db)
        with catalog.transaction(1000, "publish"):
            _insert_source(origin_db.conn, "s1", "published")
            _insert_thought(origin_db.conn, "th1", "s1")
            _insert_derivation(origin_db.conn, "de1", "s1")

        # Publish -> pull -> the follower has the row and its satellites.
        stream = [m.mutation for m in
                  catalog.iter_mutations(projection=Projection.PUBLIC)]
        materialize(follower.conn, stream)
        assert follower.conn.execute(
            "SELECT COUNT(*) FROM sources WHERE id='s1'").fetchone()[0] == 1
        assert follower.conn.execute(
            "SELECT COUNT(*) FROM thoughts WHERE id='th1'").fetchone()[0] == 1
        assert follower.conn.execute(
            "SELECT COUNT(*) FROM derivations WHERE id='de1'").fetchone()[0] == 1

        # Demote -> pull -> the follower loses the row and its satellites.
        with catalog.transaction(2000, "demote"):
            origin_db.conn.execute(
                "UPDATE sources SET publication_state='raw' WHERE id='s1'"
            )
        stream = [m.mutation for m in
                  catalog.iter_mutations(projection=Projection.PUBLIC)]
        materialize(follower.conn, stream)
        assert follower.conn.execute(
            "SELECT COUNT(*) FROM sources WHERE id='s1'").fetchone()[0] == 0
        assert follower.conn.execute(
            "SELECT COUNT(*) FROM thoughts WHERE id='th1'").fetchone()[0] == 0
        assert follower.conn.execute(
            "SELECT COUNT(*) FROM derivations WHERE id='de1'").fetchone()[0] == 0
    finally:
        origin_db.close()
        follower.close()


# ── Too-old refusal ───────────────────────────────────────────


def test_follow_delta_too_old(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = _catalog(db)
        for ts, name in ((1000, "a"), (2000, "b"), (3000, "c")):
            with catalog.transaction(ts, name):
                _insert_source(db.conn, f"s_{name}", "published")
        # The follower's cursor is ONE integer for the organization (bead
        # auto-8cpnm). The oldest retained transaction is at 1000.
        assert catalog.follow_delta_too_old(500) is True
        assert catalog.follow_delta_too_old(1500) is False
        assert catalog.follow_delta_too_old(0) is False  # fresh follower
        # Simulate a prune that retired the 1000 transaction: the floor rises.
        db.conn.execute("DELETE FROM fleet_sync_catalog WHERE timestamp_ns=1000")
        db.conn.execute("DELETE FROM fleet_sync_transactions WHERE timestamp_ns=1000")
        db.conn.commit()
        assert catalog.follow_delta_too_old(1500) is True
        assert catalog.follow_delta_too_old(2500) is False
    finally:
        db.close()


# ── FULL byte-identity ────────────────────────────────────────


def test_full_projection_is_byte_identical(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = _catalog(db)
        _seed_mixed(catalog)
        # The default and an explicit FULL are the same object stream.
        assert list(catalog.iter_mutations()) == \
            list(catalog.iter_mutations(projection=Projection.FULL))
        assert list(iter_indexed_snapshot_mutations(db.conn)) == \
            list(iter_indexed_snapshot_mutations(
                db.conn, projection=Projection.FULL))
        default_page = read_live_authored_page(
            db.conn, frontier={ORIGIN: 1 << 62},
            max_records=1000, max_bytes=1 << 20,
        )
        full_page = read_live_authored_page(
            db.conn, frontier={ORIGIN: 1 << 62},
            max_records=1000, max_bytes=1 << 20,
            projection=Projection.FULL,
        )
        assert default_page.records == full_page.records
        assert default_page.frames == full_page.frames
    finally:
        db.close()


def test_satellite_tuple_is_the_record_four() -> None:
    assert SATELLITE_TABLES == ("thoughts", "derivations", "tags", "attachments")


def test_too_old_refusal_wire_roundtrip() -> None:
    from tools.network.fleet_sync_scheduler import (
        PULL_TOO_OLD_KIND,
        encode_follow_too_old_refusal,
        encode_schema_refusal,
        is_follow_too_old_refusal,
    )
    import json

    frame = encode_follow_too_old_refusal(scope="alpha")
    assert is_follow_too_old_refusal(frame)
    body = json.loads(frame[len(b"FSR1"):])
    assert body["kind"] == PULL_TOO_OLD_KIND
    assert body["scope"] == "alpha"
    # A schema refusal (the other FSR1 frame) is not a too-old refusal.
    other = encode_schema_refusal(digest="ab" * 32)
    assert not is_follow_too_old_refusal(other)
    assert not is_follow_too_old_refusal(b"not a refusal")


# ── Follower prune: settings outside the allowlist leave the mirror ───────
def test_prune_to_generation_drops_settings_outside_the_allowlist(
    tmp_path: Path,
) -> None:
    """A mirror filled before the follower-visible set allowlist holds the
    org's ledger events and fleet reachability. The completed-sweep prune
    brings the mirror back to the public surface: those settings rows leave,
    the org identity row stays, and the source prune is unchanged."""
    from tools.network.fleet_sync import follow_mirror

    db = GraphDB(tmp_path / "mirror.db")
    try:
        catalog = _catalog(db)
        _seed_mixed(catalog)
        conn = db.conn
        # The follower prunes on its applying connection, where capture is
        # off: mirror rows are never journaled as this node's own writes.
        conn.create_function("fleet_sync_capture_enabled", 0, lambda: 0)
        before = {r[0] for r in conn.execute("SELECT set_id FROM settings")}
        assert {"autonomy.org.ledger-event", "autonomy.org.fleet-reachability",
                "dashboard.presentation.deck", "autonomy.org"} <= before
        pruned = follow_mirror.prune_to_generation(conn, {"s_pub", "s_canon"})
        assert pruned == 0  # both public sources were carried
        after = {r[0] for r in conn.execute("SELECT set_id FROM settings")}
        assert after == {"autonomy.org", "autonomy.org.primer"}
        assert {r[0] for r in conn.execute("SELECT id FROM sources")} >= {"s_pub", "s_canon"}
    finally:
        db.close()
