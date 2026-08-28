"""Setting schema registry.

Every Setting carries a ``(set_id, schema_revision)`` contract. Concrete
contracts (``autonomy.org#1``, ``autonomy.workspace#1``, ...) register
themselves here at import time. This package owns *only* the registration
machinery — specific contracts ship with their own migration beads
(auto-S1..S4). See graph://0d3f750f-f9c.
"""

from .registry import (
    SchemaValidationError,
    RemediationRef,
    REMEDIATION_ID_PATTERN,
    normalize_remediation_ref,
    SettingSchema,
    register_schema,
    register_upconverter,
    get_schema,
    schema_key,
    upconvert_chain,
    upconvert_payload,
    validate_key,
    validate_payload,
    list_registered_set_ids,
    flush_schema_meta,
    flush_schema_meta_machine_store,
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
    org_writeback_namespace,
    cache,
    # Which database a Setting lives in. Orthogonal to cardinality above:
    # how many rows there are, and whose database they are in, are
    # different questions and neither implies the other.
    home,
    declared_home,
    declared_org_writeback_key_strategy,
    derive_org_writeback_key,
    readiness_gate,
    readiness_gated_by,
    publication_band,
    declared_band,
    states_allowed,
    PUBLICATION_ORDER,
    VALID_HOMES,
    # Whether this set's payloads are secrets stored as encrypted storage
    # objects, and who must participate to read one back.
    signer,
    signer_declaration,
    vaulted,
    declared_signer,
    declared_vault_tier,
    VALID_VAULT_TIERS,
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
from . import org_member_profile  # noqa: F401 — autonomy.org.member-profile#1
from . import workspace  # noqa: F401
from . import workspace_artifact  # noqa: F401 — autonomy.workspace.artifact#1
from . import dispatch_limits  # noqa: F401 — autonomy.dispatch.limits#1
from . import vault_release_lease  # noqa: F401 — autonomy.vault.release-lease#1
from . import workspace_provision  # noqa: F401 — autonomy.workspace.provision#1
from . import workspace_image_build  # noqa: F401 — autonomy.workspace.image-build#1
from . import artifact_path  # noqa: F401 — autonomy.artifact-path#1
from . import org_peer_subscription  # noqa: F401 — autonomy.org.peer-subscription#1
from . import mount  # noqa: F401 — autonomy.workspace.mount#1
from . import agent_actions  # noqa: F401 — dashboard.agent-actions#1
from . import capability_contract  # noqa: F401 — autonomy.capability.contract#1
from . import capability_impl  # noqa: F401 — autonomy.capability.impl#1
from . import host_install_state  # noqa: F401 — dashboard.capability.host_install_state#1
from . import org_capability_install  # noqa: F401 — autonomy.org.capability.install#1
from . import workspace_capability_enable  # noqa: F401 — autonomy.workspace.capability.enable#1
from . import commit_policy  # noqa: F401 — autonomy.commit.policy#1 + operation_policy#1
from . import network_identity  # noqa: F401 — autonomy.network.{org-key,binding,link-grant}#1
from . import link_approval  # noqa: F401 — autonomy.network.link-approval-{intent,result}#1
from . import dashboard_auth  # noqa: F401 — autonomy.identity.dashboard-auth#1
from . import personal_identity  # noqa: F401 — autonomy.identity.{personal,passkey}#1
from . import client_error  # noqa: F401 — autonomy.identity.client-error#1
from . import network_ledger  # noqa: F401 — autonomy.network.{ledger-state,ledger-projection}#1
from . import worktree_review_binding  # noqa: F401 — autonomy.worktree.review_binding#1
from . import source_control_review_state  # noqa: F401 — autonomy.source_control.review_state#1
from . import worktree_watch  # noqa: F401 — dashboard.worktree.watch#1
from . import worktree_terminal_fire  # noqa: F401 — dashboard.worktree.terminal_fire#1
from . import turn_correction  # noqa: F401 — autonomy.workspace.turn_correction#1
from . import org_primer  # noqa: F401 — autonomy.org.primer#1
from . import workspace_primer  # noqa: F401 — autonomy.workspace.primer#1
from . import org_capability_primer  # noqa: F401 — autonomy.org.capability.primer#1
from . import claude_credentials  # noqa: F401 — dashboard.claude.credentials#1
from . import codex_credentials  # noqa: F401 — dashboard.codex.credentials#1
from . import claude_setup_tokens  # noqa: F401 — dashboard.claude.setup_tokens#1
from . import feature_flags  # noqa: F401 — dashboard.feature_flags#1
from . import bootstrap_allowlist  # noqa: F401 — autonomy.org.bootstrap-allowlist#1
from . import harness_bootstrap  # noqa: F401 — autonomy.harness.bootstrap#1
from . import vault_policy_class  # noqa: F401 — autonomy.vault.policy-class#1
from . import vault_credential  # noqa: F401 — autonomy.vault.audited#1 + .secured#1
from . import secure_setting  # noqa: F401 — autonomy.secure.setting#1
from . import commit_signing_key  # noqa: F401 — autonomy.commit.signing-key#1
from . import credential_file  # noqa: F401 — autonomy.credential-file#1
from . import fleet_roster  # noqa: F401 — autonomy.fleet.roster#2
from . import machine_identity  # noqa: F401 — autonomy.machine.identity#1
from . import fleet_joining  # noqa: F401 — autonomy.machine.fleet-joining#1
from . import fleet_tunnel_server  # noqa: F401 — autonomy.fleet.tunnel-server#1
from . import fleet_machine_profile  # noqa: F401 — autonomy.fleet.machine-profile#1
from . import fleet_route  # noqa: F401 — autonomy.machine.fleet-route#1
from . import fleet_sync_telemetry  # noqa: F401 — autonomy.machine.fleet-sync-telemetry#1
from . import agent_test_capacity  # noqa: F401 — dashboard.agent-test.{capacity,lease}#1
from . import dashboard_shell  # noqa: F401 — dashboard.shell.default-org#1
from . import central_attention  # noqa: F401 — dashboard.{attention,approval}.*#1

__all__ = [
    "SchemaValidationError",
    "RemediationRef",
    "REMEDIATION_ID_PATTERN",
    "normalize_remediation_ref",
    "home",
    "declared_home",
    "declared_org_writeback_key_strategy",
    "derive_org_writeback_key",
    "readiness_gate",
    "readiness_gated_by",
    "publication_band",
    "declared_band",
    "states_allowed",
    "PUBLICATION_ORDER",
    "VALID_HOMES",
    "signer",
    "signer_declaration",
    "vaulted",
    "declared_signer",
    "declared_vault_tier",
    "VALID_VAULT_TIERS",
    "SettingSchema",
    "register_schema",
    "register_upconverter",
    "get_schema",
    "schema_key",
    "upconvert_chain",
    "upconvert_payload",
    "validate_key",
    "validate_payload",
    "list_registered_set_ids",
    "flush_schema_meta",
    "flush_schema_meta_machine_store",
    "cache_expires_at",
    "SCHEMA_META_SET_ID",
    "SCHEMA_META_REVISION",
    "SYNOPSIS_META_SET_ID",
    "SYNOPSIS_META_REVISION",
    "field",
    "append_only_log",
    "singleton",
    "keyed_per_entity",
    "org_writeback_namespace",
    "cache",
    "action",
    "on_kind",
]
