"""The registry edge is reproduced from one complete authored Caddyfile."""

from __future__ import annotations

from pathlib import Path
import re


ESTATE = Path(__file__).resolve().parents[1]
CADDYFILE = ESTATE / "caddy" / "registry-ash-1.Caddyfile"
DEPLOY = ESTATE / "deploy-caddy.sh"


def test_complete_caddyfile_has_exact_current_routes():
    text = CADDYFILE.read_text(encoding="utf-8")
    blocks = re.findall(r"(?m)^([^#\s][^\s{]*) \{\n([^}]*)\}", text)
    assert [address for address, _ in blocks] == [
        "registry.auto.network",
        ":80",
        "relay.auto.network",
        "auto.network",
    ]
    assert all(body.strip() == "reverse_proxy 127.0.0.1:8477" for _, body in blocks)
    assert "encode " not in text
    assert "base-url" not in text


def test_deploy_validates_before_atomic_install_and_reload():
    script = DEPLOY.read_text(encoding="utf-8")
    validate = script.index('caddy validate --config "$REMOTE_TMP"')
    install = script.index('install -m 0644')
    replace = script.index('mv "${DEST}.new" "$DEST"')
    reload_ = script.index("systemctl reload caddy")
    assert validate < install < replace < reload_
    assert "cmp -s" in script
    assert 'diff -u "$DEST" "$REMOTE_TMP"' in script
    assert "cp -a \"$backup\" \"$DEST\"" in script
    assert "registry-ash-1.Caddyfile.captured" not in script


def test_one_time_apex_append_path_is_gone():
    assert not (ESTATE / "deploy-apex-vhost.sh").exists()
    assert not (ESTATE / "caddy" / "auto.network.caddy").exists()
