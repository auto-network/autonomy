"""The registry edge is reproduced from one authored Caddyfile shared by
every estate box — the single per-host value is the bind IP, supplied as
$AUTONOMY_BIND_IP by the caddy unit's environment (no per-host file)."""

from __future__ import annotations

from pathlib import Path
import re


ESTATE = Path(__file__).resolve().parents[1]
CADDYFILE = ESTATE / "caddy" / "estate.Caddyfile"
DEPLOY = ESTATE / "deploy-caddy.sh"
BIND = "{$AUTONOMY_BIND_IP}"


def test_one_authored_file_no_per_host_file():
    assert CADDYFILE.exists()
    assert not (ESTATE / "caddy" / "registry-ash-1.Caddyfile").exists()


def test_complete_caddyfile_has_exact_current_routes():
    text = CADDYFILE.read_text(encoding="utf-8")
    blocks = re.findall(r"(?m)^([^#\s][^\s{]*) \{\n([^}]*)\}", text)
    assert [address for address, _ in blocks] == [
        "registry.auto.network",
        f"{BIND}:80",
        "relay.auto.network",
        "auto.network",
    ]
    for _address, body in blocks:
        directives = [line.strip() for line in body.splitlines() if line.strip()]
        assert directives[-1] == "reverse_proxy 127.0.0.1:8477"
    assert "encode " not in text
    assert "base-url" not in text


def test_no_host_specific_ip_only_the_bind_placeholder():
    text = CADDYFILE.read_text(encoding="utf-8")
    # The only per-host value is the placeholder, in exactly two places —
    # the global default_bind and the raw-IP vhost. No box's public IP is
    # baked in; the sole routable literal is the loopback registry target.
    assert "5.161.219.195" not in text
    routable = [ip for ip in re.findall(r"\d+\.\d+\.\d+\.\d+", text)
                if ip not in ("0.0.0.0",)]  # 0.0.0.0 only names a fallback
    assert set(routable) == {"127.0.0.1"}
    assert text.count(BIND) == 2


def test_global_default_bind_pins_listeners_via_placeholder():
    text = CADDYFILE.read_text(encoding="utf-8")
    assert re.search(
        r"(?m)^\{\n\s*default_bind \{\$AUTONOMY_BIND_IP\}\n\}", text)
    # default_bind covers every vhost; no redundant per-vhost bind lines.
    assert "\n\tbind " not in text


def test_deploy_passes_bind_ip_and_validates_before_install():
    script = DEPLOY.read_text(encoding="utf-8")
    assert "AUTONOMY_BIND_IP" in script
    # The caddy unit must carry the per-box IP so runtime + reload resolve it.
    assert "caddy.service.d" in script
    validate = script.index('caddy validate --config "$REMOTE_TMP"')
    install = script.index('install -m 0644')
    replace = script.index('mv "${DEST}.new" "$DEST"')
    # The main-path reload is the LAST one (an earlier reload lives in the
    # already-current branch, applying the bind env without a file change).
    reload_ = script.rindex("systemctl reload caddy")
    assert validate < install < replace < reload_
    # Validation runs with the same env caddy loads, so an unresolved
    # placeholder fails at validate, not at reload.
    assert 'AUTONOMY_BIND_IP="$BIND_IP" caddy validate' in script
    assert "cmp -s" in script
    assert 'diff -u "$DEST" "$REMOTE_TMP"' in script


def test_one_time_apex_append_path_is_gone():
    assert not (ESTATE / "deploy-apex-vhost.sh").exists()
    assert not (ESTATE / "caddy" / "auto.network.caddy").exists()
