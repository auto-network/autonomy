"""The real push step for brokered publish — the push_objects implementation.

The publish executor decides *whether* to push (mode, idempotency, lease pre-flight,
scope-checked credential). This module does the push itself: it materializes the
signed commit and the objects it needs into a git repository FROM THE BROKER'S
FROZEN BYTES (never live agent git state), then updates the target ref with an
atomic compare-and-swap so a branch that moved since the pre-flight cannot be
silently overwritten.

For the end-to-end test the target is a local bare repository on disk, and the
compare-and-swap is git's own ``--force-with-lease`` (git checks the remote ref
is still at the expected value at update time, on the remote side). Against a real
provider the same shape uses that provider's atomic update-ref API with an
expected-SHA precondition; the safety property is identical.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

ZERO_OID = "0" * 40


class MaterializeError(Exception):
    """A frozen object could not be written into the staging repository."""


@dataclass(frozen=True)
class PushOutcome:
    ok: bool
    reason: str
    pushed_sha: str | None = None


def _git(repo_dir: str, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo_dir, *args],
        input=stdin,
        capture_output=True,
    )


def materialize_objects(
    *, repo_dir: str, objects: Sequence[tuple[str, bytes]]
) -> list[str]:
    """Write ``(git_type, raw_bytes)`` objects into ``repo_dir``'s object store.

    ``objects`` are the frozen bytes read from the trusted store — blobs, trees,
    then the signed commit, in dependency order. Returns the git SHA of each
    object as git computed it (which must equal the frozen identity).
    """
    written: list[str] = []
    for git_type, data in objects:
        result = _git(repo_dir, "hash-object", "-w", "-t", git_type, "--stdin", stdin=data)
        if result.returncode != 0:
            raise MaterializeError(
                f"could not write {git_type} object: {result.stderr.decode().strip()}"
            )
        written.append(result.stdout.decode().strip())
    return written


def push_signed_commit(
    *,
    repo_dir: str,
    remote: str,
    signed_commit_sha: str,
    target_ref: str,
    expected_ref_sha: str | None,
    is_new_ref: bool,
) -> PushOutcome:
    """Push ``signed_commit_sha`` to ``target_ref`` on ``remote`` with an atomic
    compare-and-swap.

    The update lands only if the remote ref is still exactly where the pre-flight
    saw it: ``expected_ref_sha`` for an existing ref, or absent for a new ref.
    Any divergence is rejected by git at update time with no change to the remote
    (fail closed). ``repo_dir`` must already contain the object (materialized
    from the frozen bytes).
    """
    if is_new_ref:
        # Expect the ref to not exist yet: lease against the zero OID.
        lease = f"{target_ref}:{ZERO_OID}"
    else:
        if not expected_ref_sha:
            return PushOutcome(False, "expected_ref_sha required to update an existing ref")
        lease = f"{target_ref}:{expected_ref_sha}"
    result = _git(
        repo_dir,
        "push",
        f"--force-with-lease={lease}",
        remote,
        f"{signed_commit_sha}:{target_ref}",
    )
    if result.returncode != 0:
        return PushOutcome(False, f"ref update rejected: {result.stderr.decode().strip()}")
    return PushOutcome(True, "pushed", signed_commit_sha)


def read_remote_tip(*, repo_dir: str, remote: str, target_ref: str) -> str | None:
    """Return the remote's current tip for ``target_ref`` (or None if absent).

    This is the reader the publish executor's lease/idempotency gates use — a
    real ``git ls-remote`` against the target, with no side effects.
    """
    result = _git(repo_dir, "ls-remote", remote, target_ref)
    if result.returncode != 0:
        return None
    out = result.stdout.decode().strip()
    if not out:
        return None
    return out.split()[0]


def build_chain_pusher(
    *,
    store,
    snapshot_dao,
    dao_conn,
    links: Sequence[tuple[str, str]],
    remote: str,
    staging_dir: str,
):
    """Build the ``push_objects`` callable for publishing a governed-rewrite
    CHAIN (a single commit is just a 1-element chain).

    ``links`` is the ordered ``(snapshot_ref, signed_object_sha256)`` for every
    link, root-to-tip. Every link's tree/blob objects AND its signed commit are
    materialized into the staging repo IN ORDER, so each link's parent (the
    previous link, or the seeded base) is present before the tip is pushed. Only
    the tip's signed commit is pushed to the target ref; its whole ancestry
    travels with it in the same pack. All bytes come FROM THE TRUSTED STORE
    (frozen bytes, never live git state).

    For an existing-ref update the base commit (the first link's parent) is
    seeded from the remote so the chain connects to real history.
    """
    if not links:
        raise ValueError("links must be a non-empty ordered list of (snapshot_ref, signed_object_sha256)")
    subprocess.run(["git", "init", "-q", staging_dir], capture_output=True, check=True)

    def push_objects(*, signed_commit_sha, target_ref, expected_ref_sha, is_new_ref, credential=None):
        if not is_new_ref:
            if not expected_ref_sha:
                return PushOutcome(False, "expected_ref_sha required to update an existing ref")
            seed = _git(staging_dir, "fetch", "--no-tags", remote, target_ref)
            if seed.returncode != 0:
                return PushOutcome(
                    False,
                    f"could not seed staging repo from remote tip: {seed.stderr.decode().strip()}",
                )
        objects: list[tuple[str, bytes]] = []
        for snapshot_ref, signed_digest in links:
            # Tree + blobs backing this link, from its frozen snapshot.
            for entry in snapshot_dao.list_entries(dao_conn, snapshot_ref):
                if entry["object_type"] == "commit":
                    continue  # the unsigned commit is not what we publish
                objects.append((entry["object_type"], store.get(entry["object_sha256"])))
            # This link's signed commit, from its stored bytes.
            objects.append(("commit", store.get(signed_digest)))
        materialize_objects(repo_dir=staging_dir, objects=objects)
        return push_signed_commit(
            repo_dir=staging_dir,
            remote=remote,
            signed_commit_sha=signed_commit_sha,
            target_ref=target_ref,
            expected_ref_sha=expected_ref_sha,
            is_new_ref=is_new_ref,
        )

    return push_objects


def build_trusted_store_pusher(
    *,
    store,
    snapshot_dao,
    dao_conn,
    snapshot_ref: str,
    signed_object_sha256: str,
    remote: str,
    staging_dir: str,
):
    """Single-commit publish — a 1-element chain. Backward-compatible wrapper
    over :func:`build_chain_pusher` so the publish handler's existing
    single-commit path is unchanged. ``snapshot_ref`` backs the commit's
    tree/blobs; ``signed_object_sha256`` is the assembled signed commit bytes
    stored at attach time.
    """
    return build_chain_pusher(
        store=store,
        snapshot_dao=snapshot_dao,
        dao_conn=dao_conn,
        links=[(snapshot_ref, signed_object_sha256)],
        remote=remote,
        staging_dir=staging_dir,
    )
