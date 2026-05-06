"""Behavioral tests for the Activity tab notifications substrate.

Bead: auto-5u8zb. The substrate ships four ``dashboard.activity.*``
SettingSchema classes that back the Notifications tab. These tests
pin the contract — schema registration, write semantics, length
caps, the refresh state machine, and operator-local dismissal.

The state machine for :class:`AskRefreshRequestV1` is the most
subtle invariant: the "requested" state must persist across operator
re-clicks, local reloads, and heartbeats, and clear ONLY when the
source session bumps ``revision_seq`` past ``target_revision`` (or
drops the ask). Re-clicks must not break the contract.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tools.dashboard import notifications_settings as ns
from tools.graph import settings_ops
from tools.graph.schemas.registry import (
    SchemaValidationError,
    get_schema,
)


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    """Pin ``GRAPH_DB`` to a fresh per-test SQLite file."""
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


def _base_rows(set_id: str, key: str | None = None) -> list[dict]:
    """Return base rows (``supersedes IS NULL AND excludes IS NULL``)."""
    db = settings_ops._open(None)
    try:
        if key is None:
            rows = db.conn.execute(
                "SELECT * FROM settings WHERE set_id = ? "
                "  AND supersedes IS NULL AND excludes IS NULL "
                "ORDER BY created_at, id",
                (set_id,),
            ).fetchall()
        else:
            rows = db.conn.execute(
                "SELECT * FROM settings WHERE set_id = ? AND key = ? "
                "  AND supersedes IS NULL AND excludes IS NULL "
                "ORDER BY created_at, id",
                (set_id, key),
            ).fetchall()
    finally:
        db.close()
    return [dict(r) for r in rows]


# ── Acceptance #1: schemas register and round-trip ───────────


def test_all_schemas_register():
    """Every schema is in the registry under its declared set_id and
    revision. Module-level ``register_schema`` calls run at import
    time. SessionAsk is registered at both v1 (legacy) and v2 (current).
    """
    assert get_schema(ns.SESSION_ASK_SET_ID, 1) is ns.SessionAskV1
    assert get_schema(ns.SESSION_ASK_SET_ID, 2) is ns.SessionAskV2
    assert get_schema(ns.ASK_VOTE_SET_ID, 1) is ns.AskVoteV1
    assert get_schema(ns.ASK_REFRESH_SET_ID, 1) is ns.AskRefreshRequestV1
    assert (
        get_schema(ns.OPERATOR_DISMISSED_SET_ID, 1)
        is ns.OperatorDismissedAsksV1
    )


def test_decorator_metadata_is_set():
    """Decorator-driven metadata: four keyed-per-entity, one singleton."""
    assert ns.SessionAskV1._access_pattern == "keyed_per_entity"
    assert ns.SessionAskV2._access_pattern == "keyed_per_entity"
    assert ns.AskVoteV1._access_pattern == "keyed_per_entity"
    assert ns.AskRefreshRequestV1._access_pattern == "keyed_per_entity"
    assert ns.OperatorDismissedAsksV1._access_pattern == "singleton"
    assert ns.OperatorDismissedAsksV1._key_strategy == "fixed:dismissed"


def test_session_ask_round_trips_through_upsert(graph_db_env):
    """The bead's contract: writes go via ``upsert_by_key``."""
    sid = settings_ops.upsert_by_key(
        ns.SESSION_ASK_SET_ID, 1, "session-1",
        {
            "session_id": "session-1",
            "to_participant_id": "operator-jeremy",
            "text": "Should I ship the migration?",
            "created_at": "2026-05-03T12:00:00Z",
            "revision_seq": 1,
        },
     org=settings_ops.CALLER_ORG)
    rows = _base_rows(ns.SESSION_ASK_SET_ID, "session-1")
    assert len(rows) == 1
    assert rows[0]["id"] == sid
    payload = json.loads(rows[0]["payload"])
    assert payload["text"] == "Should I ship the migration?"
    assert payload["revision_seq"] == 1


