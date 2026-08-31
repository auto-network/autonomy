"""Compose contract for the dormant, node-local Service gateway (auto-dbex5)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"
ACCEPTANCE_OVERRIDE = (
    REPO_ROOT / "tools/network/acceptance/compose.service-gateway.yml"
)
BOOTSTRAP = REPO_ROOT / "deploy/service-gateway/bootstrap.json"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def _volume_sources(service: dict) -> list[str]:
    sources = []
    for volume in service.get("volumes", []):
        if isinstance(volume, str):
            sources.append(volume.split(":", 1)[0])
        else:
            sources.append(str(volume.get("source", "")))
    return sources


def test_service_gateway_is_digest_pinned_dormant_and_not_host_published():
    gateway = _compose()["services"]["service-gateway"]

    assert gateway["image"] == (
        "docker.io/library/caddy:2.11.4-alpine@"
        "sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648"
    )
    assert gateway["profiles"] == ["service-gateway"]
    assert "ports" not in gateway
    assert gateway["networks"] == ["default"]
    assert gateway["expose"] == ["9443"]


def test_service_gateway_has_the_fixed_container_security_boundary():
    gateway = _compose()["services"]["service-gateway"]

    assert gateway["user"] == "1000:1000"
    assert gateway["read_only"] is True
    assert gateway["cap_drop"] == ["ALL"]
    assert gateway["security_opt"] == ["no-new-privileges:true"]
    assert gateway["pids_limit"] == 64
    assert gateway["mem_limit"] == "128m"
    assert str(gateway["cpus"]) == "0.5"
    assert set(gateway["tmpfs"]) == {
        "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
        "/data:rw,noexec,nosuid,nodev,size=16m,uid=1000,gid=1000,mode=0700",
        "/config:rw,noexec,nosuid,nodev,size=4m,uid=1000,gid=1000,mode=0700",
    }
    assert gateway["healthcheck"]["test"] == [
        "CMD",
        "curl",
        "-fsS",
        "--unix-socket",
        "/run/autonomy-service-gateway/admin.sock",
        "http://localhost/config/",
    ]


def test_service_gateway_mounts_only_config_control_and_ramfs_certificate_input():
    compose = _compose()
    gateway = compose["services"]["service-gateway"]
    dashboard = compose["services"]["dashboard"]
    sources = _volume_sources(gateway)

    assert "/var/run/docker.sock" not in sources
    assert "service-gateway-control" in sources
    assert "/run/autonomy-keycache/service-gateway" in sources
    assert "./deploy/service-gateway/bootstrap.json" in sources
    assert any(
        isinstance(volume, dict)
        and volume.get("source") == "/run/autonomy-keycache/service-gateway"
        and volume.get("target") == "/run/autonomy-service-gateway-certs"
        and volume.get("read_only") is True
        and volume.get("bind", {}).get("create_host_path") is False
        for volume in gateway["volumes"]
    )
    assert "service-gateway-control:/run/autonomy-service-gateway" in dashboard["volumes"]
    assert "service-gateway-control" in compose["volumes"]


def test_bootstrap_exposes_only_a_non_persisting_unix_admin_endpoint():
    config = json.loads(BOOTSTRAP.read_text())

    assert config == {
        "admin": {
            "listen": "unix//run/autonomy-service-gateway/admin.sock|0660",
            "config": {"persist": False},
        }
    }


def test_acceptance_override_is_the_only_host_port_publication():
    override = yaml.safe_load(ACCEPTANCE_OVERRIDE.read_text())
    gateway = override["services"]["service-gateway"]

    assert gateway["ports"] == ["127.0.0.1:${SERVICE_GATEWAY_TEST_PORT:-8443}:9443"]
