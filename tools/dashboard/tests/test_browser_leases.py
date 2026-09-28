"""auto-czoc0: lease records, launch arguments, routes and reconciler decisions.

Docker and the caller's token are stand-ins; the containment thresholds are
proven by real runs on the node.
"""

import time

import pytest

from tools.dashboard import browser_containers as containers
from tools.dashboard import browser_reconciler as reconciler
from tools.dashboard import browser_routes as routes
from tools.dashboard.capability_gate import CallerScope, CapabilityRefused
from tools.dashboard.dao import browser_leases as store
from tools.dashboard.dao import dashboard_db
from tools.graph.schemas import browser_defaults

LIMITS = browser_defaults.resolved(None)


@pytest.fixture
def db(tmp_path, monkeypatch):
    dashboard_db.init_db(tmp_path / "dashboard.db")
    monkeypatch.setattr(store, "_ready_path", None)
    yield
    dashboard_db.close_db() if hasattr(dashboard_db, "close_db") else None


def _admit(epoch, lease_hash="h" * 64, max_leases=4, session="auto-owner", name=None):
    store.admit(epoch=epoch, max_leases=max_leases, lease_hash_=lease_hash, session=session,
                org="autonomy", workspace="ws", profile_kind="persistent" if name else "ephemeral",
                profile_name=name, adapter="chrome-headed", container_name="brw-e-x",
                expires_at=time.time() + 600, secret="s" * 64, vnc_password="p@ss!wd8")
    return lease_hash


# ── records ────────────────────────────────────────────────────────────


def test_lease_ids_are_opaque_and_stored_only_as_hashes(db):
    lease_id = store.new_lease_id()
    assert lease_id.startswith("brl_") and len(lease_id) == 4 + 32
    epoch = store.take_epoch()
    h = _admit(epoch, store.lease_hash(lease_id))
    row = store.get(h)
    assert lease_id not in repr(row.__dict__)
    assert row.secret == "s" * 64 and row.vnc_password == "p@ss!wd8"
    assert b"s" * 64 not in row.secret_enc


def test_sealed_secret_is_bound_to_its_lease(db):
    blob = store.seal("secret", "a" * 64)
    assert store.unseal(blob, "a" * 64) == "secret"
    with pytest.raises(Exception):
        store.unseal(blob, "b" * 64)


def test_vnc_password_is_eight_printable_characters():
    passwords = {store.new_vnc_password() for _ in range(50)}
    assert len(passwords) == 50
    for password in passwords:
        assert len(password) == 8 and all(33 <= ord(c) <= 126 for c in password)


def test_admission_counts_active_leases(db):
    epoch = store.take_epoch()
    for i in range(3):
        _admit(epoch, f"{i}" * 64, max_leases=3)
    with pytest.raises(store.AdmissionRefused) as refused:
        _admit(epoch, "9" * 64, max_leases=3)
    assert refused.value.reason == "lease-count"
    store.transition("0" * 64, epoch=epoch, to="failed")
    _admit(epoch, "9" * 64, max_leases=3)  # a failed lease frees its slot


def test_state_machine_is_enforced(db):
    epoch = store.take_epoch()
    h = _admit(epoch)
    assert not store.transition(h, epoch=epoch, to="ready")          # requested -> ready: no
    assert store.transition(h, epoch=epoch, to="starting")
    assert store.transition(h, epoch=epoch, to="ready")
    assert store.transition(h, epoch=epoch, to="busy")
    assert store.transition(h, epoch=epoch, to="ready")
    assert store.transition(h, epoch=epoch, to="locked", lock_holder="human")
    assert store.get(h).lock_holder == "human"
    assert store.transition(h, epoch=epoch, to="releasing", audit_op="release", result="caller")
    assert not store.transition(h, epoch=epoch, to="ready")
    assert store.transition(h, epoch=epoch, to="gone")
    with pytest.raises(ValueError):
        store.transition(h, epoch=epoch, to="ready", expect=("gone",))
    assert [e["op"] for e in __import__("json").loads(store.get(h).audit)] == ["request", "release"]


