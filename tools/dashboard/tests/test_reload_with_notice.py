"""Zero-downtime reload supervisor: spawn-first, terminate-after-ready.

The supervisor is exercised through fakes for the two things it drives — the
worker processes and the file watcher — so every ordering claim in the module
docstring is asserted here without spawning Uvicorn.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from tools.dashboard import reload_with_notice as rwn
from tools.dashboard import worker_handoff as wh


class FakeProcess:
    _next_pid = 1000

    def __init__(self):
        FakeProcess._next_pid += 1
        self.pid = FakeProcess._next_pid
        self.alive = True
        self.exitcode = None
        self.events: list[str] = []

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.events.append("terminate")
        self.alive = False
        self.exitcode = -15

    def kill(self):
        self.events.append("kill")
        self.alive = False
        self.exitcode = -9

    def join(self, timeout=None):
        self.events.append("join")

    def die(self, code=1):
        self.alive = False
        self.exitcode = code


class FakeSupervisor:
    """Just the BaseReload surface the patched seams touch."""

    def __init__(self, tmp_path, incumbent):
        self.config = SimpleNamespace(port=8080, ssl_certfile=None)
        self.target = None
        self.sockets = []
        self.reloader_name = "FakeWatch"
        self.pid = os.getpid()
        self.process = incumbent
        self._ready_marker = tmp_path / "incumbent.ready"
        self._ready_marker.write_text("x")
        self._last_respawn = 0.0
        self.spawned: list[tuple[FakeProcess, object, int | None]] = []
        self.timeline: list[str] = []
        # Scripted watcher: each entry is a callable run on one should_restart
        # poll; it may return a change list, raise StopIteration, or act on
        # the fakes (create the ready marker, kill the child).
        self.script: list = []
        self._tmp = tmp_path

    def should_restart(self):
        self.timeline.append("poll")
        if not self.script:
            return None
        step = self.script.pop(0)
        return step()

    def spawn(self, predecessor_pid):
        child = FakeProcess()
        marker = self._tmp / f"child-{child.pid}.ready"
        self.spawned.append((child, marker, predecessor_pid))
        self.timeline.append(f"spawn:{child.pid}")
        return child, marker

    def current_child(self):
        return self.spawned[-1]


@pytest.fixture
def sup(tmp_path, monkeypatch):
    incumbent = FakeProcess()
    supervisor = FakeSupervisor(tmp_path, incumbent)
    monkeypatch.setattr(rwn, "_spawn", lambda self, pid, *_a: self.spawn(pid))
    notices = []
    monkeypatch.setattr(rwn, "_notify_dashboard", lambda config, changed=None, **kw: notices.append(changed) or True)
    supervisor.notices = notices
    supervisor.incumbent = incumbent
    return supervisor


def _make_ready(sup):
    def step():
        child, marker, _ = sup.current_child()
        wh.mark_ready({wh.READY_MARKER_ENV: str(marker)})
        sup.timeline.append("ready")
        return None
    return step


def _kill_child(sup, code=3):
    def step():
        child, _, _ = sup.current_child()
        child.die(code)
        sup.timeline.append("child-died")
        return None
    return step


def _changes(paths):
    return lambda: list(paths)


def _exit():
    def step():
        raise StopIteration()
    return step


# ── the happy path ────────────────────────────────────────────────────────

def test_incumbent_terminated_only_after_replacement_ready(sup):
    incumbent = sup.incumbent
    sup.script = [lambda: None, lambda: None, _make_ready(sup)]

    rwn._restart_with_handoff(sup)

    child, marker, predecessor = sup.current_child()
    assert predecessor == incumbent.pid
    # Order: spawn, polls while old serves, ready, THEN terminate old.
    assert sup.timeline == [f"spawn:{child.pid}", "poll", "poll", "poll", "ready"]
    assert incumbent.events == ["terminate", "join"]
    assert sup.process is child
    assert sup._ready_marker == marker
    assert wh.activation_marker_path(marker).exists()
    # The incumbent's own markers are gone; the new worker's stay for it.
    assert not (sup._tmp / "incumbent.ready").exists()
    assert len(sup.notices) == 1                  # incumbent asked to snapshot once


def test_notice_sent_before_spawn_so_incumbent_snapshots_first(sup, monkeypatch):
    order = []
    monkeypatch.setattr(rwn, "_notify_dashboard", lambda config, changed=None, **kw: order.append("notice") or True)
    real_spawn = sup.spawn

    def spawn(pid):
        order.append("spawn")
        return real_spawn(pid)
    monkeypatch.setattr(rwn, "_spawn", lambda self, pid, *_a: spawn(pid))
    sup.script = [_make_ready(sup)]

    rwn._restart_with_handoff(sup)

    assert order == ["notice", "spawn"]


# ── failure modes keep the incumbent ──────────────────────────────────────

def test_replacement_dying_before_ready_keeps_incumbent(sup, caplog):
    incumbent = sup.incumbent
    sup.script = [lambda: None, _kill_child(sup, code=7)]

    with caplog.at_level("ERROR", logger="uvicorn.error"):
        rwn._restart_with_handoff(sup)

    child, marker, _ = sup.current_child()
    assert incumbent.events == []                 # never touched
    assert sup.process is incumbent
    assert child.events == ["join"]               # reaped, not re-terminated
    assert not marker.exists()
    assert not wh.activation_marker_path(marker).exists()
    assert any("exited with code 7 before becoming ready" in r.message for r in caplog.records)


def test_replacement_timeout_keeps_incumbent(sup, monkeypatch, caplog):
    incumbent = sup.incumbent
    monkeypatch.setenv(rwn.READY_TIMEOUT_ENV, "0.01")
    t = [0.0]

    def clock():
        t[0] += 0.004
        return t[0]
    monkeypatch.setattr(rwn.time, "monotonic", clock)
    sup.script = [lambda: None] * 10

    with caplog.at_level("ERROR", logger="uvicorn.error"):
        rwn._restart_with_handoff(sup)

    child, marker, _ = sup.current_child()
    assert incumbent.events == []
    assert sup.process is incumbent
    assert "terminate" in child.events
    assert any("did not become ready within" in r.message for r in caplog.records)


def test_multi_minute_startup_is_not_a_timeout_by_default(sup, monkeypatch):
    """The 2026-09-07 case: startup took 181 s. Default timeout must cover it."""
    incumbent = sup.incumbent
    t = [0.0]

    def clock():
        t[0] += 30.0          # every poll is 30 s later
        return t[0]
    monkeypatch.setattr(rwn.time, "monotonic", clock)
    sup.script = [lambda: None] * 6 + [_make_ready(sup)]   # ready after ~200 s

    rwn._restart_with_handoff(sup)

    assert sup.process is sup.current_child()[0]
    assert incumbent.events == ["terminate", "join"]


def test_supervisor_exit_during_handoff_stops_replacement_keeps_incumbent(sup):
    incumbent = sup.incumbent
    sup.script = [lambda: None, _exit()]

    rwn._restart_with_handoff(sup)

    child, marker, _ = sup.current_child()
    assert sup.process is incumbent         # shutdown() will stop it
    assert incumbent.events == []
    assert "terminate" in child.events
    assert not marker.exists()


# ── newer changes while a replacement is still starting ───────────────────

def test_changes_during_startup_supersede_the_pending_replacement(sup, caplog):
    incumbent = sup.incumbent
    sup.script = [lambda: None, _changes(["server.py"]), lambda: None, _make_ready(sup)]

    with caplog.at_level("WARNING", logger="uvicorn.error"):
        rwn._restart_with_handoff(sup)

    assert len(sup.spawned) == 2
    first, first_marker, pred1 = sup.spawned[0]
    second, second_marker, pred2 = sup.spawned[1]
    assert pred1 == pred2 == incumbent.pid      # both replace the SAME incumbent
    assert "terminate" in first.events           # stale replacement stopped
    assert sup.process is second
    assert incumbent.events == ["terminate", "join"]
    assert wh.activation_marker_path(second_marker).exists()
    assert not first_marker.exists()
    assert any("superseding it" in r.message for r in caplog.records)


# ── a dead incumbent ──────────────────────────────────────────────────────

def test_dead_incumbent_means_replacement_activates_immediately(sup):
    sup.incumbent.die(139)
    sup.script = [_make_ready(sup)]

    rwn._restart_with_handoff(sup)

    child, marker, predecessor = sup.current_child()
    assert predecessor is None                   # no one to wait for
    assert sup.notices == []                     # nobody to notify
    assert sup.process is child


def test_run_loop_respawns_a_worker_that_died(sup, monkeypatch, tmp_path):
    sup.process.die(1)
    sup._last_respawn = -1000.0
    monkeypatch.setattr(rwn.time, "monotonic", lambda: 0.0)

    rwn._respawn_dead_worker(sup)

    assert len(sup.spawned) == 1
    assert sup.process is sup.spawned[0][0]
    assert sup.spawned[0][2] is None
    assert not (tmp_path / "incumbent.ready").exists()


def test_respawn_honours_backoff(sup, monkeypatch):
    sup.process.die(1)
    sup._last_respawn = 5.0
    monkeypatch.setattr(rwn.time, "monotonic", lambda: 5.0 + rwn._RESPAWN_BACKOFF_SECONDS / 2)

    rwn._respawn_dead_worker(sup)

    assert sup.spawned == []


# ── the real seams ────────────────────────────────────────────────────────

def test_spawn_sets_handoff_env_for_child_and_restores_it(monkeypatch, tmp_path):
    seen = {}

    class Proc:
        def start(self):
            seen["env"] = {
                k: os.environ.get(k)
                for k in (wh.READY_MARKER_ENV, wh.PREDECESSOR_PID_ENV)
            }

    monkeypatch.setattr(rwn, "get_subprocess", lambda **kw: Proc())
    monkeypatch.setattr(rwn, "_handoff_dir", tmp_path)
    monkeypatch.setenv(wh.READY_MARKER_ENV, "outer-value")
    monkeypatch.delenv(wh.PREDECESSOR_PID_ENV, raising=False)
    fake = SimpleNamespace(config=None, target=None, sockets=[])

    process, marker = rwn._spawn(fake, 777)

    assert seen["env"][wh.READY_MARKER_ENV] == str(marker)
    assert seen["env"][wh.PREDECESSOR_PID_ENV] == "777"
    assert marker.parent == tmp_path
    # Parent env restored exactly.
    assert os.environ[wh.READY_MARKER_ENV] == "outer-value"
    assert wh.PREDECESSOR_PID_ENV not in os.environ

    rwn._spawn(fake, None)
    assert seen["env"][wh.PREDECESSOR_PID_ENV] is None


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_notice_posts_handoff_mode_with_token(monkeypatch):
    seen = {}
    monkeypatch.setenv("DASHBOARD_RESTART_TOKEN", "shared-token")

    def urlopen(req, *, timeout, context):
        seen["url"] = req.full_url
        seen["token"] = req.headers["X-dashboard-restart-token"]
        seen["body"] = json.loads(req.data)
        seen["timeout"] = timeout
        seen["context"] = context
        return _Response()

    monkeypatch.setattr(rwn.request, "urlopen", urlopen)
    assert rwn._notify_dashboard(
        SimpleNamespace(port=8080, ssl_certfile=None),
        ["/app/tools/dashboard/server.py"],
    )
    assert seen == {
        "url": "http://127.0.0.1:8080/api/internal/restart-notice",
        "token": "shared-token",
        "body": {
            "mode": "handoff",
            "changed_files": ["/app/tools/dashboard/server.py"],
        },
        "timeout": 10,
        "context": None,
    }


def test_notify_dashboard_sends_empty_list_when_no_changes(monkeypatch):
    seen = {}
    monkeypatch.setenv("DASHBOARD_RESTART_TOKEN", "shared-token")

    def urlopen(req, *, timeout, context):
        seen["body"] = req.data
        return _Response()

    monkeypatch.setattr(rwn.request, "urlopen", urlopen)
    assert rwn._notify_dashboard(SimpleNamespace(port=8080, ssl_certfile=None))
    assert json.loads(seen["body"]) == {"mode": "handoff", "changed_files": []}


def test_notify_dashboard_forwards_changed_paths(monkeypatch):
    seen = {}
    monkeypatch.setenv("DASHBOARD_RESTART_TOKEN", "shared-token")

    def urlopen(req, *, timeout, context):
        seen["body"] = req.data
        return _Response()
    monkeypatch.setattr(rwn.request, "urlopen", urlopen)
    from pathlib import Path
    assert rwn._notify_dashboard(
        SimpleNamespace(port=8080, ssl_certfile=None), [Path("/app/server.py"), ""]
    )
    assert json.loads(seen["body"]) == {
        "mode": "handoff", "changed_files": ["/app/server.py"],
    }


def test_handoff_notice_carries_the_paths_the_watcher_saw(sup, monkeypatch):
    notices = []
    monkeypatch.setattr(
        rwn, "_notify_dashboard",
        lambda config, changed=None, **kw: notices.append(changed) or True)
    sup._last_changed_paths = ["/app/tools/dashboard/server.py"]
    sup.script = [_make_ready(sup)]

    rwn._restart_with_handoff(sup)

    assert notices == [["/app/tools/dashboard/server.py"]]


def test_supersede_remembers_the_newer_changes(sup):
    sup._last_changed_paths = ["/app/old.py"]
    sup.script = [_changes(["/app/newer.py"]), _make_ready(sup)]

    rwn._restart_with_handoff(sup)

    assert sup._last_changed_paths == ["/app/newer.py"]


def test_notice_failure_does_not_block(monkeypatch):
    monkeypatch.setenv("DASHBOARD_RESTART_TOKEN", "t")

    def urlopen(*_a, **_k):
        raise OSError("connection refused")
    monkeypatch.setattr(rwn.request, "urlopen", urlopen)
    assert rwn._notify_dashboard(SimpleNamespace(port=8080, ssl_certfile=None),
                                 sleep=lambda s: None) is False


def test_notice_retries_a_busy_incumbent_and_logs_the_exception_type(monkeypatch, caplog):
    """auto-wb6ok: a timeout is retried with backoff and named in the log."""
    monkeypatch.setenv("DASHBOARD_RESTART_TOKEN", "t")
    calls, slept = [], []

    def urlopen(*_a, **_k):
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError("timed out")
        return _Response()
    monkeypatch.setattr(rwn.request, "urlopen", urlopen)
    with caplog.at_level("WARNING", logger="uvicorn.error"):
        assert rwn._notify_dashboard(SimpleNamespace(port=8080, ssl_certfile=None),
                                     sleep=slept.append)
    assert len(calls) == 3 and slept == [1.0, 2.0]
    assert "attempt 1/3 failed: TimeoutError: timed out" in caplog.text


def test_notice_gives_up_and_reports_the_last_failure(monkeypatch, caplog):
    monkeypatch.setenv("DASHBOARD_RESTART_TOKEN", "t")
    monkeypatch.setattr(rwn.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(
        rwn.error.URLError(ConnectionRefusedError(111, "Connection refused"))))
    failures = []
    with caplog.at_level("ERROR", logger="uvicorn.error"):
        assert rwn._notify_dashboard(SimpleNamespace(port=8080, ssl_certfile=None),
                                     failures=failures, sleep=lambda s: None) is False
    assert failures == ["URLError: ConnectionRefusedError: [Errno 111] Connection refused"]
    assert "hand-off notice failed (URLError: ConnectionRefusedError" in caplog.text


def test_wrong_token_is_not_retried(monkeypatch):
    monkeypatch.setenv("DASHBOARD_RESTART_TOKEN", "t")
    calls = []

    def urlopen(req, *_a, **_k):
        calls.append(1)
        raise rwn.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
    monkeypatch.setattr(rwn.request, "urlopen", urlopen)
    failures = []
    assert rwn._notify_dashboard(SimpleNamespace(port=8080, ssl_certfile=None),
                                 failures=failures, sleep=lambda s: None) is False
    assert calls == [1] and failures == ["HTTPError 403"]


def test_failed_notice_is_handed_to_the_replacement(sup, monkeypatch):
    """The replacement learns the notice failed, so it can raise attention."""
    spawned = []

    def notify(config, changed=None, *, failures=None, **kw):
        failures.append("TimeoutError: timed out")
        return False
    monkeypatch.setattr(rwn, "_notify_dashboard", notify)
    monkeypatch.setattr(rwn, "_spawn", lambda self, pid, failure=None:
                        spawned.append(failure) or self.spawn(pid))
    sup.script = [_make_ready(sup)]
    rwn._restart_with_handoff(sup)
    assert spawned == ["TimeoutError: timed out"]


def test_spawn_passes_the_notice_failure_env_and_restores_it(monkeypatch, tmp_path):
    seen = {}

    class _Proc:
        pid = 1

        def start(self):
            seen["env"] = os.environ.get(rwn.worker_handoff.NOTICE_FAILURE_ENV)

    monkeypatch.setattr(rwn, "get_subprocess", lambda **kw: _Proc())
    monkeypatch.setattr(rwn, "_handoff_dir", tmp_path)
    monkeypatch.delenv(rwn.worker_handoff.NOTICE_FAILURE_ENV, raising=False)
    fake = SimpleNamespace(config=None, target=None, sockets=[])
    rwn._spawn(fake, 777, "TimeoutError: timed out")
    assert seen["env"] == "TimeoutError: timed out"
    assert rwn.worker_handoff.NOTICE_FAILURE_ENV not in os.environ
    rwn._spawn(fake, 777)
    assert seen["env"] is None


def test_next_capturing_stashes_changed_paths(monkeypatch):
    from pathlib import Path

    captured = SimpleNamespace()
    monkeypatch.setattr(rwn, "_original_next", lambda self: [Path("/app/server.py")])
    result = rwn._next_capturing(captured)
    assert result == [Path("/app/server.py")]
    assert captured._last_changed_paths == ["/app/server.py"]


def test_ready_timeout_env_parsing(monkeypatch):
    monkeypatch.delenv(rwn.READY_TIMEOUT_ENV, raising=False)
    assert rwn._ready_timeout_seconds() == rwn._DEFAULT_READY_TIMEOUT_SECONDS
    monkeypatch.setenv(rwn.READY_TIMEOUT_ENV, "120")
    assert rwn._ready_timeout_seconds() == 120.0
    monkeypatch.setenv(rwn.READY_TIMEOUT_ENV, "nope")
    assert rwn._ready_timeout_seconds() == rwn._DEFAULT_READY_TIMEOUT_SECONDS
    monkeypatch.setenv(rwn.READY_TIMEOUT_ENV, "-1")
    assert rwn._ready_timeout_seconds() == rwn._DEFAULT_READY_TIMEOUT_SECONDS


def test_seams_are_installed_on_basereload():
    from uvicorn.supervisors.basereload import BaseReload
    assert BaseReload.restart is rwn._restart_with_handoff
    assert BaseReload.startup is rwn._startup_with_handoff
    assert BaseReload.run is rwn._run_with_handoff
    assert BaseReload.shutdown is rwn._shutdown_with_handoff
    assert BaseReload.__next__ is rwn._next_capturing


def test_startup_binds_the_plain_socket_before_the_first_spawn(sup, monkeypatch):
    """The plain-HTTP socket is the supervisor's, bound once and inherited by
    every worker, so a hand-off never leaves it accepting into a void."""
    import socket

    from tools.dashboard import plain_listener as pl

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv(pl.PORT_ENV, str(port))
    monkeypatch.setenv(pl.HOST_ENV, "127.0.0.1")
    monkeypatch.setattr(rwn.signal, "signal", lambda *_a, **_k: None)
    sup.signal_handler = lambda *_a: None
    order = []
    original_attach = pl.attach_to_supervisor
    monkeypatch.setattr(
        pl, "attach_to_supervisor",
        lambda supervisor, environ=None: order.append("bind") or original_attach(supervisor, environ),
    )
    original_spawn = sup.spawn
    sup.spawn = lambda pid: order.append("spawn") or original_spawn(pid)

    rwn._startup_with_handoff(sup)
    try:
        assert order == ["bind", "spawn"]
        assert sup.sockets[-1].getsockname() == ("127.0.0.1", port)
        assert isinstance(sup.target, pl.SplitTarget)
    finally:
        for sock in sup.sockets:
            sock.close()


class _LoopingFake(FakeSupervisor):
    """The fake with the BaseReload surface `_run_with_handoff` drives: the
    watcher iteration, and the patched startup/restart/shutdown seams."""

    def __iter__(self):
        return self

    def __next__(self):
        return self.should_restart()

    def startup(self):
        rwn._startup_with_handoff(self)

    def restart(self):
        rwn._restart_with_handoff(self)

    def shutdown(self):
        self.timeline.append("shutdown")


def _looping(sup, monkeypatch):
    fake = _LoopingFake(sup._tmp, sup.incumbent)
    fake.notices = sup.notices
    monkeypatch.setattr(rwn, "_notify_dashboard", lambda config, changed=None, **kw: fake.notices.append(changed) or True)
    monkeypatch.setattr(rwn.signal, "signal", lambda *_a, **_k: None)
    fake.signal_handler = lambda *_a: None
    return fake


def test_a_renewed_tls_pair_triggers_the_same_handoff(sup, monkeypatch, tmp_path):
    """auto-1ei8m: the renewal script only installs the new pair; the
    supervisor notices the change and hands off (Config.load() builds the SSL
    context per worker), so the vault stays warm and no connection drops."""
    import os

    from tools.dashboard.tests.test_certificate_attention import _pem_pair

    cert, key = _pem_pair(tmp_path, ["node.tailabcd.ts.net"], days=30)
    (tmp_path / "renewed").mkdir()
    new_cert, new_key = _pem_pair(tmp_path / "renewed", ["node.tailabcd.ts.net"], days=90)
    sup = _looping(sup, monkeypatch)
    sup.config.ssl_certfile = str(cert)
    sup.config.ssl_keyfile = str(key)

    def renew():
        # As the renewal script does: the key moves first, then the certificate.
        key.write_bytes(new_key.read_bytes())
        cert.write_bytes(new_cert.read_bytes())
        os.utime(cert, ns=(cert.stat().st_atime_ns, cert.stat().st_mtime_ns + 1_000_000))
        sup.timeline.append("renewed")
        return None

    # startup spawns the first worker; the ready step makes it ready; the
    # renewal lands; the next poll hands off and that replacement is made
    # ready by the following step.
    sup.script = [_make_ready(sup), renew, _make_ready(sup), _exit()]
    rwn._run_with_handoff(sup)

    spawns = [event for event in sup.timeline if event.startswith("spawn:")]
    assert len(spawns) == 2, sup.timeline                 # the first worker, then the hand-off
    assert sup.timeline.index("renewed") < sup.timeline.index(spawns[1])
    assert sup.notices[-1] == [str(cert)]                 # the notice names the certificate


def test_an_unchanged_tls_pair_never_restarts(sup, monkeypatch, tmp_path):
    from tools.dashboard.tests.test_certificate_attention import _pem_pair

    cert, key = _pem_pair(tmp_path, ["node.tailabcd.ts.net"], days=30)
    sup = _looping(sup, monkeypatch)
    sup.config.ssl_certfile = str(cert)
    sup.config.ssl_keyfile = str(key)
    sup.script = [_make_ready(sup), lambda: None, lambda: None, _exit()]
    rwn._run_with_handoff(sup)
    assert len([e for e in sup.timeline if e.startswith("spawn:")]) == 1