# ── Acceptance #2: per-session keying — heartbeats don't append ──


def test_session_ask_keyed_per_session(graph_db_env):
    """Heartbeat-style rewrites from the same session don't append.

    Five writes for ``session-1`` plus three for ``session-2`` produces
    exactly two base rows — one per session — with the latest payload
    visible on each.
    """
    for i in range(1, 6):
        settings_ops.upsert_by_key(
            ns.SESSION_ASK_SET_ID, 1, "session-1",
            {
                "session_id": "session-1",
                "text": f"draft {i}",
                "revision_seq": i,
                "created_at": "2026-05-03T12:00:00Z",
            },
         org=settings_ops.CALLER_ORG)
    for i in range(1, 4):
        settings_ops.upsert_by_key(
            ns.SESSION_ASK_SET_ID, 1, "session-2",
            {
                "session_id": "session-2",
                "text": f"other draft {i}",
                "revision_seq": i,
                "created_at": "2026-05-03T12:01:00Z",
            },
         org=settings_ops.CALLER_ORG)

    s1 = _base_rows(ns.SESSION_ASK_SET_ID, "session-1")
    s2 = _base_rows(ns.SESSION_ASK_SET_ID, "session-2")
    assert len(s1) == 1
    assert len(s2) == 1
    p1 = json.loads(s1[0]["payload"])
    p2 = json.loads(s2[0]["payload"])
    assert p1["text"] == "draft 5" and p1["revision_seq"] == 5
    assert p2["text"] == "other draft 3" and p2["revision_seq"] == 3


# ── Acceptance #3: ~2KB length cap on text ──────────────────


def test_ask_text_size_cap():
    """Boundary test: 2047 bytes accepted, 2049 rejected.

    The substrate's cap is 2048 bytes. ASCII text uses one byte per
    character so length-as-string and length-as-bytes coincide here.
    """
    base = {
        "session_id": "s",
        "to_participant_id": "",
        "created_at": "2026-05-03T12:00:00Z",
        "revision_seq": 0,
    }
    ns.SessionAskV1.validate({**base, "text": "x" * 2047})
    ns.SessionAskV1.validate({**base, "text": "x" * 2048})
    with pytest.raises(SchemaValidationError) as exc:
        ns.SessionAskV1.validate({**base, "text": "x" * 2049})
    assert "exceeds" in str(exc.value).lower()


def test_ask_text_size_cap_is_byte_aware():
    """Multi-byte UTF-8 characters count toward the byte cap."""
    base = {
        "session_id": "s",
        "to_participant_id": "",
        "created_at": "2026-05-03T12:00:00Z",
        "revision_seq": 0,
    }
    # "λ" is two UTF-8 bytes. 1024 chars = 2048 bytes (right at the cap).
    ns.SessionAskV1.validate({**base, "text": "λ" * 1024})
    # 1025 chars = 2050 bytes — over the cap.
    with pytest.raises(SchemaValidationError):
        ns.SessionAskV1.validate({**base, "text": "λ" * 1025})


def test_ask_text_size_cap_via_upsert(graph_db_env):
    """``upsert_by_key`` runs ``validate_payload`` first, so an oversized
    write rejects before touching the database — no row gets created.
    """
    payload = {
        "session_id": "s",
        "text": "x" * 2049,
        "revision_seq": 1,
        "created_at": "2026-05-03T12:00:00Z",
    }
    with pytest.raises(SchemaValidationError):
        settings_ops.upsert_by_key(ns.SESSION_ASK_SET_ID, 1, "s", payload, org=settings_ops.CALLER_ORG)
    assert _base_rows(ns.SESSION_ASK_SET_ID, "s") == []


# ── Acceptance #4: refresh state machine ────────────────────


def _write_session_ask(
    *, session_id: str, ask_id_key: str, revision_seq: int,
) -> str:
    """Helper — write a SessionAsk row and return its setting id."""
    return settings_ops.upsert_by_key(
        ns.SESSION_ASK_SET_ID, 1, ask_id_key,
        {
            "session_id": session_id,
            "text": f"r{revision_seq}",
            "revision_seq": revision_seq,
            "created_at": "2026-05-03T12:00:00Z",
        },
     org=settings_ops.CALLER_ORG)


