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


def test_seed_prefers_dashboard_domain_then_the_certificates_tailnet_name(tmp_path, monkeypatch):
    written = []
    monkeypatch.setattr(ra, "current", lambda: None)
    monkeypatch.setattr(ra, "record", lambda payload: written.append(payload) or payload)

    row = ra.seed_origin_from_certificate(environ={"DASHBOARD_DOMAIN": "Desktop.tail1234.ts.net."})
    assert row["mode"] == "tailscale" and row["origin"] == "https://desktop.tail1234.ts.net:8080"

    cert = tmp_path / "tls.crt"
    _tailnet_certificate(cert, ["localhost", "node.tailabcd.ts.net"])
    row = ra.seed_origin_from_certificate(environ={}, cert_path=str(cert))
    assert row["origin"] == "https://node.tailabcd.ts.net:8080"
    row = ra.seed_origin_from_certificate(environ={"AUTONOMY_TLS_CERT": str(cert)})
    assert row["origin"] == "https://node.tailabcd.ts.net:8080"
    assert len(written) == 3


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

    monkeypatch.setattr(ra, "current", lambda: {"mode": "autonomy",
                                               "origin": "https://dashboard.jeremy-0123456789abcdef0123.serve.auto.network"})
    link = mission_api._item_link("11111111-1111-4111-8111-111111111111", "core", "q-1")
    assert link.startswith("https://dashboard.jeremy-0123456789abcdef0123.serve.auto.network/mission/")
    monkeypatch.setattr(ra, "current", lambda: None)
    assert mission_api._item_link("11111111-1111-4111-8111-111111111111", "core", "q-1").startswith("/mission/")


def test_renew_script_names_no_machine():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "renew-tls-cert.sh").read_text()
    assert ".ts.net" not in text.replace("this node's Tailnet name", "")
    assert 'DASHBOARD_DOMAIN:?' in text
