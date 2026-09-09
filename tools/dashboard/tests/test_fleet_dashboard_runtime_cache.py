"""The Dashboard re-arms its OWN Fleet runtime credential across a restart
(auto-5er0n).

The credential is delivered by the browser at unlock and held only in process
memory, so every Dashboard restart left the machine unable to pull Fleet sync
until a human unlocked again. ``_activate_runtime`` now caches the raw browser
payload in the ramfs warm store (a DISTINCT file from the serving connector's
copy, auto-ixwr3) and ``rearm_local_runtime_from_cache`` replays it at startup —
through the same activation, so a stale or de-rostered payload is re-verified and
refused rather than arming the machine with a credential the fleet dropped.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import time
import types

import pytest

from tools.dashboard import fleet_enrollment_routes
from tools.network import fleet_relay_sync, fleet_roster, fleet_sync_scheduler
from tools.network.idkit import KeyPair, Subject, issue_cert


@pytest.fixture(autouse=True)
def _ramfs(tmp_path, monkeypatch):
    # A temp dir stands in for the ramfs key cache; neuter the ramfs guard so
    # the test runs headless (the guard itself is covered by memory_cache).
    monkeypatch.setenv("AUTONOMY_KEYCACHE_MOUNT", str(tmp_path))
    monkeypatch.setattr(
        "tools.network.storagekit.memory_cache.assert_memory_backed",
        lambda *a, **k: None,
    )


@pytest.fixture(autouse=True)
def _clean_service():
    # The dashboard fleet-sync service is a module singleton; keep each test's
    # assertion about "configured / not configured" from leaking into the next.
    fleet_sync_scheduler.dashboard_fleet_sync_service.configure(None)
    yield
    fleet_sync_scheduler.dashboard_fleet_sync_service.configure(None)


def _wire_runtime(tmp_path, monkeypatch, *, machine_id, root, roster_entries,
                  expected_entry):
    """Wire the process-side reads ``_activate_runtime`` performs so it can run
    headless: a fixed runtime context, a registered org_uuid (so the Dashboard
    cache is keyed), a no-op catalog activation, and a non-serving tunnel."""
    monkeypatch.setattr(
        fleet_enrollment_routes, "_runtime_context",
        lambda: (root.public_hex, expected_entry),
    )
    monkeypatch.setattr(
        fleet_enrollment_routes, "_reachability_binding",
        lambda: {"org_uuid": "org-x"},
    )
    monkeypatch.setattr(
        fleet_enrollment_routes.fleet_roster, "load_entries",
        lambda *, org: list(roster_entries),
    )
    monkeypatch.setattr(
        fleet_enrollment_routes, "_org_db_path", lambda _org: tmp_path / "personal.db"
    )
    monkeypatch.setattr(
        fleet_enrollment_routes, "_ensure_fleet_catalog", lambda machine_pub: None
    )
    monkeypatch.setattr(
        fleet_enrollment_routes.fleet_tunnel_server, "state",
        # `reason` and `managed` are REQUIRED of this double since
        # auto-clune.7: `_activate_runtime` now asks
        # `tunnel_serving_permitted()`, which reads `.reason`. A stub missing
        # it raises AttributeError from inside activation, which presents as
        # "the machine did not arm" — the exact symptom under investigation.
        lambda: types.SimpleNamespace(
            allowed=False, reason="not-designated", managed=True,
            selected_machine_id=None),
    )


def _runtime_payload(machine_id, machine, *, expired=False):
    now = int(time.time())
    process = KeyPair.from_private_hex("44" * 32)
    not_before, not_after = (
        (now - 600, now - 300) if expired else (now - 30, now + 300)
    )
    root = KeyPair.from_private_hex("11" * 32)
    cert = issue_cert(
        machine,
        process.public_hex,
        scope=["fleet:sync"],
        org=f"personal:{root.public_hex}",
        subject=Subject(kind="machine", id=machine_id),
        not_before=not_before,
        not_after=not_after,
    )
    return {
        "machine_id": machine_id,
        "machine_pub": machine.public_hex,
        "process_private_seed": process.private_hex,
        "delegation_cert": cert.to_dict(),
    }


def test_rearm_returns_false_when_no_cache_file_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fleet_enrollment_routes, "_reachability_binding",
        lambda: {"org_uuid": "org-x"},
    )
    # A machine that was never unlocked (or whose ramfs was cleared by a reboot)
    # has no cached payload: rearm is a no-op and leaves the machine unarmed.
    assert fleet_enrollment_routes.rearm_local_runtime_from_cache() is False
    assert fleet_sync_scheduler.dashboard_fleet_sync_service._config is None


def test_rearm_from_a_stored_payload_configures_the_dashboard_sync(
    tmp_path, monkeypatch
):
    root = KeyPair.from_private_hex("11" * 32)
    machine = KeyPair.from_private_hex("22" * 32)
    machine_id = "33" * 32
    entry = fleet_roster.enroll(
        root, machine_id=machine_id, machine_pub=machine.public_hex
    )
    _wire_runtime(
        tmp_path, monkeypatch, machine_id=machine_id, root=root,
        roster_entries=(entry,), expected_entry=entry,
    )
    payload = _runtime_payload(machine_id, machine)

    # Stash the raw browser payload the way _activate_runtime does, under the
    # Dashboard's own file name.
    fleet_relay_sync.FleetRuntimeWarmCache(
        "org-x", name_prefix="fleet-dashboard-runtime"
    ).store(payload)

    assert fleet_enrollment_routes.rearm_local_runtime_from_cache() is True
    # The replay re-armed the real consumer: the dashboard fleet-sync service is
    # configured with no browser unlock in sight.
    assert fleet_sync_scheduler.dashboard_fleet_sync_service._config is not None
    assert (
        fleet_sync_scheduler.dashboard_fleet_sync_service._config.roster_machine_pub
        == machine.public_hex
    )


def test_activate_runtime_warms_the_dashboard_cache(tmp_path, monkeypatch):
    root = KeyPair.from_private_hex("11" * 32)
    machine = KeyPair.from_private_hex("22" * 32)
    machine_id = "33" * 32
    entry = fleet_roster.enroll(
        root, machine_id=machine_id, machine_pub=machine.public_hex
    )
    _wire_runtime(
        tmp_path, monkeypatch, machine_id=machine_id, root=root,
        roster_entries=(entry,), expected_entry=entry,
    )
    payload = _runtime_payload(machine_id, machine)

    # A live activation caches the raw payload under the Dashboard file name,
    # NOT the connector's — a non-serving machine has no connector file at all.
    fleet_enrollment_routes._activate_runtime(payload)
    assert (tmp_path / "fleet-dashboard-runtime.org-x.json").exists()
    assert not (tmp_path / "fleet-connector-runtime.org-x.json").exists()
    assert fleet_relay_sync.FleetRuntimeWarmCache(
        "org-x", name_prefix="fleet-dashboard-runtime"
    ).load() == payload


def test_a_payload_naming_an_off_roster_machine_does_not_activate(
    tmp_path, monkeypatch, caplog
):
    root = KeyPair.from_private_hex("11" * 32)
    on_roster = KeyPair.from_private_hex("22" * 32)
    on_roster_id = "33" * 32
    entry = fleet_roster.enroll(
        root, machine_id=on_roster_id, machine_pub=on_roster.public_hex
    )
    _wire_runtime(
        tmp_path, monkeypatch, machine_id=on_roster_id, root=root,
        roster_entries=(entry,), expected_entry=entry,
    )
    # A stale payload for a machine the fleet has since dropped: it names a
    # machine key absent from the current roster, so from_browser_payload (run
    # inside _activate_runtime) refuses it and the machine stays unarmed.
    off_roster = KeyPair.from_private_hex("55" * 32)
    off_roster_id = "66" * 32
    payload = _runtime_payload(off_roster_id, off_roster)
    fleet_relay_sync.FleetRuntimeWarmCache(
        "org-x", name_prefix="fleet-dashboard-runtime"
    ).store(payload)

    # The PROPERTY is that a de-rostered payload does not arm the machine.
    # Raising was the old mechanism; the replay now reports the failure and
    # returns False instead, because its only caller wraps it in
    # contextlib.suppress(Exception) and swallowed the distinction anyway.
    # Returning False and SAYING SO is strictly more informative than raising
    # into a suppressor.
    with caplog.at_level(logging.WARNING, logger=fleet_enrollment_routes.__name__):
        assert fleet_enrollment_routes.rearm_local_runtime_from_cache() is False
    assert fleet_sync_scheduler.dashboard_fleet_sync_service._config is None
    assert any("FAILED" in r.getMessage() for r in caplog.records), (
        "a stale payload must not fail silently — that silence is what made "
        "home's unarmed connectors undiagnosable for six hours")


# ── The replay must say which of three things happened (2026-09-09) ─────
#
# Home activated at 13:54:07Z and every connector generation after it started
# cold, with no explanation anywhere. `rearm_local_runtime_from_cache` returned
# False for "nothing cached" and RAISED for "cached but unusable", and its only
# caller wraps it in contextlib.suppress(Exception) — so a stale credential, an
# empty cache, and a successful replay were mutually indistinguishable from
# outside. Four restarts, four silences.


def test_an_empty_cache_says_so(monkeypatch, caplog):
    """Distinct from a failed replay, and previously identical to it."""
    from tools.dashboard import fleet_enrollment_routes as routes

    monkeypatch.setattr(
        routes, "_dashboard_runtime_cache",
        lambda: SimpleNamespace(load=lambda: None))

    with caplog.at_level(logging.WARNING, logger=routes.__name__):
        assert routes.rearm_local_runtime_from_cache() is False

    assert any("NO cached payload" in r.getMessage() for r in caplog.records)


def test_a_stale_credential_is_reported_not_swallowed(monkeypatch, caplog):
    """THE ONE THAT MATTERS. An expired delegation cert makes `_activate_runtime`
    raise; the caller suppresses every exception, so this was silent. It must
    name the cause and that a human unlock is required."""
    from tools.dashboard import fleet_enrollment_routes as routes

    monkeypatch.setattr(
        routes, "_dashboard_runtime_cache",
        lambda: SimpleNamespace(load=lambda: {"machine_id": "m"}))

    def _expired(_payload):
        raise ValueError("delegation cert is expired")

    monkeypatch.setattr(routes, "_activate_runtime", _expired)

    with caplog.at_level(logging.WARNING, logger=routes.__name__):
        assert routes.rearm_local_runtime_from_cache() is False

    message = " ".join(r.getMessage() for r in caplog.records)
    assert "FAILED" in message and "unlock" in message


def test_a_successful_replay_is_also_visible(monkeypatch, caplog):
    """Success was silent too, which is why the last known-good re-arm was six
    hours stale and sent the first diagnosis to the wrong event."""
    from tools.dashboard import fleet_enrollment_routes as routes

    monkeypatch.setattr(
        routes, "_dashboard_runtime_cache",
        lambda: SimpleNamespace(load=lambda: {"machine_id": "m"}))
    monkeypatch.setattr(routes, "_activate_runtime", lambda _p: None)

    with caplog.at_level(logging.WARNING, logger=routes.__name__):
        assert routes.rearm_local_runtime_from_cache() is True

    assert any("replayed" in r.getMessage() for r in caplog.records)
