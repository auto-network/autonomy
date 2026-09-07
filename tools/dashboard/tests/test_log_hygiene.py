"""Log hygiene (B): once-per-state logging, stall-report compaction, the
access line, the event-proxy pre-filter, and the cert manager's quiet deferral."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from tools.dashboard import log_throttle, stall_report


# ── StateChangeLogger ─────────────────────────────────────────────────────

class _Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


def test_state_logger_emits_on_new_change_and_interval(caplog):
    clock = _Clock()
    scl = log_throttle.StateChangeLogger(interval_s=60.0, clock=clock)
    log = logging.getLogger("test.throttle")
    with caplog.at_level(logging.INFO, logger="test.throttle"):
        assert scl.emit(log, logging.WARNING, "k", "behind", "behind by %d", 5)
        for n in range(10):                       # same state inside the interval: quiet
            assert not scl.emit(log, logging.WARNING, "k", "behind", "behind by %d", n)
        clock.t = 61.0                            # interval elapsed: one count line
        assert scl.emit(log, logging.WARNING, "k", "behind", "behind by %d", 99)
        clock.t = 70.0
        assert not scl.emit(log, logging.WARNING, "k", "behind", "behind by %d", 1)
        assert scl.emit(log, logging.INFO, "k", "current", "caught up")   # state change
        assert not scl.emit(log, logging.INFO, "k", "current", "caught up")
    msgs = [r.getMessage() for r in caplog.records]
    assert msgs == [
        "behind by 5",
        "behind by 99 (×10 in the last 60s)",
        "caught up (previous state repeated 1×)",
    ]


def test_state_logger_keys_are_independent():
    scl = log_throttle.StateChangeLogger(interval_s=60.0, clock=_Clock())
    assert scl.decide(("a", "GET", "/x"), "refused") == ("new", 0)
    assert scl.decide(("b", "GET", "/x"), "refused") == ("new", 0)
    assert scl.decide(("a", "GET", "/x"), "refused") == (None, 1)
    scl.forget(("a", "GET", "/x"))
    assert scl.decide(("a", "GET", "/x"), "refused") == ("new", 0)


# ── stall_report ──────────────────────────────────────────────────────────

ROOT = "/repo"


def _F(file, line, func):
    return stall_report.Frame(file, line, func)


OUTER = [_F("/usr/lib/python3.12/asyncio/base_events.py", 1, "run_forever"),
         _F("/usr/lib/python3.12/asyncio/events.py", 2, "_run")]
STACK_A = OUTER + [_F("/repo/tools/dashboard/server.py", 100, "handler"),
                   _F("/repo/tools/graph/db.py", 412, "_reconstruct_read_state"),
                   _F("/usr/lib/python3.12/sqlite3/__init__.py", 5, "execute")]
STACK_B = OUTER + [_F("/repo/tools/dashboard/server.py", 200, "other"),
                   _F("/repo/tools/graph/org_ops.py", 50, "list_orgs")]


def test_compact_drops_frames_above_the_first_repo_frame():
    compacted = stall_report.compact(STACK_A, ROOT)
    assert compacted[0].file == "/repo/tools/dashboard/server.py"
    assert len(compacted) == 3
    assert stall_report.compact(OUTER, ROOT) == OUTER          # nothing ours: keep all


def test_leaf_is_the_innermost_repo_frame():
    lf = stall_report.leaf(STACK_A, ROOT)
    assert (lf.rel(ROOT), lf.line, lf.func) == ("tools/graph/db.py", 412, "_reconstruct_read_state")
    assert stall_report.leaf([], ROOT) is None


def test_format_uses_repo_relative_paths():
    text = stall_report.format_frames(stall_report.compact(STACK_A, ROOT), ROOT)
    assert text.splitlines()[0] == "  tools/dashboard/server.py:100 in handler"
    assert "/repo/" not in text


def _tracker(caplog_logger, clock=None, rollup=None):
    return stall_report.EpisodeTracker(
        caplog_logger, repo_root=ROOT, max_dumps=60, resample_s=0.4, rollup=rollup,
    )


def test_identical_consecutive_samples_collapse_and_episode_summarises(caplog):
    log = logging.getLogger("test.stall")
    tr = _tracker(log)
    hb = 100.0
    with caplog.at_level(logging.WARNING, logger="test.stall"):
        # 8 samples of the same stack, 0.45 s apart (the sampler ticks every
        # 0.1 s, so real spacing is never exactly the resample interval).
        for i in range(8):
            now = hb + 0.5 + i * 0.45
            tr.observe(hb, now, now - hb, STACK_A)
        tr.idle(hb + 3.4, hb + 3.5)            # loop ticked at 103.4
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1, "one distinct stack → exactly one stack dump"
    assert "STALL STACK #1" in errors[0].getMessage()
    assert "  tools/graph/db.py:412 in _reconstruct_read_state" in errors[0].getMessage()
    assert "asyncio" not in errors[0].getMessage()
    summary = [r.getMessage() for r in caplog.records if r.getMessage().startswith("STALL ")]
    assert summary == [
        "STALL 3.4s leaf=tools/graph/db.py:412 via _reconstruct_read_state "
        "samples=8 distinct=1 dominant=8 collapsed=7"
    ]
    assert not tr.active


def test_varied_stacks_each_dump_once_and_dominant_wins(caplog):
    log = logging.getLogger("test.stall2")
    tr = _tracker(log)
    hb = 0.0
    with caplog.at_level(logging.WARNING, logger="test.stall2"):
        seq = [STACK_A, STACK_A, STACK_B, STACK_A, STACK_A]
        for i, st in enumerate(seq):
            now = 0.5 + i * 0.45
            tr.observe(hb, now, now, st)
        tr.idle(2.5, 2.6)
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 3                     # A, B, A — only consecutive repeats collapse
    summary = [r.getMessage() for r in caplog.records if r.getMessage().startswith("STALL ")][0]
    assert "leaf=tools/graph/db.py:412 via _reconstruct_read_state" in summary
    assert "samples=5 distinct=2 dominant=4 collapsed=2" in summary


def test_resample_interval_and_dump_cap_are_honoured(caplog):
    log = logging.getLogger("test.stall3")
    tr = stall_report.EpisodeTracker(log, repo_root=ROOT, max_dumps=3, resample_s=0.4)
    with caplog.at_level(logging.WARNING, logger="test.stall3"):
        for i in range(20):                      # 0.1 s apart → only every 4th counts, cap 3
            now = 0.5 + i * 0.1
            tr.observe(0.0, now, now, STACK_A)
        tr.idle(3.0, 3.1)
    summary = [r.getMessage() for r in caplog.records if r.getMessage().startswith("STALL ")][0]
    assert "samples=3" in summary


def test_rollup_names_top_leaves_once_per_period(caplog):
    log = logging.getLogger("test.rollup")
    clock = _Clock(0.0)
    ru = stall_report.Rollup(log, period_s=600.0, top=2, clock=clock)
    ru.add("tools/graph/db.py:412 via _reconstruct_read_state", 3.0)
    ru.add("tools/graph/db.py:412 via _reconstruct_read_state", 2.0)
    ru.add("tools/graph/org_ops.py:50 via list_orgs", 4.0)
    ru.add("tools/x.py:1 via minor", 0.1)
    with caplog.at_level(logging.WARNING, logger="test.rollup"):
        assert not ru.maybe_emit(300.0)         # period not elapsed
        assert ru.maybe_emit(600.0)
        assert not ru.maybe_emit(1200.0)        # nothing accumulated since
    msg = caplog.records[0].getMessage()
    assert msg.startswith("STALL ROLLUP 10m: 4 episode(s), 9.1s blocked; top leaves: ")
    assert "tools/graph/db.py:412 via _reconstruct_read_state n=2 total=5.0s" in msg
    assert "list_orgs n=1 total=4.0s" in msg
    assert "minor" not in msg


def test_frames_from_reads_a_live_frame():
    import sys
    frames = stall_report.frames_from(sys._getframe())
    assert frames[-1].func == "test_frames_from_reads_a_live_frame"
    assert frames[-1].file.endswith("test_log_hygiene.py")


# ── the access line ───────────────────────────────────────────────────────

def _req(path="/api/x", query="", headers=None, client=("10.0.0.7", 1234)):
    return SimpleNamespace(
        method="GET",
        url=SimpleNamespace(path=path, query=query),
        headers=headers or {},
        client=SimpleNamespace(host=client[0], port=client[1]) if client else None,
    )


def test_client_address_prefers_forwarded_then_peer():
    from tools.dashboard.server import _client_address
    assert _client_address(_req(headers={"x-forwarded-for": "203.0.113.9, 10.0.0.1"})) == "203.0.113.9"
    assert _client_address(_req()) == "10.0.0.7"
    assert _client_address(_req(client=None)) == "-"


def test_access_line_truncates_the_query():
    from tools.dashboard.server import _RequestDurationMiddleware as M
    method, target, status, dur, client = M._describe(_req(query="a=" + "x" * 200), 200, 1.5)
    assert target.startswith("/api/x?a=")
    assert len(target) == len("/api/x?") + M._QUERY_MAX + 1 and target.endswith("…")
    assert M._describe(_req(), 404, 2.0)[1] == "/api/x"


# ── event-proxy pre-filter ────────────────────────────────────────────────

def test_relay_publisher_wants_only_its_presence_set():
    from tools.dashboard.plugins.mission_control import relay_publisher as rp
    assert rp.wants_event(rp.PRESENCE_TOPIC, {"set_id": rp.PRESENCE_SET_ID, "key": "k"})
    assert not rp.wants_event(rp.PRESENCE_TOPIC, {"set_id": "dashboard.feature_flags"})
    assert not rp.wants_event(rp.PRESENCE_TOPIC, "not-a-dict")
    assert rp.wants_event(rp.CONVERSATION_TOPIC, {"mission_id": "m"})


def test_wants_helper_forwards_when_a_filter_raises():
    from tools.dashboard import link_serving

    def boom(topic, data):
        raise RuntimeError("x")
    assert link_serving._wants(boom, "t", {}) is True
    assert link_serving._wants(lambda t, d: False, "t", {}) is False


# ── cert manager: expected refusals do not traceback ─────────────────────

def test_cert_manager_defers_expected_errors_without_traceback(caplog, monkeypatch):
    import asyncio
    from tools.dashboard import service_certificate, service_certificate_manager as scm

    mgr = scm.ServiceCertificateManager(
        now=lambda: 0, desired_fn=lambda: [("autonomy", "persona-1")],
    )

    def unavailable(org, persona):
        raise service_certificate.ServiceCertificateError("certificate vault bundle is unavailable")
    monkeypatch.setattr(service_certificate, "certificate_metadata", unavailable)

    with caplog.at_level(logging.WARNING, logger=scm.logger.name):
        healthy = asyncio.run(mgr.reconcile_once())
    assert healthy is False
    assert mgr.errors[("autonomy", "persona-1")].startswith("ServiceCertificateError:")
    rec = [r for r in caplog.records if "Service certificate reconciliation" in r.getMessage()]
    assert len(rec) == 1
    assert "deferred for autonomy/persona-1: certificate vault bundle is unavailable" in rec[0].getMessage()
    assert rec[0].exc_info is None, "an expected refusal must not print a traceback"

    def unexpected(org, persona):
        raise KeyError("boom")
    monkeypatch.setattr(service_certificate, "certificate_metadata", unexpected)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=scm.logger.name):
        asyncio.run(mgr.reconcile_once())
    rec = [r for r in caplog.records if "reconciliation failed" in r.getMessage()]
    assert len(rec) == 1 and rec[0].exc_info is not None