def _write_refresh_request(
    *, ask_id: str, requested_by: str, target_revision: int,
    requested_at: str = "2026-05-03T12:05:00Z",
) -> str:
    return settings_ops.upsert_by_key(
        ns.ASK_REFRESH_SET_ID, 1, ask_id,
        {
            "ask_id": ask_id,
            "requested_at": requested_at,
            "requested_by": requested_by,
            "target_revision": target_revision,
        },
     org=settings_ops.CALLER_ORG)


def _refresh_is_requested(ask_id: str, current_revision_seq: int) -> bool:
    """The "requested" state machine, reduced to a pure function.

    The substrate stores claims (refresh row + current ask row).
    Whether the operator should see ``state=requested`` is derived:
    a refresh row exists AND the source's current revision_seq has
    NOT surpassed the pinned target. This is the rule the
    Notifications-tab UI bead will read; encoding it here pins the
    contract.
    """
    refresh_rows = _base_rows(ns.ASK_REFRESH_SET_ID, ask_id)
    if not refresh_rows:
        return False
    refresh = json.loads(refresh_rows[0]["payload"])
    return current_revision_seq <= int(refresh["target_revision"])


def test_refresh_state_machine(graph_db_env):
    """The refresh contract:

    1. Operator clicks ↻ → write refresh row pinned to current
       revision_seq. State becomes ``requested``.
    2. ``requested`` STAYS through:
       - operator re-clicks (idempotent rewrites at the same target)
       - local reloads (re-reading the row sees the same target)
       - heartbeat-style rewrites of the SessionAsk at the SAME
         revision_seq (no source-side bump)
    3. ``requested`` clears ONLY on:
       - source session bumps ``revision_seq`` past the target
       - source session drops the ask
       - chain-clear: another operator writes a fresh refresh with a
         new target (still requested, just at the new pin)
    """
    # 1. Source ask exists at revision 1.
    _write_session_ask(
        session_id="s1", ask_id_key="s1", revision_seq=1,
    )
    assert not _refresh_is_requested("s1", current_revision_seq=1)

    # Operator clicks ↻ → refresh row at target_revision = 1.
    _write_refresh_request(
        ask_id="s1", requested_by="operator-jeremy", target_revision=1,
    )
    assert _refresh_is_requested("s1", current_revision_seq=1)

    # 2a. Re-click at same target — no-op / idempotent. State stays.
    _write_refresh_request(
        ask_id="s1", requested_by="operator-jeremy", target_revision=1,
        requested_at="2026-05-03T12:06:00Z",
    )
    assert len(_base_rows(ns.ASK_REFRESH_SET_ID, "s1")) == 1
    assert _refresh_is_requested("s1", current_revision_seq=1)

    # 2b. Local reload — re-reading sees the same row. State stays.
    rows = _base_rows(ns.ASK_REFRESH_SET_ID, "s1")
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["target_revision"] == 1
    assert _refresh_is_requested("s1", current_revision_seq=1)

    # 2c. Heartbeat-style rewrite of SessionAsk at SAME revision_seq.
    #     Source has not actually responded — state stays requested.
    _write_session_ask(
        session_id="s1", ask_id_key="s1", revision_seq=1,
    )
    assert _refresh_is_requested("s1", current_revision_seq=1)

    # 3a. Source bumps revision_seq → state becomes idle.
    _write_session_ask(
        session_id="s1", ask_id_key="s1", revision_seq=2,
    )
    assert not _refresh_is_requested("s1", current_revision_seq=2)

    # 3b. Chain-clear — operator B requests refresh at new target.
    _write_refresh_request(
        ask_id="s1", requested_by="operator-other", target_revision=2,
        requested_at="2026-05-03T12:10:00Z",
    )
    assert _refresh_is_requested("s1", current_revision_seq=2)
    assert len(_base_rows(ns.ASK_REFRESH_SET_ID, "s1")) == 1

    # 3c. Source bumps past the new target.
    _write_session_ask(
        session_id="s1", ask_id_key="s1", revision_seq=3,
    )
    assert not _refresh_is_requested("s1", current_revision_seq=3)


