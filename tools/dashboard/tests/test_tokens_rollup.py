"""Settling a day of token spend into Settings (design graph://d6744fce-929).

The live counters are local, unreplicated and cumulative per session. This is
what turns them into a durable per-day record the whole fleet can read, and
the properties that matter are all about NOT lying: a day is settled once, a
session spanning days is split across them, a failed write loses nothing, and
two machines never overwrite each other.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from tools.dashboard import tokens_rollup as tr
from tools.graph.schemas import registry as schema_registry


NOW = datetime(2026, 9, 8, 5, 0, 0, tzinfo=timezone.utc)
MACHINE = "9ca8797e76b025ce6af2d2f9b730bd9b2dd2290f25f83a0f90bd1e6c5c5147fa"
OTHER_MACHINE = "1111111111111111111111111111111111111111111111111111111111111111"


def _session(name, *, inp=0, cw=0, cr=0, out=0, turns=0, flushed=None,
             account="acct-A", model="claude-fable-5-1", project="autonomy"):
    return {
        "tmux_name": name, "harness": "claude", "harness_token": account,
        "model": model, "project": project, "type": "terminal",
        "usage_input_tokens": inp, "usage_cache_creation_tokens": cw,
        "usage_cache_read_tokens": cr, "usage_output_tokens": out,
        "usage_turns": turns,
        "usage_flushed": json.dumps(flushed) if flushed else None,
    }


def _recorder():
    writes, marks = [], {}
    def upsert(set_id, rev, key, payload, *, org, state="raw"):
        writes.append({"set_id": set_id, "key": key, "payload": payload,
                       "org": org, "state": state})
        return "sid"
    def mark(name, watermark):
        marks[name] = json.loads(watermark)
    return writes, marks, upsert, mark


def _settle(rows, *, day="2026-09-07", machine=MACHINE, now=NOW):
    writes, marks, upsert, mark = _recorder()
    payload = tr.settle_day(
        day, rows=rows, machine_id=machine, now=now,
        org_of=lambda r: r.get("project") or "unknown",
        upsert_by_key=upsert, set_watermark=mark,
    )
    return payload, writes, marks


# ── the schema ────────────────────────────────────────────────


def test_the_schema_is_registered_and_personal_homed():
    cls = schema_registry.get_schema(tr.TOKENS_ROLLUP_SET_ID, 1)
    assert cls is tr.DashboardTokensRollupV1
    assert tr.TOKENS_ROLLUP_ORG == "personal"


def test_the_key_leads_with_the_day_so_a_month_is_a_prefix():
    key = tr.make_rollup_key("2026-09-07", MACHINE)
    assert key.startswith("2026-09-07:")
    assert key.startswith("2026-09-")
    assert key.endswith(MACHINE)


def test_neither_key_segment_is_repeated_in_the_payload():
    """Readers recover the day and the machine from the member key; storing
    them again would be a second source of truth for the same fact."""
    payload, _, _ = _settle([_session("s1", out=5, turns=1)])
    assert "day" not in payload
    assert "machine_id" not in payload


def _entry(**overrides):
    """A valid entry, then whatever the case changes. ``None`` removes a key,
    so a 'missing field' case is genuinely missing rather than merged over."""
    entry = {"org": "o", "account": "a", "model": "m",
             **dict.fromkeys(tr.ENTRY_COUNTERS, 0)}
    for name, value in overrides.items():
        if value is None:
            entry.pop(name, None)
        else:
            entry[name] = value
    return entry


@pytest.mark.parametrize("entry,reason", [
    (_entry(org=""), "empty org"),
    (_entry(input_tokens=-1), "negative count"),
    (_entry(input_tokens="5"), "count as a string"),
    (_entry(input_tokens=True), "bool masquerading as a count"),
    (_entry(account=None), "missing account"),
    (_entry(model=None), "missing model"),
    (_entry(turns=None), "missing counter"),
    (_entry(sessions=0), "session count below one"),
])
def test_a_malformed_entry_is_refused(entry, reason):
    with pytest.raises(schema_registry.SchemaValidationError):
        tr.DashboardTokensRollupV1.validate(
            {"flushed_at": "2026-09-08T05:00:00Z", "entries": [entry]},
        )


def test_entries_must_be_a_list_of_objects():
    for bad in ("not-a-list", [1], [None]):
        with pytest.raises(schema_registry.SchemaValidationError):
            tr.DashboardTokensRollupV1.validate(
                {"flushed_at": "2026-09-08T05:00:00Z", "entries": bad},
            )


def test_a_well_formed_day_validates():
    payload, _, _ = _settle([_session("s1", inp=2, cr=99, out=5, turns=1)])
    tr.DashboardTokensRollupV1.validate(payload)


# ── what a settled day contains ───────────────────────────────


def test_spend_is_grouped_by_organization_account_and_model():
    payload, _, _ = _settle([
        _session("a", out=10, turns=1, account="acct-A", model="m1"),
        _session("b", out=20, turns=2, account="acct-A", model="m1"),
        _session("c", out=5, turns=1, account="acct-B", model="m1"),
        _session("d", out=7, turns=1, account="acct-A", model="m2"),
    ])
    by = {(e["account"], e["model"]): e for e in payload["entries"]}
    assert by[("acct-A", "m1")]["output_tokens"] == 30
    assert by[("acct-A", "m1")]["turns"] == 3
    assert by[("acct-A", "m1")]["sessions"] == 2
    assert by[("acct-B", "m1")]["output_tokens"] == 5
    assert by[("acct-A", "m2")]["output_tokens"] == 7


def test_a_session_with_no_credential_is_its_own_bucket_not_a_guess():
    payload, _, _ = _settle([_session("s1", out=9, turns=1, account=None)])
    assert payload["entries"][0]["account"] == tr.UNATTRIBUTED_ACCOUNT


def test_only_what_was_billed_since_the_last_settlement_is_counted():
    """The property that makes a multi-day session honest: yesterday's row
    must not re-bill what the day before already settled."""
    row = _session("s1", inp=100, out=50, turns=10,
                   flushed={"day": "2026-09-06", "input_tokens": 60,
                            "output_tokens": 20, "turns": 4,
                            "cache_creation_tokens": 0, "cache_read_tokens": 0})
    payload, _, _ = _settle([row])
    entry = payload["entries"][0]
    assert entry["input_tokens"] == 40
    assert entry["output_tokens"] == 30
    assert entry["turns"] == 6


def test_a_counter_that_went_backwards_never_bills_a_negative():
    row = _session("s1", out=5, turns=1,
                   flushed={"day": "2026-09-06", "output_tokens": 500,
                            "turns": 90, "input_tokens": 0,
                            "cache_creation_tokens": 0, "cache_read_tokens": 0})
    payload, writes, _ = _settle([row])
    assert payload is None and writes == []


def test_a_day_that_billed_nothing_writes_no_row():
    """An absent row and a row of zeroes say the same thing, and only one of
    them costs a replicated write."""
    payload, writes, _ = _settle([_session("s1")])
    assert payload is None
    assert writes == []


# ── settlement bookkeeping ────────────────────────────────────


def test_the_row_lands_personal_and_raw_under_the_day_and_machine():
    _, writes, _ = _settle([_session("s1", out=5, turns=1)])
    assert len(writes) == 1
    assert writes[0]["set_id"] == tr.TOKENS_ROLLUP_SET_ID
    assert writes[0]["key"] == f"2026-09-07:{MACHINE}"
    assert writes[0]["org"] == "personal"
    assert writes[0]["state"] == "raw"


def test_watermarks_advance_to_the_settled_totals():
    _, _, marks = _settle([_session("s1", inp=7, out=5, turns=1)])
    assert marks["s1"]["day"] == "2026-09-07"
    assert marks["s1"]["input_tokens"] == 7
    assert marks["s1"]["output_tokens"] == 5


def test_a_failed_write_settles_nothing_and_loses_nothing():
    """Watermarks advance only after the row is accepted, so the next attempt
    re-sends the same day instead of dropping it."""
    def boom(*a, **k):
        raise RuntimeError("substrate unavailable")
    marks = {}
    with pytest.raises(RuntimeError):
        tr.settle_day(
            "2026-09-07", rows=[_session("s1", out=5, turns=1)],
            machine_id=MACHINE, now=NOW,
            org_of=lambda r: "autonomy", upsert_by_key=boom,
            set_watermark=lambda n, w: marks.__setitem__(n, w),
        )
    assert marks == {}


def test_two_machines_write_disjoint_rows():
    """personal.db replicates, so a shared key would mean each machine's
    settlement erased the other's."""
    rows = [_session("s1", out=5, turns=1)]
    _, writes_a, _ = _settle(rows, machine=MACHINE)
    _, writes_b, _ = _settle(rows, machine=OTHER_MACHINE)
    assert writes_a[0]["key"] != writes_b[0]["key"]
    assert writes_a[0]["key"].split(":")[0] == writes_b[0]["key"].split(":")[0]