def test_a_replaced_worker_cannot_write(db):
    old = store.take_epoch()
    h = _admit(old)
    new = store.take_epoch()
    assert new == old + 1
    assert not store.transition(h, epoch=old, to="starting")
    assert not store.update(h, epoch=old, health_failures=3)
    assert not store.adopt(h, epoch=old)
    with pytest.raises(store.StaleEpoch):
        _admit(old, "z" * 64)
    assert store.adopt(h, epoch=new)
    assert store.get(h).epoch == new
    assert store.transition(h, epoch=new, to="starting")


# ── launch arguments ───────────────────────────────────────────────────


def test_labels_and_create_arguments():
    labels = containers.labels(lease_hash="h" * 64, session="auto-1", org="autonomy",
                               workspace="ws", profile="persistent:eversource",
                               adapter="chrome-headed", expires_at=1790000000.7)
    assert labels["autonomy.browser.lease"] == "h" * 64
    assert labels["autonomy.browser.expires_at"] == "1790000000"
    argv = containers.create_args(name="brw-p-0123456789abcdef", lease_labels=labels,
                                  caps=containers.Caps(2048, 2, 1024),
                                  mount_argv=["-v", "/x:/profile"], timezone="America/New_York")
    joined = " ".join(argv)
    for expected in ("--network autonomy-browser", "--restart no", "--rm", "--memory 2048m",
                     "--memory-swap 2048m", "--cpus 2", "--pids-limit 1024", "--shm-size 1g",
                     "--security-opt no-new-privileges", "-e TZ=America/New_York"):
        assert expected in joined
    assert f"seccomp={containers.SECCOMP_PROFILE}" in joined
    assert argv[-1] == "autonomy-browser:local"
    assert not {"-p", "--publish", "-P", "--publish-all", "--privileged", "--cap-add"} & set(argv)
    # Secrets are named, never valued, on the command line.
    for name in ("BROWSER_LEASE_SECRET", "BROWSER_VNC_PASSWORD", "BROWSER_LEASE_EXPIRES_AT"):
        assert argv[argv.index(name) - 1] == "-e"
    assert not any(a.startswith("BROWSER_LEASE_SECRET=") for a in argv)


def test_profile_integrity(tmp_path):
    import sqlite3

    assert containers.profile_integrity(tmp_path) is None  # a new profile
    (tmp_path / "Local State").write_text("{not json")
    assert "Local State" in containers.profile_integrity(tmp_path)
    (tmp_path / "Local State").write_text("{}")
    (tmp_path / "Default").mkdir()
    conn = sqlite3.connect(tmp_path / "Default" / "Cookies")
    conn.execute("CREATE TABLE cookies (name TEXT)")
    conn.commit()
    conn.close()
    assert containers.profile_integrity(tmp_path) is None
    (tmp_path / "Default" / "Cookies").write_bytes(b"garbage" * 100)
    assert "Cookies" in containers.profile_integrity(tmp_path)


# ── routes ─────────────────────────────────────────────────────────────


@pytest.fixture
def broker(db, monkeypatch, tmp_path):
    epoch = store.take_epoch()
    monkeypatch.setattr(reconciler, "_epoch", epoch)
    monkeypatch.setattr(reconciler, "_isolated", True)
    monkeypatch.setattr(reconciler, "_isolation_checked", True)
    monkeypatch.setattr(reconciler, "defaults", lambda: dict(LIMITS))
    monkeypatch.setattr(routes, "_free_gib", lambda: 500.0)
    monkeypatch.setenv("BROWSER_PROFILES_DIR", str(tmp_path / "profiles"))
    callers = {"owner": CallerScope("autonomy", "ws", "auto-owner"),
               "other": CallerScope("autonomy", "ws", "auto-other")}

    def require(authorization, capability):
        assert capability == "browser"
        if authorization not in callers:
            raise CapabilityRefused("no", status=403)
        return callers[authorization]

    launched = []

    def create_and_start(*, argv, name, before_start=None, **kw):
        if name in {n for n, _ in launched}:
            raise containers.NameConflict("Conflict. The container name is already in use")
        if before_start and before_start():
            raise containers.ProfileDamaged(before_start())
        launched.append((name, argv))
        return "172.30.0.2"

    monkeypatch.setattr(routes, "require_capability", require)
    monkeypatch.setattr(containers, "create_and_start", create_and_start)
    monkeypatch.setattr(containers, "profile_mount_argv", lambda *a: ["-v", "x:/profile"])
    monkeypatch.setattr(containers, "stats", lambda name: {"cpu": 1.0, "mem_mb": 300.0})
    monkeypatch.setattr(reconciler, "wait_ready", lambda *a: None)
    released = []
    monkeypatch.setattr(reconciler, "release_async", lambda lease, why: released.append(why))
    return launched, released