def test_refresh_request_is_one_row_per_ask(graph_db_env):
    """The refresh row is keyed by ask_id — chain re-requests stay one
    row, not three. Different ask_ids get distinct rows.
    """
    for i in range(1, 4):
        _write_refresh_request(
            ask_id="ask-A", requested_by=f"op-{i}", target_revision=i,
        )
    for i in range(1, 3):
        _write_refresh_request(
            ask_id="ask-B", requested_by="op-x", target_revision=i,
        )
    assert len(_base_rows(ns.ASK_REFRESH_SET_ID, "ask-A")) == 1
    assert len(_base_rows(ns.ASK_REFRESH_SET_ID, "ask-B")) == 1
    a = json.loads(_base_rows(ns.ASK_REFRESH_SET_ID, "ask-A")[0]["payload"])
    assert a["target_revision"] == 3
    assert a["requested_by"] == "op-3"


# ── Acceptance #5: operator-local dismiss ───────────────────


def test_operator_dismissed_local_only(graph_db_env):
    """Dismissing on one operator's session does NOT remove the ask
    from the substrate. Other operators reading ``Schema.of('ask').all()``
    still see the row.

    The substrate is per-DB (one DB per operator's org). A dismissed
    list lives in the operator's own DB; the asks set sits independently.
    The badge count is computed per-operator as
    ``outstanding - dismissed``.
    """
    # Three asks land in the substrate.
    for i in range(3):
        settings_ops.upsert_by_key(
            ns.SESSION_ASK_SET_ID, 1, f"session-{i}",
            {
                "session_id": f"session-{i}",
                "text": f"ask {i}",
                "revision_seq": 1,
                "created_at": "2026-05-03T12:00:00Z",
            },
         org=settings_ops.CALLER_ORG)

    # Operator dismisses two of them.
    settings_ops.upsert_by_key(
        ns.OPERATOR_DISMISSED_SET_ID, 1, "dismissed",
        {"dismissed_ask_ids": ["session-0", "session-1"]},
     org=settings_ops.CALLER_ORG)

    asks = _base_rows(ns.SESSION_ASK_SET_ID)
    assert len(asks) == 3, "asks remain in substrate after dismissal"

    dismissed_rows = _base_rows(ns.OPERATOR_DISMISSED_SET_ID, "dismissed")
    assert len(dismissed_rows) == 1
    payload = json.loads(dismissed_rows[0]["payload"])
    assert set(payload["dismissed_ask_ids"]) == {"session-0", "session-1"}

    # Badge count = outstanding - dismissed; computed view-side from
    # the same data both operators read.
    outstanding_ids = {
        json.loads(r["payload"])["session_id"] for r in asks
    }
    dismissed_ids = set(payload["dismissed_ask_ids"])
    badge_count = len(outstanding_ids - dismissed_ids)
    assert badge_count == 1


def test_operator_dismissed_singleton_stays_one_row(graph_db_env):
    """Repeated writes to ``OperatorDismissedAsksV1`` keep one row —
    the dismissed list is a single, evolving singleton.
    """
    for ids in (
        ["a"],
        ["a", "b"],
        ["a", "b", "c"],
        ["b"],  # operator un-dismisses a, c
    ):
        settings_ops.upsert_by_key(
            ns.OPERATOR_DISMISSED_SET_ID, 1, "dismissed",
            {"dismissed_ask_ids": ids},
         org=settings_ops.CALLER_ORG)
    rows = _base_rows(ns.OPERATOR_DISMISSED_SET_ID, "dismissed")
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"])["dismissed_ask_ids"] == ["b"]


# ── Acceptance #6: v2 drops the vestigial to_participant_id ─


