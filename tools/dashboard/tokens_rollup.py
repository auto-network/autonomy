"""The durable half of the token ledger: one settled row per day per machine.

``tmux_sessions.usage_*`` (auto-pbrhs) counts what each session billed, but it
is a node-local operational store that nothing replicates, and its counters are
cumulative per SESSION with no date dimension -- a session running across three
days holds one number. This module is what turns that into a durable, fleet-
visible record of spend per day.

The split is deliberate and was settled with the operator (design
graph://d6744fce-929, correction comment 535785ea-d7a):

* The live counter stays local and unsynchronised. It is written on every drain
  acknowledgement, several times a minute per session, and Settings is not a
  write-hot store.
* A day is promoted into Settings exactly ONCE, when it closes and can no
  longer change. Fleet-wide that is one small write per machine per day.

Promoting on close rather than on a timer is the whole point. ``personal.db``
replicates across the operator's fleet, and
``tools/graph/schemas/fleet_sync_telemetry.py`` rejects the alternative in as
many words: a high-frequency row in the personal store "would turn observation
into more replicated work and create a feedback loop". A settled row is written
once and never touched again, which is also the discipline
``tools/network/accounting/rollups.py`` enforces for its own tiers.

Each machine writes only its own rows, so there is no merge and no lost update:
the machine is part of the key, and a reader sums across machines.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import logging
from typing import Any, Callable

from tools.graph.schemas.registry import (
    publication_band,
    home,
    SchemaValidationError,
    SettingSchema,
    field,
    keyed_per_entity,
)

logger = logging.getLogger(__name__)

TOKENS_ROLLUP_SET_ID = "dashboard.tokens.rollup"
TOKENS_ROLLUP_SCHEMA_REVISION = 1

#: The operator's own store, like every other row about the operator's own
#: accounts. It replicates across the operator's OWN fleet and never onto
#: another user's machine, which is exactly the reach a spend record needs.
#: ``machine`` would have been wrong: it replicates nowhere, so it could not
#: back a fleet-wide view at all.
TOKENS_ROLLUP_ORG = "personal"

#: The counters carried per entry, matching ``dashboard_db.USAGE_LEDGER_COLUMNS``
#: minus the column prefix. Components rather than one total: an input token, a
#: cache write and a cache read are priced an order of magnitude apart, so a
#: single number could not be turned back into money.
ENTRY_COUNTERS = (
    "input_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "output_tokens",
    "turns",
)

#: Sessions whose launch recorded no credential. They are counted honestly
#: under their own bucket rather than attributed to an account that may not
#: have served them (bead auto-7fm71 is the missing attribution itself).
UNATTRIBUTED_ACCOUNT = "unattributed"


# NOTE: deliberately NO @cache(ttl=...). The decorator stamps an expiry and
# `cache_gc` really does delete, but nothing triggers that sweep on this
# platform -- measured 2026-09-08, with rows from a retired set still resident
# four months past their expiry (bead auto-r95da). Declaring a lifetime here
# would claim a retention that does not happen. Retention is explicit instead:
# fold a closed month into a monthly row, then drop the dailies with
# `settings_ops.remove_settings_by_key_prefix`.
@publication_band(max="raw")
@home(TOKENS_ROLLUP_ORG)
@keyed_per_entity(key_strategy="day:machine_id")
class DashboardTokensRollupV1(SettingSchema):
    """One settled day of token spend, as observed by one machine.

    Key: ``<YYYY-MM-DD>:<machine_id>``, the UTC day and this machine's 64-hex
    identity.

    The day leads because prefixes read left to right: ``2026-09-`` selects a
    month and ``2026-`` a year, so a date window is one or two queries rather
    than a scan of every day ever recorded. Reverse the segments and every
    date question becomes a full-set read.

    The machine is in the key because ``personal.db`` replicates. If every
    machine wrote the same day key, each flush would clobber the others and
    the fleet total would be whichever machine wrote last. Disjoint keys mean
    nothing has to merge, and a reader sums across machines.

    Neither key segment is repeated in the payload; readers recover both from
    the member key.
    """

    set_id = TOKENS_ROLLUP_SET_ID
    schema_revision = TOKENS_ROLLUP_SCHEMA_REVISION

    flushed_at: str = field(
        required=True,
        description=(
            "ISO-8601 instant this day was settled. Provenance for the write, "
            "not a date dimension -- the day itself is the key."
        ),
    )
    entries: list = field(
        required=True,
        description=(
            "Flat records of spend, one per (organization, account, model) "
            "seen that day. Organization, account and model are content, not "
            "addressing: no read fetches one account's single day directly, "
            "and lifting them into the key would multiply the row count for a "
            "lookup nobody performs."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise SchemaValidationError(
                f"{cls.__name__}: 'entries' must be a list"
            )
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise SchemaValidationError(
                    f"{cls.__name__}: entries[{index}] must be an object"
                )
            for name in ("org", "account", "model"):
                if not isinstance(entry.get(name), str) or not entry[name]:
                    raise SchemaValidationError(
                        f"{cls.__name__}: entries[{index}].{name} must be a "
                        f"non-empty string"
                    )
            for name in ENTRY_COUNTERS:
                value = entry.get(name)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise SchemaValidationError(
                        f"{cls.__name__}: entries[{index}].{name} must be a "
                        f"non-negative integer"
                    )
            if "sessions" in entry:
                value = entry["sessions"]
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    raise SchemaValidationError(
                        f"{cls.__name__}: entries[{index}].sessions must be a "
                        f"positive integer"
                    )


def make_rollup_key(day: str, machine_id: str) -> str:
    return f"{day}:{machine_id}"


def day_of(moment: datetime) -> str:
    """The UTC day a moment falls in, as the key's leading segment."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d")


