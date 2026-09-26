"""The planner and the current-order lints over the org invite -> join ->
admission -> sync workflow (graph://cde6c8c6-041).

The expected counts are the ceremony record's sections 3 and 4:
current founder 3-4 root openings, joiner 2-3 plus a repeat; minimal
founder 2 (1 with a checkpoint-scoped delegate), joiner 1 (self-admit) or
2 (approval). Under built rules alone an approval role costs the founder 3:
the countersign stages the claim and the checkpoint needs it appended, so
founder 2 needs the proposed admit_on_approval rule."""

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
    table = {(s["from"], tuple(s["rules"])): (s["current"], s["minimal"])
             for s in _scenarios(registry)}
    assert table[("self_admit", ())] == ({"founder": 3, "joiner": 3}, {"founder": 2, "joiner": 1})
    assert table[("approval", ())] == ({"founder": 4, "joiner": 4}, {"founder": 3, "joiner": 2})
    assert table[("approval", ("admit_on_approval",))][1] == {"founder": 2, "joiner": 2}
    assert table[("self_admit", ("delegate_checkpoint",))][1] == {"founder": 1, "joiner": 1}
    # auto-qrmlg.12 final rules: the admission event realizes admit_on_approval,
    # bundle_adopt moves the joiner's adoption into install; counts unchanged.
    assert table[("self_admit", ("delegate_checkpoint", "bundle_adopt"))][1] == {"founder": 1, "joiner": 1}
    assert table[("approval", ("admit_on_approval", "bundle_adopt"))][1] == {"founder": 2, "joiner": 2}
    assert table[("approval", ("admit_on_approval", "bundle_adopt", "delegate_checkpoint"))][1] == {
        "founder": 2, "joiner": 2}


def test_final_rules_adopt_at_install(registry):
    result = keyreg.plan(registry, GOAL, "self_admit", ["delegate_checkpoint", "bundle_adopt"])
    adopt = [inst.mutation for inst, _w, new in result.steps if "adopted_checkpoint@joiner" in new]
    assert adopt == ["route.join_install_bundle_adopt"]


@pytest.mark.parametrize("index", range(8))
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
    result = keyreg.plan(registry, GOAL, "self_admit")
    joiner_window = [inst.mutation for inst, window, _ in result.steps if window == "joiner#1"]
    assert {"ceremony.member_claim_mint", "ceremony.fleet_runtime_mint",
            "ceremony.serve_cert_mint"} <= set(joiner_window)
    adopt = [w for inst, w, _ in result.steps if inst.mutation == "route.checkpoint_adopt"]
    assert adopt == [None]  # adoption is a machine step, re-polled without a ceremony


def test_explain_names_the_forcing_artifacts(registry):
    _current, _minimal, openings = keyreg.explain_current(registry, GOAL, "self_admit")
    by_step = {o.step: o for o in openings}
    assert by_step["F2"].extra and ("link_publish_approval", "approval.link_publish[founder]") \
        in by_step["F2"].missing
    assert by_step["J6"].extra and {r for r, _ in by_step["J6"].missing} == {
        "ledger_heads@joiner", "registry_binding@joiner"}
    assert by_step["J6.repeat"].extra and [r for r, _ in by_step["J6.repeat"].missing] == [
        "checkpoint_including_joiner"]
    assert not by_step["F4"].extra and [r for r, _ in by_step["F4"].missing] == ["member_admitted"]


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
    assert keyreg.main(["plan", GOAL, "--from", "approval", "--rule", "admit_on_approval"]) == 0
    out = capsys.readouterr().out
    assert "root openings: founder 2, joiner 2" in out
    assert keyreg.main(["explain-current", GOAL]) == 0
    out = capsys.readouterr().out
    assert "current root openings: founder 3, joiner 3" in out
    assert "EXTRA  J6.repeat" in out


# ── The current-order lints ──────────────────────────────────────────────────

def test_order_lints_pass_on_the_real_registry(registry):
    assert lint.workflow_current_order(registry) == []


def test_removing_a_known_defect_surfaces_it_by_name(registry):
    broken = copy.deepcopy(registry)
    broken["goals"][GOAL]["known_defects"] = [
        d for d in broken["goals"][GOAL]["known_defects"] if d.get("step") != "J5"]
    errors = lint.workflow_current_order(broken)
    assert any("step J5 runs route.checkpoint_adopt, which requires "
               "checkpoint_including_joiner; no producer precedes it" in e for e in errors), errors


def test_consumer_before_producer_is_an_error(registry):
    broken = copy.deepcopy(registry)
    order = broken["goals"][GOAL]["current_order"]
    f4 = next(i for i, s in enumerate(order) if s["step"] == "F4")
    j3 = next(i for i, s in enumerate(order) if s["step"] == "J3")
    order.insert(j3, order.pop(f4))  # checkpoint before the claim exists
    errors = lint.workflow_current_order(broken)
    assert any("step F4 runs ceremony.checkpoint_publish, which requires member_admitted" in e
               for e in errors), errors


def test_stale_known_defect_is_an_error(registry):
    broken = copy.deepcopy(registry)
    broken["goals"][GOAL]["known_defects"].append(
        {"step": "F1", "mutation": "ceremony.org_invite_mint", "missing": "invite_event",
         "ref": "fabricated"})
    assert any("matches no current finding" in e for e in lint.workflow_current_order(broken))


def test_order_not_reaching_goal_is_an_error(registry):
    broken = copy.deepcopy(registry)
    broken["goals"][GOAL]["current_order"] = [
        s for s in broken["goals"][GOAL]["current_order"] if s["step"] != "J6.repeat"]
    broken["goals"][GOAL]["known_defects"] = [
        d for d in broken["goals"][GOAL]["known_defects"] if d.get("step") != "J6"]
    errors = lint.workflow_current_order(broken)
    assert any("does not reach the goal" in e for e in errors), errors
