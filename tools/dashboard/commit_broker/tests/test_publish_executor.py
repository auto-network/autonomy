"""D5-11 tests — the executor gates fail closed and push only when all pass."""

from __future__ import annotations

import pytest

from tools.dashboard.commit_broker.credentials import (
    AuthorizedScope,
    InMemoryCredentialProvider,
)
from tools.dashboard.commit_broker.publish_executor import (
    MODE_LOCAL_ONLY,
    MODE_ORIGIN_PUSH,
    NEW_REF_ALREADY_EXISTS,
    NOOP_ALREADY_PUBLISHED,
    OUT_OF_SCOPE,
    PUSH_FAILED,
    PUSHED,
    REF_ADVANCED,
    SKIPPED_LOCAL_ONLY,
    execute_publish,
    resolve_publish_mode,
)

SIGNED = "s" * 40
LEASE = "a" * 40
REPO = "auto-network/autonomy"


class _Pusher:
    def __init__(self):
        self.calls = []

    def __call__(self, *, signed_commit_sha, target_ref, expected_ref_sha, is_new_ref, credential):
        # The pusher receives a redaction-wrapped Credential plus the lease inputs
        # (expected_ref_sha/is_new_ref) it must enforce atomically at the push;
        # record that a push happened and that the secret is reachable via reveal().
        self.calls.append((signed_commit_sha, target_ref, expected_ref_sha, is_new_ref, credential.reveal()))


class _FailedPusher:
    def __init__(self, reason="simulated push failure"):
        self.reason = reason
        self.calls = []

    def __call__(self, *, signed_commit_sha, target_ref, expected_ref_sha, is_new_ref, credential):
        self.calls.append((signed_commit_sha, target_ref, expected_ref_sha, is_new_ref, credential.reveal()))
        from tools.dashboard.commit_broker.pusher import PushOutcome

        return PushOutcome(False, self.reason)


def _scope(repo=REPO):
    return AuthorizedScope.for_repos("op-1", [repo])


def _provider():
    p = InMemoryCredentialProvider()
    p.set_secret("github", "ghp_secret_xyz")
    return p


def _run(*, mode=MODE_ORIGIN_PUSH, tip=LEASE, expected=LEASE, is_new=False,
         finalized=False, scope=None, pusher=None):
    return execute_publish(
        resolved_plan={"publish_mode": mode},
        target_repo=REPO,
        target_ref="refs/heads/main",
        signed_commit_sha=SIGNED,
        expected_ref_sha=expected,
        is_new_ref=is_new,
        idempotency_key="k1",
        key_already_finalized=lambda k: finalized,
        authorized_scope=scope or _scope(),
        credential_provider=_provider(),
        read_remote_tip=lambda: tip,
        push_objects=pusher or _Pusher(),
    )


def test_local_only_is_a_noop_no_push():
    pusher = _Pusher()
    r = _run(mode=MODE_LOCAL_ONLY, pusher=pusher)
    assert r.outcome == SKIPPED_LOCAL_ONLY
    assert r.pushed is False
    assert pusher.calls == []


def test_happy_path_pushes_with_the_credential():
    pusher = _Pusher()
    r = _run(tip=LEASE, expected=LEASE, pusher=pusher)
    assert r.outcome == PUSHED
    assert r.pushed is True
    assert len(pusher.calls) == 1
    assert pusher.calls[0][-1] == "ghp_secret_xyz"  # secret reached the pusher via reveal()
    assert pusher.calls[0][2] == LEASE  # expected_ref_sha passed through for atomic enforcement


def test_lease_advanced_blocks_push_and_reapproves():
    pusher = _Pusher()
    r = _run(tip="b" * 40, expected=LEASE, pusher=pusher)  # remote moved off the lease
    assert r.outcome == REF_ADVANCED
    assert r.pushed is False
    assert r.routes_to_reapproval is True
    assert pusher.calls == []


def test_new_ref_collision_is_distinct_from_reapproval():
    pusher = _Pusher()
    r = _run(is_new=True, tip="c" * 40, pusher=pusher)  # ref already exists
    assert r.outcome == NEW_REF_ALREADY_EXISTS
    assert r.pushed is False
    assert r.routes_to_reapproval is False  # NOT a lease-reapproval (D5-12 review note)
    assert pusher.calls == []


def test_already_published_retry_is_noop():
    pusher = _Pusher()
    r = _run(finalized=True, pusher=pusher)
    assert r.outcome == NOOP_ALREADY_PUBLISHED
    assert r.pushed is False
    assert pusher.calls == []


