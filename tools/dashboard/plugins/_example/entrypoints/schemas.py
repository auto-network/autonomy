"""Example organization-local Setting owned by the plugin starter."""
from __future__ import annotations

from tools.graph.schemas.registry import (
    SettingSchema,
    field,
    home,
    publication_band,
    singleton,
)


EXAMPLE_RECORD_SET_ID = "dashboard.plugin-example.record"
SCHEMA_REVISION = 1

SYNOPSIS = {
    "summary": (
        "A single organization-local record used only to demonstrate the "
        "dashboard plugin backend template."
    ),
    "nouns": ["dashboard plugin example", "plugin backend template"],
    "related_set_ids": [],
}


# Every field is one value shared by the organization. Nothing here is true
# only on one machine or belongs to one person's private settings.
@home("organization")
# This starter models organization-private application state. Remove or widen
# the band only when cross-organization read-through is an explicit feature.
@publication_band(max="raw")
@singleton(key="current")
class ExamplePluginRecordV1(SettingSchema):
    """The plugin starter's current example record. Key: ``current``."""

    set_id = EXAMPLE_RECORD_SET_ID
    schema_revision = SCHEMA_REVISION

    message: str = field(
        required=True,
        description="Short example message rendered by a plugin client.",
    )
    updated_at: str = field(
        required=True,
        description="ISO-8601 time at which the example record was written.",
    )
