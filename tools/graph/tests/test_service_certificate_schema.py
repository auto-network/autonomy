import pytest

from tools.graph.schemas.registry import SchemaValidationError
from tools.graph.schemas.service_certificate import ServiceCertificateV1


def payload():
    apex = "persona-abc.serve.auto.network"
    return {
        "org": "anchore",
        "persona_label": "persona-abc",
        "apex": apex,
        "sans": [f"*.{apex}", apex],
        "not_before": 100,
        "not_after": 200,
        "serial": "abc123",
        "vault_key": "service.tls.anchore.persona-abc",
        "staging": False,
        "activated_at": 110,
    }


def test_service_certificate_metadata_accepts_exact_identity():
    ServiceCertificateV1.validate_member_key("anchore:persona-abc")
    ServiceCertificateV1.validate(payload())


def test_service_certificate_metadata_rejects_wrong_sans():
    value = payload()
    value["sans"] = [value["apex"]]
    with pytest.raises(SchemaValidationError, match="SANs"):
        ServiceCertificateV1.validate(value)
