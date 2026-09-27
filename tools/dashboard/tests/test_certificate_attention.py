"""The served TLS certificate: facts, the expiry attention item, its
destination, the Machines card field and the supervisor's load guard
(auto-1ei8m part 2)."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from tools.dashboard import certificate_attention as ca
from tools.dashboard import tls_certificate


def _pem_pair(tmp_path, names, *, days):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "tls.crt", tmp_path / "tls.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


def test_facts_come_from_the_served_certificate(tmp_path):
    cert, _ = _pem_pair(tmp_path, ["localhost", "Node.TailABCD.ts.net"], days=60)
    facts = tls_certificate.read_certificate(cert)
    assert facts.names == ("localhost", "node.tailabcd.ts.net")
    assert facts.tailnet_name == "node.tailabcd.ts.net"
    assert 59 < facts.days_remaining() <= 60
    assert tls_certificate.read_certificate(tmp_path / "absent.crt") is None
    assert tls_certificate.certificate_path({"AUTONOMY_TLS_CERT": "/x/tls.crt"}) == tls_certificate.Path("/x/tls.crt")
    assert tls_certificate.certificate_path({"AUTONOMY_DATA_ROOT": "/app/data"}) == tls_certificate.Path("/app/data/tls.crt")


@pytest.mark.parametrize("days, state", [(60, None), (21, "expiring"), (5, "expiring"), (-1, "expired")])
def test_expiry_condition_warns_under_21_days(tmp_path, days, state):
    cert, _ = _pem_pair(tmp_path, ["node.tailabcd.ts.net"], days=days)
    condition = tls_certificate.expiry_condition(tls_certificate.read_certificate(cert))
    if state is None:
        assert condition is None
    else:
        assert condition["state"] == state and condition["names"] == ["node.tailabcd.ts.net"]


def test_derive_condition_is_versioned_by_not_after(tmp_path):
    cert, _ = _pem_pair(tmp_path, ["node.tailabcd.ts.net"], days=10)
    facts = tls_certificate.read_certificate(cert)
    row = ca.derive_condition(facts, machine_id="ab" * 32)
    assert row["kind"] == ca.KIND and row["attention_state"] == "needs_attention"
    assert row["attention_id"] == f"machine:{'ab' * 32}:tls-certificate" and row["object_ref"] == "ab" * 32
    assert row["safe_title"].startswith("Dashboard certificate expires in ")
    assert "renew-tls-cert.sh" in row["safe_summary"]
    assert row["source_version"] == int(facts.not_after.timestamp())
    fresh, _ = _pem_pair(tmp_path / "fresh", ["node.tailabcd.ts.net"], days=80) if (tmp_path / "fresh").mkdir() is None else (None, None)
    resolved = ca.derive_condition(tls_certificate.read_certificate(fresh), machine_id="ab" * 32)
    assert resolved["attention_state"] == "resolved"
    assert ca.derive_condition(None, machine_id="ab" * 32)["attention_state"] == "resolved"


class _FakeIndex:
    def __init__(self):
        self.items = {}
        self.published = []
        self.store = SimpleNamespace(get_item=lambda attention_id: self.items.get(attention_id))
        self.registry = SimpleNamespace(producer=lambda kind, scope: (kind, scope))

    def publish(self, producer, condition):
        self.published.append((producer, dict(condition)))
        self.items[condition["attention_id"]] = SimpleNamespace(payload=dict(condition))


def test_publish_opens_once_and_resolves_only_an_open_item():
    index = _FakeIndex()
    warn = {"kind": ca.KIND, "attention_id": "machine:m:tls-certificate", "object_ref": "m",
            "attention_state": "needs_attention", "safe_title": "t", "safe_summary": "s",
            "occurred_at": 1.0, "source_version": 100}
    assert ca.publish_condition(index, dict(warn)) == "published"
    assert ca.publish_condition(index, dict(warn)) == "skipped"          # same version, already open
    assert index.published[0][0] == (ca.KIND, "machine")
    resolved = {**warn, "attention_state": "resolved", "safe_summary": None, "source_version": 90}
    assert ca.publish_condition(index, dict(resolved)) == "published"
    assert index.published[-1][1]["source_version"] == 101              # newer than the open item
    assert ca.publish_condition(index, dict(resolved)) == "skipped"      # nothing open any more


def test_machine_items_open_the_machines_page_focused_on_the_machine():
    from tools.dashboard.attention_registry import destination_route

    assert destination_route("fleet.machine.v1", "fleet.machine", "ab" * 32) == f"/fleet?focus={'ab' * 32}"


def test_machines_card_carries_the_served_certificate(tmp_path, monkeypatch):
    from tools.dashboard.plugins.fleet.entrypoints import projection

    cert, _ = _pem_pair(tmp_path, ["node.tailabcd.ts.net"], days=10)
    original = tls_certificate.read_certificate
    monkeypatch.setattr(tls_certificate, "read_certificate", lambda *a, **k: original(cert))
    row = projection._dashboard_certificate()
    assert row["expiring"] is True and 9 < row["daysRemaining"] <= 10 and isinstance(row["notAfter"], int)
    monkeypatch.setattr(tls_certificate, "read_certificate", lambda *a, **k: None)
    assert projection._dashboard_certificate() is None


def test_supervisor_waits_for_a_pair_that_loads(tmp_path, monkeypatch):
    """A half-written or mismatched pair must not spawn a worker that dies on
    its SSL load (reviewer); the hand-off waits for the next tick."""
    from tools.dashboard import reload_with_notice as rwn

    cert, key = _pem_pair(tmp_path, ["node.tailabcd.ts.net"], days=30)
    sup = SimpleNamespace(config=SimpleNamespace(ssl_certfile=str(cert), ssl_keyfile=str(key)))
    sup._tls_pair = rwn._tls_pair_signature(sup.config)
    assert rwn._tls_pair_changed(sup) is False
    # A new key alone (the certificate not yet replaced): does not load, no hand-off.
    _other_cert, other_key = _pem_pair(tmp_path / "other", ["node.tailabcd.ts.net"], days=30) if (tmp_path / "other").mkdir() is None else (None, None)
    key.write_bytes(other_key.read_bytes())
    assert rwn._tls_pair_changed(sup) is False
    # The matching certificate lands: the pair loads, hand off.
    cert.write_bytes(_other_cert.read_bytes())
    assert rwn._tls_pair_changed(sup) is True
    assert rwn._tls_pair_changed(sup) is False       # and only once
