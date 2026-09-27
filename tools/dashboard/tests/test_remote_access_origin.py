"""The origin every link uses (auto-w622e): read from the recorded row, seeded
once from the certificate on nodes onboarded before the step existed."""

from __future__ import annotations

import datetime as dt

import pytest

from tools.dashboard import remote_access as ra


def _tailnet_certificate(path, names):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
            .sign(key, hashes.SHA256()))
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def test_public_origin_is_the_recorded_row_or_none(monkeypatch):
    monkeypatch.setattr(ra, "current", lambda: {"mode": "local", "origin": "http://localhost:80"})
    assert ra.dashboard_public_origin() == "http://localhost:80"
    monkeypatch.setattr(ra, "current", lambda: None)
    assert ra.dashboard_public_origin() is None

    def boom():
        raise RuntimeError("store unreachable")
    monkeypatch.setattr(ra, "current", boom)
    assert ra.dashboard_public_origin() is None


def test_relay_origin_is_used_only_while_its_route_is_live(monkeypatch):
    """Reviewer: after a relay publish every link would otherwise point at an
    origin that is unavailable until the gate, the certificate and the
    advertisement exist. Until then links use the origin the publish was made
    from, or stay paths."""
    from tools.dashboard import web_gateway_supervisor

    rid = "11111111-1111-4111-8111-111111111111"
    relay = "https://dashboard.jeremy-0123456789abcdef0123.serve.auto.network"
    row = {"mode": "autonomy", "origin": relay, "reservation_id": rid, "local_origin": "http://localhost:80"}
    monkeypatch.setattr(ra, "current", lambda: dict(row))
    gateway = {"state": "healthy", "advertised_routes": [], "auth_helpers": []}
    monkeypatch.setattr(web_gateway_supervisor, "status", lambda: dict(gateway))
    assert ra.dashboard_public_origin() == "http://localhost:80"
    gateway["advertised_routes"] = [rid]
    assert ra.dashboard_public_origin() == "http://localhost:80"      # advertised, gate not up
    gateway["auth_helpers"] = ["dashboard-passkey"]
    assert ra.dashboard_public_origin() == relay
    row.pop("local_origin")
    gateway["auth_helpers"] = []
    assert ra.dashboard_public_origin() is None                        # paths until live

    def down():
        raise RuntimeError("gateway supervisor unavailable")
    monkeypatch.setattr(web_gateway_supervisor, "status", down)
    assert ra.dashboard_public_origin() is None


def test_seed_prefers_dashboard_domain_then_the_certificates_tailnet_name(tmp_path, monkeypatch):
    written = []
    monkeypatch.setattr(ra, "current", lambda: None)
    monkeypatch.setattr(ra, "record", lambda payload: written.append(payload) or payload)

    row = ra.seed_origin_from_certificate(environ={"DASHBOARD_DOMAIN": "Desktop.tail1234.ts.net."})
    assert row["mode"] == "tailscale" and row["origin"] == "https://desktop.tail1234.ts.net:8080"
    # The published TLS port, not a constant (reviewer).
    row = ra.seed_origin_from_certificate(environ={"DASHBOARD_DOMAIN": "desktop.tail1234.ts.net", "DASHBOARD_PORT": "8443"})
    assert row["origin"] == "https://desktop.tail1234.ts.net:8443"
    row = ra.seed_origin_from_certificate(environ={"DASHBOARD_DOMAIN": "desktop.tail1234.ts.net", "DASHBOARD_PORT": "443"})
    assert row["origin"] == "https://desktop.tail1234.ts.net"

    cert = tmp_path / "tls.crt"
    _tailnet_certificate(cert, ["localhost", "node.tailabcd.ts.net"])
    row = ra.seed_origin_from_certificate(environ={}, cert_path=str(cert))
    assert row["origin"] == "https://node.tailabcd.ts.net:8080"
    row = ra.seed_origin_from_certificate(environ={"AUTONOMY_TLS_CERT": str(cert)})
    assert row["origin"] == "https://node.tailabcd.ts.net:8080"
    assert len(written) == 5


def test_seed_does_nothing_without_a_tailnet_name_or_when_a_row_exists(tmp_path, monkeypatch):
    written = []
    monkeypatch.setattr(ra, "record", lambda payload: written.append(payload) or payload)
    monkeypatch.setattr(ra, "current", lambda: None)
    plain = tmp_path / "tls.crt"
    _tailnet_certificate(plain, ["localhost"])
    assert ra.seed_origin_from_certificate(environ={}, cert_path=str(plain)) is None
    assert ra.seed_origin_from_certificate(environ={"DASHBOARD_DOMAIN": "dash.example.com"}) is None
    assert ra.seed_origin_from_certificate(environ={}, cert_path=str(tmp_path / "absent.crt")) is None
    # An onboarded node keeps its recorded choice.
    monkeypatch.setattr(ra, "current", lambda: {"mode": "local", "origin": "http://localhost"})
    assert ra.seed_origin_from_certificate(environ={"DASHBOARD_DOMAIN": "dash.tail1234.ts.net"}) is None
    assert written == []


def test_mission_item_links_use_the_recorded_origin(monkeypatch):
    from tools.dashboard.plugins.mission.entrypoints import api as mission_api

    monkeypatch.setattr(ra, "current", lambda: {"mode": "tailscale", "origin": "https://desktop.tail1234.ts.net:8080"})
    link = mission_api._item_link("11111111-1111-4111-8111-111111111111", "core", "q-1")
    assert link.startswith("https://desktop.tail1234.ts.net:8080/mission/")
    monkeypatch.setattr(ra, "current", lambda: None)
    assert mission_api._item_link("11111111-1111-4111-8111-111111111111", "core", "q-1").startswith("/mission/")


def test_renew_script_names_no_machine_and_never_fails_silently(tmp_path):
    """The script carries no machine name, renews the certificate's own Tailnet
    name when DASHBOARD_DOMAIN is unset, and logs before any refusal (reviewer:
    a silent cron failure on the first of the month)."""
    import re
    import subprocess
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "renew-tls-cert.sh"
    text = script.read_text()
    assert not re.search(r"[a-z0-9-]+\.tail[0-9a-f]+\.ts\.net", text)     # no machine name
    assert "DASHBOARD_DOMAIN:?" not in text

    # The name comes from the existing certificate's SAN.
    cert = tmp_path / "tls.crt"
    _tailnet_certificate(cert, ["localhost", "Node.TailABCD.ts.net"])
    fn = re.search(r"^tailnet_name_from_cert\(\) \{.*?^\}$", text, re.M | re.S).group(0)
    got = subprocess.run(["bash", "-c", fn + f'\ntailnet_name_from_cert "{cert}"'],
                         capture_output=True, text=True, timeout=30)
    assert got.stdout.strip() == "node.tailabcd.ts.net", got.stderr

    # No name anywhere: the refusal lands in the log, and the run stops.
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True)
    run = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=30,
                         env={"PATH": "/usr/bin:/bin", "AUTONOMY_ROOT": str(root)})
    assert run.returncode == 2
    log = (root / "data" / "cert-renew.log").read_text()
    assert "no DASHBOARD_DOMAIN and no .ts.net name" in log
