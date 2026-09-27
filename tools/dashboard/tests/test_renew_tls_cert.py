"""tools/dashboard/renew-tls-cert.sh on both node kinds (auto-1ei8m).

The host has tailscale; on a Compose node the certificate lives in the
dashboard container's data volume, not in the host checkout. Exercised with
stand-ins for docker, tailscale and start-dashboard.sh on PATH.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "renew-tls-cert.sh"
SAN = "DNS:localhost, DNS:Desktop.tail1234.ts.net"


def _exe(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


def _fake_bin(tmp_path: Path, *, container: str | None, san: str = SAN, cert_ok: bool = True) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    containers = "\n".join(container) if isinstance(container, list) else (container or "")
    _exe(bindir / "docker",
         'echo "docker $*" >>"$T_LOG"\n'
         f'if [[ "$1 $2" == "ps -q" ]]; then printf "%s\\n" "{containers}" | grep -v "^$"; exit 0; fi\n'
         'if [[ "$*" == *"openssl x509"* ]]; then echo "X509v3 Subject Alternative Name:"; '
         f'echo "    {san}"; exit 0; fi\n'
         'if [[ "$1" == exec && "$*" == *"cat >> /app/data/cert-renew.log"* ]]; then cat >>"$T_VOLUME_LOG"; exit 0; fi\n'
         'if [[ "$1" == exec && "$*" == *"cat > /app/data/tls.key.new"* ]]; then cat >"$T_VOLUME_DIR/tls.key.new"; exit 0; fi\n'
         'if [[ "$1" == exec && "$*" == *"cat > /app/data/tls.crt.new"* ]]; then cat >"$T_VOLUME_DIR/tls.crt.new"; exit 0; fi\n'
         'if [[ "$1" == exec && "$*" == *"mv /app/data/tls.key.new"* ]]; then mv "$T_VOLUME_DIR/tls.key.new" "$T_VOLUME_DIR/tls.key" && mv "$T_VOLUME_DIR/tls.crt.new" "$T_VOLUME_DIR/tls.crt"; exit 0; fi\n'
         'exit 0\n')
    _exe(bindir / "tailscale",
         'echo "tailscale $*" >>"$T_LOG"\n'
         + ('' if cert_ok else 'exit 7\n')
         + 'while [[ $# -gt 0 ]]; do case "$1" in --cert-file) echo "NEWCERT" >"$2"; shift 2;; '
           '--key-file) echo "NEWKEY" >"$2"; shift 2;; *) shift;; esac; done\nexit 0\n')
    _exe(bindir / "openssl",
         'echo "openssl $*" >>"$T_LOG"\n'
         'if [[ "$*" == *-pubkey* || "$*" == *-pubout* ]]; then f=""; while [[ $# -gt 0 ]]; do [[ "$1" == -in ]] && f="$2"; shift; done; '
         'echo "PUBKEY-$(head -c 3 "$f" 2>/dev/null)"; exit 0; fi\n'
         f'echo "X509v3 Subject Alternative Name:"; echo "    {san}"\n')
    return bindir


def _run(tmp_path: Path, bindir: Path, root: Path, env_extra: dict | None = None):
    (tmp_path / "volume").mkdir(exist_ok=True)
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "AUTONOMY_ROOT": str(root),
           "TAILSCALE_BIN": str(bindir / "tailscale"),
           "T_LOG": str(tmp_path / "calls.log"), "T_VOLUME_LOG": str(tmp_path / "volume-cert-renew.log"),
           "T_VOLUME_DIR": str(tmp_path / "volume"),
           **(env_extra or {})}
    return subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, timeout=60, env=env)


def test_compose_node_issues_into_the_container_and_restarts_it(tmp_path):
    root = tmp_path / "code"
    (root / "data").mkdir(parents=True)                      # exists, holds no tls.crt (Home)
    bindir = _fake_bin(tmp_path, container="abc123")
    result = _run(tmp_path, bindir, root)
    assert result.returncode == 0, result.stderr + (root / "data" / "cert-renew.log").read_text()
    calls = (tmp_path / "calls.log").read_text()
    assert "tailscale cert --cert-file" in calls and " desktop.tail1234.ts.net\n" in calls
    # Streamed as the autonomy user, key private from the first byte, key then cert; no root, no cp, no restart.
    assert "docker exec -i -u autonomy abc123 sh -c umask 077; cat > /app/data/tls.key.new" in calls
    assert "docker exec -i -u autonomy abc123 sh -c umask 022; cat > /app/data/tls.crt.new" in calls
    assert "mv /app/data/tls.key.new /app/data/tls.key && mv /app/data/tls.crt.new /app/data/tls.crt" in calls
    assert "docker cp" not in calls and "-u root" not in calls and "docker restart" not in calls
    assert (tmp_path / "volume" / "tls.key").read_text().strip() == "NEWKEY"
    assert (tmp_path / "volume" / "tls.crt").read_text().strip() == "NEWCERT"
    volume_log = (tmp_path / "volume-cert-renew.log").read_text()
    assert "renewing TLS cert for desktop.tail1234.ts.net" in volume_log
    assert "hands off" in volume_log and "renewal OK" in volume_log
    assert "start-dashboard.sh" not in calls


def test_host_process_node_issues_into_the_checkout_and_restarts_the_dashboard(tmp_path):
    root = tmp_path / "code"
    (root / "data").mkdir(parents=True)
    (root / "data" / "tls.crt").write_text("OLD")
    (root / "tools" / "dashboard").mkdir(parents=True)
    _exe(root / "tools" / "dashboard" / "start-dashboard.sh", 'echo "start-dashboard $*" >>"$T_LOG"\n')
    bindir = _fake_bin(tmp_path, container=None)
    result = _run(tmp_path, bindir, root)
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls.log").read_text()
    assert f"--cert-file {root}/data/tls.crt --key-file {root}/data/tls.key desktop" in calls
    assert (root / "data" / "tls.crt").read_text().strip() == "NEWCERT"
    assert "start-dashboard --restart" in calls and "docker cp" not in calls
    assert "renewal OK" in (root / "data" / "cert-renew.log").read_text()


def test_no_certificate_anywhere_is_logged_and_refused(tmp_path):
    root = tmp_path / "code"
    (root / "data").mkdir(parents=True)
    bindir = _fake_bin(tmp_path, container=None)
    result = _run(tmp_path, bindir, root)
    assert result.returncode == 2
    assert "no certificate found" in (root / "data" / "cert-renew.log").read_text()


def test_a_failed_issuance_leaves_the_container_untouched(tmp_path):
    root = tmp_path / "code"
    (root / "data").mkdir(parents=True)
    bindir = _fake_bin(tmp_path, container="abc123", cert_ok=False)
    result = _run(tmp_path, bindir, root)
    assert result.returncode == 7
    calls = (tmp_path / "calls.log").read_text()
    assert "tls.key.new" not in calls and "docker restart" not in calls
    assert "tailscale cert FAILED" in (tmp_path / "volume-cert-renew.log").read_text()


def test_two_dashboard_containers_are_a_refusal_unless_pinned(tmp_path):
    root = tmp_path / "code"
    (root / "data").mkdir(parents=True)
    bindir = _fake_bin(tmp_path, container=["abc123", "def456"])
    result = _run(tmp_path, bindir, root)
    assert result.returncode == 2
    assert "2 running dashboard containers" in (root / "data" / "cert-renew.log").read_text()
    pinned = _run(tmp_path, bindir, root, {"AUTONOMY_DASHBOARD_CONTAINER": "def456"})
    assert pinned.returncode == 0, pinned.stderr
    assert "docker exec -i -u autonomy def456" in (tmp_path / "calls.log").read_text()


def test_a_mismatched_pair_is_never_installed(tmp_path):
    root = tmp_path / "code"
    (root / "data").mkdir(parents=True)
    bindir = _fake_bin(tmp_path, container="abc123")
    # The fake tailscale writes NEWCERT/NEWKEY; the fake openssl derives the
    # public key from the first bytes, so a key that does not match fails.
    _exe(bindir / "tailscale",
         'echo "tailscale $*" >>"$T_LOG"\n'
         'while [[ $# -gt 0 ]]; do case "$1" in --cert-file) echo "NEWCERT" >"$2"; shift 2;; '
         '--key-file) echo "OTHERKEY" >"$2"; shift 2;; *) shift;; esac; done\nexit 0\n')
    result = _run(tmp_path, bindir, root)
    assert result.returncode == 3
    assert "do not match" in (tmp_path / "volume-cert-renew.log").read_text()
    assert "tls.key.new" not in (tmp_path / "calls.log").read_text()


def test_dashboard_domain_overrides_the_certificates_name(tmp_path):
    root = tmp_path / "code"
    (root / "data").mkdir(parents=True)
    bindir = _fake_bin(tmp_path, container="abc123")
    result = _run(tmp_path, bindir, root, {"DASHBOARD_DOMAIN": "other.tail9999.ts.net"})
    assert result.returncode == 0, result.stderr
    assert " other.tail9999.ts.net\n" in (tmp_path / "calls.log").read_text()
