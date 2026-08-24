"""Dashboard and session views of the two non-swappable secret mounts."""

from __future__ import annotations

from pathlib import Path

import yaml

from agents import secret_ramfs


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_dashboard_receives_delivery_mount_with_one_way_propagation():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    volumes = compose["services"]["dashboard"]["volumes"]
    delivery = next(
        volume for volume in volumes
        if isinstance(volume, dict)
        and volume.get("source") == secret_ramfs.DELIVERY_MOUNT
    )
    assert delivery["target"] == secret_ramfs.DELIVERY_MOUNT
    # rslave receives host mount events but does not propagate dashboard mount
    # events back to the host or sibling sessions.
    assert delivery["bind"]["propagation"] == "rslave"
    assert delivery["bind"]["create_host_path"] is True


def test_provisioner_verifies_both_dashboard_bound_roots(monkeypatch):
    calls = []
    monkeypatch.setattr(
        secret_ramfs,
        "provision",
        lambda path, *, container_bound: calls.append((path, container_bound)),
    )
    assert secret_ramfs.main([]) == 0
    assert calls == [
        (secret_ramfs.DELIVERY_MOUNT, True),
        (secret_ramfs.KEYCACHE_MOUNT, True),
    ]
