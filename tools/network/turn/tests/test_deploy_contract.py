from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


def load_renderer():
    spec = importlib.util.spec_from_file_location("turn_render", DEPLOY / "render_config.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def render(tmp_path: Path, *, ip="1.1.1.1", secret="a" * 64, total="32"):
    secret_path = tmp_path / "secrets"
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    secret_path.write_text(secret + "\n", encoding="ascii")
    cert.write_text("certificate\n")
    key.write_text("private-key\n")
    module = load_renderer()
    args = module.parser().parse_args([
        "--template", str(DEPLOY / "turnserver.conf.in"),
        "--secrets", str(secret_path),
        "--cert", str(cert),
        "--key", str(key),
        "--output", str(tmp_path / "run"),
        "--public-ip", ip,
        "--min-port", "49160",
        "--max-port", "49259",
        "--user-quota", "4",
        "--total-quota", total,
        "--max-bps", "5000000",
        "--bps-capacity", "100000000",
    ])
    module.render(args)
    return tmp_path / "run"


def test_renderer_keeps_secret_in_runtime_config_and_resolves_every_marker(tmp_path):
    runtime = render(tmp_path)
    config = (runtime / "turnserver.conf").read_text()
    assert "static-auth-secret=" + "a" * 64 in config
    assert "@@" not in config
    assert "external-ip=1.1.1.1" in config
    assert not any(
        line.startswith("prometheus-username-labels")
        for line in config.splitlines()
    )
    assert os.stat(runtime / "turnserver.conf").st_mode & 0o777 == 0o440
    assert os.stat(runtime / "tls-key.pem").st_mode & 0o777 == 0o440


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"])
def test_renderer_refuses_non_public_or_non_ipv4_listener(tmp_path, ip):
    with pytest.raises(ValueError, match="globally routable IPv4"):
        render(tmp_path, ip=ip)


def test_renderer_refuses_caddys_public_ip(tmp_path):
    with pytest.raises(ValueError, match="must not reuse Caddy"):
        render(tmp_path, ip="5.161.219.195")


def test_renderer_refuses_malformed_secret_without_echoing_it(tmp_path):
    with pytest.raises(ValueError) as error:
        render(tmp_path, secret="not-a-secret")
    assert "not-a-secret" not in str(error.value)


def test_renderer_refuses_quota_larger_than_relay_port_range(tmp_path):
    with pytest.raises(ValueError, match="relay port count"):
        render(tmp_path, total="101")


def test_container_is_digest_pinned_and_metrics_publish_only_on_loopback():
    script = (DEPLOY / "run-coturn.sh").read_text()
    assert "coturn/coturn@sha256:75e9ebd1e19005bec0c7f591d29afe22f959916ac8d9c852452f27db8c789828" in script
    assert "127.0.0.1:9641:9641/tcp" in script
    assert "--cap-drop=ALL" in script
    # coturn's image turnserver carries cap_net_bind_service=ep; cap-drop=ALL
    # strips it from the bounding set, so exec returns EPERM ("Operation not
    # permitted") under no-new-privileges even though the container's own ports
    # are >1024. Re-granting exactly that one capability lets the setcap'd
    # binary start while keeping every other capability dropped.
    assert "--cap-add=NET_BIND_SERVICE" in script
    assert "--security-opt no-new-privileges" in script
    assert '--user "65534:${TURN_RUNTIME_GID}"' in script
    assert "--read-only" in script
    assert "static-auth-secret" not in script
    assert '--publish "${TURN_PUBLIC_IP}:443:5349/tcp"' in script
    assert '--publish 443:5349/tcp' not in script
    assert "[::]" not in script


def test_service_uses_systemd_credentials_and_never_environment_for_secrets():
    unit = (DEPLOY / "autonomy-coturn.service").read_text()
    assert "LoadCredential=turn-rest-secrets:" in unit
    assert "LoadCredential=tls-key.pem:" in unit
    assert "Environment=TURN_REST" not in unit
    assert "--secrets %d/turn-rest-secrets" in unit
    assert "AssertPathExists=/etc/autonomy-coturn/turn-rest-secrets" in unit
    assert "ConditionPathExists" not in unit


def test_template_uses_options_accepted_by_pinned_coturn_release():
    config = (DEPLOY / "turnserver.conf.in").read_text()
    for removed_or_invalid in (
        "no-ipv6\n",
        "no-ipv6-relay",
        "no-tlsv1\n",
        "no-tlsv1_1",
        "no-sslv3",
        "no-loopback-peers",
    ):
        assert removed_or_invalid not in config
    assert "allocation-default-address-family=ipv4" in config
    assert "\ncli\n" not in config
    assert "\nno-cli\n" not in config
    assert "\nrfc5780\n" not in config
    assert "\nno-rfc5780\n" not in config
    assert "unauthorized-ratelimit\n" in config
    assert "cipher-list=ECDHE+AESGCM:ECDHE+CHACHA20:!aNULL:!MD5:!DSS" in config


def test_activation_checks_the_actual_metrics_path():
    script = (DEPLOY / "deploy.sh").read_text()
    assert "http://127.0.0.1:9641/metrics" in script
    assert "http://127.0.0.1:9641/ >/dev/null" not in script


def test_ipv4_only_contract_is_explicit_and_ipv6_requires_a_new_review():
    config = (DEPLOY / "turnserver.conf.in").read_text()
    run = (DEPLOY / "run-coturn.sh").read_text()
    readme = (DEPLOY / "README.md").read_text()
    assert "listening-ip=0.0.0.0" in config
    assert "allocation-default-address-family=ipv4" in config
    assert '--publish "${TURN_PUBLIC_IP}:' in run
    assert "--publish [::]" not in run
    assert "64:ff9b::/96" in readme


def test_shell_scripts_parse():
    for script in DEPLOY.glob("*.sh"):
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_certificate_issuance_enables_automatic_renewal():
    script = (DEPLOY / "issue-certificate.sh").read_text()
    assert "--http-01-address \"$TURN_PUBLIC_IP\"" in script
    assert "systemctl enable --now certbot.timer" in script
    assert "systemctl is-enabled --quiet certbot.timer" in script
