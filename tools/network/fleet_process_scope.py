"""The scopes a fleet runtime's process delegation may carry.

Arming a fleet runtime mints ONE delegation, machine key -> process key
(fleet-enrollment.js ``mintRuntimeCredential``), whose scope is
``["fleet:sync", "session:control"]`` (graph://7eb29bc8-31a §6.3): one
certificate, one scope per purpose. Every verifier asks the same question of
it -- is the scope I need present, and nothing outside the process set? --
so a certificate armed before ``session:control`` existed (scope
``["fleet:sync"]``) still syncs and is refused only for session control.
"""

from __future__ import annotations

from collections.abc import Iterable

FLEET_SYNC_SCOPE = "fleet:sync"
#: The standing grant to drive sessions on another fleet machine.
SESSION_CONTROL_SCOPE = "session:control"
#: Everything a process delegation may carry. fleet:sync is always present.
PROCESS_SCOPES = frozenset({FLEET_SYNC_SCOPE, SESSION_CONTROL_SCOPE})

#: ``scope_problem`` results.
SCOPE_MISSING = "missing"
SCOPE_EXCESS = "excess"


def scope_problem(scope: Iterable[str], required: str) -> str | None:
    """None when *scope* carries *required* and nothing outside
    PROCESS_SCOPES; else SCOPE_MISSING or SCOPE_EXCESS (excess wins, since a
    certificate with foreign authority is refused whatever it also carries)."""
    held = set(scope)
    if not held <= PROCESS_SCOPES:
        return SCOPE_EXCESS
    if required not in held:
        return SCOPE_MISSING
    return None
