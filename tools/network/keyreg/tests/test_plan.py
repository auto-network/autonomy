"""The planner and the current-order lints over the org invite -> join ->
admission -> sync workflow (graph://cde6c8c6-041).

The expected counts are the ceremony record's sections 3 and 4:
current founder 3-4 root openings, joiner 2-3 plus a repeat; minimal
founder 2 (1 with a checkpoint-scoped delegate), joiner 1 (self-admit) or
2 (approval). Under built rules alone an approval role costs the founder 3:
the countersign stages the claim and the checkpoint needs it appended, so
founder 2 follows from the built admission event."""

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyreg  # noqa: E402
import lint  # noqa: E402

GOAL = "org_sync_pull"


@pytest.fixture(scope="module")
def registry():
    return keyreg.load()


def _scenarios(registry):
    return registry["goals"][GOAL]["scenarios"]


def test_recorded_scenarios_match_the_record(registry):
    """Every proposed rule is built (auto-qrmlg.3 C3..C5): the bundle adopt,
    the delegate checkpoint at admission and the admission event. The
    founder's checkpoint-scoped delegate is minted at its own sign-on (F0),
    so both starting states hold it. The invitation's publish runs in the
    invite's own opening (F2 continues F1, auto-xvqxz), so the built order is
    minimal: self-admitting role 1 / 1, approval role 2 / 2 (the final-rules
    table of auto-qrmlg.12)."""
    table = {(s["from"], tuple(s["rules"])): (s["current"], s["minimal"])
             for s in _scenarios(registry)}
    assert table[("self_admit", ())] == ({"founder": 1, "joiner": 1}, {"founder": 1, "joiner": 1})
    assert table[("approval", ())] == ({"founder": 2, "joiner": 2}, {"founder": 2, "joiner": 2})


def test_final_rules_adopt_at_install(registry):
    result = keyreg.plan(registry, GOAL, "self_admit")
    adopt = [inst.mutation for inst, _w, new in result.steps if "adopted_checkpoint@joiner" in new]
    assert adopt == ["route.join_install"]


@pytest.mark.parametrize("index", range(2))
def test_planner_and_current_order_reproduce_each_scenario(registry, index):
    scenario = _scenarios(registry)[index]
    current, minimal, openings = keyreg.explain_current(
        registry, GOAL, scenario["from"], scenario["rules"])
    assert current.reached
    assert current.ceremonies == scenario["current"]
    assert minimal.openings == scenario["minimal"]
    # every opening explain-current calls extra accounts for the difference
    for actor, count in scenario["current"].items():
        extra = sum(1 for o in openings if o.actor == actor and o.extra)
        assert count - extra == scenario["minimal"][actor], (actor, openings)


def test_minimal_schedule_is_valid_and_reaches_goal(registry):
    result = keyreg.plan(registry, GOAL, "self_admit")
    held = frozenset(registry["goals"][GOAL]["states"]["self_admit"]["holds"])
    for inst, _window, _new in result.steps:
        assert keyreg._all_satisfied(inst.requires, held, inst.opens), inst.label()
        held = held | inst.produces
    goal = tuple(keyreg._norm_expr(e) for e in registry["goals"][GOAL]["requires"])
    assert keyreg._all_satisfied(goal, held)


def test_minimal_joiner_window_holds_claim_install_and_certificates(registry):
    """One joiner root opening covers the claim and the fleet:sync
    certificate. The serve certificate is no longer forced into it: the
    install seed's addresses (produced by route.join_install since b01b938e)
    satisfy peer selection without a relay slot."""
    result = keyreg.plan(registry, GOAL, "self_admit")
    joiner_window = [inst.mutation for inst, window, _ in result.steps if window == "joiner#1"]
    assert {"ceremony.member_claim_mint", "ceremony.fleet_runtime_mint"} <= set(joiner_window)
    assert "ceremony.serve_cert_mint" not in joiner_window
    # The joiner adopts at install (a machine step inside the join), so no
    # separate adoption step remains in the minimal schedule.
    adopt = [w for inst, w, _ in result.steps if inst.mutation == "route.checkpoint_adopt"]
    assert adopt == []