# ── the promoter ──────────────────────────────────────────────


def _promote(rows, *, now=NOW, machine=MACHINE):
    writes, marks, upsert, mark = _recorder()
    day = tr.promote_closed_days(
        now=now, read_rows=lambda: rows, org_of=lambda r: r.get("project"),
        upsert_by_key=upsert, set_watermark=mark, machine_id=machine,
    )
    return day, writes, marks


def test_it_settles_yesterday_not_today():
    """Today is still accruing; a settled row must never need revising."""
    day, writes, _ = _promote([_session("s1", out=5, turns=1)])
    assert day == "2026-09-07"
    assert writes[0]["key"].startswith("2026-09-07:")


def test_running_twice_in_a_day_settles_once():
    rows = [_session("s1", out=5, turns=1)]
    _, writes, marks = _promote(rows)
    assert len(writes) == 1
    rows[0]["usage_flushed"] = json.dumps(marks["s1"])
    _, writes_again, _ = _promote(rows)
    assert writes_again == []


def test_a_session_already_settled_for_that_day_is_left_alone():
    row = _session("s1", out=5, turns=1,
                   flushed={"day": "2026-09-07", "output_tokens": 5, "turns": 1,
                            "input_tokens": 0, "cache_creation_tokens": 0,
                            "cache_read_tokens": 0})
    day, writes, _ = _promote([row])
    assert day is None and writes == []


def test_nothing_is_settled_without_a_machine_identity():
    """The machine is half the key. Guessing one would let two machines
    collide on the same row."""
    day, writes, _ = _promote([_session("s1", out=5, turns=1)], machine=None)
    assert day is None and writes == []
