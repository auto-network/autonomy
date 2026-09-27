"""Look-alike refusal for operator-chosen serving labels (F6)."""

from __future__ import annotations

import pytest

from tools.dashboard import label_lookalike as ll


@pytest.mark.parametrize("label, shape", [
    ("autonomy", "autonomy"),
    ("aut0n0my", "autonomy"),
    ("auton0rny", "autonomy"),      # 0 -> o, rn -> m
    ("a-u-t-o-n-o-m-y", "autonomy"),
    ("jererny", "jeremy"),
    ("vvorkshop", "workshop"),
    ("c1ock", "clock"),          # cl is not folded: clock must not read as dock
    ("acrne", "acme"),
])
def test_skeleton_folds_the_ascii_confusables(label, shape):
    assert ll.skeleton(label) == shape


@pytest.mark.parametrize("candidate, code, against", [
    ("aut0n0my", "platform_name", "autonomy"),
    ("autonomy-support", "platform_name", "autonomy"),
    ("my-autonorny", "platform_name", "autonomy"),
    ("auto-netvvork", "platform_name", "autonetwork"),
    ("dashb0ard", "platform_name", "dashboard"),
    ("reglstry", "platform_name", "registry"),
    ("re1ay", "platform_name", "relay"),
    ("adrnin", "platform_name", "admin"),
    ("l0gin", "platform_name", "login"),
])
def test_platform_and_infrastructure_names_are_refused(candidate, code, against):
    refusal = ll.lookalike_conflict(candidate)
    assert refusal == ll.LookalikeRefusal(code, against)


def test_reserved_app_labels_are_refused_by_shape():
    # "_autonomy" is reserved at the registry; its shape is the platform name.
    assert ll.lookalike_conflict("serve") is not None
    assert ll.lookalike_conflict("vvww").against == "www"


def test_the_operators_existing_labels_are_protected_from_each_other():
    existing = ["jeremy", "phase1a-docs"]
    assert ll.lookalike_conflict("jererny", existing) == ll.LookalikeRefusal("existing_label", "jeremy")
    assert ll.lookalike_conflict("phaselad0cs", existing) == ll.LookalikeRefusal("existing_label", "phase1a-docs")
    # The operator's own exact label is not a look-alike of itself.
    assert ll.lookalike_conflict("jeremy", existing) is None


@pytest.mark.parametrize("candidate", ["boatlore", "anchore-docs", "hello", "jeremy", "persona"])
def test_distinct_labels_pass(candidate):
    assert ll.lookalike_conflict(candidate, ["oss-metrics", "collector"]) is None


def test_well_formed_is_the_dns_label_rule():
    assert ll.is_well_formed("boat-lore") and ll.is_well_formed("a")
    assert not ll.is_well_formed("-boat") and not ll.is_well_formed("Boat") and not ll.is_well_formed("")
    assert not ll.is_well_formed("x" * 64)
    # RFC 5891 tagged labels (Punycode) are refused before any shape check.
    assert not ll.is_well_formed("xn--80ak6aa92e") and not ll.is_well_formed("ab--cd")
    assert ll.is_well_formed("a--b")   # hyphens elsewhere are ordinary