def test_v2_does_not_carry_to_participant_id():
    """v2 removes ``to_participant_id`` entirely — the field had no
    behavioral consumer on the activity surface, only a misleading
    UI label. Writers targeting v2 cannot include it; the schema
    rejects it as an unknown field.
    """
    # The field is not declared on v2.
    assert "to_participant_id" not in ns.SessionAskV2._field_metadata
    # And v2 actively rejects it as an unknown field.
    base = {
        "session_id": "s",
        "compact": "",
        "normal": "hi",
        "expanded": "",
        "created_at": "2026-05-03T12:00:00Z",
        "revision_seq": 0,
    }
    ns.SessionAskV2.validate(base)  # baseline passes
    with pytest.raises(SchemaValidationError):
        ns.SessionAskV2.validate(
            {**base, "to_participant_id": "operator-jeremy"},
        )

    # The substrate has zero role-lookup helpers — the field's
    # original premise (multi-recipient targeting) never had a
    # consumer. Pin that by inspecting the module.
    role_lookup_attrs = [
        attr for attr in dir(ns)
        if "role" in attr.lower() and "lookup" in attr.lower()
    ]
    assert role_lookup_attrs == [], (
        "no role-lookup helper should exist on the notifications "
        "module"
    )


def test_v1_retains_to_participant_id_for_back_compat_reads(graph_db_env):
    """v1 keeps the field declared so legacy stored rows still validate
    on read. Writers shouldn't target v1; this is purely a
    back-compat path until any v1 rows have been re-written.
    """
    settings_ops.upsert_by_key(
        ns.SESSION_ASK_SET_ID, 1, "s-legacy",
        {
            "session_id": "s-legacy",
            "to_participant_id": "operator-old",
            "text": "legacy body",
            "revision_seq": 1,
            "created_at": "2026-05-03T12:00:00Z",
        },
        org=settings_ops.CALLER_ORG,
    )
    rows = _base_rows(ns.SESSION_ASK_SET_ID, "s-legacy")
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload"])
    # The vestigial field round-trips on v1 — that's how stored rows
    # don't trip the unknown-field guard. v2 readers see the field
    # disappear via the upconverter; that's covered separately.
    assert payload["to_participant_id"] == "operator-old"


# ── Schema validation surface ───────────────────────────────


def test_session_ask_revision_seq_must_be_int():
    base = {
        "session_id": "s",
        "text": "hi",
        "created_at": "2026-05-03T12:00:00Z",
    }
    ns.SessionAskV1.validate({**base, "revision_seq": 0})
    ns.SessionAskV1.validate({**base, "revision_seq": 42})
    with pytest.raises(SchemaValidationError):
        ns.SessionAskV1.validate({**base, "revision_seq": "1"})
    with pytest.raises(SchemaValidationError):
        ns.SessionAskV1.validate({**base, "revision_seq": True})


def test_ask_vote_direction_enum():
    base = {
        "ask_id": "a",
        "voter_id": "v",
        "voted_at": "2026-05-03T12:00:00Z",
    }
    ns.AskVoteV1.validate({**base, "direction": "up"})
    ns.AskVoteV1.validate({**base, "direction": "down"})
    with pytest.raises(SchemaValidationError):
        ns.AskVoteV1.validate({**base, "direction": "sideways"})


def test_refresh_target_revision_must_be_int():
    base = {
        "ask_id": "a",
        "requested_by": "op",
        "requested_at": "2026-05-03T12:00:00Z",
    }
    ns.AskRefreshRequestV1.validate({**base, "target_revision": 0})
    ns.AskRefreshRequestV1.validate({**base, "target_revision": 7})
    with pytest.raises(SchemaValidationError):
        ns.AskRefreshRequestV1.validate({**base, "target_revision": "7"})
    with pytest.raises(SchemaValidationError):
        ns.AskRefreshRequestV1.validate({**base, "target_revision": True})