def test_explain_finds_no_extra_opening(registry):
    """The invitation's publish runs inside the invite's opening (F2
    continues F1, auto-xvqxz), and the joiner's install-time mints inside the
    claim's (J5 continues J3): no opening is extra any more."""
    _current, _minimal, openings = keyreg.explain_current(registry, GOAL, "self_admit")
    by_step = {o.step: o for o in openings}
    assert "F2" not in by_step
    assert "J6" not in by_step and "F4" not in by_step and "J6.repeat" not in by_step
    assert [o.step for o in openings if o.extra] == []


def test_unknown_rule_is_refused(registry):
    with pytest.raises(keyreg.PlanError, match="unknown rule"):
        keyreg.plan(registry, GOAL, "self_admit", ["no_such_rule"])


def test_unreachable_goal_is_reported(registry):
    broken = copy.deepcopy(registry)
    del broken["mutations"]["route.join_install"]
    for step in broken["goals"][GOAL]["current_order"]:
        step["runs"] = [m for m in step["runs"] if m != "route.join_install"] or ["route.invite_resolve"]
    with pytest.raises(keyreg.PlanError, match="unreachable"):
        keyreg.plan(broken, GOAL, "self_admit")


def test_cli_plan_and_explain(capsys, registry):
    assert keyreg.main(["plan", GOAL, "--from", "approval"]) == 0
    out = capsys.readouterr().out
    assert "root openings: founder 2, joiner 2" in out
    assert keyreg.main(["explain-current", GOAL]) == 0
    out = capsys.readouterr().out
    assert "current root openings: founder 1, joiner 1" in out
    assert "EXTRA" not in out and "J6" not in out


# ── The current-order lints ──────────────────────────────────────────────────

def test_order_lints_pass_on_the_real_registry(registry):
    assert lint.workflow_current_order(registry) == []


def test_an_unrecorded_defect_surfaces_by_name(registry):
    """No defects remain in the built order; a step that consumes before its
    producer, with no defect recorded for it, is named by the lint."""
    broken = copy.deepcopy(registry)
    order = broken["goals"][GOAL]["current_order"]
    j5 = next(s for s in order if s["step"] == "J5")
    j5["runs"] = ["route.checkpoint_adopt"]   # needs a checkpoint nothing before it publishes
    for s in order:
        if s["step"] in ("J3.submit", "F3.admit"):
            s["runs"] = [m for m in s["runs"] if m != "delegate.checkpoint_publish"]
    errors = lint.workflow_current_order(broken)
    assert any("step J5 runs route.checkpoint_adopt, which requires "
               "checkpoint_including_joiner; no producer precedes it" in e for e in errors), errors


def test_consumer_before_producer_is_an_error(registry):
    broken = copy.deepcopy(registry)
    order = broken["goals"][GOAL]["current_order"]
    serve = next(i for i, s in enumerate(order) if s["step"] == "J5.serve")
    j3 = next(i for i, s in enumerate(order) if s["step"] == "J3")
    order.insert(j3, order.pop(serve))  # the bootstrap before the claim exists
    errors = lint.workflow_current_order(broken)
    assert any("step J5.serve runs route.join_bootstrap, which requires member_admitted" in e
               for e in errors), errors


def test_stale_known_defect_is_an_error(registry):
    broken = copy.deepcopy(registry)
    broken["goals"][GOAL]["known_defects"].append(
        {"step": "F1", "mutation": "ceremony.org_invite_mint", "missing": "invite_event",
         "ref": "fabricated"})
    assert any("matches no current finding" in e for e in lint.workflow_current_order(broken))


def test_order_not_reaching_goal_is_an_error(registry):
    broken = copy.deepcopy(registry)
    # Without the joiner's install step nothing produces the joiner's
    # ledger, binding or fleet:sync certificate, which the goal requires.
    broken["goals"][GOAL]["current_order"] = [
        s for s in broken["goals"][GOAL]["current_order"] if s["step"] != "J5"]
    errors = lint.workflow_current_order(broken)
    assert any("does not reach the goal" in e for e in errors), errors
