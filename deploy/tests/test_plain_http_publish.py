"""The plain-HTTP first screen: serve.sh turns the listener on, compose publishes
it on localhost, and both installers pick its host port deterministically.

The listener itself is tested in tools/dashboard/tests/test_plain_listener.py;
this file pins the deployment wiring around it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = REPO_ROOT / "docker-compose.yml"
SERVE = REPO_ROOT / "deploy" / "serve.sh"
QUICKSTART = REPO_ROOT / "deploy" / "quickstart.sh"
INSTALL = REPO_ROOT / "deploy" / "install-published.sh"


def _compose_config(extra_env: dict[str, str] | None = None) -> dict:
    if not shutil.which("docker"):
        pytest.skip("docker compose CLI not available")
    env = dict(os.environ)
    env.setdefault("AUTONOMY_HOST_HOME", str(Path.home()))
    env.setdefault("AUTONOMY_SUBNET", "172.16.0.0/24")
    env.update(extra_env or {})
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config", "--format", "json"],
        capture_output=True, text=True, cwd=str(REPO_ROOT), env=env, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _plain_publish(config: dict) -> dict:
    ports = config["services"]["dashboard"]["ports"]
    plain = [p for p in ports if int(p["target"]) == 8081]
    assert len(plain) == 1, ports
    return plain[0]


def test_compose_publishes_the_plain_listener_on_localhost_port_80_by_default():
    assert "DASHBOARD_HTTP_PORT" not in os.environ
    publish = _plain_publish(_compose_config())
    assert str(publish["published"]) == "80"
    assert publish["host_ip"] == "127.0.0.1"
    assert publish["protocol"] == "tcp"


def test_compose_honours_the_recorded_http_port():
    publish = _plain_publish(_compose_config({"DASHBOARD_HTTP_PORT": "8088"}))
    assert str(publish["published"]) == "8088"
    assert publish["host_ip"] == "127.0.0.1"


def test_serve_sh_turns_the_listener_on_at_8081_and_keeps_the_tls_bind():
    text = SERVE.read_text()
    assert re.search(r'^export DASHBOARD_PLAIN_PORT="\$\{DASHBOARD_PLAIN_PORT:-8081\}"$', text, re.M)
    assert '--port "${DASHBOARD_PORT:-8080}"' in text
    assert "$SSL_ARGS" in text
    # The export precedes the exec that inherits it.
    assert text.index("export DASHBOARD_PLAIN_PORT") < text.index("exec python3 -m tools.dashboard.reload_with_notice")


# ── the installers' port probe ────────────────────────────────────────────

def _functions(script: Path, names: tuple[str, ...]) -> str:
    """The named top-level bash functions, extracted verbatim from the script."""
    text = script.read_text()
    out = []
    for name in names:
        match = re.search(rf"^{name}\(\) \{{.*?^\}}$|^{name}\(\) \{{[^\n]*\}}$", text, re.M | re.S)
        assert match, f"{name} not found in {script.name}"
        out.append(match.group(0))
    return "\n".join(out)


def _choose(script: Path, workdir: Path, requested: str, *candidates: int) -> tuple[int, str]:
    names = ("set_env", "port_is_free", "choose_http_port") if script is INSTALL else ("port_is_free", "choose_http_port")
    body = _functions(script, names) + f'\nchoose_http_port "{requested}" ' + " ".join(map(str, candidates)) + "\n"
    result = subprocess.run(["bash", "-euo", "pipefail", "-c", body], cwd=str(workdir),
                            capture_output=True, text=True, timeout=30)
    return result.returncode, result.stdout.strip()


def _busy_and_free_ports():
    holder = socket.socket()
    holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    return holder, holder.getsockname()[1], free


@pytest.mark.parametrize("script", [QUICKSTART, INSTALL], ids=["quickstart", "install-published"])
def test_probe_skips_a_busy_port_and_records_the_first_free_one_once(script, tmp_path):
    holder, busy, free = _busy_and_free_ports()
    env_file = tmp_path / ".env"
    env_file.write_text("DASHBOARD_PORT=8080\n")
    try:
        code, chosen = _choose(script, tmp_path, "", busy, free)
        assert (code, chosen) == (0, str(free))
        assert env_file.read_text() == f"DASHBOARD_PORT=8080\nDASHBOARD_HTTP_PORT={free}\n"

        # A second run keeps the recorded port even though the busy one is
        # now first and free would be chosen again: the record wins.
        holder.close()
        code, chosen = _choose(script, tmp_path, "", busy, free)
        assert (code, chosen) == (0, str(free))
        assert env_file.read_text().count("DASHBOARD_HTTP_PORT=") == 1
    finally:
        holder.close()


@pytest.mark.parametrize("script", [QUICKSTART, INSTALL], ids=["quickstart", "install-published"])
def test_probe_fails_when_every_candidate_is_busy(script, tmp_path):
    holder, busy, _free = _busy_and_free_ports()
    (tmp_path / ".env").write_text("")
    try:
        code, chosen = _choose(script, tmp_path, "", busy, busy)
        assert code == 1 and chosen == ""
        assert "DASHBOARD_HTTP_PORT" not in (tmp_path / ".env").read_text()
    finally:
        holder.close()


@pytest.mark.parametrize("script", [QUICKSTART, INSTALL], ids=["quickstart", "install-published"])
def test_an_explicit_http_port_is_recorded_without_probing(script, tmp_path):
    holder, busy, _free = _busy_and_free_ports()
    (tmp_path / ".env").write_text("DASHBOARD_HTTP_PORT=80\n")
    try:
        code, chosen = _choose(script, tmp_path, str(busy), busy)
        assert (code, chosen) == (0, str(busy))
        assert (tmp_path / ".env").read_text() == f"DASHBOARD_HTTP_PORT={busy}\n"
    finally:
        holder.close()


def test_installers_print_the_plain_url_as_the_first_screen():
    assert "http://localhost:${HTTP_PORT}" in QUICKSTART.read_text()
    assert 'open http://localhost:${HTTP_PORT}/' in INSTALL.read_text()
    for script in (QUICKSTART, INSTALL):
        assert "--http-port) HTTP_PORT=" in script.read_text()
