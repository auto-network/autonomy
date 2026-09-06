"""The backup plugin's API: derivation logic and the request contracts.

Two properties matter most and both are pinned here:

- Staleness/health derive from persisted rows alone (driver S2), and
  every handler serves without ANY settings backend or filesystem —
  the reads are monkeypatched, proving no request path can block on
  the NFS backup root (driver S3).
- Configuration writes demand global authority; a worker session's
  bearer cannot re-schedule the machine's backups.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from tools.dashboard.plugins.backup.entrypoints import api

NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)


def _run(tier: str, stamp: str, verdict: str = "complete",
         at: datetime | None = None, **kw) -> dict:
    at = at or NOW - timedelta(hours=1)
    row = {"key": f"{tier}:{stamp}", "verdict": verdict,
           "started_at": at.isoformat(), "finished_at": at.isoformat(),
           "offsite": "complete", "total_bytes": 1000,
           "store_count": 24, "beads_databases": 3,
           "failures": [] if verdict == "complete" else ["auth: MISSING"]}
    row.update(kw)
    return row


class TestTierHealth:
    def test_fresh_success_is_ok(self):
        runs = [_run("hourly", "20260906-110000")]
        health = api.tier_health(runs, {}, "hourly", now=NOW)
        assert health["status"] == "ok"
        assert health["age_seconds"] == pytest.approx(3600)

    def test_old_success_is_stale(self):
        runs = [_run("hourly", "20260906-050000",
                     at=NOW - timedelta(hours=7))]
        health = api.tier_health(runs, {"staleness_multiple": 3.0}, "hourly",
                                 now=NOW)
        assert health["status"] == "stale"

    def test_no_runs_is_stale(self):
        assert api.tier_health([], {}, "hourly", now=NOW)["status"] == "stale"

    def test_newest_failed_run_is_failing(self):
        runs = [_run("hourly", "20260906-110000", verdict="failed"),
                _run("hourly", "20260906-100000",
                     at=NOW - timedelta(hours=2))]
        health = api.tier_health(runs, {}, "hourly", now=NOW)
        assert health["status"] == "failing"
        # ...but the last GOOD capture is still reported.
        assert health["last_success_key"] == "hourly:20260906-100000"

    def test_daily_interval_is_a_day(self):
        runs = [_run("daily", "20260905-030000",
                     at=NOW - timedelta(hours=30))]
        health = api.tier_health(runs, {"staleness_multiple": 3.0}, "daily",
                                 now=NOW)
        assert health["status"] == "ok"

    def test_tiers_do_not_bleed(self):
        runs = [_run("daily", "20260906-030000")]
        assert api.tier_health(runs, {}, "hourly", now=NOW)["status"] == "stale"


class TestSummarize:
    def test_failing_beats_stale(self):
        runs = [_run("hourly", "20260906-110000", verdict="failed"),
                _run("daily", "20260901-030000",
                     at=NOW - timedelta(days=5))]
        summary = api.summarize(runs, [], {}, now=NOW)
        assert summary["overall"] == "failing"

    def test_all_fresh_is_ok_with_passing_drill(self):
        runs = [_run("hourly", "20260906-110000"),
                _run("daily", "20260906-030000",
                     at=NOW - timedelta(hours=9))]
        drills = [{"key": "20260906-100000", "verdict": "pass",
                   "checks": [{"name": "integrity", "status": "ok"}]}]
        assert api.summarize(runs, drills, {}, now=NOW)["overall"] == "ok"

    def test_failed_drill_degrades_overall(self):
        runs = [_run("hourly", "20260906-110000"),
                _run("daily", "20260906-030000",
                     at=NOW - timedelta(hours=9))]
        drills = [{"key": "20260906-100000", "verdict": "fail"}]
        assert api.summarize(runs, drills, {}, now=NOW)["overall"] == "stale"

    def test_running_drill_surfaces(self):
        drills = [{"key": "20260906-113000", "verdict": "running"},
                  {"key": "20260906-100000", "verdict": "pass",
                   "checks": [{"name": "integrity", "status": "ok"}]}]
        summary = api.summarize([], drills, {}, now=NOW)
        assert summary["running_drill"]["key"] == "20260906-113000"
        assert summary["last_drill"]["key"] == "20260906-100000"


def _request(method="GET", query=b"", body: dict | None = None,
             ) -> Request:
    scope = {"type": "http", "method": method, "query_string": query,
             "headers": [(b"content-type", b"application/json")]}
    payload = json.dumps(body or {}).encode()

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(scope, receive)


@pytest.fixture
def rows(monkeypatch):
    """Handlers serve entirely from these fixtures — no settings
    backend, no filesystem. If a handler grew a filesystem probe, these
    tests would fail loudly on the missing monkeypatch."""
    data = {"runs": [], "drills": [], "config": {}}
    monkeypatch.setattr(api, "_rows", lambda set_id: (
        data["runs"] if set_id == api.RUN_SET_ID else data["drills"]))
    monkeypatch.setattr(api, "_read_config",
                        lambda: {**api._config_defaults(), **data["config"]})
    return data


def _call(handler, request):
    return asyncio.run(handler(request))


class TestRoutes:
    def test_summary_serves_from_rows(self, rows):
        rows["runs"] = [_run("hourly", "20260906-110000")]
        response = _call(api.get_summary, _request())
        body = json.loads(response.body)
        assert {t["tier"] for t in body["tiers"]} == {"hourly", "daily"}
        assert body["config"]["staleness_multiple"] == 3.0

    def test_runs_filters_tier_and_bounds_limit(self, rows):
        rows["runs"] = [_run("hourly", f"2026090{i}-000000")
                        for i in range(1, 7)]
        response = _call(api.get_runs,
                         _request(query=b"tier=hourly&limit=2"))
        body = json.loads(response.body)
        assert len(body["runs"]) == 2
        assert body["runs"][0]["key"] == "hourly:20260906-000000"

    def test_runs_refuses_unknown_tier(self, rows):
        response = _call(api.get_runs, _request(query=b"tier=weekly"))
        assert response.status_code == 400

    def test_put_config_requires_global_authority(self, rows, monkeypatch):
        monkeypatch.setattr(api, "principal_from_request",
                            lambda r: SimpleNamespace(global_authority=False))
        response = _call(api.put_config,
                         _request("PUT", body={"staleness_multiple": 2.0}))
        assert response.status_code == 403

    def test_put_config_validates_and_upserts(self, rows, monkeypatch):
        monkeypatch.setattr(api, "principal_from_request",
                            lambda r: SimpleNamespace(global_authority=True))
        written = {}

        def fake_add(set_id, rev, key, payload, *, org, **kw):
            written.update(set_id=set_id, key=key, payload=payload, org=org)

        import tools.graph.settings_ops as settings_ops
        monkeypatch.setattr(settings_ops, "write_by_key", fake_add)
        response = _call(api.put_config,
                         _request("PUT", body={"staleness_multiple": 2.0}))
        assert response.status_code == 200
        assert written["org"] == "machine"
        assert written["payload"]["staleness_multiple"] == 2.0

    def test_put_config_refuses_unknown_fields(self, rows, monkeypatch):
        monkeypatch.setattr(api, "principal_from_request",
                            lambda r: SimpleNamespace(global_authority=True))
        response = _call(api.put_config,
                         _request("PUT", body={"nas_path": "/mnt/x"}))
        assert response.status_code == 400