def _call(fn, *args):
    try:
        return fn(*args)
    except routes._Reply as reply:
        return reply.status, reply.payload


def test_create_status_release(broker):
    launched, released = broker
    status, body = _call(routes.create_lease, "owner", {"adapter": "chrome-headed",
                                                        "profile": {"kind": "ephemeral"}})
    assert status == 201 and body["state"] == "starting"
    assert set(body) == {"lease", "state", "adapter", "expires_at"}
    assert body["expires_at"] <= time.time() + LIMITS["ephemeral_ttl_s"] + 1
    assert launched[0][0].startswith("brw-e-")
    status, info = _call(routes.lease_status, "owner", body["lease"])
    assert status == 200 and info["state"] == "starting" and info["mem_mb"] == 300.0
    assert _call(routes.lease_status, "other", body["lease"])[0] == 404
    assert _call(routes.lease_status, "owner", "brl_" + "0" * 32)[0] == 404
    assert _call(routes.lease_status, "owner", "../etc")[0] == 404
    assert _call(routes.release_lease, "other", body["lease"])[0] == 404
    assert _call(routes.release_lease, "owner", body["lease"]) == (200, {"state": "releasing"})
    assert released == ["caller"]


def test_ttl_is_capped_at_the_default(broker):
    status, body = _call(routes.create_lease, "owner", {
        "adapter": "chrome-headed", "profile": {"kind": "ephemeral"}, "ttl_s": 10 ** 6})
    assert status == 201 and body["expires_at"] <= time.time() + LIMITS["ephemeral_ttl_s"] + 1


def test_second_request_for_a_persistent_profile_gets_409(broker):
    launched, _ = broker
    request = {"adapter": "chrome-headed", "profile": {"kind": "persistent", "store": "eversource"}}
    assert _call(routes.create_lease, "owner", request)[0] == 201
    assert _call(routes.create_lease, "other", request) == (409, {"error": "profile-busy"})
    assert len(launched) == 1
    assert len(store.list_leases()) == 1  # the refused request left no record


def test_admission_refusals(broker, monkeypatch):
    request = {"adapter": "chrome-headed", "profile": {"kind": "ephemeral"}}
    monkeypatch.setattr(reconciler, "defaults", lambda: {**LIMITS, "max_leases": 1})
    assert _call(routes.create_lease, "owner", request)[0] == 201
    assert _call(routes.create_lease, "owner", request) == (
        503, {"error": "admission", "reason": "lease-count"})
    monkeypatch.setattr(routes, "_free_gib", lambda: 19.9)
    assert _call(routes.create_lease, "owner", request) == (
        503, {"error": "admission", "reason": "disk"})


def test_damaged_profile_fails_with_a_diagnostic(broker, tmp_path):
    from tools.browser_broker.profiles import profile_path

    path = profile_path("autonomy", "ws", "bank")
    path.mkdir(parents=True)
    (path / "Local State").write_text("{broken")
    status, body = _call(routes.create_lease, "owner", {
        "adapter": "chrome-headed", "profile": {"kind": "persistent", "store": "bank"}})
    assert status == 201 and body["state"] == "failed" and "Local State" in body["diagnostic"]


