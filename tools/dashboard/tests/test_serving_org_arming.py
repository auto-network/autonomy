"""Every serving org connector must be armed, not just the personal one.

An org connector cannot start without a machine key, and the only channel that
ever delivered one was the control socket the connector itself serves — which
does not exist until it starts. activate_local_runtime published for org=None
alone, so once ramfs cleared (any container restart) every org connector on the
machine exited at launch, forever, and no unlock could change it: observed live
on sjc-2 2026-09-09, three org connectors down for an hour while the vault was
warm the whole time.

These drive the arming path itself: what gets written, where, and that a
connector which is not up yet still ends up with a usable credential.
"""

from __future__ import annotations

import pytest

from tools.dashboard import fleet_enrollment_routes as fer


BASE = {
    "machine_id": "ab" * 32,
    "machine_pub": "cd" * 32,
    "process_private_seed": "ef" * 32,
    "delegation_cert": {"v": 1},
}


class _Cache:
    """Stands in for the ramfs warm cache, recording what each org was given."""

    stored: dict = {}

    def __init__(self, org_uuid, *, name_prefix="fleet-connector-runtime"):
        self._org_uuid = org_uuid
        self._prefix = name_prefix

    def store(self, payload):
        _Cache.stored[(self._prefix, self._org_uuid)] = payload


@pytest.fixture
def arming(monkeypatch):
    _Cache.stored = {}
    published = []
    monkeypatch.setattr(fer.fleet_relay_sync, "FleetRuntimeWarmCache", _Cache)
    monkeypatch.setattr(
        fer.fleet_relay_sync, "publish_connector_runtime",
        lambda payload, org=None: published.append((org, payload)),
    )
    monkeypatch.setattr(fer, "serving_org_targets", lambda: [
        {"scope": "anchore", "org_uuid": "uuid-anchore", "genesis_id": "a" * 64},
        {"scope": "dynbench", "org_uuid": "uuid-dynbench", "genesis_id": "b" * 64},
    ])
    return published


def test_each_org_is_armed_with_its_own_key(arming):
    fer._arm_serving_orgs(BASE, {
        "uuid-anchore": "11" * 32,
        "uuid-dynbench": "22" * 32,
    })
    anchore = _Cache.stored[("fleet-connector-runtime", "uuid-anchore")]
    dynbench = _Cache.stored[("fleet-connector-runtime", "uuid-dynbench")]
    assert anchore["serving_machine_private_seed"] == "11" * 32
    assert dynbench["serving_machine_private_seed"] == "22" * 32
    # The rest of the credential is shared; only the serving key differs.
    assert anchore["process_private_seed"] == BASE["process_private_seed"]
    assert dynbench["machine_id"] == BASE["machine_id"]


def test_a_connector_that_is_not_running_is_still_armed(arming, monkeypatch):
    """The failure this fixes: nothing to publish TO, and therefore nothing
    cached, so the next launch failed exactly like the last one."""
    def refuse(payload, org=None):
        raise fer.fleet_relay_sync.FleetRelaySyncError("connector is not up")

    monkeypatch.setattr(
        fer.fleet_relay_sync, "publish_connector_runtime", refuse,
    )
    fer._arm_serving_orgs(BASE, {"uuid-anchore": "11" * 32})
    cached = _Cache.stored[("fleet-connector-runtime", "uuid-anchore")]
    assert cached["serving_machine_private_seed"] == "11" * 32


def test_an_org_with_no_seed_is_left_alone(arming):
    fer._arm_serving_orgs(BASE, {"uuid-anchore": "11" * 32})
    assert ("fleet-connector-runtime", "uuid-dynbench") not in _Cache.stored
    assert [org for org, _ in arming] == ["anchore"]


def test_no_seeds_at_all_touches_nothing(arming):
    fer._arm_serving_orgs(BASE, {})
    fer._arm_serving_orgs(BASE, None)
    assert _Cache.stored == {}
    assert arming == []


