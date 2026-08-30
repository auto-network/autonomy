"""The connector re-arms itself across a restart from a warm ramfs cache
(auto-ixwr3).

The Fleet runtime credential is minted in the browser, pushed once, and held
only in the connector's memory — so a connector that crashed or was respawned by
the watchdog came back with ``scheduler is None`` and refused every pull until a
human unlocked again. The warm cache — the same ramfs treatment the delegate
signing key gets (auto-a1pub) — hands the credential to the next process so it
re-arms with nobody present. A reboot clears ramfs and still fails closed to a
human unlock; a crash costs a few seconds of serving.
"""

from __future__ import annotations

import json
import time

import pytest

from tools.network import fleet_relay_sync, fleet_roster
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


def _valid_payload(tmp_path, monkeypatch, *, expired=False):
    """Build a real, roster-verified fleet runtime payload and wire the
    process-side reads configure() performs. ``expired`` mints a delegation
    whose lifetime is already in the past, so configure() refuses it."""
    root = KeyPair.from_private_hex("11" * 32)
    machine = KeyPair.from_private_hex("22" * 32)
    machine_id = "33" * 32
    entries = (
        fleet_roster.enroll(
            root, machine_id=machine_id, machine_pub=machine.public_hex
        ),
    )
    now = int(time.time())
    process = KeyPair.from_private_hex("44" * 32)
    not_before, not_after = (
        (now - 600, now - 300) if expired else (now - 30, now + 300)
    )
    cert = issue_cert(
        machine,
        process.public_hex,
        scope=["fleet:sync"],
        org=f"personal:{root.public_hex}",
        subject=Subject(kind="machine", id=machine_id),
        not_before=not_before,
        not_after=not_after,
    )
    payload = {
        "machine_id": machine_id,
        "machine_pub": machine.public_hex,
        "process_private_seed": process.private_hex,
        "delegation_cert": cert.to_dict(),
    }
    personal = tmp_path / "personal.db"
    personal.touch()
    monkeypatch.setattr(
        "tools.network.fleet_tunnel_server._personal_root_pub",
        lambda: root.public_hex,
    )
    monkeypatch.setattr(
        fleet_relay_sync.fleet_roster, "load_entries",
        lambda *, org: list(entries),
    )
    monkeypatch.setattr(fleet_relay_sync, "_org_db_path", lambda _org: personal)
    return payload, machine_id


def test_cache_roundtrips_the_payload(tmp_path):
    cache = fleet_relay_sync.FleetRuntimeWarmCache("org-a")
    assert cache.load() is None  # a fresh (post-reboot) cache reads empty
    cache.store({"machine_id": "ab", "process_private_seed": "cd"})
    assert cache.load() == {"machine_id": "ab", "process_private_seed": "cd"}
    cache.clear()
    assert cache.load() is None


def test_dashboard_cache_uses_a_distinct_file_name(tmp_path):
    # The Dashboard keeps its OWN copy of the same credential under a different
    # file name (auto-5er0n), so it never reads the connector's entry.
    cache = fleet_relay_sync.FleetRuntimeWarmCache(
        "org-a", name_prefix="fleet-dashboard-runtime"
    )
    cache.store({"machine_id": "ab"})
    assert (tmp_path / "fleet-dashboard-runtime.org-a.json").exists()
    assert not (tmp_path / "fleet-connector-runtime.org-a.json").exists()
    assert cache.load() == {"machine_id": "ab"}


def test_connector_and_dashboard_entries_coexist_for_one_org(tmp_path):
    # Same org_uuid, two credentials: the distinct file names mean neither
    # write overwrites the other. On a serving machine BOTH files exist.
    connector = fleet_relay_sync.FleetRuntimeWarmCache("org-a")
    dashboard = fleet_relay_sync.FleetRuntimeWarmCache(
        "org-a", name_prefix="fleet-dashboard-runtime"
    )
    connector.store({"which": "connector"})
    dashboard.store({"which": "dashboard"})
    assert connector.load() == {"which": "connector"}
    assert dashboard.load() == {"which": "dashboard"}
    # And the Dashboard cache stays empty of the connector's entry when only the
    # connector wrote (a serving machine's connector entry must not re-arm a
    # non-serving Dashboard's read path).
    connector.clear()
    assert connector.load() is None
    assert dashboard.load() == {"which": "dashboard"}


def test_configure_warms_the_cache_and_a_fresh_process_rearms(
    tmp_path, monkeypatch
):
    payload, machine_id = _valid_payload(tmp_path, monkeypatch)

    # The armed connector caches the credential the moment it configures.
    armed = fleet_relay_sync.ConnectorFleetRuntime()
    armed.attach_warm_cache(fleet_relay_sync.FleetRuntimeWarmCache("org-a"))
    assert armed.configure(payload) == {"ok": True, "machine_id": machine_id}

    cache_file = tmp_path / "fleet-connector-runtime.org-a.json"
    assert json.loads(cache_file.read_text()) == payload

    # A restarted connector (fresh runtime, scheduler is None) re-arms itself
    # from that cache with no human unlock.
    restarted = fleet_relay_sync.ConnectorFleetRuntime()
    assert restarted.scheduler is None
    restarted.attach_warm_cache(fleet_relay_sync.FleetRuntimeWarmCache("org-a"))
    assert restarted.rearm_from_cache() is True
    assert restarted.scheduler is not None


def test_only_the_matching_org_rearms(tmp_path, monkeypatch):
    payload, _ = _valid_payload(tmp_path, monkeypatch)
    armed = fleet_relay_sync.ConnectorFleetRuntime()
    armed.attach_warm_cache(fleet_relay_sync.FleetRuntimeWarmCache("org-a"))
    armed.configure(payload)

    # A different org's connector shares the mount but not the file: it reads
    # nothing and stays exactly as it was.
    other = fleet_relay_sync.ConnectorFleetRuntime()
    other.attach_warm_cache(fleet_relay_sync.FleetRuntimeWarmCache("org-b"))
    assert other.rearm_from_cache() is False
    assert other.scheduler is None


def test_an_expired_credential_does_not_rearm_and_is_cleared(
    tmp_path, monkeypatch
):
    payload, _ = _valid_payload(tmp_path, monkeypatch, expired=True)
    # A credential left behind by a much earlier arm: its delegation lifetime
    # has already passed, so it can never verify again.
    cache = fleet_relay_sync.FleetRuntimeWarmCache("org-a")
    cache.store(payload)

    runtime = fleet_relay_sync.ConnectorFleetRuntime()
    runtime.attach_warm_cache(fleet_relay_sync.FleetRuntimeWarmCache("org-a"))
    assert runtime.rearm_from_cache() is False
    assert runtime.scheduler is None
    # A credential that will never verify again is dropped, not retried forever.
    assert cache.load() is None


def test_no_cache_attached_is_a_silent_no_op(tmp_path, monkeypatch):
    payload, machine_id = _valid_payload(tmp_path, monkeypatch)
    runtime = fleet_relay_sync.ConnectorFleetRuntime()
    # No warm cache: configure still arms, rearm is a no-op, nothing persists.
    assert runtime.configure(payload) == {"ok": True, "machine_id": machine_id}
    assert runtime.rearm_from_cache() is False
    assert not (tmp_path / "fleet-connector-runtime.org-a.json").exists()