@pytest.mark.parametrize("body", [
    None, [], {}, {"adapter": "chrome-headed"},
    {"adapter": "firefox", "profile": {"kind": "ephemeral"}},
    {"adapter": "chrome-headed", "profile": {"kind": "persistent"}},
    {"adapter": "chrome-headed", "profile": {"kind": "persistent", "store": "../x"}},
    {"adapter": "chrome-headed", "profile": {"kind": "ephemeral"}, "ttl_s": 5},
    {"adapter": "chrome-headed", "profile": {"kind": "ephemeral"}, "org": "other"},
    {"adapter": "chrome-headed", "profile": {"kind": "ephemeral"}, "session": "auto-x"},
])
def test_malformed_requests_are_refused(broker, body):
    assert _call(routes.create_lease, "owner", body)[0] == 400


def test_agent_headless_is_not_offered(broker):
    status, body = _call(routes.create_lease, "owner", {"adapter": "agent-headless",
                                                        "profile": {"kind": "ephemeral"}})
    assert (status, body["error"]) == (400, "adapter-unavailable")


def test_no_capability_is_403_and_no_epoch_is_503(broker, monkeypatch):
    request = {"adapter": "chrome-headed", "profile": {"kind": "ephemeral"}}
    assert _call(routes.create_lease, "stranger", request)[0] == 403
    monkeypatch.setattr(reconciler, "_epoch", None)
    assert _call(routes.create_lease, "owner", request) == (
        503, {"error": "unavailable", "reason": "broker-starting"})


# ── reconciler decisions ───────────────────────────────────────────────


@pytest.fixture
def live(monkeypatch):
    state = {"live": True, "enabled": True}
    monkeypatch.setattr(dashboard_db, "is_session_live", lambda name: state["live"])
    monkeypatch.setattr("tools.dashboard.capability_gate.capability_enabled",
                        lambda org, ws, cap: state["enabled"])
    return state


def test_end_reasons(db, live):
    epoch = store.take_epoch()
    h = _admit(epoch)
    store.transition(h, epoch=epoch, to="starting")
    store.transition(h, epoch=epoch, to="ready")
    lease = store.get(h)
    now = time.time()
    assert reconciler._end_reason(lease, now, LIMITS, {}) is None
    assert reconciler._end_reason(lease, now + LIMITS["idle_s"], LIMITS, {}) == "idle"
    assert reconciler._end_reason(lease, lease.expires_at + 16, {**LIMITS, "idle_s": 10 ** 6},
                                  {}) == "time-limit"
    live["live"] = False
    assert reconciler._end_reason(lease, now, LIMITS, {}) == "session-ended"
    live["live"], live["enabled"] = True, False
    assert reconciler._end_reason(lease, now, LIMITS, {}) == "capability-revoked"


def test_two_failed_health_checks_release(db, live, monkeypatch):
    epoch = store.take_epoch()
    monkeypatch.setattr(reconciler, "_epoch", epoch)
    h = _admit(epoch)
    store.transition(h, epoch=epoch, to="starting", address="172.30.0.9")
    store.transition(h, epoch=epoch, to="ready")
    stopped = []
    monkeypatch.setattr(containers, "stop", lambda name, *, lease_hash: stopped.append(name))
    monkeypatch.setattr(reconciler, "_healthy", lambda lease: False)
    monkeypatch.setattr(reconciler, "_tick", 1)  # skip the docker pass
    reconciler.reconcile_once()
    assert store.get(h).state == "ready" and store.get(h).health_failures == 1
    monkeypatch.setattr(reconciler, "_tick", 1)
    reconciler.reconcile_once()
    assert store.get(h).state == "gone" and stopped == ["brw-e-x"]


def test_activation_adopts_and_stops_orphans(db, monkeypatch):
    old = store.take_epoch()
    h = _admit(old)
    store.transition(h, epoch=old, to="starting", address="172.30.0.9")
    listed = [containers.LeaseContainer("brw-e-x", h, "running", {}),
              containers.LeaseContainer("brw-e-orphan", "o" * 64, "running", {})]
    stopped = []
    monkeypatch.setattr(containers, "ensure_network", lambda: None)
    monkeypatch.setattr(containers, "list_containers", lambda: listed)
    monkeypatch.setattr(containers, "stop", lambda name, *, lease_hash: stopped.append((name, lease_hash)))
    new = reconciler.activate()
    assert new == old + 1 and store.get(h).epoch == new
    assert stopped == [("brw-e-orphan", "o" * 64)]
    assert not store.transition(h, epoch=old, to="ready")