def test_a_cache_that_cannot_be_written_does_not_stop_the_next_org(
    arming, monkeypatch,
):
    """One org's ramfs failure must not strand the others."""
    class _Selective(_Cache):
        def store(self, payload):
            if self._org_uuid == "uuid-anchore":
                raise OSError("no ramfs here")
            super().store(payload)

    monkeypatch.setattr(fer.fleet_relay_sync, "FleetRuntimeWarmCache", _Selective)
    fer._arm_serving_orgs(BASE, {
        "uuid-anchore": "11" * 32,
        "uuid-dynbench": "22" * 32,
    })
    assert ("fleet-connector-runtime", "uuid-dynbench") in _Cache.stored


def test_the_credential_parser_rejects_the_seed_map(monkeypatch):
    """Why _activate_runtime peels the map: the credential contract is one
    scope's material, and an unknown key is refused outright."""
    from tools.network import fleet_runtime

    with pytest.raises(fleet_runtime.FleetRuntimeError):
        fleet_runtime.FleetRuntimeCredential.from_browser_payload(
            {**BASE, "serving_machine_private_seeds": {"uuid-anchore": "11" * 32}},
            personal_root_pub="ab" * 32,
            roster_entries=(),
        )


class _FakeLedger:
    def __init__(self, genesis_id):
        self.ledger = type("_L", (), {"genesis_id": genesis_id})()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def targets(monkeypatch):
    """Wire the four facts serving_org_targets consults, per scope."""
    from tools.dashboard import link_approvals, link_serving_supervisor
    from tools.network import ledger

    state = {
        "scopes": [None, "anchore", "dynbench"],
        "cert": {"anchore": "ok", "dynbench": "ok"},
        "binding": {"anchore": "uuid-anchore", "dynbench": "uuid-dynbench"},
        "ledger": {"anchore": "a" * 64, "dynbench": "b" * 64},
    }
    monkeypatch.setattr(
        link_serving_supervisor, "_discover_startup_orgs",
        lambda: state["scopes"],
    )
    monkeypatch.setattr(
        link_serving_supervisor, "serve_cert_state",
        lambda scope, **kw: {"status": state["cert"].get(scope, "missing")},
    )
    monkeypatch.setattr(
        link_approvals, "_load_binding",
        lambda scope: (
            {"org_uuid": state["binding"][scope]} if scope in state["binding"]
            else None,
            None,
        ),
    )
    monkeypatch.setattr(
        ledger, "org_ledger_db_path",
        lambda slug, root=None: type(
            "_P", (), {"exists": lambda self, s=slug: s in state["ledger"]},
        )(),
    )
    monkeypatch.setattr(
        ledger, "LedgerStore",
        lambda path: _FakeLedger(
            state["ledger"][
                next(s for s in state["ledger"] if path.exists())
            ]
        ),
    )
    return state


def test_targets_name_every_serving_org(targets):
    found = {t["scope"]: t for t in fer.serving_org_targets()}
    assert set(found) == {"anchore", "dynbench"}
    assert found["anchore"]["org_uuid"] == "uuid-anchore"


def test_the_personal_scope_is_not_a_target(targets):
    """Personal keeps the fleet machine key: its org is the operator's own, so
    a distinct key buys no unlinkability, and re-keying the connector that
    carries Fleet sync is risk for nothing."""
    assert all(t["scope"] is not None for t in fer.serving_org_targets())


def test_a_scope_with_no_serve_cert_is_not_a_target(targets):
    """Never provisioned to serve is a quiet fact, not a fault — the browser
    must not be asked to derive a key nothing will use."""
    targets["cert"].pop("dynbench")
    assert [t["scope"] for t in fer.serving_org_targets()] == ["anchore"]


def test_an_org_with_no_founded_ledger_is_skipped(targets):
    """No genesis id, no derivation input. Skipping keeps that org exactly as
    it is today rather than failing the whole unlock."""
    targets["ledger"].pop("anchore")
    assert [t["scope"] for t in fer.serving_org_targets()] == ["dynbench"]
