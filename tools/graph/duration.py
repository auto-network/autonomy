"""Shared duration parser for `--since` / time-window flags.

Accepts strings like `10m`, `2h`, `12h`, `3d`, `1w`. Used by:

  * `graph` CLI commands (`notes --since`, `crosstalk --since`, etc.)
  * `tools/dashboard/dao/sessions.py` (`get_recent_sessions` since filter)
  * Future `graph sessions --status --since` (auto-0r86).
"""

from __future__ import annotations

import re

_MULTIPLIERS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(s: str) -> float:
    """Parse a duration string like '1h', '30m', '2d', '1w' to seconds.

    Raises ValueError on malformed input.
    """
    m = re.match(r"^(\d+)\s*([smhdw])$", s.strip())
    if not m:
        raise ValueError(f"Invalid duration: {s!r}. Use e.g. 1h, 30m, 2d, 1w")
    val, unit = int(m.group(1)), m.group(2)
    return val * _MULTIPLIERS[unit]
