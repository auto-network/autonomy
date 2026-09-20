"""R for a note link (auto-xs9hz): the author personas of its rows and its
grant row, with the newest timestamp per persona; an unattributed author
machine refuses the publish by name."""
from __future__ import annotations

from pathlib import Path

import pytest

from tools.dashboard.link_requirements import (
    LinkRequirementError, link_requirements, note_addresses, row_origins,
)
from tools.graph.db import GraphDB
from tools.network.fleet_sync import write_floors
from tools.network.fleet_sync.catalog import MutationCatalog
from tools.network.fleet_sync.codec import encode_value
from tools.network.idkit import KeyPair, Subject, issue_cert

M1, M2, M3 = "a1" * 32, "b2" * 32, "c3" * 32     # machines: own, a co-member's, a stranger's
P1, P2 = "11" * 32, "22" * 32                     # personas


def _insert_source(conn, identity: str) -> None:
    conn.execute(
        "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) VALUES(?,?,?,?,?,?)",
        (identity, "note", identity, "{}", "2026-09-19T00:00:00Z", "2026-09-19T00:00:00Z"),
    )


def _insert_thought(conn, identity: str, source_id: str) -> None:
    conn.execute(
        "INSERT INTO thoughts(id,source_id,content,role,created_at) VALUES(?,?,?,?,?)",
        (identity, source_id, "body", "user", "2026-09-19T00:00:00Z"),
    )


def _served(server, origin):
    out, position = [], (0, None)
    while True:
        page = server.next_transactions_for_origin(origin, position[0], position[1], limit=50)
        if not page:
            return out
        for _ref, ts, txid, items in page:
            out.append(items); position = (ts, txid)


def _persona_record(persona_pub: str, machines: dict) -> dict:
    """A persona write floor record listing *machines*, sealed by a delegate."""
    import time
    persona, signer = KeyPair.generate(), KeyPair.generate()
    now = int(time.time())
    cert = issue_cert(persona, signer.public_hex, scope=["fleet:sync"], org="g",
                      subject=Subject("persona", persona.public_hex),
                      not_before=now - 60, not_after=now + 3600)
    body = write_floors.persona_write_floor_body(persona.public_hex, "g", min(machines.values()), machines, signer.public_hex)
    return {"persona": persona.public_hex, "org": "g", "write_floor_ns": min(machines.values()),
            "machines": machines, "signer": signer.public_hex, "cert": cert.to_dict(),
            "sig": signer.sign_hex(body)}


def test_requirements_fold_row_origins_to_personas_with_the_newest_stamp(tmp_path: Path) -> None:
    # The publisher's own org store (machine M1) with a note it wrote...
    own = GraphDB(tmp_path / "own.db")
    mine = MutationCatalog(own.conn, M1); mine.install()
    with mine.transaction(1_000, "t-note"):
        _insert_source(own.conn, "note-1")
        _insert_thought(own.conn, "th-1", "note-1")
    # ...and a thought on that note authored on a co-member's machine M2.
    remote = GraphDB(tmp_path / "remote.db")
    theirs = MutationCatalog(remote.conn, M2); theirs.install()
    with theirs.transaction(1_000, "seed"):
        _insert_source(remote.conn, "note-1")
    with theirs.transaction(1_500, "t-reply"):
        _insert_thought(remote.conn, "th-2", "note-1")
    for items in _served(theirs, M2):
        mine.apply_remote_batch(items)
    # M2 is attributed to persona P2 by a persona write floor record the store holds.
    record = _persona_record(P2, {M2: 1_400})
    write_floors.store_persona_write_floor(own.conn, record)
    p2 = record["persona"]

    origins = row_origins(own.conn, note_addresses(own.conn, "note-1"))
    assert origins == {M1: 1_000, M2: 1_500}
    own_record = _persona_record(P1, {M1: 900})
    write_floors.store_persona_write_floor(own.conn, own_record)
    p1 = own_record["persona"]
    requires = link_requirements(
        own.conn, source_id="note-1", grant_set_id="autonomy.network.link-grant",
        grant_key="tok", own_machines={M1}, own_persona=p1,
    )
    assert requires == {p1: 1_000, p2: 1_500}
    own.close(); remote.close()


def test_an_unattributed_author_machine_refuses_by_name(tmp_path: Path) -> None:
    own = GraphDB(tmp_path / "own.db")
    mine = MutationCatalog(own.conn, M1); mine.install()
    with mine.transaction(1_000, "t-note"):
        _insert_source(own.conn, "note-1")
    stranger = GraphDB(tmp_path / "stranger.db")
    theirs = MutationCatalog(stranger.conn, M3); theirs.install()
    with theirs.transaction(1_000, "seed"):
        _insert_source(stranger.conn, "note-1")
    with theirs.transaction(2_000, "t-reply"):
        _insert_thought(stranger.conn, "th-9", "note-1")
    for items in _served(theirs, M3):
        mine.apply_remote_batch(items)
    own_record = _persona_record(P1, {M1: 900})
    write_floors.store_persona_write_floor(own.conn, own_record)
    with pytest.raises(LinkRequirementError, match=M3[:12]):
        link_requirements(
            own.conn, source_id="note-1", grant_set_id="s", grant_key="k",
            own_machines={M1}, own_persona=own_record["persona"],
        )
    own.close(); stranger.close()


def test_a_row_never_replicated_is_skipped_not_attributed(tmp_path: Path) -> None:
    own = GraphDB(tmp_path / "own.db")
    mine = MutationCatalog(own.conn, M1); mine.install()
    assert row_origins(own.conn, [encode_value(["sources", ["missing"]])]) == {}
    own.close()
