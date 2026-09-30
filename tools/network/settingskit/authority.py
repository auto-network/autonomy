"""The fold behind a store, cached by ledger depth (auto-qrmlg.6 S3).

Both boundaries — the local write path in ``settings_ops`` and sync apply in
``fleet_sync.materialize`` — need the organization's fold to judge a signing
key. The ledger's events ARE settings rows in the same database (design
graph://53b5bb04-bc0), so the fold is a function of the store the row is
entering, read on the very connection that is writing it: no second file
open, no ledger store hydration per write.

The fold is rebuilt only when the ledger's depth changes. The depth key is
one count query (0.02 ms on SJC-2's autonomy store, 2026-09-29); a rebuild
is ~6 ms at 22 events and happens once per ledger advance per process. The
signer check against a cached fold is microseconds. Measured in
/workspace/output/qrmlg6-sign-latency-sjc2.txt.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from tools.network.settingskit.boundary import SignerVerdict, check_signer

LEDGER_EVENT_SET_ID = "autonomy.org.ledger-event"

#: Key strategies whose row key IS the member persona: the signing authority
#: must terminate at the row key (design of record, "key_strategy is
#: unchanged"). Every other strategy authorizes the signer by scope.
PERSONA_KEY_STRATEGIES = frozenset({
    "member_public_key", "persona", "persona_pub", "member_persona",
})
#: Key strategies whose row key STARTS with the member persona
#: (``persona_pub:<rest>``): the signer must be that persona, and the rest of
#: the key is the set's own dimensions, so a persona holds many rows.
PERSONA_PREFIX_KEY_STRATEGIES = frozenset({"persona_pub:*"})

#: (store path) -> (depth key, FoldState | None)
_FOLD_CACHE: dict[str, tuple[tuple, object]] = {}


def signing_key_strategy(set_id: str, schema_revision: int) -> str:
    """``"persona"`` when the set's declared key strategy names the member
    persona, else ``"delegate"``. A set with no registered schema, or a
    process without the schema registry, resolves to ``delegate`` — the
    boundary's scope check then decides, which is the stricter branch."""
    try:
        from tools.graph.schemas.registry import SCHEMAS
    except ImportError:   # pragma: no cover - dependency-light callers
        return "delegate"
    cls = SCHEMAS.get(f"{set_id}#{int(schema_revision)}")
    strategy = getattr(cls, "_key_strategy", None) if cls is not None else None
    if strategy in PERSONA_KEY_STRATEGIES:
        return "persona"
    if strategy in PERSONA_PREFIX_KEY_STRATEGIES:
        return "persona_prefix"
    return "delegate"


def _store_path(conn: sqlite3.Connection) -> str:
    for _seq, name, path in conn.execute("PRAGMA database_list"):
        if name == "main":
            return str(path) or f"memory:{id(conn)}"
    return f"memory:{id(conn)}"   # pragma: no cover


def ledger_depth(conn: sqlite3.Connection) -> tuple:
    """The store's ledger depth: (event row count, newest event rowid).
    Changes whenever an event row is added or removed."""
    try:
        row = conn.execute(
            'SELECT COUNT(*), MAX(rowid) FROM settings WHERE set_id=? '
            "AND supersedes IS NULL AND excludes IS NULL AND deprecated=0",
            (LEDGER_EVENT_SET_ID,),
        ).fetchone()
    except sqlite3.Error:
        return (0, None)
    return (int(row[0] or 0), row[1])


def store_fold(conn: sqlite3.Connection):
    """The fold of the organization ledger held in *conn*'s store, or None
    when the store holds no ledger (unfounded, or a personal/machine store).
    Rebuilt only when :func:`ledger_depth` changes; otherwise the cached
    state is returned untouched. A ledger that cannot be ingested (a
    malformed or orphaned event row) folds as None, so every signed row is
    parked rather than judged against a partial authority."""
    from tools.network.ledger import Event, Ledger, fold
    from tools.network.ledger.settings_bridge import read_event_wires

    depth = ledger_depth(conn)
    path = _store_path(conn)
    cached = _FOLD_CACHE.get(path)
    if cached is not None and cached[0] == depth:
        return cached[1]
    state = None
    if depth[0]:
        try:
            wires = read_event_wires(conn)
            ledger = Ledger()
            ledger.ingest([Event.from_json(wire.encode("utf-8")) for wire in wires.values()])
            if ledger.genesis_id is not None:
                state = fold(ledger)
        except Exception:
            state = None
    if len(_FOLD_CACHE) > 64:
        _FOLD_CACHE.clear()
    _FOLD_CACHE[path] = (depth, state)
    return state


def forget(conn: Optional[sqlite3.Connection] = None) -> None:
    """Drop the cached fold for *conn*'s store (all stores when None)."""
    if conn is None:
        _FOLD_CACHE.clear()
    else:
        _FOLD_CACHE.pop(_store_path(conn), None)


def judge_row(fold, row, *, key_strategy: Optional[str] = None) -> SignerVerdict:
    """Steps 2 to 5 for a signed settings *row* (any mapping with the
    settings columns) against *fold*."""
    strategy = key_strategy or signing_key_strategy(str(row["set_id"]), int(row["schema_revision"]))
    return check_signer(
        fold, signing_key=str(row["signing_key"]), set_id=str(row["set_id"]),
        key_strategy=strategy, row_key=str(row["key"]),
    )
