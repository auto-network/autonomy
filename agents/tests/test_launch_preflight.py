"""Unit tests for agents.launch_preflight — the launch-input validator that
names a missing image / runtime / mount source before docker run instead of
letting docker fail namelessly."""
from __future__ import annotations

import types

import pytest

from agents import launch_preflight as lp
from agents.launch_preflight import LaunchProblem
from agents.mount_plan import MountPlan, mount_spec


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── image_present ────────────────────────────────────────────────────────────

def test_image_present_true_when_inspect_succeeds(monkeypatch):
    monkeypatch.setattr(lp, "_docker", lambda args, timeout=15: _Completed(0))
    assert lp.image_present("autonomy-session-platform") is True


def test_image_present_false_when_inspect_fails(monkeypatch):
    monkeypatch.setattr(lp, "_docker", lambda args, timeout=15: _Completed(1, stderr="No such image"))
    assert lp.image_present("autonomy-session-platform") is False


def test_image_present_none_when_docker_unreachable(monkeypatch):
    monkeypatch.setattr(lp, "_docker", lambda args, timeout=15: None)
    assert lp.image_present("autonomy-session-platform") is None  # unknowable -> fail open


# ── runtime_name / runtime_available ─────────────────────────────────────────

def test_runtime_name_extracts_only_a_runtime_flag():
    assert lp.runtime_name(["--runtime=sysbox-runc"]) == "sysbox-runc"
    assert lp.runtime_name([]) is None
    assert lp.runtime_name(["--privileged"]) is None       # nothing to check
    assert lp.runtime_name(["--runtime="]) is None         # empty -> nothing


def test_runtime_available_reads_daemon_runtime_set(monkeypatch):
    monkeypatch.setattr(lp, "_docker",
                        lambda args, timeout=15: _Completed(0, stdout='{"runc": {}, "sysbox-runc": {}}'))
    assert lp.runtime_available("sysbox-runc") is True
    assert lp.runtime_available("nonesuch") is False


def test_runtime_available_none_on_bad_json_or_error(monkeypatch):
    monkeypatch.setattr(lp, "_docker", lambda args, timeout=15: _Completed(0, stdout="not json"))
    assert lp.runtime_available("sysbox-runc") is None
    monkeypatch.setattr(lp, "_docker", lambda args, timeout=15: _Completed(1))
    assert lp.runtime_available("sysbox-runc") is None


# ── preflight aggregation ────────────────────────────────────────────────────

def _fake_plan_and_topo():
    return MountPlan(), object()  # topology is opaque; source resolution is patched


