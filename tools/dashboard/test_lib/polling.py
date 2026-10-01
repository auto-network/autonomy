"""Wait for a browser-side condition instead of sleeping a fixed time.

Browser tests used to ``time.sleep(N)`` and then assert. Under a loaded
parallel run N was too short (flaky), and alone it was far too long (slow).
``wait_until`` re-reads the value the test is about to assert on until it
settles, so a test waits exactly as long as the page needs. The caller still
makes the same assertion on the returned value; only the waiting changes.
"""
from __future__ import annotations

import time
from typing import Any, Callable


def wait_until(
    read: Callable[[], Any],
    settled: Callable[[Any], bool],
    timeout: float = 15.0,
    interval: float = 0.25,
) -> Any:
    """Call *read* until ``settled(value)`` holds or *timeout* passes.

    Returns the last value read, settled or not, for the caller to assert on.
    A predicate that raises counts as not settled.
    """
    deadline = time.monotonic() + timeout
    value = read()
    while not _holds(settled, value) and time.monotonic() < deadline:
        time.sleep(interval)
        value = read()
    return value


def _holds(predicate: Callable[[Any], bool], value: Any) -> bool:
    try:
        return bool(predicate(value))
    except Exception:
        return False
