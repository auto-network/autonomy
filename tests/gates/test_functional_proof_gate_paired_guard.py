"""Paired-guard tests for the golden-rule functional-proof gates (auto-hwrho).

Standard v3: every gate's RED (refuses / blocks) and GREEN (allows / passes)
case lives in ONE file, so the guarantee and its counter-example can never
drift apart. The three gates:

  1. bd-close shim  (tools/beads/bd) — container/host close of a
     runtime-critical bead; refuses without a functional-proof reference.
  2. dispatcher pre-merge gate  (functional_proof_missing) — blocks the
     DONE auto-merge when decision.json carries no functional artifact, and
     now accepts a passing per-bead functional_check.log as that artifact.
  3. dispatcher host-side closer  (release_bead) — the dispatcher's own host
     bd close; refuses-and-reopens with an atomic readiness flip.

Plus the smoke per-bead functional path (smoke.py run_functional_check), the
plumbing that produces the functional_check.log gate 2 accepts.

Expected refusal strings are sourced from the proven gates themselves (the
fleet-proof transcript, graph attachment edf85f2c-5bf, and w41na's notes),
which is why they are asserted verbatim: the copy IS the contract.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BD_SHIM = REPO_ROOT / "tools" / "beads" / "bd"

from agents.dispatcher import (  # noqa: E402
    functional_proof_missing,
    functional_check_proven,
    release_bead,
)
from tools.dashboard.smoke import run_functional_check  # noqa: E402


# ── Gate 1: bd-close shim ────────────────────────────────────────────────

FAKE_BD = """#!/bin/bash
# Stand-in for the real bd binary. Serves labels/notes from the environment
# and records closes so the shim's delegation can be observed.
if [ "$1" = "show" ]; then
    shift
    want_json=0
    for a in "$@"; do [ "$a" = "--json" ] && want_json=1; done
    if [ "$want_json" = "1" ]; then
        printf '%s\\n' "$FAKE_BD_LABELS_JSON"
    else
        printf '%s\\n' "$FAKE_BD_NOTES"
    fi
    exit 0
fi
if [ "$1" = "close" ]; then
    printf 'CLOSED %s\\n' "$*" >> "$FAKE_BD_CLOSE_LOG"
    exit 0
fi
exit 0
"""


def _run_shim(tmp_path, args, *, labels, notes="", monkeypatch_env=None):
    """Run the bd-close shim against a fake real-bd on PATH.

    Returns (returncode, stdout, stderr, close_log_text).
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "bd"
    fake.write_text(FAKE_BD)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    close_log = tmp_path / "close.log"

    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "FAKE_BD_LABELS_JSON": json.dumps([{"labels": labels}]),
        "FAKE_BD_NOTES": notes,
        "FAKE_BD_CLOSE_LOG": str(close_log),
    }
    if monkeypatch_env:
        env.update(monkeypatch_env)

    proc = subprocess.run(
        [str(BD_SHIM), *args],
        capture_output=True, text=True, env=env, timeout=30,
    )
    close_text = close_log.read_text() if close_log.exists() else ""
    return proc.returncode, proc.stdout, proc.stderr, close_text


def test_shim_red_refuses_runtime_critical_close_without_proof(tmp_path):
    rc, out, err, closed = _run_shim(
        tmp_path, ["close", "auto-xxxx", "-r", "done, tests green"],
        labels=["runtime-critical"],
    )
    assert rc == 2, err
    assert "REFUSING to close auto-xxxx" in err
    # The verbatim contract line — a green suite is not functional proof.
    assert "A green test suite" in err
    assert "is not functional proof for a runtime change." in err
    assert closed == "", "shim must not delegate the close when refusing"


def test_shim_green_allows_close_with_functional_proof_ref(tmp_path):
    rc, out, err, closed = _run_shim(
        tmp_path,
        ["close", "auto-xxxx", "-r",
         "done — functional-proof: /workspace/output/run/functional_check.log"],
        labels=["runtime-critical"],
    )
    assert rc == 0, err
    assert "auto-xxxx" in closed, "shim must delegate the close when proven"


def test_shim_green_accepts_proof_from_bead_notes(tmp_path):
    # Proof may live in a prior bead note, not just the close reason.
    rc, out, err, closed = _run_shim(
        tmp_path, ["close", "auto-xxxx", "-r", "closing"],
        labels=["runtime-critical"],
        notes="earlier: functional-proof: 7bf1d812-ab attached",
    )
    assert rc == 0, err
    assert "auto-xxxx" in closed


