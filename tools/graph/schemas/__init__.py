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
    flush_schema_meta,
    cache_expires_at,
    SCHEMA_META_SET_ID,
    SCHEMA_META_REVISION,
    SYNOPSIS_META_SET_ID,
    SYNOPSIS_META_REVISION,
    # Authoring API: schemas declare their fields and access pattern
    # using these helpers / decorators.
    field,
    append_only_log,
    singleton,
    keyed_per_entity,
    cache,
    # Mediator-action marker decorators: declare a default action or a
    # per-``kind`` action method directly on the schema class. Discovery
    # in ``SettingSchema.__init_subclass__`` registers each via the
    # settings-mediator substrate.
    action,
    on_kind,
)

# Concrete schema registrations. Importing for side effects — each module
# calls ``register_schema`` at import time.
from . import org  # noqa: F401
from . import workspace  # noqa: F401
from . import workspace_artifact  # noqa: F401 — autonomy.workspace.artifact#1
from . import artifact_path  # noqa: F401 — autonomy.artifact-path#1
from . import org_peer_subscription  # noqa: F401 — autonomy.org.peer-subscription#1
from . import mount  # noqa: F401 — autonomy.workspace.mount#1
from . import agent_actions  # noqa: F401 — dashboard.agent-actions#1
from . import capability_contract  # noqa: F401 — autonomy.capability.contract#1
from . import capability_impl  # noqa: F401 — autonomy.capability.impl#1
from . import org_capability_install  # noqa: F401 — autonomy.org.capability.install#1
from . import workspace_capability_enable  # noqa: F401 — autonomy.workspace.capability.enable#1
from . import worktree_review_binding  # noqa: F401 — autonomy.worktree.review_binding#1
from . import source_control_review_state  # noqa: F401 — autonomy.source_control.review_state#1
from . import worktree_watch  # noqa: F401 — dashboard.worktree.watch#1
from . import worktree_terminal_fire  # noqa: F401 — dashboard.worktree.terminal_fire#1
from . import turn_correction  # noqa: F401 — autonomy.workspace.turn_correction#1
from . import claude_credentials  # noqa: F401 — dashboard.claude.credentials#1
from . import claude_setup_tokens  # noqa: F401 — dashboard.claude.setup_tokens#1
from . import feature_flags  # noqa: F401 — dashboard.feature_flags#1

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
    "flush_schema_meta",
    "cache_expires_at",
    "SCHEMA_META_SET_ID",
    "SCHEMA_META_REVISION",
    "SYNOPSIS_META_SET_ID",
    "SYNOPSIS_META_REVISION",
    "field",
    "append_only_log",
    "singleton",
    "keyed_per_entity",
    "cache",
    "action",
    "on_kind",
]