def test_dismissed_ids_must_be_list_of_strings():
    ns.OperatorDismissedAsksV1.validate({"dismissed_ask_ids": []})
    ns.OperatorDismissedAsksV1.validate(
        {"dismissed_ask_ids": ["a", "b"]},
    )
    with pytest.raises(SchemaValidationError):
        ns.OperatorDismissedAsksV1.validate(
            {"dismissed_ask_ids": "not-a-list"},
        )
    with pytest.raises(SchemaValidationError):
        ns.OperatorDismissedAsksV1.validate(
            {"dismissed_ask_ids": ["a", 7]},
        )


def test_unknown_fields_rejected():
    """Extra fields surface as schema violations on every schema."""
    with pytest.raises(SchemaValidationError):
        ns.SessionAskV1.validate({"session_id": "s", "bogus": True})
    with pytest.raises(SchemaValidationError):
        ns.AskVoteV1.validate({"ask_id": "a", "bogus": True})
    with pytest.raises(SchemaValidationError):
        ns.AskRefreshRequestV1.validate({"ask_id": "a", "bogus": True})
    with pytest.raises(SchemaValidationError):
        ns.OperatorDismissedAsksV1.validate(
            {"dismissed_ask_ids": [], "bogus": True},
        )


def test_export_json_schema_round_trip():
    """All schemas produce valid JSON-schema payloads with the
    decorator-driven access_pattern populated. SessionAsk has both
    v1 (legacy) and v2 (current) registered.
    """
    for cls in (
        ns.SessionAskV1, ns.SessionAskV2, ns.AskVoteV1,
        ns.AskRefreshRequestV1, ns.OperatorDismissedAsksV1,
    ):
        js = cls.export_json_schema()
        assert js["set_id"] == cls.set_id
        assert js["schema_revision"] == cls.schema_revision
        assert js["type"] == "object"
        assert isinstance(js["properties"], dict)
        assert js["access_pattern"] in (
            "keyed_per_entity", "singleton",
        )


def test_synopsis_present():
    """Module-level SYNOPSIS exposes nouns the registry flush surfaces."""
    syn = ns.SYNOPSIS
    assert "summary" in syn
    assert isinstance(syn["nouns"], list) and syn["nouns"]
    # The synopsis names the set_ids in the summary, and surfaces v2
    # zoom vocabulary.
    summary = syn["summary"]
    assert "ask" in summary
    assert "operator" in summary
    assert "compact" in summary
    nouns_lower = " ".join(syn["nouns"]).lower()
    assert "compact ask" in nouns_lower
    assert "normal ask" in nouns_lower
    assert "expanded ask" in nouns_lower


# ── SessionAskV2 acceptance ────────────────────────────────────────


def test_session_ask_v2_round_trip_via_upsert(graph_db_env):
    """v2 writes carry compact / normal / expanded body fields; the
    payload round-trips with no extras dropped or invented.
    """
    sid = settings_ops.upsert_by_key(
        ns.SESSION_ASK_SET_ID, ns.SESSION_ASK_LATEST_REVISION, "session-v2",
        {
            "session_id": "session-v2",
            "compact": "Ship migration?",
            "normal": "Should I ship the migration?",
            "expanded": "Long context on the migration plan…",
            "created_at": "2026-05-06T12:00:00Z",
            "revision_seq": 1,
        },
        org=settings_ops.CALLER_ORG,
    )
    rows = _base_rows(ns.SESSION_ASK_SET_ID, "session-v2")
    assert len(rows) == 1
    assert rows[0]["id"] == sid
    payload = json.loads(rows[0]["payload"])
    assert payload["compact"] == "Ship migration?"
    assert payload["normal"] == "Should I ship the migration?"
    assert payload["expanded"].startswith("Long context")
    assert "to_participant_id" not in payload
    assert "text" not in payload