def test_shim_angle_bracket_placeholder_does_not_satisfy_gate(tmp_path):
    # The exact false-positive the gate caught on its own first red run:
    # documentation quoting the syntax must NOT count as proof.
    rc, out, err, closed = _run_shim(
        tmp_path,
        ["close", "auto-xxxx", "-r", "see functional-proof: <ref> in the docs"],
        labels=["runtime-critical"],
    )
    assert rc == 2, err
    assert "REFUSING to close auto-xxxx" in err
    assert closed == ""


def test_shim_no_id_close_is_refused(tmp_path):
    # A bare `bd close` closes the last-touched bead in real bd — a proven
    # field misfire. The shim requires an explicit id.
    rc, out, err, closed = _run_shim(
        tmp_path, ["close", "-r", "done"],
        labels=["runtime-critical"],
    )
    assert rc == 2, err
    assert "REFUSING close with no explicit issue id" in err
    assert closed == ""


def test_shim_non_runtime_critical_close_delegates_without_proof(tmp_path):
    # The gate only bites runtime-critical beads; everything else passes
    # through byte-for-byte, no proof required.
    rc, out, err, closed = _run_shim(
        tmp_path, ["close", "auto-yyyy", "-r", "ordinary close"],
        labels=["docs"],
    )
    assert rc == 0, err
    assert "auto-yyyy" in closed


# ── Gate 2: dispatcher pre-merge gate (functional_proof_missing) ─────────


def _dispatch_result(*, labels, decision=None, output_dir=""):
    return SimpleNamespace(
        labels=labels, decision=decision or {}, output_dir=output_dir,
    )


def test_premerge_red_runtime_critical_without_artifact_is_blocked():
    dr = _dispatch_result(labels=["runtime-critical"], decision={})
    assert functional_proof_missing(dr) is True


def test_premerge_green_decision_json_artifact_allows_merge():
    dr = _dispatch_result(
        labels=["runtime-critical"],
        decision={"functional_artifacts": [
            {"path": "/workspace/output/run/functional_check.log",
             "kind": "log", "note": "real run tail"}]},
    )
    assert functional_proof_missing(dr) is False


def test_premerge_angle_bracket_artifact_path_does_not_satisfy_gate():
    # The dispatcher mirror of the shim's angle-bracket guard: a placeholder
    # path is not a concrete artifact.
    dr = _dispatch_result(
        labels=["runtime-critical"],
        decision={"functional_artifacts": [{"path": "<ref>"}]},
    )
    assert functional_proof_missing(dr) is True


def test_premerge_non_runtime_critical_never_gated():
    dr = _dispatch_result(labels=["docs"], decision={})
    assert functional_proof_missing(dr) is False


def test_premerge_green_functional_check_log_satisfies_gate(tmp_path):
    # Pipeline-driven proof: a passing functional_check.log recorded in
    # smoke_result.json's functional block counts as the artifact.
    log = tmp_path / "functional_check.log"
    log.write_text("exercised the real path: GET / -> 200\n")
    (tmp_path / "smoke_result.json").write_text(json.dumps({
        "pass": True,
        "functional": {"present": True, "pass": True, "log": str(log)},
    }))
    dr = _dispatch_result(
        labels=["runtime-critical"], decision={}, output_dir=str(tmp_path))
    assert functional_check_proven(dr) is True
    assert functional_proof_missing(dr) is False


def test_premerge_red_failing_functional_check_does_not_satisfy_gate(tmp_path):
    log = tmp_path / "functional_check.log"
    log.write_text("got 500\n")
    (tmp_path / "smoke_result.json").write_text(json.dumps({
        "pass": False,
        "functional": {"present": True, "pass": False, "log": str(log)},
    }))
    dr = _dispatch_result(
        labels=["runtime-critical"], decision={}, output_dir=str(tmp_path))
    assert functional_check_proven(dr) is False
    assert functional_proof_missing(dr) is True


def test_premerge_red_missing_log_file_does_not_satisfy_gate(tmp_path):
    # The block claims pass but the referenced log is absent — shape check
    # requires the artifact to exist on disk.
    (tmp_path / "smoke_result.json").write_text(json.dumps({
        "pass": True,
        "functional": {"present": True, "pass": True,
                       "log": str(tmp_path / "functional_check.log")},
    }))
    dr = _dispatch_result(
        labels=["runtime-critical"], decision={}, output_dir=str(tmp_path))
    assert functional_check_proven(dr) is False
    assert functional_proof_missing(dr) is True


