"""The staleness/failure deriver and its Central publication (auto-fnydv).

Derivation is pure (rows + config + now → condition rows) and pinned
against the S1/S2 drivers; publication runs through the REAL production
registry composition (backup runtimes included) onto the in-memory
attention store, so sealing, policy, and state coherence are all the
production code paths.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tools.dashboard.attention_index_service import (
    AttentionIndexService,
    InMemoryAttentionIndexStore,
)
from tools.dashboard.attention_registry import (
    build_production_attention_registry,
)
from tools.dashboard.plugins.backup import deriver as D
from tools.dashboard.plugins.backup.attention import publication_runtimes

NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)


def _run(tier: str, stamp: str, verdict: str = "complete",
         at: datetime | None = None, **kw) -> dict:
    at = at or NOW - timedelta(hours=1)
    row = {"key": f"{tier}:{stamp}", "verdict": verdict,
           "started_at": at.isoformat(), "finished_at": at.isoformat(),
           "offsite": "complete",
           "failures": [] if verdict == "complete" else ["auth: MISSING"]}
    row.update(kw)
    return row


def _by_id(conditions):
    return {c["attention_id"]: c for c in conditions}


class TestDerivation:
    def test_healthy_tiers_emit_only_resolutions(self):
        runs = [_run("hourly", "20260906-110000"),
                _run("daily", "20260906-030000", at=NOW - timedelta(hours=9))]
        conditions = _by_id(D.derive_conditions(runs, {}, now=NOW))
        assert conditions["backup:failed:hourly"]["attention_state"] == "resolved"
        assert conditions["backup:stale:hourly"]["attention_state"] == "resolved"
        assert conditions["backup:offsite"]["attention_state"] == "resolved"
        assert not any(c["attention_state"] == "needs_attention"
                       for c in conditions.values())

    def test_failed_newest_run_raises_with_reason(self):
        runs = [_run("hourly", "20260906-110000", verdict="failed")]
        conditions = _by_id(D.derive_conditions(runs, {}, now=NOW))
        failed = conditions["backup:failed:hourly"]
        assert failed["attention_state"] == "needs_attention"
        assert "auth: MISSING" in failed["safe_summary"]
        assert failed["source_version"] == 20260906110000

    def test_no_runs_means_stale_not_failed(self):
        # reports_exist declares a SOURCE machine: a machine that is
        # supposed to back up and never has is stale. A machine with no
        # backup configuration at all is a different case entirely
        # (TestFleetNonSourceMachine).
        conditions = _by_id(D.derive_conditions([], {}, now=NOW,
                                                reports_exist=True))
        assert conditions["backup:stale:hourly"]["attention_state"] == "needs_attention"
        assert "backup:failed:hourly" not in conditions

    def test_stale_threshold_uses_config_multiple(self):
        runs = [_run("hourly", "20260906-050000", at=NOW - timedelta(hours=7))]
        stale3 = _by_id(D.derive_conditions(runs, {"staleness_multiple": 3.0},
                                            now=NOW))
        stale8 = _by_id(D.derive_conditions(runs, {"staleness_multiple": 8.0},
                                            now=NOW))
        assert stale3["backup:stale:hourly"]["attention_state"] == "needs_attention"
        assert stale8["backup:stale:hourly"]["attention_state"] == "resolved"

    def test_offsite_failed_on_newest_complete_run(self):
        runs = [_run("hourly", "20260906-110000", offsite="failed"),
                _run("hourly", "20260906-100000",
                     at=NOW - timedelta(hours=2))]
        conditions = _by_id(D.derive_conditions(runs, {}, now=NOW))
        assert conditions["backup:offsite"]["attention_state"] == "needs_attention"

    def test_offsite_skipped_is_a_choice_not_an_outage(self):
        runs = [_run("hourly", "20260906-110000", offsite="skipped")]
        conditions = _by_id(D.derive_conditions(runs, {}, now=NOW))
        assert "backup:offsite" not in conditions


def _index():
    registry = build_production_attention_registry(
        runtimes=publication_runtimes())
    return AttentionIndexService(
        registry=registry, store=InMemoryAttentionIndexStore())


def _open_ids(index):
    return {item.attention_id for item in index.store.list_items()
            if item.payload.get("attention_state") == "needs_attention"}


class TestPublication:
    def test_failure_publishes_and_recovery_clears(self):
        index = _index()
        failed = [_run("hourly", "20260906-110000", verdict="failed")]
        outcome = D.publish_conditions(
            index, D.derive_conditions(failed, {}, now=NOW))
        assert "backup:failed:hourly" in outcome["published"]
        assert "backup:failed:hourly" in _open_ids(index)

        recovered = [_run("hourly", "20260906-120000", at=NOW),
                     _run("hourly", "20260906-110000", verdict="failed",
                          at=NOW - timedelta(hours=1))]
        outcome = D.publish_conditions(
            index, D.derive_conditions(recovered, {}, now=NOW))
        assert "backup:failed:hourly" in outcome["published"]
        assert "backup:failed:hourly" not in _open_ids(index)

    def test_resolved_without_open_item_publishes_nothing(self):
        index = _index()
        healthy = [_run("hourly", "20260906-110000"),
                   _run("daily", "20260906-030000",
                        at=NOW - timedelta(hours=9))]
        outcome = D.publish_conditions(
            index, D.derive_conditions(healthy, {}, now=NOW))
        assert outcome["published"] == []
        assert index.store.list_items() == []

    def test_still_open_conditions_do_not_churn_the_store(self):
        index = _index()
        failed = [_run("hourly", "20260906-110000", verdict="failed")]
        D.publish_conditions(index, D.derive_conditions(failed, {}, now=NOW))
        writes_before = len(index.store.item_writes)
        # Same state a cycle later: stale item stays open without a
        # rewrite; failed item is version-idempotent.
        later = NOW + timedelta(minutes=2)
        outcome = D.publish_conditions(
            index, D.derive_conditions(failed, {}, now=later))
        assert outcome["published"] == []
        assert len(index.store.item_writes) == writes_before

    def test_stale_reraises_after_a_resolve(self):
        index = _index()
        # 1: stale (a source machine that has never captured)
        D.publish_conditions(index, D.derive_conditions(
            [], {}, now=NOW, reports_exist=True))
        assert "backup:stale:hourly" in _open_ids(index)
        # 2: fresh capture resolves it
        fresh = [_run("hourly", "20260906-120000", at=NOW),
                 _run("daily", "20260906-030000", at=NOW - timedelta(hours=9))]
        D.publish_conditions(index, D.derive_conditions(fresh, {}, now=NOW))
        assert "backup:stale:hourly" not in _open_ids(index)
        # 3: hours later it goes stale again — a NEW needs_attention at a
        # higher wall-clock version reopens the same identity.
        later = NOW + timedelta(hours=8)
        D.publish_conditions(index, D.derive_conditions(fresh, {}, now=later))
        assert "backup:stale:hourly" in _open_ids(index)

    def test_one_bad_condition_cannot_silence_the_rest(self):
        index = _index()
        conditions = D.derive_conditions(
            [_run("hourly", "20260906-110000", verdict="failed")], {},
            now=NOW)
        conditions.insert(0, {
            "kind": "backup.failed", "attention_id": "backup:failed:hourly",
            "object_ref": "backup:tier:hourly",
            "attention_state": "needs_attention",
            "safe_title": "x", "safe_summary": "y",
            "occurred_at": NOW.timestamp(),
            "source_version": -1,  # refused by the index's validation
        })
        outcome = D.publish_conditions(index, conditions)
        assert outcome["errors"]
        assert "backup:failed:hourly" in outcome["published"]


class TestFleetNonSourceMachine:
    """A fleet machine with no backup configuration must not alarm about
    a job it was never given (operator-observed on SJC, 2026-09-08)."""

    def test_no_evidence_retracts_rather_than_falling_silent(self):
        """SJC carried two open needs_attention rows written before the
        source check existed; silence would strand them (host-measured
        2026-09-08). Every emitted row is a resolve, never a raise."""
        conditions = D.derive_conditions([], {}, now=NOW,
                                         reports_exist=False)
        assert conditions, "must retract, not return nothing"
        assert all(c["attention_state"] == "resolved" for c in conditions)
        assert {c["attention_id"] for c in conditions} >= {
            "backup:stale:hourly", "backup:stale:daily"}

    def test_retraction_clears_an_open_item_and_is_a_noop_otherwise(self):
        index = _index()
        # A machine that raised while it looked like a source...
        D.publish_conditions(index, D.derive_conditions(
            [], {}, now=NOW, reports_exist=True))
        assert "backup:stale:hourly" in _open_ids(index)
        # ...and is then recognized as a non-source, clears itself.
        D.publish_conditions(index, D.derive_conditions(
            [], {}, now=NOW, reports_exist=False))
        assert _open_ids(index) == set()
        # A second pass writes nothing: nothing is open to resolve.
        outcome = D.publish_conditions(index, D.derive_conditions(
            [], {}, now=NOW, reports_exist=False))
        assert outcome["published"] == []

    def test_one_past_run_makes_it_a_source_forever(self):
        runs = [_run("hourly", "20260901-110000",
                     at=NOW - timedelta(days=5))]
        conditions = D.derive_conditions(runs, {}, now=NOW,
                                         reports_exist=False)
        assert any(c["attention_state"] == "needs_attention"
                   for c in conditions)

    def test_configured_provider_makes_it_a_source_before_any_run(self):
        conditions = D.derive_conditions(
            [], {"offsite_provider": "b2"}, now=NOW, reports_exist=False)
        assert any(c["attention_id"] == "backup:stale:hourly"
                   and c["attention_state"] == "needs_attention"
                   for c in conditions)

    def test_report_directory_alone_makes_it_a_source(self):
        conditions = D.derive_conditions([], {}, now=NOW, reports_exist=True)
        assert any(c["attention_state"] == "needs_attention"
                   for c in conditions)

    def test_is_backup_source_predicate(self):
        assert not D.is_backup_source([], {}, reports_exist=False)
        assert D.is_backup_source([], {"offsite_provider": "b2"},
                                  reports_exist=False)
