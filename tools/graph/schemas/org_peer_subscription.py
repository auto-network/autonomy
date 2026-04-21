"""Schema: ``autonomy.org.peer-subscription#1``.

Personal Setting declaring which peer orgs the caller subscribes to for
cross-org reads (search, Settings reads, published surface). Lives in
``personal.db`` — peer subscription is per-operator, not shared.

Key: the caller org slug (``<org>``). Payload carries a ``peers`` list of
peer-org slugs whose public surface the caller sees. An empty list means
"fully isolated"; an **omitted** Setting means "subscribe to every peer"
— the default. Consumers MUST distinguish absence from empty list.

Spec: graph://0d3f750f-f9c (Setting Primitive), graph://bcce359d-a1d
(Cross-Org Search Architecture).
"""

from __future__ import annotations

from .registry import (
    SchemaValidationError,
    SettingSchema,
    register_schema,
)


SET_ID = "autonomy.org.peer-subscription"
SCHEMA_REVISION = 1


class OrgPeerSubscriptionV1(SettingSchema):
    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    @classmethod
    def validate(cls, payload: dict) -> None:
        super().validate(payload)

        extra = set(payload) - {"peers"}
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown fields {sorted(extra)!r}; "
                f"the caller org slug lives in the Setting key"
            )

        if "peers" not in payload:
            raise SchemaValidationError(
                f"{cls.__name__}: 'peers' is required (use an empty list "
                f"for full isolation; omit the Setting entirely for the "
                f"default 'subscribe to all peers')"
            )
        peers = payload["peers"]
        if not isinstance(peers, list):
            raise SchemaValidationError(
                f"{cls.__name__}: 'peers' must be a list, got "
                f"{type(peers).__name__}"
            )
        for i, p in enumerate(peers):
            if not isinstance(p, str) or not p:
                raise SchemaValidationError(
                    f"{cls.__name__}: peers[{i}] must be a non-empty string"
                )


register_schema(SET_ID, SCHEMA_REVISION, OrgPeerSubscriptionV1)