# ── Gate 3: dispatcher host-side closer (release_bead) ───────────────────


def _make_run_bd(labels, notes=""):
    labels_json = json.dumps([{"labels": labels}])

    def _run_bd(args):
        if args[:1] == ["show"]:
            return labels_json if "--json" in args else notes
        return ""
    return _run_bd


def test_release_bead_red_refuses_and_reopens_with_readiness_flip(monkeypatch):
    calls = {"run_bd": [], "retry_bd": []}

    def run_bd(args):
        calls["run_bd"].append(args)
        if args[:1] == ["show"]:
            return (json.dumps([{"labels": ["runtime-critical"]}])
                    if "--json" in args else "")
        return ""

    def retry_bd(args):
        calls["retry_bd"].append(args)
        return ""

    monkeypatch.setattr("agents.dispatcher.run_bd", run_bd)
    monkeypatch.setattr("agents.dispatcher._retry_bd", retry_bd)

    ok = release_bead("auto-zzzz", "DONE", "done, no proof provided")
    assert ok is True

    # Reopened, not closed.
    assert ["update", "auto-zzzz", "-s", "open"] in calls["retry_bd"]
    assert not any(c[:1] == ["close"] for c in calls["retry_bd"]), \
        "host closer must NOT close a runtime-critical bead without proof"

    # Atomic readiness flip: approved -> host-verify in a single update.
    assert any(
        c == ["update", "auto-zzzz", "--remove-label", "readiness:approved",
              "--add-label", "readiness:host-verify"]
        for c in calls["run_bd"]
    ), "refusal must flip readiness:approved -> readiness:host-verify atomically"

    # The recorded note names the gate.
    note_calls = [c for c in calls["run_bd"]
                  if len(c) >= 3 and c[2] == "--append-notes"]
    assert any("golden-rule gate (host closer): refusing DONE close" in c[3]
               for c in note_calls)


def test_release_bead_green_closes_with_functional_proof_ref(monkeypatch):
    calls = {"run_bd": [], "retry_bd": []}

    def run_bd(args):
        calls["run_bd"].append(args)
        if args[:1] == ["show"]:
            return (json.dumps([{"labels": ["runtime-critical"]}])
                    if "--json" in args else "")
        return ""

    def retry_bd(args):
        calls["retry_bd"].append(args)
        return ""

    monkeypatch.setattr("agents.dispatcher.run_bd", run_bd)
    monkeypatch.setattr("agents.dispatcher._retry_bd", retry_bd)

    reason = ("done — functional-proof: "
              "/workspace/output/run/functional_check.log")
    ok = release_bead("auto-zzzz", "DONE", reason)
    assert ok is True

    # Closed, not reopened/flipped.
    assert ["close", "auto-zzzz", "--reason", reason] in calls["retry_bd"]
    assert not any(
        len(c) >= 4 and c[2] == "--add-label" and c[3] == "readiness:host-verify"
        for c in calls["run_bd"]
    ), "a proven close must not flip readiness to host-verify"


# ── Smoke per-bead functional path (smoke.py run_functional_check) ───────


def _write_script(dir_path, body, executable=True):
    script = dir_path / "functional_check.sh"
    script.write_text(body)
    if executable:
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_smoke_functional_absent_script_is_inert(tmp_path):
    assert run_functional_check(str(tmp_path)) == {"present": False}


def test_smoke_functional_passing_script_tees_transcript(tmp_path):
    _write_script(tmp_path, "#!/bin/bash\necho 'real path: GET / -> 200'\nexit 0\n")
    result = run_functional_check(str(tmp_path))
    assert result["present"] is True
    assert result["pass"] is True
    log = Path(result["log"])
    assert log.exists()
    assert "real path: GET / -> 200" in log.read_text()


def test_smoke_functional_failing_script_reports_fail(tmp_path):
    _write_script(tmp_path, "#!/bin/bash\necho 'got 500'\nexit 1\n")
    result = run_functional_check(str(tmp_path))
    assert result["present"] is True
    assert result["pass"] is False
    assert "got 500" in Path(result["log"]).read_text()


def test_smoke_functional_non_executable_script_runs_via_bash(tmp_path):
    _write_script(tmp_path, "echo ran-via-bash\nexit 0\n", executable=False)
    result = run_functional_check(str(tmp_path))
    assert result["present"] is True
    assert result["pass"] is True
    assert "ran-via-bash" in Path(result["log"]).read_text()
