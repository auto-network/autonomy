"""Round-1 review probe pins (auto-16g9t consolidation).

The seed payloads below are the EXECUTION REVIEWER'S OWN probe seeds
(relayed via graph note a5fef3e5-368, probe1_identity_collision.py), not
reconstructions — per the consolidated addendum, both B5 variants are
pinned with these:

  - SPLIT variant: exec call published in window N, its end in window
    N+1 (the use_refs stamping path).
  - IN-BATCH variant: call + end served in ONE batch where the call is
    NOT the batch's first entry (pre-fix: the fresh semantic tool_use
    dict was refless, so finalize_entry_refs either left it refless or
    inherited the PREVIOUS line's ref — the client then minted a
    ~local synthetic ref and duplicated / misordered the tile).

The invariant under test: the SAME logical tool tile carries the SAME
entry_ref on the live split-window projection and the cold whole-file
projection, for single-op and multi-op execs alike. Plus the case-C
property: a cold page starting after the call line may only produce a
ref SUBSET of the live projection (no disjoint phantom refs), and any
same-ref status divergence must be in the upgrade direction the client's
downgrade guard permits.

Also here: the S3 old-tab semantic-compat contract, encoded verbatim
from the reviewer's delta list.
"""
from __future__ import annotations

import json
from pathlib import Path

from tools.dashboard import session_harness as sh
from tools.dashboard.session_harness import CODEX_HARNESS

from .test_viewer_identity_tail import (  # noqa: F401  (fixture re-export)
    _claude_text_line,
    _insert_session,
    _write_lines,
    tail_client,
)

STEM = "rollout-2026-08-10T12-00-00-abc"

# ── The reviewer's seed payloads, verbatim ─────────────────────────────

EXEC_CALL = {
    "timestamp": "2026-08-10T01:16:23.000Z",
    "type": "response_item",
    "payload": {
        "type": "function_call", "name": "exec_command",
        "arguments": json.dumps({"cmd": "cat /etc/hostname", "workdir": "/w"}),
        "call_id": "call_exec_1",
    },
}
EXEC_END_1OP = {
    "timestamp": "2026-08-10T01:16:41.000Z",
    "type": "event_msg",
    "payload": {
        "type": "exec_command_end", "call_id": "call_exec_1",
        "process_id": "27299",
        "command": ["/bin/bash", "-lc", "cat /etc/hostname"],
        "cwd": "/w",
        "parsed_cmd": [{"type": "read", "cmd": "cat /etc/hostname",
                        "name": "/etc/hostname"}],
        "aggregated_output": "myhost\n",
        "exit_code": 0, "duration": {"secs": 1, "nanos": 0},
        "status": "completed",
    },
}
ASSISTANT = {
    "timestamp": "2026-08-10T01:16:50.000Z",
    "type": "response_item",
    "payload": {"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "done."}]},
}
USER = {
    "timestamp": "2026-08-10T01:16:20.000Z",
    "type": "response_item",
    "payload": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "run it"}]},
}
RUNNING_OUT = {
    "timestamp": "2026-08-10T01:16:24.000Z",
    "type": "response_item",
    "payload": {
        "type": "function_call_output", "call_id": "call_exec_1",
        "output": ("Chunk ID: start\nWall time: 1.0017 seconds\n"
                   "Process running with session ID 27299\n"
                   "Original token count: 12\nOutput:\nworking...\n"),
    },
}


def _line(obj) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _build(lines) -> bytes:
    return b"".join(_line(o) for o in lines)


def _key(e):
    r = e.get("entry_ref")
    assert isinstance(r, dict), f"refless entry served: {e.get('type')}/{e.get('tool_name')}"
    return (r["file"], r["off"], r["sub"])