def test_a_record_whose_container_vanished_is_closed(db, live, monkeypatch):
    epoch = store.take_epoch()
    monkeypatch.setattr(reconciler, "_epoch", epoch)
    h = _admit(epoch)
    store.transition(h, epoch=epoch, to="starting", address="172.30.0.9")
    store.transition(h, epoch=epoch, to="ready")
    monkeypatch.setattr(containers, "list_containers", lambda: [])
    monkeypatch.setattr(containers, "stop", lambda name, *, lease_hash: None)
    monkeypatch.setattr(reconciler, "_tick", 0)  # the docker pass runs
    reconciler.reconcile_once()
    assert store.get(h).state == "gone"
    assert "container-gone" in store.get(h).audit


def test_no_lease_starts_without_the_dashboard_refusal(broker, monkeypatch):
    launched, _ = broker
    monkeypatch.setattr(reconciler, "_isolated", False)
    assert _call(routes.create_lease, "owner", {"adapter": "chrome-headed",
                                                "profile": {"kind": "ephemeral"}}) == (
        503, {"error": "unavailable", "reason": "isolation"})
    assert launched == [] and store.list_leases() == []


def test_activation_pauses_the_broker_with_one_log_line_and_no_retries(db, monkeypatch, caplog):
    import logging

    monkeypatch.setattr(containers, "ensure_network", lambda: None)
    monkeypatch.setattr(containers, "list_containers", lambda: [])
    monkeypatch.setattr(reconciler, "_epoch", None)
    with caplog.at_level(logging.WARNING, logger=reconciler.__name__):
        reconciler.activate()
        for _ in range(12):  # a minute of reconciler ticks
            reconciler.reconcile_once()
    assert reconciler.isolated() is False
    assert [r.getMessage() for r in caplog.records].count(reconciler.PAUSE_MESSAGE) == 1


def test_ensure_network_creates_the_network_and_never_attaches_the_dashboard(monkeypatch):
    import subprocess

    calls = []

    def fake(*args, check=True, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if args[:2] == ("network", "inspect") else 0, "", "")

    monkeypatch.setattr(containers, "_docker", fake)
    containers.ensure_network()
    assert calls[0][:2] == ("network", "inspect") and calls[1][:2] == ("network", "create")
    assert not any(c[:2] == ("network", "connect") for c in calls)
    assert not any("iptables" in c for c in calls)
    assert not hasattr(containers, "isolate_dashboard") and not hasattr(containers, "restrict_egress")