def test_ref_already_at_signed_sha_is_noop():
    pusher = _Pusher()
    r = _run(tip=SIGNED, expected=LEASE, pusher=pusher)  # remote already at signed sha
    assert r.outcome == NOOP_ALREADY_PUBLISHED
    assert pusher.calls == []


def test_out_of_scope_repo_denied_before_push():
    pusher = _Pusher()
    r = _run(scope=AuthorizedScope.for_repos("op-1", ["auto-network/other"]), pusher=pusher)
    assert r.outcome == OUT_OF_SCOPE
    assert r.pushed is False
    assert pusher.calls == []


def test_push_failure_propagates_out_of_executor():
    pusher = _FailedPusher("simulated push failure")
    r = _run(pusher=pusher)
    assert r.outcome == PUSH_FAILED
    assert r.pushed is False
    assert r.reason == "simulated push failure"
    assert len(pusher.calls) == 1


def test_gates_run_before_credential_is_even_resolved():
    # A lease failure must short-circuit before any credential is touched.
    class _Boom(InMemoryCredentialProvider):
        def get_real_credential(self, *a, **k):
            raise AssertionError("credential resolved despite a failed lease gate")

    r = execute_publish(
        resolved_plan={"publish_mode": MODE_ORIGIN_PUSH},
        target_repo=REPO, target_ref="refs/heads/main", signed_commit_sha=SIGNED,
        expected_ref_sha=LEASE, is_new_ref=False, idempotency_key="k1",
        key_already_finalized=lambda k: False, authorized_scope=_scope(),
        credential_provider=_Boom(), read_remote_tip=lambda: "b" * 40,
        push_objects=_Pusher(),
    )
    assert r.outcome == REF_ADVANCED


def test_remote_tip_is_read_exactly_once_across_both_gates():
    # Proves the atomicity claim: the idempotency gate and the lease gate reuse a
    # single remote read, so there is no TOCTOU between them (mirrors D5-12's own
    # call-count test; suggested in Sonnet's D5-11 review).
    reads = []

    def counting_reader():
        reads.append(1)
        return LEASE

    pusher = _Pusher()
    execute_publish(
        resolved_plan={"publish_mode": MODE_ORIGIN_PUSH},
        target_repo=REPO, target_ref="refs/heads/main", signed_commit_sha=SIGNED,
        expected_ref_sha=LEASE, is_new_ref=False, idempotency_key="k1",
        key_already_finalized=lambda k: False, authorized_scope=_scope(),
        credential_provider=_provider(), read_remote_tip=counting_reader,
        push_objects=pusher,
    )
    assert reads == [1]  # exactly one read, reused for idempotency + lease
    assert len(pusher.calls) == 1


def test_modes_exactly_match_the_api_frozen_contract():
    # Anti-drift guard: the executor's mode constants MUST equal the API's frozen
    # PUBLISH_MODES exactly. This would have caught the original contract break
    # (local_only vs local_only_noop, direct_target vs direct_target_update,
    # pr_branch vs pr_branch_push).
    from tools.dashboard.commit_api.types import PUBLISH_MODES
    from tools.dashboard.commit_broker.publish_executor import (
        MODE_DIRECT_TARGET,
        MODE_LOCAL_ONLY,
        MODE_ORIGIN_PUSH,
        MODE_PR_BRANCH,
        MODE_WORKSPACE_SHARED,
    )

    assert {
        MODE_LOCAL_ONLY, MODE_WORKSPACE_SHARED, MODE_ORIGIN_PUSH,
        MODE_DIRECT_TARGET, MODE_PR_BRANCH,
    } == PUBLISH_MODES


def test_every_pushing_api_mode_reaches_the_pusher():
    # Each non-noop frozen mode must drive an actual push (not ValueError).
    from tools.dashboard.commit_api.types import PUBLISH_MODES
    for mode in PUBLISH_MODES - {MODE_LOCAL_ONLY}:
        pusher = _Pusher()
        r = _run(mode=mode, pusher=pusher)
        assert r.outcome == PUSHED, f"mode {mode} did not push"
        assert len(pusher.calls) == 1


def test_resolve_mode_requires_a_mode():
    with pytest.raises(ValueError):
        resolve_publish_mode({})


def test_update_requires_expected_ref_sha():
    with pytest.raises(ValueError):
        _run(expected=None, is_new=False)
