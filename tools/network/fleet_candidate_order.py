"""Order a peer's dial candidates by locally recorded evidence.

The dialing machine cannot tell from an address whether a peer can be
reached at it: address shape does not establish it (container subnet
collision is the expected case but not an invariant), range lists do not
(each host's compose subnet comes from a per-host preflight), and
interface names do not (``_ip_command_addresses`` returns nothing when
the ``ip`` binary is absent, which is the case in these containers). The
only evidence that an address works is that this machine already reached
the peer on it.

``fleet_sync_telemetry`` already records that evidence per peer, so this
module reads it and reorders; it stores nothing of its own.

Scope boundary: this module decides ORDER only. Attempting candidates,
concurrency, racing the relay, cancellation, choosing a winner and
closing losing channels all belong to the dial loop in
``fleet_sync_scheduler`` and are not touched here.

One imprecision is deliberate and bounded. ``record_iteration`` advances
``last_success_at_ns`` on every successful iteration, but only rewrites
``last_success_address`` when that iteration also carried a
``path_class``. So a row's success timestamp can be newer than the
address it is paired with, and the freshness test below can overestimate
how recently that address worked. The cost is one wasted attempt against
an address that is tried first and then falls through to the next
candidate, which is why this is a hint that can only reorder and never
exclude. A handshake-level record would remove the imprecision and is
separate work.

Note also that ``last_success_address`` marks a completed sync
iteration, which is a stronger fact than a completed handshake and a
rarer one: an address that authenticated and then failed the pull for an
unrelated reason leaves no record. Absence therefore carries no
information and is never treated as a negative signal.
"""

from __future__ import annotations

from typing import Callable, Iterable, Sequence

#: Ignore a recorded address older than this for ordering purposes. The
#: record is never deleted; it simply stops being promoted, so an address
#: that has stopped working is not tried first indefinitely. PROPOSED
#: VALUE, NOT APPROVED.
DEFAULT_MAX_AGE_NS = 24 * 60 * 60 * 1_000_000_000


def _best_recorded_address(rows: Iterable, peer_machine_pub: str):
    """The most recently recorded successful address for one peer.

    Returns ``(address, recorded_at_ns)`` or ``None``.
    """
    best: tuple[str, int] | None = None
    for row in rows:
        if not isinstance(row, dict) or row.get("peer") != peer_machine_pub:
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        address = payload.get("last_success_address")
        if not isinstance(address, str) or not address:
            continue
        try:
            at_ns = int(payload.get("last_success_at_ns") or 0)
        except (TypeError, ValueError):
            continue
        if at_ns <= 0:
            continue
        if best is None or at_ns > best[1]:
            best = (address, at_ns)
    return best


def order_candidates(
    peer_machine_pub: str,
    addresses: Sequence[str],
    *,
    org: str = "machine",
    now_ns: int | None = None,
    max_age_ns: int = DEFAULT_MAX_AGE_NS,
    read_rows: Callable[..., list] | None = None,
) -> list[str]:
    """Return ``addresses`` with a known-good address moved to the front.

    The returned list always holds exactly the input entries, including
    any duplicates: only the first occurrence of the promoted address
    moves, so this cannot make a peer unreachable or shorten the list,
    only change the order in which candidates are tried. The relative order of every other candidate is
    preserved, so the existing tailnet-before-private ranking still
    holds among them.

    Returns the input unchanged when there is no usable record, when the
    recorded address is no longer a candidate, when the record is older
    than ``max_age_ns``, when it is dated ahead of the clock, or when the
    telemetry read fails for any reason. Ordering must never be able to
    break dialing.
    """
    candidates = list(addresses or ())
    if len(candidates) < 2 or not peer_machine_pub:
        return candidates

    if read_rows is None:
        from tools.network import fleet_sync_telemetry

        read_rows = fleet_sync_telemetry.read_channel_rows

    try:
        rows = read_rows(org=org)
        best = _best_recorded_address(rows, peer_machine_pub)
    except Exception:
        # A telemetry failure must degrade to the caller's existing
        # order, never to a raised exception in the dial path.
        return candidates

    if best is None:
        return candidates

    address, at_ns = best
    if address not in candidates:
        return candidates

    if now_ns is None:
        import time

        now_ns = time.time_ns()
    age_ns = now_ns - at_ns
    # A record dated ahead of the clock is stale, not fresh. Treating a
    # negative age as "recent" would hold a promoted address past the
    # window forever after a clock jump. The value comes from this
    # machine's own telemetry, so this is a local clock defence, not an
    # untrusted-input one.
    if age_ns < 0:
        return candidates
    if max_age_ns > 0 and age_ns > max_age_ns:
        return candidates

    promoted = list(candidates)
    promoted.remove(address)  # first occurrence only; length is preserved
    return [address] + promoted