def _fake_container_lifecycle(monkeypatch, *, removal_polls=3, daemon_error=False):
    """A container named N with id OLD that disappears after a few polls, after
    which a NEW lease's container takes the same name with id NEW."""
    import subprocess

    state = {"old_polls": removal_polls, "old_gone": False}
    calls = []

    def fake(*args, check=True, **kw):
        calls.append(args)
        target = args[-1]
        if args[0] == "inspect":
            if target == "brw-p-0123456789abcdef":
                holder = ("NEW " + "n" * 64) if state["old_gone"] else ("OLD " + "o" * 64)
                return subprocess.CompletedProcess(args, 0, holder + "\n", "")
        if args[0] == "ps":
            if daemon_error:
                return subprocess.CompletedProcess(args, 1, "", DAEMON_DOWN_29)
            assert "id=OLD" in args, args
            state["old_polls"] -= 1
            if state["old_polls"] < 0:
                state["old_gone"] = True  # --rm finished; the name is free and gets reused
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(args, 0, "OLD\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(containers, "_docker", fake)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return calls, state


def test_stop_waits_for_removal_and_only_ever_touches_that_container(monkeypatch):
    calls, state = _fake_container_lifecycle(monkeypatch)
    containers.stop("brw-p-0123456789abcdef", lease_hash="o" * 64)
    assert state["old_gone"]
    assert calls[0][0] == "inspect" and calls[0][-1] == "brw-p-0123456789abcdef"
    for call in calls[1:]:
        assert call[-1] in ("OLD", "id=OLD"), call  # never the name, never the new container
    assert not any(call[-1] == "NEW" for call in calls)


def test_stop_is_idempotent_and_does_not_mistake_a_daemon_error_for_removal(monkeypatch):
    import subprocess

    for wording in ("error: no such object: brw-e-x",            # Docker 29 (Home)
                    "Error: No such object: brw-e-x",            # older daemons
                    "Error response from daemon: No such container: brw-e-x"):
        monkeypatch.setattr(containers, "_docker", lambda *a, w=wording, **k: subprocess.CompletedProcess(
            a, 1, "", w))
        containers.stop("brw-e-x", lease_hash="x" * 64)  # already gone: returns quietly
    _fake_container_lifecycle(monkeypatch, daemon_error=True)
    with pytest.raises(containers.DockerUnavailable):  # the removal probe hit a daemon error
        containers.stop("brw-p-0123456789abcdef", lease_hash="o" * 64)


def test_a_retried_release_never_stops_a_new_lease_holding_the_name(monkeypatch):
    calls, state = _fake_container_lifecycle(monkeypatch)
    state["old_gone"] = True  # ours finished removal between ticks; a new lease took the name
    containers.stop("brw-p-0123456789abcdef", lease_hash="o" * 64)
    assert [c[0] for c in calls] == ["inspect"]  # looked, saw another lease's label, left it alone


def test_one_stuck_release_does_not_stall_the_pass(db, live, monkeypatch):
    epoch = store.take_epoch()
    monkeypatch.setattr(reconciler, "_epoch", epoch)
    stuck, fine = _admit(epoch, "a" * 64), _admit(epoch, "b" * 64)
    for h in (stuck, fine):
        store.transition(h, epoch=epoch, to="starting", address="172.30.0.9")
        store.transition(h, epoch=epoch, to="ready")
        store.update(h, epoch=epoch, last_activity=0)  # both past their idle limit

    def stop(name, *, lease_hash):
        if stop.calls == 0:
            stop.calls += 1
            raise RuntimeError("not removed within 20 s")
    stop.calls = 0
    monkeypatch.setattr(containers, "stop", stop)
    monkeypatch.setattr(reconciler, "_tick", 1)
    reconciler.reconcile_once()
    states = sorted(store.get(h).state for h in (stuck, fine))
    assert states == ["gone", "releasing"]  # the second lease was still released


# ── caller command route (auto-8q7oe.6) ────────────────────────────────


@pytest.fixture
def ready_lease(broker, monkeypatch):
    status, body = _call(routes.create_lease, "owner", {"adapter": "chrome-headed",
                                                        "profile": {"kind": "ephemeral"}})
    h = store.lease_hash(body["lease"])
    store.transition(h, epoch=reconciler.epoch(), to="ready")
    forwarded = []

    def agent_request(address, secret, method, path, body=None, timeout=5):
        forwarded.append((path, body))
        if path == "/command":
            hook = agent_request.during
            if hook:
                hook()
            return 200, {"ok": True, "result": "Example Domain"}
        return 200, {}

    agent_request.during = None
    monkeypatch.setattr(containers, "agent_request", agent_request)
    return body["lease"], h, forwarded, agent_request


def test_command_forwards_and_frees_the_lease(ready_lease):
    lease_id, h, forwarded, _ = ready_lease
    assert _call(routes.run_command, "owner", lease_id, {"op": "title"}) == (
        200, {"ok": True, "result": "Example Domain"})
    assert forwarded == [("/command", {"op": "title", "args": {}})]
    assert store.get(h).state == "ready"
    audit = __import__("json").loads(store.get(h).audit)
    assert audit[-1]["op"] == "command:title" and audit[-1]["result"] == "ok"
    assert "Example Domain" not in store.get(h).audit  # never page content


def test_other_sessions_and_invented_leases_never_reach_the_agent(ready_lease):
    lease_id, _, forwarded, _ = ready_lease
    assert _call(routes.run_command, "other", lease_id, {"op": "title"})[0] == 404
    assert _call(routes.run_command, "owner", "brl_" + "0" * 32, {"op": "title"})[0] == 404
    assert forwarded == []


@pytest.mark.parametrize("op", ["eval", "evaluate", "cookies", "tabs", "add_init_script", "cdp"])
def test_operations_outside_the_list_are_400(ready_lease, op):
    lease_id, _, forwarded, _ = ready_lease
    assert _call(routes.run_command, "owner", lease_id, {"op": op, "args": {}})[0] == 400
    assert forwarded == []


def test_locked_and_busy_leases_answer_409(ready_lease):
    lease_id, h, forwarded, _ = ready_lease
    store.transition(h, epoch=reconciler.epoch(), to="locked", lock_holder="human")
    for _ in range(50):
        assert _call(routes.run_command, "owner", lease_id, {"op": "title"}) == (
            409, {"error": "locked", "holder": "human"})
    store.transition(h, epoch=reconciler.epoch(), to="ready", lock_holder=None)
    store.transition(h, epoch=reconciler.epoch(), to="busy")
    assert _call(routes.run_command, "owner", lease_id, {"op": "title"}) == (409, {"error": "busy"})
    assert forwarded == []


def test_take_control_during_a_command_discards_its_result(ready_lease):
    lease_id, h, _, agent_request = ready_lease

    def operator_takes_control():
        store.transition(h, epoch=reconciler.epoch(), to="locked", expect=("busy",), lock_holder="human")

    agent_request.during = operator_takes_control
    assert _call(routes.run_command, "owner", lease_id, {"op": "title"}) == (
        409, {"error": "locked", "holder": "human"})
    assert store.get(h).state == "locked"  # the command did not hand the lease back


def test_a_lease_left_busy_by_a_dead_request_is_freed(ready_lease, live, monkeypatch):
    lease_id, h, forwarded, _ = ready_lease
    epoch = reconciler.epoch()
    store.transition(h, epoch=epoch, to="busy", last_activity=time.time() - reconciler.BUSY_LIMIT_S - 1)
    monkeypatch.setattr(reconciler, "_tick", 1)       # no docker pass
    monkeypatch.setattr(reconciler, "_healthy", lambda lease: True)
    reconciler.reconcile_once()
    assert store.get(h).state == "ready" and ("/abort", {}) in forwarded
    assert "command:interrupted" in store.get(h).audit
    assert _call(routes.run_command, "owner", lease_id, {"op": "title"})[0] == 200


def test_a_recently_busy_lease_is_left_alone(ready_lease, live, monkeypatch):
    lease_id, h, forwarded, _ = ready_lease
    store.transition(h, epoch=reconciler.epoch(), to="busy")
    monkeypatch.setattr(reconciler, "_tick", 1)
    monkeypatch.setattr(reconciler, "_healthy", lambda lease: True)
    reconciler.reconcile_once()
    assert store.get(h).state == "busy" and forwarded == []


@pytest.mark.parametrize("status", [400, 401, 403])
def test_an_agent_refusal_is_a_broker_fault(ready_lease, monkeypatch, status):
    lease_id, h, _, _ = ready_lease
    monkeypatch.setattr(containers, "agent_request",
                        lambda *a, **k: (status, {"error": "unauthorized"}))
    assert _call(routes.run_command, "owner", lease_id, {"op": "title"}) == (
        502, {"error": "lease agent refused"})
    assert store.get(h).state == "ready"



def test_a_live_maximum_length_command_is_not_reclaimed(ready_lease, live, monkeypatch):
    lease_id, h, forwarded, _ = ready_lease
    store.transition(h, epoch=reconciler.epoch(), to="busy", last_activity=time.time() - 95)
    monkeypatch.setattr(reconciler, "_tick", 1)
    monkeypatch.setattr(reconciler, "_healthy", lambda lease: True)
    reconciler.reconcile_once()  # a pass at t = 95 s: the route may still be waiting
    assert store.get(h).state == "busy" and forwarded == []


def test_an_interrupted_command_does_not_claim_a_take_control(ready_lease, monkeypatch):
    lease_id, h, _, _ = ready_lease
    monkeypatch.setattr(containers, "agent_request", lambda *a, **k: (200, {"ok": False, "error": "aborted"}))
    assert _call(routes.run_command, "owner", lease_id, {"op": "title"}) == (409, {"error": "interrupted"})
    assert store.get(h).state == "ready"


# ── Docker's wording, captured from Docker 29 (auto-y8o21) ─────────────

CONFLICT_29 = ('Error response from daemon: Conflict. The container name "/y8probe" is already in use by '
               'container "2d4722e782ad". You have to remove (or rename) that container to be able to reuse that name.')
NETWORK_EXISTS_29 = "Error response from daemon: network with name autonomy-browser already exists"
NO_SUCH_29 = "error: no such object: no-such-y8"
DAEMON_DOWN_29 = ("failed to connect to the docker API at unix:///var/run/docker.sock; check if the path is "
                  "correct and if the daemon is running: dial unix /var/run/docker.sock: connect: no such file")
DAEMON_DOWN_OLD = "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"


def _completed(stderr, rc=1):
    import subprocess
    return subprocess.CompletedProcess(["docker"], rc, "", stderr)


@pytest.mark.parametrize("stderr", [DAEMON_DOWN_29, DAEMON_DOWN_OLD])
def test_a_daemon_outage_is_docker_unavailable_in_every_wording(monkeypatch, stderr):
    monkeypatch.setattr(containers.subprocess, "run", lambda *a, **k: _completed(stderr))
    with pytest.raises(containers.DockerUnavailable):
        containers._docker("ps")
    with pytest.raises(containers.DockerUnavailable):  # even when the caller does not check
        containers._docker("network", "inspect", "x", check=False)


def test_a_name_conflict_is_recognised_in_docker_29_wording(monkeypatch):
    monkeypatch.setattr(containers.subprocess, "run", lambda *a, **k: _completed(CONFLICT_29))
    with pytest.raises(containers.NameConflict):
        containers._docker("create", "--name", "y8probe", "img")


def test_stderr_matching_ignores_case():
    assert containers._missing(_completed(NO_SUCH_29))
    assert containers._missing(_completed("Error: No such object: x"))
    assert containers._missing(_completed("Error response from daemon: No such container: x"))
    assert containers._says(_completed(NETWORK_EXISTS_29), "already exists")
    assert not containers._says(_completed(NETWORK_EXISTS_29), "already attached")


def test_lease_requests_say_broker_starting_until_isolation_was_checked(broker, monkeypatch):
    monkeypatch.setattr(reconciler, "_isolation_checked", False)
    assert _call(routes.create_lease, "owner", {"adapter": "chrome-headed",
                                                "profile": {"kind": "ephemeral"}}) == (
        503, {"error": "unavailable", "reason": "broker-starting"})


def test_a_failed_activation_is_retried_whole_and_then_adopts(db, monkeypatch):
    old = store.take_epoch()
    h = _admit(old)
    store.transition(h, epoch=old, to="starting", address="172.30.0.9")
    monkeypatch.setattr(reconciler, "_epoch", None)
    monkeypatch.setattr(containers, "list_containers",
                        lambda: [containers.LeaseContainer("brw-e-x", h, "running", {})])
    calls = {"n": 0}

    def flaky_network():
        calls["n"] += 1
        if calls["n"] == 1:
            raise containers.DockerUnavailable("failed to connect to the docker API")

    monkeypatch.setattr(containers, "ensure_network", flaky_network)
    with pytest.raises(containers.DockerUnavailable):
        reconciler.activate()
    assert reconciler.epoch() is None  # not published: the loop will retry activation
    epoch = reconciler.activate()
    assert reconciler.epoch() == epoch and store.get(h).epoch == epoch  # adopted
