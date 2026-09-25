"""The workflow sections (actors, artifacts, goals) and the workflow fields
on mutations (actors, opens, requires, produces, rule): the real data
validates and resolves, and each validation rule fails precisely."""

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gen  # noqa: E402
import keyreg  # noqa: E402
import lint  # noqa: E402

# Every artifact graph://cde6c8c6-041 section 2 and section 6 name, under the
# registry's ids (per-actor artifacts cover the founder_/joiner_ pairs).
REQUIRED_ARTIFACTS = [
    "invite_event", "link_publish_approval", "join_link_grant", "member_claim",
    "claim_staged", "claim_approval", "member_claim_final", "member_admitted",
    "bootstrap_snapshot", "ledger_heads", "registry_binding", "checkpoint_seed",
    "checkpoint_including_joiner", "adopted_checkpoint", "persona_cert_fleet_sync",
    "delegate_grant", "serve_cert", "registered_serving_key", "reachability_row",
    "install_seed_addresses", "relay_slot", "fleet_roster",
]


@pytest.fixture(scope="module")
def registry():
    return keyreg.load()


def _errors(broken):
    return keyreg.validate(broken)


def test_required_artifacts_present(registry):
    missing = sorted(set(REQUIRED_ARTIFACTS) - set(registry["artifacts"]))
    assert not missing, missing


def test_every_artifact_anchor_resolves(registry):
    assert [e for e in lint.code_anchors_resolve(registry) if "artifacts." in e] == []


def test_goal_org_sync_pull_is_recorded(registry):
    goal = registry["goals"]["org_sync_pull"]
    refs = {r for e in goal["requires"] for r in keyreg._expr_refs(e)}
    assert {"adopted_checkpoint@founder", "adopted_checkpoint@joiner",
            "persona_cert_fleet_sync@founder", "persona_cert_fleet_sync@joiner"} <= refs


def test_ceremony_and_route_mutations_of_the_workflow_carry_fields(registry):
    order = registry["goals"]["org_sync_pull"]["current_order"]
    for step in order:
        for mut_id in step["runs"]:
            entry = registry["mutations"][mut_id]
            for field in ("actors", "opens", "requires", "produces"):
                assert field in entry, (mut_id, field)


# ── Calibrations: each rule fails with a message naming the entry ──────────

def test_unknown_artifact_in_requires(registry):
    broken = copy.deepcopy(registry)
    broken["mutations"]["route.join_bootstrap"]["requires"] = ["ghost_artifact"]
    assert any("mutations.route.join_bootstrap.requires" in e and "ghost_artifact" in e
               for e in _errors(broken))


def test_goal_per_actor_ref_must_name_actor(registry):
    broken = copy.deepcopy(registry)
    broken["goals"]["org_sync_pull"]["requires"].append("adopted_checkpoint")
    assert any("goals.org_sync_pull.requires" in e and "must name its actor" in e
               for e in _errors(broken))


def test_unknown_actor_suffix(registry):
    broken = copy.deepcopy(registry)
    broken["goals"]["org_sync_pull"]["requires"].append("serve_cert@mallory")
    assert any("unknown actor 'mallory'" in e for e in _errors(broken))


def test_actor_suffix_on_shared_artifact(registry):
    broken = copy.deepcopy(registry)
    broken["goals"]["org_sync_pull"]["requires"].append("invite_event@founder")
    assert any("invite_event@founder" in e and "not per_actor" in e for e in _errors(broken))


def test_key_requirement_must_match_opens(registry):
    broken = copy.deepcopy(registry)
    broken["mutations"]["route.checkpoint_adopt"]["requires"].append("persona_signing_key")
    assert any("mutations.route.checkpoint_adopt.requires" in e and "persona_signing_key" in e
               and "opens 'none'" in e for e in _errors(broken))


def test_bad_opens_value(registry):
    broken = copy.deepcopy(registry)
    broken["mutations"]["route.checkpoint_adopt"]["opens"] = "warm"
    assert any("mutations.route.checkpoint_adopt.opens" in e and "warm" in e
               for e in _errors(broken))


def test_partial_workflow_fields(registry):
    broken = copy.deepcopy(registry)
    del broken["mutations"]["route.checkpoint_adopt"]["produces"]
    assert any("mutations.route.checkpoint_adopt" in e and "'produces'" in e
               for e in _errors(broken))


def test_malformed_and_or(registry):
    broken = copy.deepcopy(registry)
    broken["mutations"]["route.claim_submit_admit"]["requires"] = [{"xor": ["member_claim"]}]
    assert any("mutations.route.claim_submit_admit.requires[0]" in e for e in _errors(broken))


def test_designed_mutation_needs_rule(registry):
    broken = copy.deepcopy(registry)
    del broken["mutations"]["route.admit_on_approval"]["rule"]
    assert any("mutations.route.admit_on_approval" in e and "rule" in e for e in _errors(broken))