def test_destination_conflict_names_missing_child_below_readonly_parent(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    plan = MountPlan()
    plan.set(mount_spec(package, "/opt/capability:ro"))
    plan.set(mount_spec(runtime, "/opt/capability/runtime:ro"))

    problems = lp.destination_conflicts(plan)

    assert len(problems) == 1
    assert problems[0].kind == "mount-destination"
    assert problems[0].name == "/opt/capability/runtime"
    assert "read-only" in problems[0].detail


def test_destination_conflict_allows_existing_child_mountpoint(tmp_path):
    package = tmp_path / "package"
    (package / "runtime").mkdir(parents=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    plan = MountPlan()
    plan.set(mount_spec(package, "/opt/capability:ro"))
    plan.set(mount_spec(runtime, "/opt/capability/runtime:ro"))

    assert lp.destination_conflicts(plan) == []


def test_preflight_reports_destination_conflict_before_docker(tmp_path, monkeypatch):
    package = tmp_path / "package"
    package.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    plan = MountPlan()
    plan.set(mount_spec(package, "/opt/capability:ro"))
    plan.set(mount_spec(runtime, "/opt/capability/runtime:ro"))
    monkeypatch.setattr(lp, "image_present", lambda image: True)
    monkeypatch.setattr("agents.mount_plan.preflight_sources", lambda p, t: [])
    monkeypatch.setattr("agents.secret_ramfs.daemon_missing", lambda paths: [])

    problems = lp.preflight(image="image", runtime_args=[], plan=plan, topo=object())

    assert [problem.kind for problem in problems] == ["mount-destination"]


def test_preflight_gathers_all_problems_at_once(monkeypatch):
    plan, topo = _fake_plan_and_topo()
    # image absent, runtime absent, one mount source absent — all three at once.
    monkeypatch.setattr(lp, "image_present", lambda image: False)
    monkeypatch.setattr(lp, "runtime_available", lambda rt: False)
    monkeypatch.setattr("agents.mount_plan.preflight_sources",
                        lambda p, t: [("/x/.beads", "/data/.beads"), ("/ok", "/ok")])
    monkeypatch.setattr("agents.secret_ramfs.daemon_missing",
                        lambda paths: ["/x/.beads"])
    problems = lp.preflight(image="autonomy-session-platform",
                            runtime_args=["--runtime=sysbox-runc"], plan=plan, topo=topo)
    kinds = {p.kind for p in problems}
    assert kinds == {"image", "runtime", "mount"}                 # every kind reported
    assert [p for p in problems if p.kind == "mount"][0].name == "/x/.beads"
    assert len(problems) == 3                                     # the present mount is not flagged


def test_preflight_clean_when_everything_present(monkeypatch):
    plan, topo = _fake_plan_and_topo()
    monkeypatch.setattr(lp, "image_present", lambda image: True)
    monkeypatch.setattr(lp, "runtime_available", lambda rt: True)
    monkeypatch.setattr("agents.mount_plan.preflight_sources",
                        lambda p, t: [("/ok", "/ok")])
    monkeypatch.setattr("agents.secret_ramfs.daemon_missing", lambda paths: [])
    assert lp.preflight(image="autonomy-session-platform",
                        runtime_args=[], plan=plan, topo=topo) == []


def test_preflight_names_cold_vault_when_credentials_needed(monkeypatch):
    plan, topo = _fake_plan_and_topo()
    monkeypatch.setattr(lp, "image_present", lambda image: True)
    monkeypatch.setattr("agents.mount_plan.preflight_sources", lambda p, t: [])
    monkeypatch.setattr("agents.secret_ramfs.daemon_missing", lambda paths: [])
    monkeypatch.setattr(lp, "vault_is_cold", lambda: True)
    problems = lp.preflight(image="autonomy-session-platform", runtime_args=[],
                            plan=plan, topo=topo, credential_keys={"github.token"})
    assert len(problems) == 1 and problems[0].kind == "vault"
    assert "github.token" in problems[0].detail


def test_preflight_no_vault_problem_when_warm_or_no_credentials(monkeypatch):
    plan, topo = _fake_plan_and_topo()
    monkeypatch.setattr(lp, "image_present", lambda image: True)
    monkeypatch.setattr("agents.mount_plan.preflight_sources", lambda p, t: [])
    monkeypatch.setattr("agents.secret_ramfs.daemon_missing", lambda paths: [])
    # cold vault but no credentials needed -> not this launch's problem
    monkeypatch.setattr(lp, "vault_is_cold", lambda: True)
    assert lp.preflight(image="x", runtime_args=[], plan=plan, topo=topo,
                        credential_keys=set()) == []
    # credentials needed but vault warm -> fine
    monkeypatch.setattr(lp, "vault_is_cold", lambda: False)
    assert lp.preflight(image="x", runtime_args=[], plan=plan, topo=topo,
                        credential_keys={"github.token"}) == []
    # credentials needed but vault state unknowable -> fail open, no problem
    monkeypatch.setattr(lp, "vault_is_cold", lambda: None)
    assert lp.preflight(image="x", runtime_args=[], plan=plan, topo=topo,
                        credential_keys={"github.token"}) == []


def test_preflight_unknowable_checks_never_add_problems(monkeypatch):
    """A check that returns None (docker unreachable, bad output) must NOT
    manufacture a problem — fail open, docker stays the backstop."""
    plan, topo = _fake_plan_and_topo()
    monkeypatch.setattr(lp, "image_present", lambda image: None)   # unknowable
    monkeypatch.setattr(lp, "runtime_available", lambda rt: None)  # unknowable
    monkeypatch.setattr("agents.mount_plan.preflight_sources",
                        lambda p, t: [("/x", "/x")])
    monkeypatch.setattr("agents.secret_ramfs.daemon_missing", lambda paths: None)  # unknowable
    assert lp.preflight(image="autonomy-session-platform",
                        runtime_args=["--runtime=sysbox-runc"], plan=plan, topo=topo) == []
