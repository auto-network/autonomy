"""Publish idempotency decision (DN5 D5-13).

A publish must be safe to retry: a second call after a successful publish is a
no-op that writes no second terminal event and double-counts no SHA. Two guards,
checked in order:

1. **Idempotency key** (DN1) — if a publish with this key already finalized, the
   retry is a duplicate submission and short-circuits. This is the primary guard
   for concurrent/duplicate calls.
2. **Observed ref state** — if the remote ref is already at ``signed_commit_sha``
   the publish already happened, even if its event hasn't been recorded yet (a
   retry racing the first attempt's bookkeeping). Short-circuit here too.

Both the key lookup and the remote-tip read are injected, so the decision is a
pure function with no DB or network in it; the caller wires the real workflow-db
lookup and remote reader.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

PROCEED = "proceed"
NOOP_KEY_ALREADY_FINALIZED = "noop_idempotency_key_finalized"
NOOP_REF_ALREADY_PUBLISHED = "noop_ref_already_at_signed_sha"


@dataclass(frozen=True)
class PublishIdempotencyDecision:
    """Whether to actually run the publish. When ``proceed`` is False the caller
    returns the existing terminal state and writes NO new event or SHA row."""

    proceed: bool
    reason: str

    @property
    def is_noop(self) -> bool:
        return not self.proceed


def decide_publish(
    *,
    idempotency_key: str,
    signed_commit_sha: str,
    key_already_finalized: Callable[[str], bool],
    read_remote_tip: Callable[[], str | None],
) -> PublishIdempotencyDecision:
    """Decide whether a publish should run or short-circuit as a no-op.

    ``key_already_finalized(idempotency_key)`` reports whether a publish under
    this key already reached a terminal event. ``read_remote_tip()`` returns the
    current remote ref tip (or None if absent). The key guard is checked first
    (it covers duplicate/concurrent submissions); the observed-ref guard covers
    a retry whose predecessor's event isn't durably recorded yet.
    """
    if not idempotency_key:
        raise ValueError("idempotency_key is required")
    if not signed_commit_sha:
        raise ValueError("signed_commit_sha is required")
    if key_already_finalized(idempotency_key):
        return PublishIdempotencyDecision(False, NOOP_KEY_ALREADY_FINALIZED)
    if read_remote_tip() == signed_commit_sha:
        return PublishIdempotencyDecision(False, NOOP_REF_ALREADY_PUBLISHED)
    return PublishIdempotencyDecision(True, PROCEED)
