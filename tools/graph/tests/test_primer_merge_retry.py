"""L1 test for the MERGE RETRY primer banner (auto-je5rv item 2).

A MERGE_FAILED retry agent's prompt must make it unmistakable that the
implementation is already committed and the only task is a rebase + re-verify
+ re-submit — and that an empty diff vs master after the rebase means the work
already merged, so it should record DONE and STOP rather than re-implement.
"""

from __future__ import annotations

from tools.graph.primer import format_for_agent


def _data_with_merge_retry():
    return {
        "bead_id": "auto-mr1",
        "bead": {
            "title": "Wire the thing",
            "priority": 1,
            "status": "open",
            "description": "Do the thing.",
            "acceptance_criteria": "",
            "design": "",
            "comments": [],
            "notes": "",
        },
        "provenance": [],
        "related_notes": [],
        "pitfalls": [],
        "related_beads": [],
        "merge_retry": {
            "branch": "agent/auto-mr1",
            "commit": "deadbeef",
            "merge_error": "CONFLICT in server.py",
        },
    }


def test_merge_retry_banner_says_already_done_rebase_only():
    out = format_for_agent(_data_with_merge_retry())
    assert "MERGE RETRY" in out
    # Already implemented — do not re-implement.
    assert "already" in out.lower()
    assert "re-implement" in out.lower() or "rewrite" in out.lower()
    # The exact previous commit to replay.
    assert "deadbeef" in out
    # Empty-diff-after-rebase => already merged => record DONE and STOP.
    assert "empty" in out.lower()
    assert "DONE" in out
    assert "STOP" in out


def test_no_merge_retry_banner_when_absent():
    data = _data_with_merge_retry()
    data["merge_retry"] = None
    out = format_for_agent(data)
    assert "MERGE RETRY" not in out
