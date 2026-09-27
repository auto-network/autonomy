"""The existing gateway consumes the proven OIDC pre-check."""
from dataclasses import replace
import json
import subprocess

import pytest

from tools.dashboard import service_gateway as gateway
from tools.dashboard.tests.test_service_gateway import _route
from tools.network.service_auth import GateConfig, render_auth_config
from tools.dashboard.web_gateway_supervisor import AuthHelper, ComposeGatewayRuntime, helper_listener_ports


def test_gated_route_keeps_auth_before_application():
    route = replace(_route(), gate=gateway.GatedRoute("org-oidc:anchore", "127.0.0.1:4181"))
    config = gateway.render_caddyfile([route])
    assert config.index("handle /oauth2/*") < config.index("forward_auth 127.0.0.1:4181")
    assert config.index("forward_auth 127.0.0.1:4181") < config.index("reverse_proxy 172.16.0.42:8000")
    assert config.count(f'header_up X-Forwarded-Host "{route.hostname}"') == 2
    assert config.count('header_up X-Forwarded-Proto "https"') == 2
    assert "request_header -Authorization" in config
    assert "request_header -X-Auth-Request-*" in config
    assert "redir * /oauth2/start?rd=https%3A%2F%2F" in config


def test_logging_exclusion_does_not_open_a_path():
    route = replace(_route(), gate=gateway.GatedRoute(
        "org-oidc:anchore", "127.0.0.1:4180", ("/oauth2/callback",)))
    config = gateway.render_caddyfile([route])
    assert 'log_skip "/oauth2/callback"' in config
    assert "forward_auth 127.0.0.1:4180" in config


def test_public_route_keeps_existing_proxy():
    config = gateway.render_caddyfile([_route()])
    assert "forward_auth" not in config
    assert "\treverse_proxy 172.16.0.42:8000 {\n\t\tflush_interval -1" in config


def test_helper_parameters_do_not_change_org_defaults():
    config = GateConfig("example", ("hello.example.com",), "https://example.okta.com", "client", 8101)
    normal = render_auth_config(config, listener_port=4181)
    assert 'http_address = "127.0.0.1:4181"' in normal
    assert 'cookie_expire = "15m"' in normal
    assert 'cookie_refresh = "0"' in normal
    personal = render_auth_config(config, listener_port=4182, cookie_expire="12h", cookie_refresh="1h")
    assert 'cookie_expire = "12h"' in personal
    assert 'cookie_refresh = "1h"' in personal


def test_one_listener_table_for_all_helpers():
    assert helper_listener_ports(["org-oidc:z", "dashboard-passkey", "org-oidc:a"]) == {
        "dashboard-passkey": 4180, "org-oidc:a": 4181, "org-oidc:z": 4182,
    }


@pytest.mark.asyncio
async def test_helpers_recreate_with_gateway_and_remove_only_their_services(tmp_path):
    calls = []

    async def runner(argv, timeout):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    override = tmp_path / "compose.json"
    runtime = ComposeGatewayRuntime(runner=runner, helper_override=str(override))
    helper = AuthHelper("org-oidc:anchore", str(tmp_path / "anchore"), "revision-1")
    await runtime.reconcile_helpers((helper,), "gateway-1")
    service = json.loads(override.read_text())["services"]["org-oidc-anchore"]
    assert service["network_mode"] == "service:service-gateway"
    assert "ports" not in service
    assert "mem_limit" not in service
    assert "--force-recreate" in calls[-1]
    count = len(calls)
    await runtime.reconcile_helpers((helper,), "gateway-1")
    assert len(calls) == count
    await runtime.reconcile_helpers((helper,), "gateway-2")
    assert len(calls) == count + 1
    assert "--force-recreate" in calls[-1]
    await runtime.reconcile_helpers((), "gateway-2")
    assert calls[-1][-4:] == ["rm", "-f", "-s", "org-oidc-anchore"]
    assert all("--remove-orphans" not in command for command in calls)


@pytest.mark.asyncio
async def test_dashboard_hot_reload_recovers_managed_helpers_for_removal(tmp_path):
    calls = []
    async def runner(argv, timeout):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    override = tmp_path / "compose.json"
    helper = AuthHelper("org-oidc:anchore", str(tmp_path / "anchore"), "v1")
    before = ComposeGatewayRuntime(runner=runner, helper_override=str(override))
    await before.reconcile_helpers((helper,), "gateway-1")
    after = ComposeGatewayRuntime(runner=runner, helper_override=str(override))
    await after.reconcile_helpers((), "gateway-1")
    assert calls[-1][-4:] == ["rm", "-f", "-s", "org-oidc-anchore"]
    assert json.loads(override.read_text()) == {"services": {}}
