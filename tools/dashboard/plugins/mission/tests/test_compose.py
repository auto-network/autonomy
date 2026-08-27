"""The settings->document render path (compose.py).

Drives ``render_screen`` through stubbed set members shaped exactly like
``graph ops.read_set`` rows, and asserts on the produced document: key
recovery (mission:pillar:item -> surface/item ids), pillar ordering,
blank-field pruning, chat/bead injection, escaping, and the unknown-
mission refusal. The full end-to-end variant through real settings
lands with the integration-suite bead; this pins the pure function.
"""
from __future__ import annotations

import json
import re
from collections import namedtuple

import pytest

from tools.dashboard.plugins.mission import compose

Member = namedtuple("Member", "key payload created_at updated_at")
MID = "eec0efa1-fba7-42bf-8fd9-4790166b8f59"


def _members(monkeypatch, rows: dict[str, list[Member]]):
    def fake_read(set_id, org):
        return rows.get(set_id, [])
    monkeypatch.setattr(compose, "_read", fake_read)


def _doc_data(doc: str) -> dict:
    m = re.search(
        r'<script id="mc-data" type="application/json">(.*?)</script>',
        doc, re.S)
    assert m, "data block missing"
    return json.loads(m.group(1).replace("<\\/", "</"))


@pytest.fixture()
def rows():
    return {
        compose.MISSION_SET_ID: [
            Member(MID, {"name": "Multi-User Autonomy", "status": "active"},
                   "c", "u")],
        compose.PILLAR_SET_ID: [
            Member(f"{MID}:relay",
                   {"name": "Relay", "color": "#3987e5", "order": 2.0},
                   "c", "u"),
            Member(f"{MID}:crypto",
                   {"name": "Crypto", "color": "#8b6fd0", "order": 1.0},
                   "c", "u"),
            Member("other-mission:stray", {"name": "Stray"}, "c", "u")],
        compose.ITEM_SET_ID: [
            Member(f"{MID}:relay:crit-join",
                   {"kind": "checkpoint", "state": "confirmed",
                    "title": "A second machine joins",
                    "body": "", "order": 0.0, "faq": False,
                    "evidence": [], "work": [],
                    "history": [{"from": "in_progress", "to": "confirmed",
                                 "at": "2026-08-19T12:00:00Z", "by": "s"}]},
                   "2026-08-14T09:00:00Z", "2026-08-19T12:00:00Z"),
            Member("other-mission:x:y",
                   {"kind": "scope", "title": "not ours"}, "c", "u")],
        compose.CHAT_SET_ID: [
            Member(f"{MID}:relay:msg-a",
                   {"by": "Jeremy", "at": "2026-08-23T16:10:00Z",
                    "text": "What's going on? </script>"},
                   "c", "u")],
    }


def test_render_recovers_keys_and_prunes_blanks(monkeypatch, rows):
    _members(monkeypatch, rows)
    doc = compose.render_screen("autonomy", MID)
    data = _doc_data(doc)
    assert data["mission"]["name"] == "Multi-User Autonomy"
    # declared order wins over name order; foreign pillar excluded
    assert [p["pillar_id"] for p in data["pillars"]] == ["crypto", "relay"]
    assert len(data["items"]) == 1
    it = data["items"][0]
    assert (it["surface_id"], it["item_id"]) == ("relay", "crit-join")
    assert it["key"] == "relay:crit-join"
    # blanks pruned, real values kept
    assert "body" not in it and "faq" not in it and "evidence" not in it
    assert it["history"][0]["to"] == "confirmed"


def test_chat_injection_is_escaped_and_scoped(monkeypatch, rows):
    _members(monkeypatch, rows)
    doc = compose.render_screen("autonomy", MID)
    m = re.search(
        r'<script type="application/json" id="mc-chat">(.*?)</script>',
        doc, re.S)
    assert m, "chat block missing"
    chat = json.loads(m.group(1).replace("<\\/", "</"))
    assert list(chat) == ["relay"]
    assert chat["relay"][0]["text"].endswith("</script>")
    # the raw close tag never appears unescaped inside the block
    assert "</script>" not in m.group(1)


def test_no_beads_block_while_bridge_is_stubbed(monkeypatch, rows):
    _members(monkeypatch, rows)
    doc = compose.render_screen("autonomy", MID)
    assert 'id="mc-beads"' not in doc


def test_unknown_mission_returns_none(monkeypatch, rows):
    _members(monkeypatch, rows)
    assert compose.render_screen("autonomy", "nope") is None


def test_document_shell_is_phone_correct(monkeypatch, rows):
    """The composed document must be a complete page with the viewport
    meta — without it, phones lay out at ~980px and shrink everything."""
    _members(monkeypatch, rows)
    doc = compose.render_screen("autonomy", MID)
    assert doc.startswith("<!doctype html>")
    assert 'name="viewport"' in doc and "width=device-width" in doc
    assert doc.rstrip().endswith("</html>")
    # injected data blocks still precede the viewer's script execution
    assert doc.index('id="mc-chat"') < doc.index("<script>")


