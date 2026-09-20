"""The compact pull-request map (auto-0my5i, graph://d9153c5a-76e O-I,
detailed design comment f7f9f8d0-646).

A puller sends a floor F and lists an origin only when its cursor is below
F, or when it knows the origin exists but has never seen a transaction of
it. Every other origin it holds is unlisted. The server serves a listed
origin above its listed cursor; an unlisted origin the puller provably
knows above F; any other origin from the beginning. Safety: an unlisted
origin has a cursor at or above F, so serving above F re-serves only rows
the puller holds plus everything above its cursor.

A retired origin leaves the map when the puller's cursor equals its final
position exactly, final being the newest verified cut of that origin the
puller holds; retirement is a roster kick (personal scope) or a machine an
earlier persona cut listed and the newest omits (org scope).
"""
from __future__ import annotations

from typing import Iterable, Mapping

#: A cut younger than this marks its origin as online for the floor.
ONLINE_CUT_AGE_NS = 10 * 60 * 1_000_000_000


def compact_watermark_map(
    watermarks: Mapping[str, int], cuts: Mapping[str, int], now_ns: int, *,
    known: Iterable[str], retired: Mapping[str, int],
) -> tuple[int, dict[str, int]]:
    """-> (floor, exceptions).

    *watermarks*: cursor per origin this store holds. *cuts*: newest
    verified cut per origin. *known*: origins the store knows exist (its
    roster, or the machines of persona cuts it holds); an unseen known
    origin is listed at 0. *retired*: origin -> final position for origins
    the store has established as retired; one whose cursor equals its
    final is unlisted whatever the floor."""
    online = [
        int(cursor) for origin, cursor in watermarks.items()
        if origin in cuts and now_ns - int(cuts[origin]) < ONLINE_CUT_AGE_NS
        and origin not in retired
    ]
    floor = min(online) if online else 0
    exceptions: dict[str, int] = {}
    for origin, cursor in watermarks.items():
        final = retired.get(origin)
        if final is not None and int(cursor) == int(final):
            continue                       # retired and held exactly through its end
        if int(cursor) < floor:
            exceptions[origin] = int(cursor)
    for origin in known:
        if origin not in watermarks:
            exceptions[origin] = 0         # known to exist, never seen: from the start
    return floor, exceptions


def expand_watermark_map(
    listed: Mapping[str, int], floor: int, known: Iterable[str], origins: Iterable[str],
) -> dict[str, int]:
    """The served-from position per origin the server holds, from a
    compact map: listed cursor, else the floor for an origin the puller
    provably knows, else 0."""
    known_set = set(known)
    out: dict[str, int] = {}
    for origin in origins:
        if origin in listed:
            out[origin] = int(listed[origin])
        elif origin in known_set:
            out[origin] = int(floor)
        else:
            out[origin] = 0
    return out
