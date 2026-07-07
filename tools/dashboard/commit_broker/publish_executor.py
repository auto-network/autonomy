"""Brokered publish executor orchestration (DN5 D5-11).

The publish handler records a ``published`` event, but the actual host-side push
lives here. ``execute_publish`` orchestrates the already-landed pieces in order,
fail-closed at each gate, and pushes the signed object from the TRUSTED STORE
(never an agent worktree) using a credential resolved through the redaction seam:

  1. resolve the publish mode from the frozen plan — ``local_only`` is a no-op.
  2. idempotency (D5-13): a retry after success short-circuits to a no-op.
  3. force-with-lease pre-flight (D5-12): the remote ref must still match the
     pinned lease (or be absent, for a new ref) or nothing is pushed.
  4. credential (D5-10): resolved for the server-resolved scope only; an
     out-of-scope repo is denied before any push.
  5. push the signed object + needed objects from the trusted store via the
     injected pusher, which receives the credential and never logs it.

The remote tip is read exactly ONCE and reused for both the idempotency and
lease gates, so there is no TOCTOU between them (per D5-12/D5-13 review). Git and
network are injected (``read_remote_tip``, ``push_objects``) so this is pure
orchestration; the caller MUST run it inside one serialized scope (the handler's
BEGIN IMMEDIATE idempotency transaction) so two concurrent retries can't both
pass the gates.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from tools.dashboard.commit_api.types import PUBLISH_MODES
from tools.dashboard.commit_broker.credentials import AuthorizedScope, Credential
from tools.dashboard.commit_broker.idempotency import decide_publish
from tools.dashboard.commit_broker.lease import (
    NEW_REF_EXISTS,
    check_new_ref,
    check_update_lease,
)

# Mode strings MUST match the API's frozen PUBLISH_MODES exactly — the executor
# gates the plan the real commit.publish request froze. Aliased to the frozen
# values (not re-invented) so a drift is impossible; the anti-drift test asserts
# these constants cover PUBLISH_MODES.
MODE_LOCAL_ONLY = "local_only_noop"
MODE_WORKSPACE_SHARED = "workspace_shared"
MODE_ORIGIN_PUSH = "origin_push"
MODE_DIRECT_TARGET = "direct_target_update"
MODE_PR_BRANCH = "pr_branch_push"

# Every frozen mode except the local-only no-op actually pushes.
_PUSH_MODES = PUBLISH_MODES - {MODE_LOCAL_ONLY}

# Outcomes — stable strings the handler maps to workflow events.
PUSHED = "pushed"
SKIPPED_LOCAL_ONLY = "skipped_local_only"
NOOP_ALREADY_PUBLISHED = "noop_already_published"
REF_ADVANCED = "ref_advanced"
NEW_REF_ALREADY_EXISTS = "new_ref_already_exists"
OUT_OF_SCOPE = "out_of_scope"


@dataclass(frozen=True)
class PublishResult:
    outcome: str
    reason: str
    pushed: bool

    @property
    def routes_to_reapproval(self) -> bool:
        # A lease that no longer holds sends the workflow back to re-approval.
        # A new-ref collision is a distinct 'someone created it' state, NOT a
        # lease-reapproval (per D5-12 review) — so it is excluded here.
        return self.outcome == REF_ADVANCED


def resolve_publish_mode(resolved_plan: dict) -> str:
    """Read the publish mode from the frozen resolved plan.

    The mode is whatever the plan froze at approval time; this never re-derives
    it from live request data.
    """
    mode = resolved_plan.get("publish_mode") or resolved_plan.get("mode")
    if not mode:
        raise ValueError("resolved_plan carries no publish mode")
    return str(mode)


def execute_publish(
    *,
    resolved_plan: dict,
    target_repo: str,
    target_ref: str,
    signed_commit_sha: str,
    expected_ref_sha: str | None,
    is_new_ref: bool,
    idempotency_key: str,
    key_already_finalized: Callable[[str], bool],
    authorized_scope: AuthorizedScope,
    credential_provider,
    read_remote_tip: Callable[[], str | None],
    push_objects: Callable[..., None],
    provider: str = "github",
) -> PublishResult:
    """Run the brokered publish. Returns a typed result; performs the push only
    when every gate passes. Must be called inside the handler's serialized
    idempotency scope."""
    mode = resolve_publish_mode(resolved_plan)
    if mode == MODE_LOCAL_ONLY:
        return PublishResult(SKIPPED_LOCAL_ONLY, "publish is local-only by policy", False)
    if mode not in _PUSH_MODES:
        raise ValueError(f"unknown publish mode {mode!r}")

    # Read the remote tip ONCE and reuse it for both gates (no TOCTOU between
    # the idempotency check and the lease check).
    observed_tip = read_remote_tip()
    _cached_tip: Callable[[], str | None] = lambda: observed_tip

    idem = decide_publish(
        idempotency_key=idempotency_key,
        signed_commit_sha=signed_commit_sha,
        key_already_finalized=key_already_finalized,
        read_remote_tip=_cached_tip,
    )
    if idem.is_noop:
        return PublishResult(NOOP_ALREADY_PUBLISHED, idem.reason, False)

    if is_new_ref:
        lease = check_new_ref(read_remote_tip=_cached_tip)
    else:
        if not expected_ref_sha:
            raise ValueError("expected_ref_sha is required to update an existing ref")
        lease = check_update_lease(expected_ref_sha=expected_ref_sha, read_remote_tip=_cached_tip)
    if not lease.ok:
        outcome = NEW_REF_ALREADY_EXISTS if lease.reason == NEW_REF_EXISTS else REF_ADVANCED
        return PublishResult(outcome, f"lease pre-flight failed: {lease.reason}", False)

    # Credential is resolved for the server-resolved scope only; an out-of-scope
    # repo is denied BEFORE any push.
    if not authorized_scope.permits(target_repo):
        return PublishResult(OUT_OF_SCOPE, f"repo {target_repo!r} outside authorized scope", False)
    credential: Credential = credential_provider.get_real_credential(provider, authorized_scope)

    # Push the signed object + needed objects from the trusted store. The pusher
    # receives the Credential (redaction-wrapped) and must pass its secret via
    # env/stdin, never argv, and never log it.
    #
    # HARD REQUIREMENT for the real pusher (D5-11 review): the lease pre-flight
    # above closes the TOCTOU BETWEEN the two gates, but NOT the window between
    # this pre-flight and the network push. The pusher MUST re-enforce the lease
    # atomically at the ref update itself — a compare-and-swap ref update keyed to
    # expected_ref_sha (e.g. the provider's update-ref API with an expected-SHA
    # precondition), never a raw `git push --force-with-lease`, which cannot prove
    # which ref-update a pack contains (R1/R2, see lease.py). expected_ref_sha and
    # is_new_ref are passed through for exactly that atomic precondition.
    push_objects(
        signed_commit_sha=signed_commit_sha,
        target_ref=target_ref,
        expected_ref_sha=expected_ref_sha,
        is_new_ref=is_new_ref,
        credential=credential,
    )
    return PublishResult(PUSHED, "published to remote", True)
