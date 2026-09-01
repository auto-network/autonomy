"""Compose contract for the dormant, node-local Service gateway (auto-dbex5)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"
NODE_DOCKERFILE = REPO_ROOT / "deploy/Dockerfile"
ACCEPTANCE_OVERRIDE = (
    REPO_ROOT / "tools/network/acceptance/compose.service-gateway.yml"
)
BOOTSTRAP = REPO_ROOT / "deploy/service-gateway/bootstrap.json"
GATEWAY_DOCKERFILE = REPO_ROOT / "deploy/Dockerfile.service-gateway"
INSTALL = REPO_ROOT / "deploy/install/INSTALL.md"


def test_node_image_bakes_the_compose_plugin_used_by_gateway_supervision():
    dockerfile = NODE_DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG DOCKER_COMPOSE_VERSION=5.5.0" in dockerfile
    assert (
        "ARG DOCKER_COMPOSE_SHA256="
        "c57ab918abd5b05ca7e7d0f275875dd1330a695074f309dc9eab1b49efafcd4b"
        in dockerfile
    )
    assert "docker-compose-linux-x86_64" in dockerfile
    assert "/usr/local/lib/docker/cli-plugins/docker-compose" in dockerfile
    assert "docker compose version" in dockerfile


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
        "${AUTONOMY_SERVICE_GATEWAY_IMAGE:-autonomy-service-gateway:local}"
    )
    assert gateway["build"] == {
        "context": ".",
        "dockerfile": "deploy/Dockerfile.service-gateway",
    }
    assert gateway["profiles"] == ["service-gateway"]
    # This pinned image has no ENTRYPOINT; its default CMD includes the binary.
    # Replacing CMD must therefore retain `caddy` as argv[0].
    assert gateway["command"] == [
        "caddy",
        "run",
        "--config",
        "/etc/caddy/bootstrap.json",
    ]
    assert "ports" not in gateway
    assert gateway["networks"] == ["default"]
    assert gateway["expose"] == ["9443"]


def test_service_gateway_has_the_fixed_container_security_boundary():
    gateway = _compose()["services"]["service-gateway"]

    assert gateway["user"] == "1000:1000"
    assert gateway["read_only"] is True
    assert gateway["cap_drop"] == ["ALL"]
    assert "cap_add" not in gateway
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
        "CMD-SHELL",
        "test -S /run/autonomy-service-gateway/admin.sock && kill -0 1",
    ]
    assert gateway["healthcheck"]["interval"] == "1s"
    assert gateway["healthcheck"]["start_interval"] == "1s"


def test_service_gateway_image_strips_the_unused_privileged_port_capability():
    dockerfile = GATEWAY_DOCKERFILE.read_text()

    assert dockerfile.startswith(
        "FROM docker.io/library/caddy:2.11.4-alpine@"
        "sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648\n"
    )
    assert "setcap -r /usr/bin/caddy" in dockerfile
    assert 'test -z "$(getcap /usr/bin/caddy)"' in dockerfile


def test_service_gateway_mounts_only_config_control_and_ramfs_certificate_input():
    compose = _compose()
    gateway = compose["services"]["service-gateway"]
    dashboard = compose["services"]["dashboard"]
    sources = _volume_sources(gateway)

    assert "/var/run/docker.sock" not in sources
    assert "service-gateway-control" in sources
    assert "/run/autonomy-keycache/service-gateway" in sources
    assert (
        "${AUTONOMY_HOST_ROOT:-.}/deploy/service-gateway/bootstrap.json"
        in sources
    )
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


def test_install_prebuilds_the_profile_image_without_starting_the_gateway():
    install = INSTALL.read_text()

    assert "docker compose --profile service-gateway build service-gateway" in install


def test_certbot_job_is_digest_pinned_ephemeral_and_receives_only_acme_ramfs():
    compose = _compose()
    certbot = compose["services"]["service-certbot"]

    assert certbot["profiles"] == ["service-certbot"]
    assert certbot["image"] == (
        "docker.io/certbot/certbot:v5.7.0@"
        "sha256:34ee91d2f43008eb78a007d22f23ed4b2eaa9a454cb27ca2c042b49527a695b4"
    )
    assert certbot["restart"] == "no"
    assert certbot["read_only"] is True
    assert certbot["cap_drop"] == ["ALL"]
    assert certbot["security_opt"] == ["no-new-privileges:true"]
    assert certbot["user"] == "1000:1000"
    assert "ports" not in certbot
    assert "/var/run/docker.sock" not in _volume_sources(certbot)
    assert _volume_sources(certbot) == [
        "/run/autonomy-keycache/service-acme",
        "${AUTONOMY_HOST_ROOT:-.}/deploy/service-certbot/dns01-hook.py",
    ]
    assert set(certbot["tmpfs"]) == {
        "/tmp:rw,noexec,nosuid,nodev,size=16m,uid=1000,gid=1000,mode=0700",
    }
