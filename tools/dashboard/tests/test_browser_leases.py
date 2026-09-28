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
    monkeypatch.setattr(containers, "stop", lambda name: stopped.append(name))
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
    monkeypatch.setattr(containers, "isolate_dashboard", lambda: None)
    monkeypatch.setattr(containers, "list_containers", lambda: listed)
    monkeypatch.setattr(containers, "stop", lambda name: stopped.append(name))
    new = reconciler.activate()
    assert new == old + 1 and store.get(h).epoch == new
    assert stopped == ["brw-e-orphan"]
    assert not store.transition(h, epoch=old, to="ready")


def test_a_record_whose_container_vanished_is_closed(db, live, monkeypatch):
    epoch = store.take_epoch()
    monkeypatch.setattr(reconciler, "_epoch", epoch)
    h = _admit(epoch)
    store.transition(h, epoch=epoch, to="starting", address="172.30.0.9")
    store.transition(h, epoch=epoch, to="ready")
    monkeypatch.setattr(containers, "list_containers", lambda: [])
    monkeypatch.setattr(containers, "stop", lambda name: None)
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


def test_isolation_rule_is_checked_inserted_and_verified(monkeypatch):
    import subprocess

    calls, rules = [], set()

    def fake_docker(*args, check=True, **kw):
        calls.append(args)
        if args[:2] == ("network", "inspect"):
            return subprocess.CompletedProcess(args, 0, "172.19.0.0/16\n", "")
        if args[0] == "run":
            op, rule = args[args.index("-w") + 1], args[args.index("-w") + 2:]
            if op == "-I":
                rules.add(rule)
            return subprocess.CompletedProcess(args, 0 if rule in rules else 1, "", "")
        raise AssertionError(args)

    monkeypatch.setattr(containers, "_docker", fake_docker)
    monkeypatch.setattr("agents.mount_plan._own_container_id", lambda: "dash123")
    containers.isolate_dashboard()
    run = [c for c in calls if c[0] == "run"]
    assert [c[c.index("-w") + 1] for c in run] == ["-C", "-I", "-C"]
    helper = run[1]
    assert helper[helper.index("--network") + 1] == "container:dash123"
    assert "NET_ADMIN" in helper and helper[helper.index("--cap-drop") + 1] == "ALL"
    assert helper[helper.index("-w") + 2:] == ("INPUT", "-s", "172.19.0.0/16", "-m", "conntrack",
                                               "--ctstate", "NEW", "-j", "DROP")
    calls.clear()
    containers.isolate_dashboard()  # idempotent: one check, no second insert
    assert [c[c.index("-w") + 1] for c in calls if c[0] == "run"] == ["-C"]


def test_isolation_needs_a_containerized_dashboard(monkeypatch):
    monkeypatch.setattr("agents.mount_plan._own_container_id", lambda: None)
    with pytest.raises(containers.IsolationUnavailable):
        containers.isolate_dashboard()
