"""Identity creation and founding as a goal (bead auto-2vseu).

Founding in the browser ends on its success screen having produced the
ledger and the sealed org root, but no registration, checkpoint seed or
serve certificate: those come from the link-publish approval and the next
sign-on. The org_founded goal records that built order; these tests make a
founding order that skips one of its artifacts fail, and hold the one-window
order auto-2vseu builds to the planner's minimum.
"""

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyreg  # noqa: E402

GOAL = "org_founded"


@pytest.fixture(scope="module")
def registry():
    return keyreg.load()


def _all_scenarios(registry):
    return [(goal_id, i, s)
            for goal_id, goal in registry["goals"].items()
            for i, s in enumerate(goal.get("scenarios") or [])]


def test_every_goal_records_scenarios(registry):
    """A goal with no scenarios has nothing the plan tests reproduce."""
    assert all(goal.get("scenarios") for goal in registry["goals"].values())


def test_every_recorded_scenario_reproduces(registry):
    for goal_id, _i, scenario in _all_scenarios(registry):
        current, minimal, openings = keyreg.explain_current(
            registry, goal_id, scenario["from"], scenario["rules"])
        assert current.reached, (goal_id, scenario["from"])
        assert current.ceremonies == scenario["current"], (goal_id, scenario["from"])
        assert minimal.openings == scenario["minimal"], (goal_id, scenario["from"])
        for actor, count in scenario["current"].items():
            extra = sum(1 for o in openings if o.actor == actor and o.extra)
            assert count - extra == scenario["minimal"][actor], (goal_id, actor, openings)


def test_founding_recorded_counts(registry):
    """Built: founding, the link-publish approval's registration, the next
    sign-on. Minimal: everything in the founding window."""
    table = {s["from"]: (s["current"], s["minimal"])
             for s in registry["goals"][GOAL]["scenarios"]}
    assert table["identity_held"] == ({"founder": 3, "joiner": 0}, {"founder": 1, "joiner": 0})
    assert table["fresh"] == ({"founder": 4, "joiner": 0}, {"founder": 1, "joiner": 0})


def _with_order(registry, order):
    data = copy.deepcopy(registry)
    data["goals"][GOAL]["current_order"] = order
    return data


def _step(name, runs, ceremony=True):
    return {"step": name, "actor": "founder", "ceremony": ceremony, "runs": runs}


@pytest.mark.parametrize("dropped", [
    "ceremony.registration", "ceremony.checkpoint_seed", "ceremony.serve_cert_mint"])
def test_a_founding_that_skips_a_phase_does_not_reach_the_goal(registry, dropped):
    """The Windows walk: founding reports success with no registration. An
    order that never runs a phase leaves its artifact missing."""
    runs = ["ceremony.org_found", "ceremony.registration",
            "ceremony.checkpoint_seed", "ceremony.serve_cert_mint"]
    data = _with_order(registry, [
        _step("F0.shell", ["route.org_shell_create"], ceremony=False),
        _step("F0", [r for r in runs if r != dropped]),
    ])
    current, _minimal, _openings = keyreg.explain_current(data, GOAL, "identity_held")
    assert not current.reached


def test_the_one_window_founding_reaches_the_goal_at_the_minimum(registry):
    """auto-2vseu's order: founding signs the registration with the root it
    just minted, then the org-scoped sign-on phases seed the checkpoint and
    mint the serve certificate with the still-held seed, in one opening."""
    data = _with_order(registry, [
        _step("F0.shell", ["route.org_shell_create"], ceremony=False),
        _step("F0", ["ceremony.org_found", "ceremony.registration",
                     "ceremony.checkpoint_seed", "ceremony.serve_cert_mint"]),
    ])
    current, minimal, openings = keyreg.explain_current(data, GOAL, "identity_held")
    assert current.reached
    assert current.ceremonies == {"founder": 1, "joiner": 0}
    assert current.ceremonies == minimal.openings
    assert not any(o.extra for o in openings)


@pytest.mark.parametrize("order", [
    ["ceremony.org_found", "ceremony.checkpoint_seed", "ceremony.registration",
     "ceremony.serve_cert_mint"],
    ["ceremony.org_found", "ceremony.registration", "ceremony.serve_cert_mint",
     "ceremony.checkpoint_seed"],
])
def test_the_phases_run_in_dependency_order(registry, order):
    """Registration before the seed (the server assembles the seed only for
    a bound organization); the seed before the serve certificate."""
    data = _with_order(registry, [
        _step("F0.shell", ["route.org_shell_create"], ceremony=False),
        _step("F0", order),
    ])
    current, _minimal, _openings = keyreg.explain_current(data, GOAL, "identity_held")
    assert not current.reached


def test_founding_produces_what_org_sync_pull_assumes_of_f0(registry):
    """Every founder-side artifact org_sync_pull's starting states take as
    "F0 done" and a founding mutation produces is required by org_founded."""
    producers = keyreg.workflow_producers(registry)
    founding = {"ceremony.org_found", "ceremony.registration",
                "ceremony.checkpoint_seed", "ceremony.serve_cert_mint"}
    required = set(registry["goals"][GOAL]["requires"])
    for state in registry["goals"]["org_sync_pull"]["states"].values():
        for ref in state["holds"]:
            if founding & set(producers.get(ref, [])):
                assert ref in required, ref
