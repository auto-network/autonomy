"""ledger — the auto.network org authority ledger core (F1).

A hash-linked DAG of signed authority events plus the deterministic fold
that turns it into authority state. Pure library on top of idkit: no
storage, no network, no UI. Spec: graph note ``eb245082-b76`` §2–3, §11
(invariants L1–L4, L8). Bead: ``auto-16cjl``.
"""

from .errors import (
    CausalityError,
    GenesisError,
    LedgerError,
    MalformedEventError,
    SchemaError,
    SignatureError,
    UnknownParentError,
)
from .events import (
    APPROVAL_DOMAIN,
    EVENT_DOMAIN,
    EVENT_TYPES,
    EVENT_VERSION,
    MAX_EVENT_BYTES,
    MAX_PARENTS,
    ROTATE_DOMAIN,
    Event,
    approval_signing_input,
    make_event,
    rotate_continuity_input,
    sign_approval,
    sign_rotate_continuity,
    validate_payload,
)
from .fold import (
    INVITE_CLAIMED,
    INVITE_DEAD,
    INVITE_EXPIRED,
    INVITE_LIVE,
    INVITE_REVOKED,
    FoldState,
    MemberView,
    RoleDefView,
    fold,
    scope_invite,
    scope_role_grant,
)
from .hlc import HLC
from .ledger import Ledger
from .projections import (
    LEDGER_PROJECTION_SET_ID,
    LEDGER_STATE_SET_ID,
    PROJECTION_NAMES,
    build_live_keys,
    build_projections,
    build_role_matrix,
    build_roster,
    ledger_state_payload,
    projection_bytes,
)
from .store import (
    LEDGER_DB_SUFFIX,
    LEDGER_SCHEMA_VERSION,
    LedgerStore,
    StoreError,
    TamperError,
    checkpoint_state_hash,
    org_ledger_db_path,
)
from .scopes import (
    UNIVERSE,
    attenuates,
    covered_subset,
    pattern_covers,
    set_covers,
    validate_scope,
    validate_scope_list,
)

__all__ = [
    # container
    "Ledger",
    # store
    "LedgerStore",
    "StoreError",
    "TamperError",
    "checkpoint_state_hash",
    "org_ledger_db_path",
    "LEDGER_DB_SUFFIX",
    "LEDGER_SCHEMA_VERSION",
    # projections
    "PROJECTION_NAMES",
    "build_projections",
    "build_roster",
    "build_role_matrix",
    "build_live_keys",
    "projection_bytes",
    "ledger_state_payload",
    "LEDGER_STATE_SET_ID",
    "LEDGER_PROJECTION_SET_ID",
    # events
    "Event",
    "make_event",
    "validate_payload",
    "EVENT_TYPES",
    "EVENT_VERSION",
    "EVENT_DOMAIN",
    "APPROVAL_DOMAIN",
    "ROTATE_DOMAIN",
    "MAX_EVENT_BYTES",
    "MAX_PARENTS",
    "approval_signing_input",
    "sign_approval",
    "rotate_continuity_input",
    "sign_rotate_continuity",
    # fold
    "fold",
    "FoldState",
    "MemberView",
    "RoleDefView",
    "scope_invite",
    "scope_role_grant",
    "INVITE_LIVE",
    "INVITE_CLAIMED",
    "INVITE_REVOKED",
    "INVITE_DEAD",
    "INVITE_EXPIRED",
    # hlc
    "HLC",
    # scopes
    "UNIVERSE",
    "pattern_covers",
    "set_covers",
    "attenuates",
    "covered_subset",
    "validate_scope",
    "validate_scope_list",
    # errors
    "LedgerError",
    "MalformedEventError",
    "SchemaError",
    "SignatureError",
    "UnknownParentError",
    "CausalityError",
    "GenesisError",
]