def test_activity_summary_derives_from_streams(monkeypatch, rows):
    """The homepage read: stamps from items + streams, counts from
    states, 14 daily buckets with today last."""
    import time as _time
    _members(monkeypatch, rows)
    now = _time.mktime((2026, 8, 24, 12, 0, 0, 0, 0, 0))
    a = compose.activity_summary("autonomy", MID, now=now)
    assert a["blockers"] == 0 and a["open_questions"] == 0
    assert a["in_progress"] == 0
    assert a["last_at"] is not None
    assert len(a["days"]) == 28
    assert sum(a["days"]) >= 1          # the history entry lands in-window


def test_focus_pillar_carried(monkeypatch, rows):
    _members(monkeypatch, rows)
    doc = compose.render_screen("autonomy", MID, "relay")
    assert _doc_data(doc)["focus"] == "relay"


def test_render_stages_streams_markers_then_doc(monkeypatch, rows):
    """The ?progress=1 contract: ascending stage markers narrating real
    counts, a doc marker carrying the exact byte length, then the
    document itself — and nothing about the document changes."""
    _members(monkeypatch, rows)
    from tools.graph import ops as graph_ops
    counts = {compose.PILLAR_SET_ID: 3, compose.ITEM_SET_ID: 4,
              compose.CHAT_SET_ID: 2}
    monkeypatch.setattr(
        graph_ops, "count_set_rows",
        lambda set_id, org=None, prefix=None: counts[set_id])
    chunks = list(compose.render_stages("autonomy", MID))
    doc = chunks[-1]
    assert doc.startswith("<!doctype html>")
    stages = [re.match(r"<!--msn:(\d+)\|(.*?)-->", c)
              for c in chunks[:-2]]
    assert all(stages), "every pre-doc chunk is a stage marker"
    pcts = [int(m.group(1)) for m in stages]
    assert pcts == sorted(pcts) and pcts[-1] <= 88
    # count = 3 pillars + 4 items + 2 chat rows + the registry row
    assert any("of 10 settings" in m.group(2) for m in stages)
    dm = re.fullmatch(r"<!--msn:doc:(\d+)-->", chunks[-2])
    assert dm and int(dm.group(1)) == len(doc.encode("utf-8"))
    assert _doc_data(doc)["mission"]["name"] == "Multi-User Autonomy"


def test_render_stages_unknown_mission_raises(monkeypatch, rows):
    _members(monkeypatch, rows)
    from tools.graph import ops as graph_ops
    monkeypatch.setattr(graph_ops, "count_set_rows",
                        lambda *a, **k: 0)
    with pytest.raises(KeyError):
        list(compose.render_stages("autonomy", "nope"))


def test_chat_bake_is_bounded_per_pillar(monkeypatch, rows):
    """A chatty pillar bakes only the newest CHAT_BAKE_LIMIT messages;
    the settings rows are untouched and other pillars unaffected."""
    n = compose.CHAT_BAKE_LIMIT + 20
    rows[compose.CHAT_SET_ID] = [
        Member(f"{MID}:relay:m{i:04d}",
               {"by": "auto-relay", "at": f"2026-08-01T00:{i//60:02d}:"
                f"{i%60:02d}Z", "text": f"msg {i}"}, "c", "u")
        for i in range(n)
    ] + [Member(f"{MID}:crypto:solo",
                {"by": "auto-c", "at": "2026-08-02T00:00:00Z",
                 "text": "only one"}, "c", "u")]
    _members(monkeypatch, rows)
    chat = compose.load_chat("autonomy", MID)
    assert len(chat["relay"]) == compose.CHAT_BAKE_LIMIT
    assert chat["relay"][0]["text"] == "msg 20"      # oldest 20 dropped
    assert chat["relay"][-1]["text"] == f"msg {n - 1}"
    assert len(chat["crypto"]) == 1


def test_payload_identity_copies_never_stomp_the_key(monkeypatch, rows):
    """Migrated rows carried item_id/surface_id/key payload fields —
    sometimes null — and the payload-last spread rendered an all-None
    item_id column. The key-derived identity and store timestamps win."""
    rows[compose.ITEM_SET_ID].append(
        Member(f"{MID}:relay:cp-cas-single",
               {"kind": "checkpoint", "state": "pending",
                "title": "CAS single",
                "item_id": None, "surface_id": "somewhere-else",
                "key": "cp-cas-single",
                "created_at": "1999-01-01T00:00:00Z"},
               "2026-08-20T00:00:00Z", "2026-08-21T00:00:00Z"))
    _members(monkeypatch, rows)
    items = compose.load_items("autonomy", MID)
    it = next(i for i in items if i["title"] == "CAS single")
    assert it["item_id"] == "cp-cas-single"
    assert it["surface_id"] == "relay"
    assert it["key"] == "relay:cp-cas-single"
    assert it["created_at"] == "2026-08-20T00:00:00Z"
