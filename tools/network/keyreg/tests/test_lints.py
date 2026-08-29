"""The code-completeness checks: green against the real registry, and each
one demonstrably able to fail with a message naming the offending entry."""

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import keyreg  # noqa: E402
import lint  # noqa: E402


@pytest.fixture(scope="module")
def registry():
    return keyreg.load()


# ── The merge gates: every check passes against the real registry ──────────

def test_fold_handlers_match(registry):
    assert lint.fold_handlers_match(registry) == []


def test_purpose_labels_match(registry):
    assert lint.purpose_labels_match(registry) == []


def test_code_anchors_resolve(registry):
    assert lint.code_anchors_resolve(registry) == []


def test_proof_refs_resolve(registry):
    assert lint.proof_refs_resolve(registry) == []


# ── The calibrations: each check fails precisely when it should ────────────

def test_missing_fold_mutation_is_caught_by_name(registry):
    broken = copy.deepcopy(registry)
    del broken["mutations"]["fold.member_rekey"]
    errors = lint.fold_handlers_match(broken)
    assert any("_h_member_rekey" in err and "no mutation entry" in err for err in errors), errors


def test_claiming_a_nonexistent_handler_is_caught(registry):
    broken = copy.deepcopy(registry)
    broken["mutations"]["fold.member_rekey"]["source"]["symbol"] = "_h_ghost"
    errors = lint.fold_handlers_match(broken)
    assert any("_h_ghost" in err and "does not exist" in err for err in errors), errors
    assert any("_h_member_rekey" in err for err in errors), errors


def test_two_mutations_claiming_one_handler_is_caught(registry):
    broken = copy.deepcopy(registry)
    broken["mutations"]["fold.duplicate"] = copy.deepcopy(
        broken["mutations"]["fold.member_rekey"]
    )
    errors = lint.fold_handlers_match(broken)
    assert any("claimed by two mutations" in err for err in errors), errors


def test_unregistered_purpose_label_is_caught(registry):
    broken = copy.deepcopy(registry)
    del broken["purposes"]["autonomy/capability-grant/v1"]
    errors = lint.purpose_labels_match(broken)
    assert any(
        "autonomy/capability-grant/v1" in err and "not in registry.yaml" in err
        for err in errors
    ), errors


def test_stale_purpose_entry_is_caught(registry):
    broken = copy.deepcopy(registry)
    broken["purposes"]["autonomy/no-such-purpose/v9"] = {"description": "stale"}
    errors = lint.purpose_labels_match(broken)
    assert any(
        "autonomy/no-such-purpose/v9" in err and "no scanned code file" in err
        for err in errors
    ), errors


def test_bad_anchor_file_is_caught(registry):
    broken = copy.deepcopy(registry)
    broken["keys"]["per_machine_key"]["code"] = ["tools/network/idkit/gone.py:derive_machine_key"]
    errors = lint.code_anchors_resolve(broken)
    assert any(
        "keys.per_machine_key" in err and "gone.py" in err for err in errors
    ), errors


def test_bad_anchor_symbol_is_caught(registry):
    broken = copy.deepcopy(registry)
    broken["mutations"]["fold.member_rekey"]["source"]["symbol"] = "_h_member_rekey_v2"
    errors = lint.code_anchors_resolve(broken)
    assert any(
        "mutations.fold.member_rekey" in err and "_h_member_rekey_v2" in err
        for err in errors
    ), errors


def test_bad_proof_lemma_is_caught(registry):
    broken = copy.deepcopy(registry)
    broken["keys"]["member_recovery_key"]["proofs"][0]["lemma"] = "no_such_lemma"
    errors = lint.proof_refs_resolve(broken)
    assert any(
        "member_recovery_key" in err and "no_such_lemma" in err for err in errors
    ), errors


def test_bad_proof_theory_is_caught(registry):
    broken = copy.deepcopy(registry)
    broken["keys"]["member_recovery_key"]["proofs"][0]["theory"] = "VaultGhost"
    errors = lint.proof_refs_resolve(broken)
    assert any(
        "member_recovery_key" in err and "VaultGhost" in err for err in errors
    ), errors


# ── Bounds the bead's acceptance states: fast, offline ─────────────────────

def test_all_checks_run_fast(registry):
    import time

    start = time.monotonic()
    for check in lint.ALL_CHECKS:
        check(registry)
    assert time.monotonic() - start < 5.0
