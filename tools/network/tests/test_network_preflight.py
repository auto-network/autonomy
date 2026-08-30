"""Tests for the network preflight subnet chooser, overlap report, and the
fail-closed Compose network pin.

The chooser and overlap report are pure functions over fixture routing tables —
never the live host — so they are deterministic and CI-portable. The
``docker compose config`` checks run only where a Docker CLI is present and skip
otherwise.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tools.network import network_preflight as np

REPO_ROOT = Path(__file__).resolve().parents[3]


# ── subnet chooser ───────────────────────────────────────────────────────

def test_chooses_172_block_clear_of_a_home_lan():
    """A routing table with a 192.168.1.0/24 LAN yields a 172.16.0.0/12 block
    that overlaps nothing in that table."""
    routes = [
        "default",  # ignored by the chooser's caller; harmless here
        "192.168.1.0/24",
        "192.168.1.42/32",
    ]
    chosen = np.choose_subnet(routes)
    assert chosen == "172.16.0.0/24"  # first free /24 in the space
    block = np.ipaddress.ip_network(chosen)
    for r in ("192.168.1.0/24", "192.168.1.42/32"):
        assert not block.overlaps(np.ipaddress.ip_network(r))


def test_chooser_avoids_a_routed_part_of_the_172_space():
    """When part of 172.16.0.0/12 is already routed, the chosen block avoids it."""
    routes = ["172.16.0.0/24", "192.168.1.0/24"]
    chosen = np.choose_subnet(routes)
    assert chosen == "172.16.1.0/24"
    assert not np.ipaddress.ip_network(chosen).overlaps(
        np.ipaddress.ip_network("172.16.0.0/24")
    )


def test_chooser_skips_a_larger_routed_block():
    """A routed /20 at the base of the space pushes the choice past all of it."""
    routes = ["172.16.0.0/20"]
    chosen = np.choose_subnet(routes)
    # 172.16.0.0/20 covers 172.16.0.0–172.16.15.255, so the first free /24 is next.
    assert chosen == "172.16.16.0/24"


def test_chooser_is_deterministic():
    """The same fixture yields the same block on every run."""
    routes = ["192.168.1.0/24", "172.16.0.0/24", "10.0.0.0/8"]
    results = {np.choose_subnet(routes) for _ in range(5)}
    assert results == {"172.16.1.0/24"}


def test_chooser_ignores_ipv6_and_junk_routes():
    routes = ["fe80::/64", "not-a-cidr", "", "192.168.1.0/24"]
    assert np.choose_subnet(routes) == "172.16.0.0/24"


def test_chooser_returns_none_when_space_is_full():
    """Contrived: the whole space routed leaves no block."""
    assert np.choose_subnet(["172.16.0.0/12"]) is None


# ── overlap report ───────────────────────────────────────────────────────

def test_pool_overlap_flags_a_pool_containing_a_host_route():
    """The built-in 192.168.0.0/16 pool over a 192.168.1.0/24 LAN is the incident."""
    pools, is_builtin = np.pools_in_effect(None)
    assert is_builtin is True
    hits = np.overlapping_pairs(pools, ["192.168.1.0/24"])
    assert ("192.168.0.0/16", "192.168.1.0/24") in hits


def test_pool_overlap_clear_when_no_pool_contains_a_route():
    """A configured pool disjoint from the host's networks reports clear."""
    pools, is_builtin = np.pools_in_effect(
        [{"Base": "10.10.0.0/16", "Size": 24}]
    )
    assert is_builtin is False
    assert np.overlapping_pairs(pools, ["192.168.1.0/24"]) == []


def test_pools_in_effect_defaults_are_builtin():
    for empty in (None, [], ""):
        pools, is_builtin = np.pools_in_effect(empty)
        assert is_builtin is True
        assert {p["base"] for p in pools} == {"172.17.0.0/12", "192.168.0.0/16"}


# ── parsers ──────────────────────────────────────────────────────────────