def test_current_order_runs_built_code_only(registry):
    broken = copy.deepcopy(registry)
    broken["goals"]["org_sync_pull"]["current_order"][0]["runs"].append("route.admit_on_approval")
    assert any("current_order[0].runs" in e and "designed" in e for e in _errors(broken))


def test_window_step_must_be_a_ceremony(registry):
    broken = copy.deepcopy(registry)
    order = broken["goals"]["org_sync_pull"]["current_order"]
    step = next(s for s in order if s["step"] == "F4")
    step["ceremony"] = False
    assert any("step F4 must be a ceremony" in e for e in _errors(broken))


def test_artifact_missing_store(registry):
    broken = copy.deepcopy(registry)
    del broken["artifacts"]["invite_event"]["store"]
    assert any("artifacts.invite_event" in e and "store" in e for e in _errors(broken))


def test_bad_artifact_anchor_is_caught(registry):
    broken = copy.deepcopy(registry)
    broken["artifacts"]["invite_event"]["code"] = ["tools/network/ledger/events.py:_v_ghost"]
    assert any("artifacts.invite_event" in e and "_v_ghost" in e
               for e in lint.code_anchors_resolve(broken))


def test_scenario_with_unknown_rule(registry):
    broken = copy.deepcopy(registry)
    broken["goals"]["org_sync_pull"]["scenarios"][0]["rules"] = ["no_such_rule"]
    assert any("no_such_rule" in e for e in _errors(broken))


def test_tla_proof_ref_is_resolved(registry):
    broken = copy.deepcopy(registry)
    broken["goals"]["org_sync_pull"]["proofs"] = [
        {"framework": "tla", "theory": "NoSuchModule", "lemma": "Safety"}]
    assert any("NoSuchModule" in e for e in lint.proof_refs_resolve(broken))


# ── Generated view ──────────────────────────────────────────────────────────

def test_workflow_goals_view_lists_artifacts_producers_goals(registry):
    view = gen.generate()["workflow-goals.md"]
    producers = keyreg.workflow_producers(registry)
    for art_id in registry["artifacts"]:
        assert f'id="artifact-{art_id}"' in view, art_id
    for copy in keyreg.artifact_copies(registry):
        assert f"{copy} |" in view, copy
        for producer in producers.get(copy, []):
            assert producer in view
    for goal_id in registry["goals"]:
        assert f"## Goal {goal_id}" in view
    row = next(line for line in view.splitlines()
               if 'id="artifact-install_seed_addresses"' in line)
    assert "**none — defect:**" in row  # the J5 defect: no producer, named
    founder_heads = next(line for line in view.splitlines() if "ledger_heads@founder |" in line)
    assert "| given |" in founder_heads and "fold.genesis" in founder_heads


def test_every_copy_is_given_produced_or_named(registry):
    given = keyreg.given_copies(registry)
    producers = keyreg.workflow_producers(registry)
    for copy in keyreg.artifact_copies(registry):
        origin = registry["artifacts"][copy.partition("@")[0]].get("origin", "workflow")
        assert copy in given or producers.get(copy) or origin in ("defect", "open"), copy
    assert "ledger_heads@founder" in given and "ledger_heads@joiner" not in given
    assert producers["ledger_heads@joiner"] == ["route.join_install"]


def test_goal_names_both_actors_copies(registry):
    refs = {r for e in registry["goals"]["org_sync_pull"]["requires"]
            for r in keyreg._expr_refs(e)}
    for base in ("persona_cert_fleet_sync", "adopted_checkpoint"):
        assert {f"{base}@founder", f"{base}@joiner"} <= refs


def test_unproduced_copy_is_an_error(registry):
    broken = copy.deepcopy(registry)
    broken["artifacts"]["ledger_heads"]["given_by"] = ["fold.genesis@joiner"]
    errors = _errors(broken)
    assert any("ledger_heads@founder is neither given nor produced" in e for e in errors), errors


def test_state_may_hold_only_given_copies(registry):
    broken = copy.deepcopy(registry)
    broken["goals"]["org_sync_pull"]["states"]["self_admit"]["holds"].append("member_admitted")
    assert any("states.self_admit.holds" in e and "member_admitted" in e for e in _errors(broken))


def test_defect_with_a_producer_is_stale(registry):
    broken = copy.deepcopy(registry)
    broken["mutations"]["route.join_install"]["produces"].append("install_seed_addresses")
    assert any("artifacts.install_seed_addresses" in e and "origin defect" in e
               for e in _errors(broken))


def test_given_by_names_a_mutation(registry):
    broken = copy.deepcopy(registry)
    broken["artifacts"]["checkpoint_seed"]["given_by"] = ["ceremony.no_such"]
    assert any("artifacts.checkpoint_seed.given_by" in e for e in _errors(broken))
