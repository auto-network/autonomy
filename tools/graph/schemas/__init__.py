"""Setting schema registry.

Every Setting carries a ``(set_id, schema_revision)`` contract. Concrete
contracts (``autonomy.org#1``, ``autonomy.workspace#1``, ...) register
themselves here at import time. This package owns *only* the registration
machinery — specific contracts ship with their own migration beads
(auto-S1..S4). See graph://0d3f750f-f9c.
"""

from .registry import (
    SchemaValidationError,
    SettingSchema,
    register_schema,
    register_upconverter,
    get_schema,
    schema_key,
    upconvert_chain,
    upconvert_payload,
    validate_payload,
    list_registered_set_ids,
)

# Concrete schema registrations. Importing for side effects — each module
# calls ``register_schema`` at import time.
from . import org  # noqa: F401
from . import workspace  # noqa: F401
from . import workspace_artifact  # noqa: F401 — autonomy.workspace.artifact#1

__all__ = [
    "SchemaValidationError",
    "SettingSchema",
    "register_schema",
    "register_upconverter",
    "get_schema",
    "schema_key",
    "upconvert_chain",
    "upconvert_payload",
    "validate_payload",
    "list_registered_set_ids",
]
