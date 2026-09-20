"""Targeted row re-fetch (auto-l4h2c): the drain for quarantined rows that
stored no frame.

A row deferred during a bootstrap sweep is parked by address only: the
sweep was expected to carry it again, but an established store never
sweeps again, so such a row could never land. After every successful pull
the puller asks the peer for exactly those addresses; the peer rebuilds
each current row from its catalog with the origin and transaction that
authored it, and the puller applies them through ordinary
last-writer-wins, which lands the row or re-parks it WITH its frame for
the frame drain.

Wire: request {v, op: "rows", scope, addresses: [hex address blobs]};
reply, per row, one transaction header (one operation, last) and one
operation frame, then {v, kind: "rows.done", served}. Bounded both ways.
"""
from __future__ import annotations

import json
from typing import Iterable, Iterator

from tools.network.idkit import canonical_json

ROWS_PROTOCOL_VERSION = 1
MAX_ROWS_REQUEST = 256
ROWS_DONE_KIND = "rows.done"


class RowFetchError(ValueError):
    pass


def encode_rows_request(scope: str, addresses: Iterable[bytes]) -> bytes:
    blobs = [bytes(a).hex() for a in addresses]
    if not blobs or len(blobs) > MAX_ROWS_REQUEST:
        raise RowFetchError("rows request address count out of bounds")
    if not isinstance(scope, str) or not scope or ":" in scope:
        raise RowFetchError("rows request scope is malformed")
    return canonical_json({
        "v": ROWS_PROTOCOL_VERSION, "op": "rows", "scope": scope, "addresses": blobs,
    })


def decode_rows_request(raw: bytes) -> tuple[str, list[bytes]]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RowFetchError("rows request is not JSON") from exc
    if not isinstance(value, dict) or set(value) != {"v", "op", "scope", "addresses"}:
        raise RowFetchError("rows request fields are malformed")
    if value["v"] != ROWS_PROTOCOL_VERSION or value["op"] != "rows":
        raise RowFetchError("unsupported rows request")
    scope = value["scope"]
    if not isinstance(scope, str) or not scope or ":" in scope:
        raise RowFetchError("rows request scope is malformed")
    blobs = value["addresses"]
    if (
        not isinstance(blobs, list) or not blobs or len(blobs) > MAX_ROWS_REQUEST
        or not all(isinstance(b, str) and b and len(b) % 2 == 0 for b in blobs)
    ):
        raise RowFetchError("rows request addresses are malformed")
    try:
        return scope, [bytes.fromhex(b) for b in blobs]
    except ValueError as exc:
        raise RowFetchError("rows request address is not hex") from exc


def iter_row_frames(items, version: int) -> Iterator[bytes]:
    """Frames for *items* (AuthoredMutation): header + operation per row,
    then the terminal rows.done."""
    from tools.network.fleet_sync_scheduler import (
        encode_operation_frame, encode_transaction_header,
    )
    served = 0
    for item in items:
        yield encode_transaction_header(
            item.origin_incarnation, item.transaction_id, 1, group=0, last=True,
        )
        yield encode_operation_frame(item)
        served += 1
    yield canonical_json({"v": version, "kind": ROWS_DONE_KIND, "served": served})


class RowReceiver:
    """Collects the fetched rows as AuthoredMutation; ``done`` on rows.done."""

    def __init__(self) -> None:
        self.items: list = []
        self.done = False
        self._pending: tuple[str, str] | None = None

    def feed(self, frame: bytes) -> None:
        from tools.network.fleet_sync.compaction import AuthoredMutation
        from tools.network.fleet_sync_scheduler import (
            _OPERATION_MAGIC, _TRANSACTION_MAGIC, decode_operation_frame,
            decode_transaction_header,
        )
        if frame.startswith(_TRANSACTION_MAGIC):
            origin, transaction_id, _n, _g, _last = decode_transaction_header(frame)
            self._pending = (origin, transaction_id)
            return
        if frame.startswith(_OPERATION_MAGIC):
            if self._pending is None:
                raise RowFetchError("row operation before its header")
            operation, mutation = decode_operation_frame(frame)
            self.items.append(AuthoredMutation(
                self._pending[0], self._pending[1], operation, mutation,
            ))
            self._pending = None
            return
        try:
            control = json.loads(frame.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RowFetchError("unknown row fetch frame") from exc
        if isinstance(control, dict) and control.get("kind") == ROWS_DONE_KIND:
            self.done = True
            return
        raise RowFetchError("unknown row fetch control frame")
