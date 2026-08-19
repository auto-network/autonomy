"""A revision's schema class may not be deleted while rows can still exist at it.

A stored row at a superseded revision is not an error anyone sees: a
revision-targeted read DROPS it and the caller gets ABSENCE. So deleting the
class that describes an old revision does not raise -- it silently turns every
row still at that revision into nothing.

That is not hypothetical. ``autonomy.network.serve-cert`` revision 1 was
deleted when revision 2 landed. Two organizations still held revision-1 rows,
which then read as ``missing``: serving was gated off for both, renewal could
not see them, and their certificates ran to the edge of expiry with no error
anywhere. The symptom surfaced weeks later, and named the wrong cause.

Whether a revision can UPCONVERT is a separate and legitimately optional
question -- ``org-key`` cannot without that organization's passphrase, and
``secure.setting`` cannot without the plaintext. Both are fine, because both
revisions stay registered and a row at either still parses.
"""

from __future__ import annotations

import pytest

from tools.graph import schemas
from tools.graph.schemas import registry as reg

#: A revision may be absent ONLY when no store holds a row at it. That is a
#: fact about live data, which this test cannot see, so each entry records the
#: audit that justified it. Re-run the audit before adding one.
RETIRED_REVISIONS: dict[str, dict[int, str]] = {
    "autonomy.network.org-key": {
        1: (
            "Retired 2026-08-19 with armor V1 (auto-wx94n). Revision 1 held "
            "the org root under a passphrase armor of its own; revision 2 "
            "seals it to the owner's personal root, and no conversion exists "
            "or could -- converting needs that organization's passphrase, "
            "which nothing holds. Audited before deleting the class, not "
            "after: every set of every store enumerated over the settings "
            "API -- 622 rows across 164 set reads in six stores (anchore, "
            "autonomy, blindhash, dynbench, machine, personal) -- yielded "
            "exactly ONE armor blob in the estate, the operator's personal "
            "root, at revision 2. Zero rows at org-key revision 1 anywhere."
        ),
    },
    "autonomy.network.serve-cert": {
        1: (
            "Retired 2026-08-17. Revision 2 added the identity-neutral viewer "
            "certificate and no conversion exists -- an invalid row is freshly "
            "minted, never converted. Verified zero rows at revision 1 across "
            "every store after all three organizations re-minted."
        ),
    },
}

MAX_REVISION_PROBE = 12


def _registered_revisions(set_id: str) -> list[int]:
    revisions = []
    for revision in range(1, MAX_REVISION_PROBE + 1):
        try:
            if reg.get_schema(set_id, revision) is not None:
                revisions.append(revision)
        except Exception:
            continue
    return revisions


@pytest.mark.parametrize("set_id", sorted(schemas.list_registered_set_ids()))
def test_every_revision_up_to_the_current_one_is_still_registered(set_id):
    """No gaps below the current revision, so a stored row always parses."""
    revisions = _registered_revisions(set_id)
    if not revisions:
        pytest.skip(f"{set_id} registers no revision this probe can see")
    current = max(revisions)
    retired = RETIRED_REVISIONS.get(set_id, {})
    missing = [
        revision
        for revision in range(1, current + 1)
        if revision not in revisions and revision not in retired
    ]
    assert not missing, (
        f"{set_id} has no schema for revision(s) {missing}, but declares "
        f"revision {current}. A row stored at a missing revision does not "
        f"raise -- it reads as ABSENT, which turns the capability behind it "
        f"off silently. Either keep the class, or verify against LIVE data "
        f"that no store holds a row at that revision and record the audit in "
        f"RETIRED_REVISIONS."
    )


def test_a_retired_revision_records_the_audit_that_justified_it():
    """A revision may only be dropped on evidence, and the evidence is a
    statement about live data that no test can re-derive later."""
    for set_id, retired in RETIRED_REVISIONS.items():
        assert set_id in set(schemas.list_registered_set_ids()), (
            f"{set_id} is listed as having a retired revision but is not a "
            f"registered set -- the entry is stale"
        )
        for revision, reason in retired.items():
            assert isinstance(reason, str) and len(reason) > 60, (
                f"{set_id} revision {revision} is retired without recording "
                f"the audit that justified it"
            )
            assert "zero rows" in reason.lower(), (
                f"{set_id} revision {revision}: the justification must state "
                f"that zero rows were found at that revision across every "
                f"store, which is the only thing that makes deletion safe"
            )
