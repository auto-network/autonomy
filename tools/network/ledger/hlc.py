"""Hybrid logical clocks — ordering *hints*, never authority.

Causality in the ledger is carried entirely by parent hashes; the HLC exists
for human-facing ordering and for advisory expiry checks (invite expiry,
delegation TTL). Wire form is ``[ts_ms, count]``. The one hard rule — an
event's HLC must strictly exceed every parent's (enforced structurally at
append) — keeps HLCs monotone along causal paths so an author cannot
backdate an event below anything it has already seen.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import MalformedEventError

_MAX_TS = 2**63 - 1


@dataclass(frozen=True, order=True)
class HLC:
    ts: int
    count: int = 0

    def __post_init__(self):
        for name, value in (("ts", self.ts), ("count", self.count)):
            # bool is an int subclass; reject it explicitly.
            if type(value) is not int or value < 0 or value > _MAX_TS:
                raise MalformedEventError(f"hlc.{name} must be an integer in [0, 2**63)")

    def to_list(self) -> list:
        return [self.ts, self.count]

    @classmethod
    def from_value(cls, value: object) -> "HLC":
        if not isinstance(value, list) or len(value) != 2:
            raise MalformedEventError("hlc must be a two-element list [ts, count]")
        return cls(ts=value[0], count=value[1])

    def tick(self, physical_ts: int) -> "HLC":
        """The next HLC for the same node given wall-clock *physical_ts*."""
        if physical_ts > self.ts:
            return HLC(physical_ts, 0)
        return HLC(self.ts, self.count + 1)

    def observe(self, other: "HLC", physical_ts: int) -> "HLC":
        """The next HLC after receiving *other* (classic HLC merge)."""
        ts = max(self.ts, other.ts, physical_ts)
        if ts == self.ts == other.ts:
            count = max(self.count, other.count) + 1
        elif ts == self.ts:
            count = self.count + 1
        elif ts == other.ts:
            count = other.count + 1
        else:
            count = 0
        return HLC(ts, count)
