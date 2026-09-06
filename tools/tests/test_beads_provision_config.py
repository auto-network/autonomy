"""The org-tracker config.yaml host convention (auto-7iw4r).

The 2026-09-01 defect: provisioning wrote the compose DNS name
(``host: dolt``) into a file host processes read, and the first
contract-correct backup run failed blindhash's dump with "Unknown MySQL
server host 'dolt'". These tests pin the fixed convention: the
published (host-reachable) binding is what lands in config.yaml, an
unpublished Dolt yields no dolt block at all, and repair-config
rewrites an existing tracker in place.
"""
from __future__ import annotations

import json

import pytest

from tools import beads_provision as BP


def _with_inspect(monkeypatch, ports):
    def fake_docker(*args, timeout=120):
        assert args[0] == "inspect"
        return json.dumps(ports)
    monkeypatch.setattr(BP, "_docker", fake_docker)
    monkeypatch.setattr(BP, "_dolt_container", lambda: "dolt-cid")


class TestHostBinding:
    def test_published_bridge_binding_wins(self, monkeypatch):
        _with_inspect(monkeypatch, {
            "3306/tcp": [{"HostIp": "172.17.0.1", "HostPort": "3306"}],
        })
        assert BP._dolt_host_binding("dolt-cid") == ("172.17.0.1", 3306)

    def test_all_interfaces_becomes_loopback(self, monkeypatch):
        _with_inspect(monkeypatch, {
            "3306/tcp": [{"HostIp": "0.0.0.0", "HostPort": "13306"}],
        })
        assert BP._dolt_host_binding("dolt-cid") == ("127.0.0.1", 13306)

    def test_unpublished_is_none(self, monkeypatch):
        _with_inspect(monkeypatch, {"3306/tcp": None})
        assert BP._dolt_host_binding("dolt-cid") is None


class TestConfigText:
    def test_carries_host_reachable_address(self):
        text = BP._config_yaml_text("blindhash", ("172.17.0.1", 3306))
        assert "host: 172.17.0.1" in text
        assert "port: 3306" in text
        assert "host: dolt" not in text

    def test_unpublished_writes_no_dolt_block(self):
        text = BP._config_yaml_text("blindhash", None)
        assert "dolt:" not in text
        assert "none is recorded" in text


class TestRepair:
    def test_rewrites_existing_tracker(self, tmp_path, monkeypatch):
        tracker = tmp_path / "blindhash"
        tracker.mkdir()
        (tracker / "metadata.json").write_text("{}")
        (tracker / "config.yaml").write_text("dolt:\n  host: dolt\n")
        _with_inspect(monkeypatch, {
            "3306/tcp": [{"HostIp": "172.17.0.1", "HostPort": "3306"}],
        })
        text = BP.repair_config("blindhash", orgs_root=tmp_path)
        assert "host: 172.17.0.1" in text
        assert (tracker / "config.yaml").read_text() == text

    def test_refuses_unprovisioned_slug(self, tmp_path):
        with pytest.raises(BP.BeadsProvisionError, match="no provisioned"):
            BP.repair_config("ghost", orgs_root=tmp_path)
