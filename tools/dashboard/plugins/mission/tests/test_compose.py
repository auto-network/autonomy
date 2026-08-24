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
            Member(f"{MID}:relay",
                   {"entries": [{"by": "Jeremy",
                                 "at": "2026-08-23T16:10:00Z",
                                 "text": "What's going on? </script>"}]},
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


def test_focus_pillar_carried(monkeypatch, rows):
    _members(monkeypatch, rows)
    doc = compose.render_screen("autonomy", MID, "relay")
    assert _doc_data(doc)["focus"] == "relay"
