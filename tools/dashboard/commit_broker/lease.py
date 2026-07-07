"""Force-with-lease pre-flight for brokered publish (DN5 D5-12).

Before any ref update, the broker reads the current remote ref tip and compares
it to the ``expected_ref_sha`` pinned when the operator approved the
force_with_lease (the DN1 lease). A mismatch fails closed with ``ref_advanced``:
no push happens and the workflow routes back to re-approval. The broker enforces
the lease *itself* rather than delegating to raw ``git push --force-with-lease``,
because a raw push cannot prove which ref-update a pack actually contains — the
comparison must be an explicit, observable pre-flight in broker code.

New-ref creation is pre-flighted the other way: the ref must be *absent*, so a
concurrently-created ref can't be silently clobbered.

The git-remote read is injected (``read_remote_tip``) so this logic is a pure,
testable decision with no network in it; the caller supplies the broker's actual
remote-tip reader (an API/ls-remote call).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# Decision reasons — stable strings the publish executor branches on / emits.
LEASE_MATCHES = "lease_matches"
REF_ADVANCED = "ref_advanced"
REF_UNEXPECTEDLY_ABSENT = "ref_unexpectedly_absent"
NEW_REF_OK = "new_ref_absent_ok"
NEW_REF_EXISTS = "new_ref_already_exists"


@dataclass(frozen=True)
class LeaseCheckResult:
    """Outcome of a pre-flight. ``ok`` gates whether the update may proceed;
    ``reason`` is the typed decision; ``observed_tip`` is what the remote showed
    (None if the ref was absent)."""

    ok: bool
    reason: str
    observed_tip: str | None

    @property
    def routes_to_reapproval(self) -> bool:
        """A lease that no longer holds sends the workflow back to re-approval."""
        return self.reason in (REF_ADVANCED, REF_UNEXPECTEDLY_ABSENT)


def check_update_lease(
    *,
    expected_ref_sha: str,
    read_remote_tip: Callable[[], str | None],
) -> LeaseCheckResult:
    """Pre-flight an update to an EXISTING ref against the pinned lease.

    ``expected_ref_sha`` is the tip captured at force_with_lease approval. The
    update proceeds only if the remote tip still equals it exactly; any
    divergence (advanced, rewound, or vanished) fails closed with no push.
    """
    if not expected_ref_sha:
        raise ValueError("expected_ref_sha is required for a lease check")
    observed = read_remote_tip()
    if observed is None:
        # The ref we hold a lease on is gone — do not recreate it under an
        # update lease; that is a different operation and needs re-approval.
        return LeaseCheckResult(False, REF_UNEXPECTEDLY_ABSENT, None)
    if observed != expected_ref_sha:
        return LeaseCheckResult(False, REF_ADVANCED, observed)
    return LeaseCheckResult(True, LEASE_MATCHES, observed)


def check_new_ref(
    *,
    read_remote_tip: Callable[[], str | None],
) -> LeaseCheckResult:
    """Pre-flight creation of a NEW ref: it must be absent.

    A ref that already exists means someone created it since planning; creating
    over it would clobber their work, so this fails closed with
    ``new_ref_already_exists`` and no push.
    """
    observed = read_remote_tip()
    if observed is not None:
        return LeaseCheckResult(False, NEW_REF_EXISTS, observed)
    return LeaseCheckResult(True, NEW_REF_OK, None)