def test_session_ask_v2_per_zoom_size_caps():
    """Each zoom field has its own byte cap. Caps are independent —
    a 2KB normal write does not constrain a 256B compact write or
    an 8KB expanded write.
    """
    base = {
        "session_id": "s",
        "compact": "",
        "normal": "",
        "expanded": "",
        "created_at": "2026-05-06T12:00:00Z",
        "revision_seq": 0,
    }
    # Compact: 256B cap.
    ns.SessionAskV2.validate({**base, "compact": "x" * 256})
    with pytest.raises(SchemaValidationError) as exc:
        ns.SessionAskV2.validate({**base, "compact": "x" * 257})
    assert "compact" in str(exc.value).lower()

    # Normal: 2KB cap.
    ns.SessionAskV2.validate({**base, "normal": "x" * 2048})
    with pytest.raises(SchemaValidationError) as exc:
        ns.SessionAskV2.validate({**base, "normal": "x" * 2049})
    assert "normal" in str(exc.value).lower()

    # Expanded: 8KB cap.
    ns.SessionAskV2.validate({**base, "expanded": "x" * 8192})
    with pytest.raises(SchemaValidationError) as exc:
        ns.SessionAskV2.validate({**base, "expanded": "x" * 8193})
    assert "expanded" in str(exc.value).lower()


def test_session_ask_v2_revision_seq_must_be_int():
    base = {
        "session_id": "s",
        "compact": "",
        "normal": "hi",
        "expanded": "",
        "created_at": "2026-05-06T12:00:00Z",
    }
    ns.SessionAskV2.validate({**base, "revision_seq": 0})
    ns.SessionAskV2.validate({**base, "revision_seq": 42})
    with pytest.raises(SchemaValidationError):
        ns.SessionAskV2.validate({**base, "revision_seq": "1"})
    with pytest.raises(SchemaValidationError):
        ns.SessionAskV2.validate({**base, "revision_seq": True})


def test_session_ask_v2_unknown_field_rejected():
    """v2 explicitly rejects ``text`` (the legacy single-zoom body) and
    ``to_participant_id`` (the vestigial recipient label) so writers
    can't accidentally send v1-shaped payloads against v2.
    """
    base = {
        "session_id": "s",
        "compact": "",
        "normal": "hi",
        "expanded": "",
        "created_at": "2026-05-06T12:00:00Z",
        "revision_seq": 0,
    }
    with pytest.raises(SchemaValidationError) as exc:
        ns.SessionAskV2.validate({**base, "text": "legacy"})
    assert "text" in str(exc.value)
    with pytest.raises(SchemaValidationError) as exc:
        ns.SessionAskV2.validate(
            {**base, "to_participant_id": "operator-jeremy"},
        )
    assert "to_participant_id" in str(exc.value)


def test_session_ask_v1_to_v2_upconverter_maps_text_to_normal():
    """The upconverter declared on :class:`SessionAskV2` maps stored
    v1 payloads into v2 shape: ``text`` becomes ``normal``;
    ``to_participant_id`` is dropped silently.
    """
    v1_payload = {
        "session_id": "s-legacy",
        "to_participant_id": "operator-jeremy",
        "text": "Should I ship?",
        "created_at": "2026-05-06T12:00:00Z",
        "revision_seq": 7,
    }
    out = ns.SessionAskV2.upconvert_from_prev(v1_payload)
    assert out["session_id"] == "s-legacy"
    assert out["normal"] == "Should I ship?"
    assert out["compact"] == ""
    assert out["expanded"] == ""
    assert out["created_at"] == "2026-05-06T12:00:00Z"
    assert out["revision_seq"] == 7
    # Vestigial field is gone in v2.
    assert "to_participant_id" not in out
    # v1's single-zoom body is gone in v2.
    assert "text" not in out
    # And the upconverted payload validates clean.
    ns.SessionAskV2.validate(out)


def test_session_ask_v1_to_v2_upconverter_handles_missing_text():
    """An old row written without a ``text`` field (rare but possible)
    upconverts to a v2 row with ``normal=""`` rather than crashing.
    """
    v1_minimal = {
        "session_id": "s-bare",
        "revision_seq": 0,
    }
    out = ns.SessionAskV2.upconvert_from_prev(v1_minimal)
    assert out["normal"] == ""
    ns.SessionAskV2.validate(out)
