"""Watch a session on another fleet machine through Home's own viewer
(graph://7eb29bc8-31a §6.2, §9.6, bead auto-fd68i).

The viewer addresses a remote session as ``<name>@<machine>`` end to end:
its page, its tail requests (which api_session_tail proxies over
session-control's ``tail`` op) and the ``session:messages`` events it
listens for. Those events come from here: while someone is viewing a remote
session -- a tail request for it within :data:`WATCH_TTL_S` -- a watcher
asks the far machine for entries after its cursor every
:data:`POLL_INTERVAL_S` and republishes them on Home's bus under the
remote address, so the existing session store merges them like local ones.

This is the v1 of the design's observe stream: a forward-tail poll over the
same authenticated channel. It meets the 3 s scenario (S2) at a
1.5 s interval; a standing observe pair replaces it only if it does not.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 1.5
#: A watch lives this long after the viewer's last tail request for it.
WATCH_TTL_S = 600.0
#: Query keys the tail op forwards; every tail mode the viewer uses.
TAIL_QUERY_KEYS = ("tail_lines", "tail_entries", "before", "before_file",
                   "after_file", "after")


def rewrite_identity(data: dict, address: str) -> dict:
    """Make a far machine's tail response name the session by its Home
    address, so the viewer's store, SSE routing and attachment URLs
    (which go back through ``<name>@<machine>``) all agree."""
    for key in ("session_id", "tmux_session", "tmux_name"):
        if key in data:
            data[key] = address
    for entry in data.get("entries") or []:
        if isinstance(entry, dict) and entry.get("type") == "viewer_attachment":
            entry["session"] = address
    data["machine_address"] = address
    return data


def forward_cursor(data: dict) -> dict | None:
    """Where the next forward read starts: the response's cursor, else the
    end of the newest chain file (a reverse window's position)."""
    cursor = data.get("cursor")
    if isinstance(cursor, dict) and "file" in cursor and "off" in cursor:
        return {"file": str(cursor["file"]), "off": int(cursor["off"])}
    chain = data.get("chain") or []
    if chain and isinstance(data.get("offset"), int):
        return {"file": str(chain[-1]), "off": int(data["offset"])}
    return None


async def fetch_tail(machine: str, name: str, project: str, query: dict,
                     *, timeout: float = 20.0) -> dict:
    """The far machine's tail response, or a refusal record."""
    from tools.dashboard import session_control_client

    reply = await session_control_client.request(
        machine, "tail", {"session_id": name, "project": project, "query": query},
        timeout=timeout, stream=True)
    if not reply.get("ok"):
        return reply
    result = reply.get("result") or {}
    if "file" in result:
        path = Path(result["file"])
        try:
            data = json.loads(await asyncio.to_thread(path.read_bytes))
        finally:
            path.unlink(missing_ok=True)
    else:
        data = result.get("tail")
    if not isinstance(data, dict):
        return {"v": 1, "ok": False, "refusal": "bad-request", "detail": "no tail"}
    return {"v": 1, "ok": True, "tail": data}


@dataclass
class _Watch:
    address: str
    machine: str
    name: str
    project: str
    cursor: dict
    until: float
    seq: int = 0
    task: asyncio.Task | None = field(default=None, repr=False)


class RemoteWatcher:
    """One forward-poll loop per remote session someone is viewing."""

    def __init__(self, event_bus, *, fetch=fetch_tail,
                 interval: float = POLL_INTERVAL_S, ttl: float = WATCH_TTL_S):
        self._bus = event_bus
        self._fetch = fetch
        self._interval = interval
        self._ttl = ttl
        self._watches: dict[str, _Watch] = {}

    def watching(self) -> list[str]:
        return sorted(self._watches)

    def watch(self, address: str, machine: str, name: str, project: str,
              cursor: dict | None) -> None:
        """Start, or keep alive, the watch for *address*."""
        until = time.monotonic() + self._ttl
        existing = self._watches.get(address)
        if existing is not None:
            existing.until = until
            return
        if cursor is None:
            return
        watch = _Watch(address, machine, name, project, cursor, until)
        self._watches[address] = watch
        watch.task = asyncio.get_running_loop().create_task(self._run(watch))

    async def _run(self, watch: _Watch) -> None:
        try:
            while time.monotonic() < watch.until:
                await asyncio.sleep(self._interval)
                if not await self.poll_once(watch):
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("remote view %s: watch ended with an error",
                           watch.address, exc_info=True)
        finally:
            if self._watches.get(watch.address) is watch:
                del self._watches[watch.address]

    async def poll_once(self, watch: _Watch) -> bool:
        """One forward read; False when the session has ended."""
        reply = await self._fetch(
            watch.machine, watch.name, watch.project,
            {"after_file": watch.cursor["file"], "after": str(watch.cursor["off"])})
        if not reply.get("ok"):
            # Unreachable for now: keep the cursor and try again next tick.
            return True
        data = rewrite_identity(reply["tail"], watch.address)
        entries = data.get("entries") or []
        cursor = forward_cursor(data)
        if cursor is not None:
            watch.cursor = cursor
        is_live = bool(data.get("is_live", True))
        if entries:
            watch.seq += 1
            await self._bus.broadcast("session:messages", {
                "session_id": watch.address,
                "entries": entries,
                "is_live": is_live,
                "seq": watch.seq,
                "machine_address": watch.address,
            }, dedup=False)
        return is_live or bool(entries)

    async def stop(self) -> None:
        for watch in list(self._watches.values()):
            if watch.task is not None:
                watch.task.cancel()
        self._watches.clear()
