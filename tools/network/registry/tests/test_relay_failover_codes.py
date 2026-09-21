"""The two follow close codes are member-local refusals the relay fails over
(design of record graph://5f2f5a49-00d §10.1, §10.3).

A follow (org:follow) dial refused with CLOSE_FOLLOW_NO_FRONTIER (this member
holds no covered persona write floor) or CLOSE_FOLLOW_BEHIND (its org frontier
is below the follower's cursor) is about THIS member, not the link, so the
relay moves the dial to the next candidate — exactly the way it already fails
over CLOSE_CONNECTOR_UNARMED. The failover loop is code-agnostic: it fails a
refusal over iff its code is in FAILOVER_CODES (relay.py: ``code not in
FAILOVER_CODES`` ends the dial), so membership IS the behavior.
"""
from __future__ import annotations

from tools.network.registry.relay import FAILOVER_CODES, FAILOVER_PRECEDENCE
from tools.network.relaykit.close_codes import (
    CLOSE_CONNECTOR_UNARMED,
    CLOSE_FOLLOW_BEHIND,
    CLOSE_FOLLOW_NO_FRONTIER,
)


def test_both_follow_codes_fail_over_like_the_unarmed_code():
    assert CLOSE_CONNECTOR_UNARMED in FAILOVER_CODES  # the precedent
    assert CLOSE_FOLLOW_NO_FRONTIER in FAILOVER_CODES
    assert CLOSE_FOLLOW_BEHIND in FAILOVER_CODES


def test_both_follow_codes_have_a_final_verdict_rank():
    # A dial that fails over EVERY candidate reports the most actionable code;
    # a code absent from the precedence tuple could never be that verdict.
    assert CLOSE_FOLLOW_NO_FRONTIER in FAILOVER_PRECEDENCE
    assert CLOSE_FOLLOW_BEHIND in FAILOVER_PRECEDENCE
