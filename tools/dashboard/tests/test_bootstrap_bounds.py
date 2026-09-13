"""The org:join bootstrap reply is bounded (operator ruling 2026-09-13).

A joiner is seeded with SOME peers to dial, not the whole directory: the
sponsor's own rows first, then the machines this node most recently
synced with, capped at BOOTSTRAP_ROW_CAP. Photos never ride the bootstrap;
they replicate with the directory row on the joiner's first org pull.
"""
from __future__ import annotations

from tools.dashboard import claim_service, member_directory
from tools.network import fleet_org_reachability


def _row(i: int, persona: str, updated_at: int) -> tuple[str, dict]:
    key = f"{i:064x}"
    return key, {"machine_pub": key, "persona_pub": persona,
                 "addresses": [f"ws://m{i}:9410"], "updated_at": updated_at}


def test_reachability_rows_are_capped_own_first_then_most_recently_synced(monkeypatch):
    own = "a" * 64
    rows = dict(_row(i, f"{i:064x}", updated_at=1_000 + i) for i in range(1, 2001))
    rows.update([_row(0, own, updated_at=1)])  # own row: oldest, must lead
    recency = {f"{i:064x}": 5_000_000 - i for i in range(1, 1_500)}  # 1 synced most recently
    monkeypatch.setattr(fleet_org_reachability, "read_rows", lambda path: rows)
    monkeypatch.setattr(claim_service, "_peer_recency", lambda path: recency)
    monkeypatch.setattr("tools.graph.db._org_db_path", lambda org: "/nonexistent")

    out = claim_service._reachability_rows("acme", own)

    assert len(out) == claim_service.BOOTSTRAP_ROW_CAP == 1024
    assert out[0]["persona_pub"] == own
    synced = [r["key"] for r in out[1:]]
    assert synced == [f"{i:064x}" for i in range(1, 1024)]


def test_reachability_rows_under_the_cap_never_touch_peer_state(monkeypatch):
    rows = dict(_row(i, f"{i:064x}", updated_at=i) for i in range(1, 4))
    monkeypatch.setattr(fleet_org_reachability, "read_rows", lambda path: rows)
    monkeypatch.setattr("tools.graph.db._org_db_path", lambda org: "/nonexistent")
    monkeypatch.setattr(claim_service, "_peer_recency",
                        lambda path: (_ for _ in ()).throw(AssertionError("read")))
    assert len(claim_service._reachability_rows("acme", None)) == 3


def test_member_profiles_drop_photos_follow_reachability_order_and_cap(monkeypatch):
    own = "a" * 64
    directory = [{"persona_pub": f"{i:064x}", "display_name": f"m{i}",
                  "avatar": "data:image/png;base64,AAAA"} for i in range(1, 1_300)]
    directory.append({"persona_pub": own, "display_name": "Alice",
                      "avatar": "data:image/png;base64,BBBB"})
    monkeypatch.setattr(member_directory, "rows", lambda slug: directory)
    reachability = [{"persona_pub": f"{i:064x}"} for i in (7, 3, 9)]

    out = claim_service._member_profiles("acme", reachability, own)

    assert len(out) == claim_service.BOOTSTRAP_ROW_CAP
    assert [r["display_name"] for r in out[:4]] == ["Alice", "m7", "m3", "m9"]
    assert all("avatar" not in r for r in out)
    assert all(r["display_name"] for r in out)