def previous_day(day: str) -> str:
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


def _counter_delta(row: dict[str, Any], watermark: dict[str, Any]) -> dict[str, int]:
    """Tokens this session billed since it was last settled.

    Cumulative minus watermark. A negative difference means the row was reset
    beneath us (a rebuilt database, a re-registered session); clamp to zero
    rather than subtracting spend that was really incurred.
    """
    delta: dict[str, int] = {}
    for name in ENTRY_COUNTERS:
        current = int(row.get(f"usage_{name}") or 0)
        already = int(watermark.get(name) or 0)
        delta[name] = max(0, current - already)
    return delta


def read_watermark(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("usage_flushed")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def watermark_for(row: dict[str, Any], day: str) -> str:
    """The watermark to store after settling ``day`` for this session."""
    return json.dumps(
        {"day": day, **{name: int(row.get(f"usage_{name}") or 0)
                        for name in ENTRY_COUNTERS}},
        sort_keys=True,
    )


def build_entries(
    rows: list[dict[str, Any]],
    *,
    org_of: Callable[[dict[str, Any]], str],
) -> list[dict[str, Any]]:
    """Aggregate per-session deltas into one record per org/account/model.

    A session contributes only what it billed since it was last settled, so a
    session that spans several days is attributed to each of them in turn
    rather than landing entirely on the day it happens to end.
    """
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        delta = _counter_delta(row, read_watermark(row))
        if not any(delta.values()):
            continue
        bucket_key = (
            org_of(row) or "unknown",
            str(row.get("harness_token") or UNATTRIBUTED_ACCOUNT),
            str(row.get("model") or "unknown"),
        )
        entry = buckets.setdefault(bucket_key, {
            "org": bucket_key[0], "account": bucket_key[1], "model": bucket_key[2],
            **dict.fromkeys(ENTRY_COUNTERS, 0), "sessions": 0,
        })
        for name in ENTRY_COUNTERS:
            entry[name] += delta[name]
        entry["sessions"] += 1
    return [buckets[k] for k in sorted(buckets)]


def machine_identity() -> str | None:
    """This machine's 64-hex id, or None if it has none yet.

    Read through ``machine_boot`` in the process that owns the live machine
    store. A session container reading its own store gets None, which is a
    seat artifact rather than an absent identity -- the same class of mistake
    as reading credential rows from a container's empty personal store.
    """
    try:
        from tools.network import machine_boot
        return machine_boot.machine_id(org="machine")
    except Exception:
        logger.exception("tokens rollup: machine identity unavailable")
        return None


def settle_day(
    day: str,
    *,
    rows: list[dict[str, Any]],
    machine_id: str,
    now: datetime,
    org_of: Callable[[dict[str, Any]], str],
    upsert_by_key: Callable[..., Any],
    set_watermark: Callable[[str, str], None],
) -> dict[str, Any] | None:
    """Write one settled day for this machine and advance the watermarks.

    Returns the payload written, or None when the day billed nothing at all,
    in which case no row is created: an absent row and a row of zeroes say the
    same thing, and only one of them costs a replicated write.

    The watermarks advance only after the row is accepted. A failed write
    therefore settles nothing, and the next attempt re-sends the same day
    rather than losing it.
    """
    entries = build_entries(rows, org_of=org_of)
    if not entries:
        return None
    payload = {
        "flushed_at": now.astimezone(timezone.utc).replace(
            microsecond=0,
        ).isoformat().replace("+00:00", "Z"),
        "entries": entries,
    }
    upsert_by_key(
        TOKENS_ROLLUP_SET_ID,
        TOKENS_ROLLUP_SCHEMA_REVISION,
        make_rollup_key(day, machine_id),
        payload,
        org=TOKENS_ROLLUP_ORG,
        state="raw",
    )
    for row in rows:
        set_watermark(str(row.get("tmux_name") or ""), watermark_for(row, day))
    logger.info(
        "tokens rollup: settled %s for machine %s (%d entries, %d sessions)",
        day, machine_id[:12], len(entries),
        sum(int(e.get("sessions") or 0) for e in entries),
    )
    return payload


def promote_closed_days(
    *,
    now: datetime | None = None,
    read_rows: Callable[[], list[dict[str, Any]]] | None = None,
    org_of: Callable[[dict[str, Any]], str] | None = None,
    upsert_by_key: Callable[..., Any] | None = None,
    set_watermark: Callable[[str, str], None] | None = None,
    machine_id: str | None = None,
) -> str | None:
    """Settle yesterday, once, if it has not been settled already.

    Only a CLOSED day is written, so a row never has to be revised: today is
    still accruing and is served from the local counters instead. Every
    session carries the day it was last settled for, so a second run on the
    same day is a no-op rather than a double count.

    Deliberately not gated on operator idleness -- a day must settle whether
    or not anyone is watching -- and deliberately not gated on the Fleet
    singular-ownership check, because every machine settles its OWN rows and
    a gate would silently drop the others' spend.
    """
    now = now or datetime.now(timezone.utc)
    machine_id = machine_id or machine_identity()
    if not machine_id:
        logger.debug("tokens rollup: no machine identity yet; nothing settled")
        return None
    read_rows = read_rows or _default_read_rows
    upsert_by_key = upsert_by_key or _default_upsert
    set_watermark = set_watermark or _default_set_watermark
    if org_of is None:
        from tools.dashboard.org_identity import session_org_slug
        org_of = session_org_slug

    day = previous_day(day_of(now))
    # A row is pending when its last settled day is earlier than the day we
    # are closing. An empty watermark sorts before every date, so a session
    # that has never been settled is included.
    pending = [
        row for row in read_rows()
        if str(read_watermark(row).get("day") or "") < day
    ]
    if not pending:
        return None
    written = settle_day(
        day, rows=pending, machine_id=machine_id, now=now,
        org_of=org_of, upsert_by_key=upsert_by_key, set_watermark=set_watermark,
    )
    if written is None:
        # Nothing billed, but the watermarks still advance so the same rows
        # are not re-examined every tick for the rest of the machine's life.
        for row in pending:
            set_watermark(str(row.get("tmux_name") or ""), watermark_for(row, day))
    return day


def _default_read_rows() -> list[dict[str, Any]]:
    from tools.dashboard.dao import dashboard_db
    return dashboard_db.get_sessions_for_settlement()


def _default_set_watermark(tmux_name: str, watermark: str) -> None:
    from tools.dashboard.dao import dashboard_db
    dashboard_db.set_usage_watermark(tmux_name, watermark)


def _default_upsert(*args: Any, **kwargs: Any) -> Any:
    from tools.graph import ops as graph_ops
    return graph_ops.upsert_by_key(*args, **kwargs)

