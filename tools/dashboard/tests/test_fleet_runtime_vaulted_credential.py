"""The Dashboard re-arms its Fleet runtime credential from this machine's
audited vault (graph://67d0aa5f-885 D3, approved 2026-09-20).

The credential is delivered by the browser at sign-on. It used to be held in a
hand-carried ramfs file (auto-5er0n); the vault's own warmth already crosses a
hot reload in ramfs, so that second carrier bought nothing. ``_activate_runtime``
now seals the COMPLETE payload into ``autonomy.machine.vault.audited`` (row
``fleet-runtime``, machine store, never replicated) and
``rearm_local_runtime_from_vault`` replays it after the vault restore — through
the same activation, so a stale or de-rostered payload is re-verified and
refused rather than arming the machine with a credential the fleet dropped.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import time
import types

import pytest

from tools.dashboard import fleet_enrollment_routes
from tools.network import fleet_relay_sync, fleet_roster, fleet_sync_scheduler
from tools.network.idkit import KeyPair, Subject, issue_cert


@pytest.fixture(autouse=True)
def _vault(tmp_path, monkeypatch):
    """A cold vault over a fresh personal.db with the audited delegate
    recipient published (as unlock does), a machine store under tmp_path,
    and a ramfs stand-in for the connector caches. Yields the delegate's
    private half; a test warms the vault by installing it."""
    from tools.graph.db import GraphDB
    from tools.graph import settings_ops
    from tools.vault import key_holder
    from tools.vault.personal_object import derive_delegate_audited_recipient
    from tools.vault.store import VaultStore

    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.setenv("AUTONOMY_KEYCACHE_MOUNT", str(tmp_path))
    monkeypatch.setattr(
        "tools.network.storagekit.memory_cache.assert_memory_backed",
        lambda *a, **k: None,
    )
    personal = tmp_path / "personal.db"
    monkeypatch.setattr(key_holder, "_scoped_db", lambda _set_id, _org: personal)
    GraphDB(personal).close()
    GraphDB.close_all_pooled()
    private_hex, public_hex = derive_delegate_audited_recipient(bytes(range(32)))
    with VaultStore(personal) as store:
        store.put_delegate_audited_recipient(public_hex)
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)
    settings_ops.set_personal_delegate_audited_key(None)
    yield private_hex
    GraphDB.close_all_pooled()
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)
    settings_ops.set_personal_delegate_audited_key(None)


def _warm(private_hex):
    from tools.graph import settings_ops
    settings_ops.set_personal_delegate_audited_key(private_hex)


def _vaulted_credential():
    from tools.graph import settings_ops
    from tools.graph.schemas.machine_vault import (
        MACHINE_VAULT_AUDITED_SET_ID, RUNTIME_CREDENTIAL_KEY,
    )
    rows = {s.key: s for s in settings_ops.read_set(MACHINE_VAULT_AUDITED_SET_ID, org="machine")}
    return rows.get(RUNTIME_CREDENTIAL_KEY)


def _vault_revisions():
    from tools.graph.db import GraphDB, _org_db_path
    from tools.graph.schemas.machine_vault import MACHINE_VAULT_AUDITED_SET_ID
    n = GraphDB(_org_db_path("machine")).conn.execute(
        "SELECT count(*) FROM settings WHERE set_id=?", (MACHINE_VAULT_AUDITED_SET_ID,)
    ).fetchone()[0]
    GraphDB.close_all_pooled()
    return n


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


def test_rearm_returns_false_when_nothing_is_vaulted(tmp_path, monkeypatch, caplog):
    """A machine that never activated has no vaulted credential: the replay is
    a no-op, says so, and leaves the machine unarmed."""
    with caplog.at_level(logging.WARNING, logger=fleet_enrollment_routes.__name__):
        assert fleet_enrollment_routes.rearm_local_runtime_from_vault() is False
    assert fleet_sync_scheduler.dashboard_fleet_sync_service._config is None
    assert any("NO vaulted credential" in r.getMessage() for r in caplog.records)


def test_activate_runtime_seals_the_complete_credential_into_the_machine_vault(
    tmp_path, monkeypatch, _vault,
):
    root = KeyPair.from_private_hex("11" * 32)
    machine = KeyPair.from_private_hex("22" * 32)
    machine_id = "33" * 32
    entry = fleet_roster.enroll(root, machine_id=machine_id, machine_pub=machine.public_hex)
    _wire_runtime(tmp_path, monkeypatch, machine_id=machine_id, root=root,
                  roster_entries=(entry,), expected_entry=entry)
    monkeypatch.setattr(fleet_enrollment_routes, "serving_org_targets", lambda: [])
    payload = _runtime_payload(machine_id, machine)
    payload["serving_machine_private_seeds"] = {"uuid-anchore": "77" * 32}
    payload["org_sync_certs"] = {"anchore": {"child_pub": "a1" * 32}}

    # The write is COLD: no sealer, no key holder, delegate not warm.
    fleet_enrollment_routes._activate_runtime(payload, publish_connector=False)

    # No hand-carried dashboard file any more.
    assert not (tmp_path / "fleet-dashboard-runtime.org-x.json").exists()
    # Cold read fails closed; warm read opens the WHOLE payload, seeds included.
    row = _vaulted_credential()
    assert row is not None and row.vault_error is not None
    _warm(_vault)
    row = _vaulted_credential()
    assert row.vault_error is None
    stored = json.loads(row.payload["value"])
    assert stored == payload


def test_rearm_from_the_vaulted_credential_configures_the_dashboard_sync(
    tmp_path, monkeypatch, _vault,
):
    root = KeyPair.from_private_hex("11" * 32)
    machine = KeyPair.from_private_hex("22" * 32)
    machine_id = "33" * 32
    entry = fleet_roster.enroll(root, machine_id=machine_id, machine_pub=machine.public_hex)
    _wire_runtime(tmp_path, monkeypatch, machine_id=machine_id, root=root,
                  roster_entries=(entry,), expected_entry=entry)
    monkeypatch.setattr(fleet_enrollment_routes, "serving_org_targets", lambda: [])
    fleet_enrollment_routes._activate_runtime(
        _runtime_payload(machine_id, machine), publish_connector=False)
    revisions_after_activation = _vault_revisions()
    # A restart: the scheduler forgets its configuration.
    fleet_sync_scheduler.dashboard_fleet_sync_service.configure(None)

    # Cold: the row is there but cannot be opened yet. Says so, returns False.
    with caplog_for(fleet_enrollment_routes) as records:
        assert fleet_enrollment_routes.rearm_local_runtime_from_vault() is False
    assert any("cannot be opened in this process yet" in m for m in records)
    assert fleet_sync_scheduler.dashboard_fleet_sync_service._config is None

    # After the vault restore (the delegate is warm): the replay re-arms.
    _warm(_vault)
    assert fleet_enrollment_routes.rearm_local_runtime_from_vault() is True
    assert (
        fleet_sync_scheduler.dashboard_fleet_sync_service._config.roster_machine_pub
        == machine.public_hex
    )
    # The replay does not re-seal what it just opened.
    assert _vault_revisions() == revisions_after_activation


def test_a_payload_naming_an_off_roster_machine_does_not_activate(
    tmp_path, monkeypatch, caplog, _vault,
):
    root = KeyPair.from_private_hex("11" * 32)
    on_roster = KeyPair.from_private_hex("22" * 32)
    on_roster_id = "33" * 32
    entry = fleet_roster.enroll(root, machine_id=on_roster_id, machine_pub=on_roster.public_hex)
    _wire_runtime(tmp_path, monkeypatch, machine_id=on_roster_id, root=root,
                  roster_entries=(entry,), expected_entry=entry)
    # A stale credential vaulted for a machine the fleet has since dropped.
    off_roster = KeyPair.from_private_hex("55" * 32)
    fleet_enrollment_routes._store_runtime_credential(_runtime_payload("66" * 32, off_roster))
    _warm(_vault)
    with caplog.at_level(logging.WARNING, logger=fleet_enrollment_routes.__name__):
        assert fleet_enrollment_routes.rearm_local_runtime_from_vault() is False
    assert fleet_sync_scheduler.dashboard_fleet_sync_service._config is None
    assert any("FAILED" in r.getMessage() for r in caplog.records), (
        "a stale payload must not fail silently — that silence is what made "
        "home's unarmed connectors undiagnosable for six hours")


def test_a_successful_replay_is_also_visible(monkeypatch, caplog):
    """Success was silent too, which is why the last known-good re-arm was six
    hours stale and sent the first diagnosis to the wrong event."""
    monkeypatch.setattr(
        fleet_enrollment_routes, "_load_runtime_credential",
        lambda: ({"machine_id": "m"}, None))
    monkeypatch.setattr(fleet_enrollment_routes, "_activate_runtime", lambda _p, **kw: None)
    with caplog.at_level(logging.WARNING, logger=fleet_enrollment_routes.__name__):
        assert fleet_enrollment_routes.rearm_local_runtime_from_vault() is True
    assert any("replayed" in r.getMessage() for r in caplog.records)


def test_a_serving_machine_pre_seeds_the_personal_connector_cache(
    tmp_path, monkeypatch,
):
    """graph://1418ca10-588 D1/D4. On the machine designated to serve, an
    activation writes the personal CONNECTOR's cache file before the socket
    publish, so a connector whose file was missing arms on its next launch
    with nobody present. The connector files are the hand-off and stay
    (graph://67d0aa5f-885 D3); the dashboard's own copy is in the vault."""
    root = KeyPair.from_private_hex("11" * 32)
    machine = KeyPair.from_private_hex("22" * 32)
    machine_id = "33" * 32
    entry = fleet_roster.enroll(root, machine_id=machine_id, machine_pub=machine.public_hex)
    _wire_runtime(tmp_path, monkeypatch, machine_id=machine_id, root=root,
                  roster_entries=(entry,), expected_entry=entry)
    monkeypatch.setattr(fleet_enrollment_routes, "serving_org_targets", lambda: [])
    published = []
    monkeypatch.setattr(
        fleet_enrollment_routes.fleet_relay_sync, "publish_connector_runtime",
        lambda payload, org=None: published.append(org),
    )
    payload = _runtime_payload(machine_id, machine)
    fleet_enrollment_routes._activate_runtime(payload, publish_connector=True)
    assert (tmp_path / "fleet-connector-runtime.org-x.json").exists()
    assert fleet_relay_sync.FleetRuntimeWarmCache("org-x").load() == payload
    assert not (tmp_path / "fleet-dashboard-runtime.org-x.json").exists()
    assert published == ["personal"]


def test_a_replay_holds_the_org_key_after_a_restart(tmp_path, monkeypatch, _vault):
    """Home, 2026-09-20: the dashboard's hand-carried copy held the certificates
    and not the seeds, so every restart re-armed without an org serving key
    and the persona write floor declined every round. The vaulted copy is the
    complete payload; a replay installs the key."""
    from tools.dashboard import org_sync_channels

    root = KeyPair.from_private_hex("11" * 32)
    machine = KeyPair.from_private_hex("22" * 32)
    machine_id = "33" * 32
    entry = fleet_roster.enroll(root, machine_id=machine_id, machine_pub=machine.public_hex)
    _wire_runtime(tmp_path, monkeypatch, machine_id=machine_id, root=root,
                  roster_entries=(entry,), expected_entry=entry)
    serving = KeyPair.from_private_hex("77" * 32)
    monkeypatch.setattr(
        org_sync_channels, "sync_org_targets",
        lambda: [{"scope": "anchore", "genesis_id": "a" * 64,
                  "persona_pub": "b" * 64, "org_uuid": "uuid-anchore"}],
    )
    monkeypatch.setattr(fleet_enrollment_routes, "serving_org_targets", lambda: [])
    payload = _runtime_payload(machine_id, machine)
    payload["serving_machine_private_seeds"] = {"uuid-anchore": serving.private_hex}
    payload["org_sync_certs"] = {"anchore": {"child_pub": serving.public_hex}}
    fleet_enrollment_routes._activate_runtime(payload, publish_connector=False)

    org_sync_channels.install({}, {})
    assert org_sync_channels.report() == {}
    _warm(_vault)
    assert fleet_enrollment_routes.rearm_local_runtime_from_vault() is True
    held = org_sync_channels.report()["anchore"]
    assert held["certificate"]["child_pub"] == serving.public_hex
    assert held["key_held"] is True, held


class caplog_for:
    """A tiny context manager collecting WARNING messages of one logger,
    for tests that need two separate capture windows."""

    def __init__(self, module):
        self._logger = logging.getLogger(module.__name__)
        self._records = []
        self._handler = logging.Handler()
        self._handler.emit = lambda r: self._records.append(r.getMessage())

    def __enter__(self):
        self._handler.setLevel(logging.WARNING)
        self._logger.addHandler(self._handler)
        return self._records

    def __exit__(self, *exc):
        self._logger.removeHandler(self._handler)
        return False