def _live_projection(data: bytes, split_offsets: list[int]) -> dict:
    """The monitor's per-window publication, client-merged by ref."""
    ctx: dict = {"codex_cli_version": "0.148.0"}
    state = CODEX_HARNESS.new_postprocess_state()
    bounds = [0] + split_offsets + [len(data)]
    merged: dict = {}
    for i in range(len(bounds) - 1):
        batch = sh.parse_lines_with_refs(
            CODEX_HARNESS, data[bounds[i]:bounds[i + 1]],
            stem=STEM, base_offset=bounds[i], ctx=ctx,
        )
        batch = CODEX_HARNESS.postprocess_entries(batch, state=state)
        sh.finalize_entry_refs(batch)
        for e in batch:
            k = _key(e)
            if k in merged:
                merged[k] = {**merged[k],
                             **{a: b for a, b in e.items() if a != "entry_ref"}}
            else:
                merged[k] = e
    return dict(sorted(merged.items()))


def _cold_projection(data: bytes, base_offset: int = 0) -> dict:
    batch = sh.parse_lines_with_refs(
        CODEX_HARNESS, data, stem=STEM, base_offset=base_offset,
        ctx={"codex_cli_version": "0.148.0"},
    )
    batch = CODEX_HARNESS.postprocess_entries(
        batch, state=CODEX_HARNESS.new_postprocess_state(),
    )
    sh.finalize_entry_refs(batch)
    return dict(sorted((_key(e), e) for e in batch))


def _summary(e):
    return (e.get("type"), e.get("tool_name"), e.get("status"), e.get("tool_id"))


class TestB5ReviewerSeeds:

    def test_split_variant_single_op(self):
        """Probe case A: call/end split across a window boundary."""
        data = _build([EXEC_CALL, EXEC_END_1OP, ASSISTANT])
        live = _live_projection(data, [len(_line(EXEC_CALL))])
        cold = _cold_projection(data)
        assert set(live) == set(cold), (
            f"ref sets diverge: live-only {set(live) - set(cold)}, "
            f"cold-only {set(cold) - set(live)}"
        )
        for k in live:
            assert live[k].get("type") == cold[k].get("type"), (k, live[k], cold[k])

    def test_split_variant_multi_op(self):
        """Probe case B: multi-op exec (#2 synthetic entries) split."""
        end2 = json.loads(json.dumps(EXEC_END_1OP))
        end2["payload"]["command"] = ["/bin/bash", "-lc", "cat a; cat b"]
        end2["payload"]["parsed_cmd"] = [
            {"type": "read", "cmd": "cat a", "name": "a"},
            {"type": "read", "cmd": "cat b", "name": "b"},
        ]
        end2["payload"]["aggregated_output"] = "content-a\ncontent-b\n"
        data = _build([EXEC_CALL, end2, ASSISTANT])
        live = _live_projection(data, [len(_line(EXEC_CALL))])
        cold = _cold_projection(data)
        assert set(live) == set(cold)
        assert len(live) == len(cold), (
            f"a split changed the tile count: live {len(live)} cold {len(cold)}"
        )

    def test_in_batch_variant_call_not_first(self):
        """Probe case D — the addendum's second B5 variant: call + end in
        ONE batch with the call NOT first. The semantic tool_use must
        carry the CALL line's ref on both projections (pre-fix it was
        refless or inherited the user line's ref)."""
        data = _build([USER, EXEC_CALL, EXEC_END_1OP, ASSISTANT])
        call_off = len(_line(USER))
        split = call_off + len(_line(EXEC_CALL))
        live = _live_projection(data, [split])
        cold = _cold_projection(data)
        live_use = [k for k, v in live.items() if v.get("type") == "tool_use"]
        cold_use = [k for k, v in cold.items() if v.get("type") == "tool_use"]
        assert live_use == cold_use == [(STEM, call_off, 0)], (
            f"tool tile ref diverged: live {live_use} cold {cold_use} "
            f"expected call-line off {call_off}"
        )

    def test_cold_page_after_call_stays_ref_subset(self):
        """Probe case C: a cold scroll-back page starting after the call
        line may lack live-only enrichment but must never mint DISJOINT
        refs; same-ref status divergence must be upgrade-direction only
        (running→completed — the client guard blocks the reverse)."""
        data = _build([EXEC_CALL, RUNNING_OUT, ASSISTANT])
        split = len(_line(EXEC_CALL))
        live = _live_projection(data, [split])
        cold_page = _cold_projection(data[split:], base_offset=split)
        live_page = {k: v for k, v in live.items() if k[1] >= split}
        lk, ck = set(live_page), set(cold_page)
        assert ck <= lk or lk <= ck, (
            f"disjoint phantom refs: live-only {lk - ck}, cold-only {ck - lk}"
        )
        for k in lk & ck:
            ls, cs = _summary(live_page[k])[2], _summary(cold_page[k])[2]
            if ls != cs:
                assert (ls, cs) == ("running", "completed"), (
                    f"status divergence at {k} is {ls}->{cs}; only "
                    "running->completed (upgrade) is tolerable"
                )