def test_parse_ip_route_extracts_destinations():
    text = (
        "default via 172.24.0.1 dev eth0\n"
        "172.24.0.0/20 dev eth0 proto kernel scope link src 172.24.5.6\n"
        "192.168.1.0/24 dev eth1 proto kernel scope link src 192.168.1.10\n"
        "blackhole 10.9.0.0/16\n"
        "8.8.8.8 via 172.24.0.1 dev eth0\n"
    )
    dests = np.parse_ip_route(text)
    assert "172.24.0.0/20" in dests
    assert "192.168.1.0/24" in dests
    assert "10.9.0.0/16" in dests
    assert "8.8.8.8/32" in dests
    assert "default" not in " ".join(dests)


def test_parse_ip_addr_flags_docker_bridges():
    text = (
        "1: lo    inet 127.0.0.1/8 scope host lo\n"
        "2: eth0    inet 192.168.1.10/24 brd 192.168.1.255 scope global eth0\n"
        "3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\n"
        "4: br-abcdef    inet 172.30.0.1/24 scope global br-abcdef\n"
    )
    got = {i["iface"]: i for i in np.parse_ip_addr(text)}
    assert got["eth0"]["cidr"] == "192.168.1.10/24"
    assert got["eth0"]["docker"] is False
    assert got["docker0"]["docker"] is True
    assert got["br-abcdef"]["docker"] is True


def test_parse_docker_networks_pulls_subnets():
    inspect = (
        '[{"Name":"autonomy-trial_default",'
        '"IPAM":{"Config":[{"Subnet":"192.168.0.0/20"}]}},'
        '{"Name":"host","IPAM":{"Config":null}}]'
    )
    nets = np.parse_docker_networks(inspect)
    by_name = {n["name"]: n for n in nets}
    assert by_name["autonomy-trial_default"]["subnets"] == ["192.168.0.0/20"]
    assert by_name["host"]["subnets"] == []


def test_parse_docker_pools_null_is_none():
    assert np.parse_docker_pools("null") is None
    assert np.parse_docker_pools(
        '[{"Base":"172.17.0.0/12","Size":16}]'
    ) == [{"Base": "172.17.0.0/12", "Size": 16}]


# ── docker compose config: fail-closed on unset AUTONOMY_SUBNET ───────────

def _compose_argv() -> list[str] | None:
    if shutil.which("docker"):
        return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return None


def _run_compose_config(env: dict) -> subprocess.CompletedProcess:
    base = _compose_argv()
    assert base is not None
    return subprocess.run(
        [*base, "-f", str(REPO_ROOT / "docker-compose.yml"), "config"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=60,
    )


def test_compose_config_fails_without_subnet():
    base = _compose_argv()
    if base is None:
        pytest.skip("docker compose CLI not available")
    import os
    env = {k: v for k, v in os.environ.items() if k != "AUTONOMY_SUBNET"}
    result = _run_compose_config(env)
    assert result.returncode != 0, result.stdout
    assert "AUTONOMY_SUBNET" in (result.stderr + result.stdout)


def test_compose_config_resolves_with_subnet():
    base = _compose_argv()
    if base is None:
        pytest.skip("docker compose CLI not available")
    import os
    env = dict(os.environ)
    env["AUTONOMY_SUBNET"] = "172.16.0.0/24"
    result = _run_compose_config(env)
    assert result.returncode == 0, result.stderr
    assert "172.16.0.0/24" in result.stdout


def test_compose_file_declares_subnet_without_a_default():
    """Static guard so the fail-closed contract survives even where the Docker
    CLI is absent: the top-level network pins ${AUTONOMY_SUBNET} with the `:?`
    required-variable operator and no `:-` default."""
    text = (REPO_ROOT / "docker-compose.yml").read_text()
    assert "${AUTONOMY_SUBNET:?" in text
    assert "${AUTONOMY_SUBNET:-" not in text
