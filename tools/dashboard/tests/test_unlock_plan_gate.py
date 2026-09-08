"""What the root ceremony ATTEMPTS, given a plan — the know-first contract.

The unlock plan exists so the ceremony stops asking the server, per org per
unlock, about work it is not entitled to do and does not need. These drive the
REAL step runner (`_ORG_ROOT_STEPS` + `_reconcileOrgUnderRoot` in
network-signon.mjs) and assert on the URLs it actually fetched:

* a committed org with nothing due fetches NOTHING;
* a local/personal store never reaches for a checkpoint — the case the
  operator called out: most nodes may not publish one, and a personal database
  has no committed membership at all;
* a persona outside `checkpointer_pubs` never attempts a publication it cannot
  sign;
* the real cases still run: a due checkpoint whose persona IS a checkpointer,
  and a credential inside its renewal window (still `status: ok`, still due);
* NO PLAN, NO GATE — with the plan absent every step falls back to the probe
  it has always done, so an older server, the mock, or a failed prefetch can
  never make an unlock do LESS maintenance than before the plan existed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parent / "unlock_plan_gate_harness.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not on PATH")


@pytest.fixture(scope="module")
def proof() -> dict:
    result = subprocess.run(
        ["node", str(HARNESS)], env={**os.environ},
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    return json.loads(
        [ln for ln in result.stdout.splitlines() if ln.strip()][-1])


def _step(case: dict, name: str) -> dict:
    return next(s for s in case["steps"] if s["step"] == name)


def test_an_org_with_nothing_due_makes_no_calls_at_all(proof):
    """THE ONE THAT MATTERS for cost: the common unlock — a committed org,
    up to date, credential current — costs ZERO round-trips beyond the single
    plan fetch."""
    case = proof["nothing_to_do"]

    assert case["fetched"] == []
    assert _step(case, "checkpoint")["skipped"] is True
    assert _step(case, "serve-cert")["skipped"] is True


def test_a_personal_store_never_reaches_for_a_checkpoint(proof):
    """A local database has no committed membership; attempting a publication
    for it is meaningless and its failure is noise that has already misled one
    investigation."""
    case = proof["personal"]
    checkpoint = _step(case, "checkpoint")

    assert checkpoint["skipped"] is True
    assert checkpoint["reason"] == "not-a-committed-org"
    assert case["fetched"] == []


def test_a_non_checkpointer_never_attempts_a_publication(proof):
    """Permission is knowable up front: the plan carries the checkpointer set,
    so a persona outside it declines locally instead of asking."""
    case = proof["not_checkpointer"]
    checkpoint = _step(case, "checkpoint")

    assert checkpoint["skipped"] is True
    assert checkpoint["reason"] == "not-checkpointer"
    assert case["fetched"] == []


def test_a_real_due_checkpoint_still_runs(proof):
    """The gate must not be a mute button — the case it exists to protect
    still fires."""
    case = proof["checkpoint_due"]

    assert _step(case, "checkpoint")["skipped"] is False
    assert any("membership-checkpoint/decision" in u for u in case["fetched"])


def test_a_credential_in_its_renewal_window_still_renews(proof):
    """`serve_cert.required` — not `status != 'ok'`. A gate on status alone
    would skip this and let a live credential die."""
    case = proof["serve_due"]

    assert _step(case, "serve-cert")["skipped"] is False
    assert any("/api/network/serve-cert" in u for u in case["fetched"])


def test_without_a_plan_every_step_falls_back_to_its_own_probe(proof):
    """NO PLAN, NO GATE. The plan can only ever skip work that was going to
    decline itself; it can never make an unlock do less than it did before."""
    case = proof["no_plan"]

    assert [s["skipped"] for s in case["steps"]] == [False, False, False, False]
    assert any("rekey-policy" in u for u in case["fetched"])
    assert any("/api/network/serve-cert" in u for u in case["fetched"])
    assert any("membership-checkpoint/decision" in u for u in case["fetched"])


def test_a_planned_unlock_makes_ZERO_round_trips(proof):
    """THE BEAD'S CLAIM, MEASURED. Driving the real
    repairAllServeCredentialsWithRootSeed: an org with nothing due used to cost
    five calls (binding, ledger/heads, rekey-policy, serve-cert,
    membership-checkpoint/decision). With the plan in hand it costs NONE — the
    binding and genesis come from the plan, and every step's precondition is
    answered locally."""
    assert proof["with_plan"]["fetched"] == []


def test_without_a_plan_the_ceremony_still_fetches_what_it_needs(proof):
    """The fallback is not theoretical: with no plan the loop fetches the
    binding and the ledger heads per org exactly as it did before, so an older
    server or a failed prefetch loses nothing."""
    fetched = proof["without_plan"]["fetched"]

    assert any("/api/network/binding" in u for u in fetched)
    assert any("/api/network/ledger/heads" in u for u in fetched)


def test_no_step_failed_in_any_case(proof):
    """Isolation holds: a skipped step is a SUCCESS with a reason, never an
    error, so a skip can never read as a failure in the unlock report."""
    step_cases = {n: c for n, c in proof.items() if "steps" in c}
    assert step_cases, "the harness reported no step-bearing cases"
    for name, case in step_cases.items():
        for step in case["steps"]:
            assert step["ok"] is True, f"{name}/{step['step']}"
