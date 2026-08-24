"""Scenario documents for the integration suite.

Each scenario is a set of REAL settings payloads (validated by the
registered schemas on every write, via the same in-memory store the
write-path tests use) composed into a complete mission document by
``compose.render_screen``. The jsdom half loads the document and
asserts on the rendered DOM.
"""
from __future__ import annotations

from collections import namedtuple

from tools.dashboard.plugins.mission import compose
from tools.dashboard.plugins.mission.entrypoints import schemas as S
from tools.graph.schemas.registry import validate_payload

Member = namedtuple("Member", "key payload created_at updated_at")

MID = "11111111-2222-3333-4444-555555555555"
NOW = "2026-08-24T12:00:00Z"


class Store:
    """Schema-validating in-memory settings + bead payload."""

    def __init__(self):
        self.rows: dict[str, list[Member]] = {}
        self.beads: dict[str, list] = {}

    def put(self, set_id, key, payload, created=NOW, updated=NOW):
        validate_payload(set_id, S.SCHEMA_REVISION, payload)
        self.rows.setdefault(set_id, []).append(
            Member(key, payload, created, updated))

    def render(self, monkeypatch, focus=None) -> str:
        monkeypatch.setattr(
            compose, "_read",
            lambda set_id, org: self.rows.get(set_id, []))
        monkeypatch.setattr(
            compose, "load_beads",
            lambda org, mission_id, pillars: self.beads)
        doc = compose.render_screen("testorg", MID, focus)
        assert doc is not None
        return doc


def _t(hours_ago: float) -> str:
    """Fixed timestamps relative to NOW keep ordering deterministic."""
    from datetime import datetime, timedelta, timezone
    base = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
    return (base - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def full() -> Store:
    """The kitchen-sink mission: every kind, every state, epics,
    blockers, resolved arcs, comments, favorites."""
    st = Store()
    st.put(S.MISSION_SET_ID, MID,
           {"name": "Integration Mission", "status": "active",
            "coordinator_session": "auto-coord"})
    st.put(S.PILLAR_SET_ID, f"{MID}:relay",
           {"name": "Relay Network", "color": "#3987e5", "order": 1.0,
            "coordinator_session": "auto-relay",
            "bead_labels": ["pillar:relay-network"]})
    st.put(S.PILLAR_SET_ID, f"{MID}:crypto",
           {"name": "Crypto", "color": "#8b6fd0", "order": 2.0,
            "bead_labels": []})

    def item(pillar, iid, payload, **kw):
        st.put(S.ITEM_SET_ID, f"{MID}:{pillar}:{iid}", payload, **kw)

    # charter + two news posts (order proves newest-first)
    item("relay", "charter",
         {"kind": "scope", "title": "Charter",
          "body": "One personal fleet, two real machines."})
    item("relay", "news-old",
         {"kind": "status", "title": "Older update",
          "body": "Older update body.", "happened_at": _t(50)},
         updated=_t(50))
    item("relay", "news-new",
         {"kind": "status", "title": "Newest update",
          "body": "Newest update body.", "happened_at": _t(2)},
         updated=_t(2))

    # delivery ladder: confirmed (evidence + history), in_progress
    # (work + linked beads), pending (bead-complete -> ready to confirm)
    item("relay", "crit-join",
         {"kind": "checkpoint", "state": "confirmed",
          "title": "A second machine joins",
          "body": "Demonstrated end to end.",
          "confirmed_by": "auto-relay", "confirmed_turn": 143,
          "confirmed_at": _t(30),
          "evidence": [{"text": "Scale proof: **1,000,000 rows**.",
                        "at": _t(31), "by": "auto-relay", "turn": 98}],
          "history": [{"from": "in_progress", "to": "confirmed",
                       "at": _t(30), "by": "auto-relay"}]})
    item("relay", "crit-live",
         {"kind": "checkpoint", "state": "in_progress",
          "title": "The live exchange runs outside the LAN",
          "refs": ["bead:auto-run1"],
          "work": [{"by": "auto-relay", "at": _t(20),
                    "text": "provisioned the pair"},
                   {"by": "auto-relay", "at": _t(4),
                    "text": "reconnect fixed; rerunning"}],
          "history": [{"from": "pending", "to": "in_progress",
                       "at": _t(21), "by": "auto-relay"}]})
    item("relay", "crit-revoke",
         {"kind": "checkpoint", "state": "pending",
          "title": "A revoked machine loses access",
          "refs": ["bead:auto-done1"]})

    # questions: one open+blocking with a discussion, one answered
    item("relay", "q-provision",
         {"kind": "question", "state": "open", "blocking": True,
          "title": "Who provisions the runtime?",
          "asked_by": "auto-relay", "asked_at": _t(40),
          "discussion": [
              {"type": "progress", "by": "auto-relay", "at": _t(39),
               "text": "mapping provisioning today"},
              {"by": "Jeremy", "at": _t(38), "text": "Lean dashboard."}]},
         updated=_t(40))
    item("relay", "q-ttl",
         {"kind": "question", "state": "answered",
          "title": "Does the cache TTL bound exposure?",
          "asked_by": "Jeremy", "asked_at": _t(60),
          "answer": {"text": "Yes - one hour, read-only.",
                     "by": "auto-relay", "at": _t(55)}},
         updated=_t(60))

    # decisions: one favorite
    item("relay", "dec-roster",
         {"kind": "decision", "title": "The roster authorizes",
          "chosen": "The channel authenticates.", "faq": True,
          "happened_at": _t(80)}, updated=_t(80))
    item("relay", "dec-plugin",
         {"kind": "decision", "title": "Fleet is a plugin",
          "fork": "Engine vs plugin.", "chosen": "Plugin.",
          "if_wrong": "A second engine grows.",
          "happened_at": _t(70)}, updated=_t(70))

    # chat log
    st.put(S.CHAT_SET_ID, f"{MID}:relay",
           {"entries": [
               {"by": "Jeremy", "at": _t(10), "text": "What's going on?"},
               {"by": "auto-relay", "at": _t(9),
                "text": "Reconnect fixed; rerunning the flow."}]})

    # bead payload (bridge output shape)
    st.beads = {"relay": [
        {"id": "auto-run1", "title": "Distinct close codes",
         "state": "running", "desc": "## Spec\n- code table", "deps": [],
         "comments": [{"by": "terminal:auto-relay", "at": _t(15),
                       "text": "clarified in chat"}]},
        {"id": "auto-done1", "title": "Tunnel routing",
         "state": "complete", "desc": "", "evidence": "landed abc123",
         "deps": []},
        {"id": "auto-spec1", "title": "Vault the serving key",
         "state": "specified", "desc": "", "deps": ["auto-run1"]},
        {"id": "auto-epic1", "title": "Relay epic",
         "state": "defined", "epic": True, "desc": "",
         "deps": ["auto-run1", "auto-spec1"]},
    ]}
    return st


def empty() -> Store:
    """A mission with pillars but no content at all."""
    st = Store()
    st.put(S.MISSION_SET_ID, MID, {"name": "Empty Mission"})
    st.put(S.PILLAR_SET_ID, f"{MID}:solo", {"name": "Solo Pillar"})
    return st
