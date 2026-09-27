"""Organization OIDC configuration for published Services."""
from .registry import SettingSchema, field, home, publication_band, singleton, vaulted
from tools.network.service_auth import validate_provider

SERVICE_AUTH_SET_ID = "autonomy.network.service-auth"
SERVICE_AUTH_SECRET_SET_ID = "autonomy.network.service-auth-secret"

SYNOPSIS = {
    "summary": "Organization OIDC provider and overrideable default for published Services; client secret is org-vaulted.",
    "nouns": ["OIDC", "Okta", "service authentication", "identity provider"],
    "related_set_ids": ["autonomy.network.service-target#1", "autonomy.network.serve-zone#1"],
}


@home("organization")
@publication_band(max="raw")
@singleton(key="default")
class ServiceAuthV1(SettingSchema):
    set_id = SERVICE_AUTH_SET_ID
    schema_revision = 1

    provider: str = field(required=True, enum=["okta", "other"],
                          description="The OIDC provider family: okta, or other (any conforming issuer).")
    issuer: str = field(required=True, description="The provider's OIDC issuer URL (discovery is read from it).")
    client_id: str = field(required=True, description="The OIDC client id registered for this organization's Services.")
    default_access: str = field(required=True, enum=["public", "oidc"],
                                description="Access a newly published Service gets unless its target overrides it.")

    @classmethod
    def validate(cls, payload):
        super().validate(payload)
        validate_provider(payload["issuer"], payload["client_id"])


@home("organization")
@publication_band(max="raw")
@singleton(key="default")
@vaulted("audited")
class ServiceAuthSecretV1(SettingSchema):
    set_id = SERVICE_AUTH_SECRET_SET_ID
    schema_revision = 1

    client_secret: str = field(required=True, description="The OIDC client secret; held in the org vault (audited), never in a plaintext row.")