class TestS3OldTabCompat:
    """The reviewer's old-tab delta list, verbatim: for shapes ?after=0,
    ?after=mid, ?after=EOF, ?tail_lines=3, ?tail_lines=3&before=off the
    legacy fields (entries/counts/has_more/older_before) are identical to
    master EXCEPT: 'chain' added (additive), per-entry 'entry_ref' added
    (additive), 'seq:0' removed (master clients guard every seq read and
    the fake 0 triggered their halved-seq reset — removal strictly
    safer), and on a partial trailing line the offset stops at the last
    newline (deliberate legacy loss-fix). Encoded as invariants on the
    branch contract."""

    LEGACY_KEYS = {"entries", "offset", "is_live", "type", "role",
                   "activity_state", "pending_tool_ids", "resolved"}

    def _mk(self, tail_client, name="auto-s3compat", partial=False):
        client, tmp_path, db_path = tail_client
        d = tmp_path / name
        d.mkdir()
        jsonl = d / f"{name}-1234.jsonl"
        lines = [_claude_text_line(f"m{i}") for i in range(5)]
        offsets = _write_lines(jsonl, lines)
        if partial:
            with open(jsonl, "a") as fh:
                fh.write('{"partial":')
        _insert_session(db_path, tmux_name=name, jsonl_path=str(jsonl))
        complete = sum(len(l) + 1 for l in lines)
        return client, jsonl, offsets, complete, name

    def test_legacy_shapes_carry_legacy_fields_no_seq(self, tail_client):
        client, jsonl, offsets, complete, name = self._mk(tail_client)
        shapes = [
            f"?after=0", f"?after={offsets[2]}", f"?after={complete}",
            "?tail_lines=3", f"?tail_lines=3&before={offsets[3]}",
        ]
        for shape in shapes:
            data = client.get(f"/api/session/autonomy/{name}/tail{shape}").json()
            missing = self.LEGACY_KEYS - set(data)
            assert not missing, f"{shape}: legacy fields missing {missing}"
            assert "seq" not in data, f"{shape}: fake seq must stay removed"
            assert "chain" in data, f"{shape}: additive chain expected"
            for e in data["entries"]:
                assert "entry_ref" in e, f"{shape}: additive entry_ref expected"
        # Reverse shapes keep legacy pagination semantics.
        rev = client.get(f"/api/session/autonomy/{name}/tail?tail_lines=3").json()
        assert rev["older_before"] == offsets[2]
        assert rev["has_more"] is True
        assert len(rev["entries"]) == 3

    def test_partial_trailing_line_offset_stops_at_newline(self, tail_client):
        """The one deliberate legacy behavior change: master returned
        offset=EOF and silently lost the partial line's completion; the
        branch stops the cursor at the last newline (reviewer fixture:
        656 vs master's 696)."""
        client, jsonl, offsets, complete, name = self._mk(
            tail_client, name="auto-s3part", partial=True)
        physical = jsonl.stat().st_size
        assert physical > complete
        fwd = client.get(f"/api/session/autonomy/{name}/tail?after=0").json()
        assert fwd["offset"] == complete, (
            f"legacy forward cursor must stop at the last newline "
            f"({complete}), not physical EOF ({physical})"
        )
        assert len(fwd["entries"]) == 5
