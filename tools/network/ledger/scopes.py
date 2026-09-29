"""Scope patterns and the attenuation ordering.

A scope is an opaque ASCII string like ``link:publish`` or
``invite:member``. Two pattern forms widen a scope into a family:

- ``*`` — every scope (held implicitly by the org root, grantable).
- ``prefix:*`` — every scope under ``prefix:`` (e.g. ``role:grant:*``).

``pattern_covers(parent, child)`` is the partial order the whole ledger's
attenuation rule (L2) reduces to: a child scope set is grantable iff every
entry is covered by some entry of the granter's (delegable) set.
"""

from __future__ import annotations

from .errors import SchemaError

MAX_SCOPE_LEN = 128
MAX_SCOPE_ENTRIES = 64

_SCOPE_OK = frozenset(
    "abcdefghijklmnopqrstuvwxyz" "ABCDEFGHIJKLMNOPQRSTUVWXYZ" "0123456789" ":-_.*/"
)


def validate_scope(value: object, what: str = "scope") -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_SCOPE_LEN:
        raise SchemaError(f"{what} must be a non-empty string of at most {MAX_SCOPE_LEN} chars")
    if not set(value) <= _SCOPE_OK:
        raise SchemaError(f"{what} contains characters outside [A-Za-z0-9:_.*/-]")
    if "*" in value and value != "*" and not value.endswith(":*"):
        raise SchemaError(f"{what}: '*' may only appear alone or as a ':*' suffix")
    if value.count("*") > 1:
        raise SchemaError(f"{what}: at most one '*' allowed")
    return value


def validate_scope_list(value: object, what: str = "scope", allow_empty: bool = False) -> tuple:
    if not isinstance(value, list) or len(value) > MAX_SCOPE_ENTRIES:
        raise SchemaError(f"{what} must be a list of at most {MAX_SCOPE_ENTRIES} entries")
    if not value and not allow_empty:
        raise SchemaError(f"{what} must not be empty")
    for entry in value:
        validate_scope(entry, f"{what} entry")
    if value != sorted(set(value)):
        raise SchemaError(f"{what} must be sorted and free of duplicates")
    return tuple(value)


def pattern_covers(parent: str, child: str) -> bool:
    """True iff *parent* covers *child* (child may itself be a pattern)."""
    if parent == "*":
        return True
    if parent.endswith(":*"):
        return child.startswith(parent[:-1]) or child == parent
    return child == parent


def set_covers(patterns, target: str) -> bool:
    """True iff some pattern in *patterns* covers *target*."""
    return any(pattern_covers(p, target) for p in patterns)


def attenuates(child_set, parent_set) -> bool:
    """True iff every child entry is covered by the parent set (L2 order)."""
    return all(set_covers(parent_set, c) for c in child_set)


def covered_subset(scopes, parent_set) -> frozenset:
    """The entries of *scopes* the parent set covers (effective grant)."""
    return frozenset(s for s in scopes if set_covers(parent_set, s))


#: The universal scope set — what the org root holds implicitly.
UNIVERSE = frozenset({"*"})


#: The signed-settings scope family (graph://21a0da9e-1c2, boundary step 4;
#: auto-qrmlg.6 S1): ``settings:sign:<set_id>`` entitles a persona to sign
#: organization rows of that set under a delegate; ``settings:sign:*`` covers
#: every set. A ROLE scope held by the persona, never a scope on the storage
#: delegate grant (scopes.py self_delegable_exact admits only the storage
#: shape): the boundary resolves the signing key to its member persona and
#: re-derives the entitlement from the fold at use time.
SETTINGS_SIGN_PREFIX = "settings:sign:"
SETTINGS_SIGN_ANY = SETTINGS_SIGN_PREFIX + "*"
#: Every scope of the ``settings:`` family, for "does this role name any".
SETTINGS_FAMILY_PREFIX = "settings:"


def settings_sign_scope(set_id: str) -> str:
    """The scope that entitles signing rows of *set_id*."""
    return validate_scope(SETTINGS_SIGN_PREFIX + validate_scope(set_id, "set_id"), "settings scope")


def names_settings_scope(scopes) -> bool:
    """Whether a scope set names ANY scope of the settings family — the
    opt-in to restriction (operator ruling 2026-09-27 23:41Z: every member
    role carries ``settings:sign:*`` unless the organization narrows it)."""
    return any(isinstance(s, str) and s.startswith(SETTINGS_FAMILY_PREFIX) for s in scopes)


#: The two storage scope families a CURRENT MEMBER PERSONA may
#: self-delegate (auto-wrkaq): a strictly weaker, non-redelegable,
#: expiring instrument of its own held authority — PIN 6b, "a persona
#: provisions and expires its own delegates". Restricted BY SCOPE
#: deliberately: these are the scopes whose ACCEPTANCE re-derives
#: authority from current membership at USE time
#: (storagekit/acceptance.py consults the roster projection, never a
#: generic scope holding), so mint-time attenuation does no security work
#: for them. A scope whose acceptance reads the delegated holding instead
#: would have mint-time attenuation as its ONLY gate — an unrestricted
#: rule would open it silently.
_SELF_DELEGABLE_GRANT_PREFIX = "storage:capability:grant:"
_SELF_DELEGABLE_ADVANCE_PREFIX = "storage:state:advance:"


#: The one scope a checkpointer may add to its storage delegate: a hot
#: delegate that signs the checkpoint published at admission (OrgAdmission
#: P2; DelegateCheckpoint.spthy; operator ruling 1, 2026-09-25). Its
#: acceptance also re-derives authority at use time: the verifier places the
#: GRANTING persona under the previous checkpointers root, never the key.
SELF_DELEGABLE_CHECKPOINT_SCOPE = "membership:checkpoint"


def self_delegable_exact(scopes) -> bool:
    """EXACTLY the storage delegate's scope shape: one grant scope and one
    advance scope, over ONE shared domain, optionally plus the membership
    checkpoint scope. An exact predicate, not pattern coverage — coverage
    would admit a singleton (an instrument the design does not define), a
    mixed-domain pair (a single delegate spanning two domains), and the
    pair plus any other third (reach beyond the defined shapes). The
    storage delegate is defined as exactly two scopes (§8, §9), the
    checkpointer's as those two plus the checkpoint scope, and those
    definitions are enforced here, at admission."""
    scopes = frozenset(scopes) - {SELF_DELEGABLE_CHECKPOINT_SCOPE}
    if len(scopes) != 2:
        return False
    domains = {"grant": None, "advance": None}
    for s in scopes:
        if s.startswith(_SELF_DELEGABLE_GRANT_PREFIX):
            domains["grant"] = s[len(_SELF_DELEGABLE_GRANT_PREFIX):]
        elif s.startswith(_SELF_DELEGABLE_ADVANCE_PREFIX):
            domains["advance"] = s[len(_SELF_DELEGABLE_ADVANCE_PREFIX):]
        else:
            return False
    return (
        domains["grant"] is not None
        and domains["grant"] != ""
        and domains["grant"] == domains["advance"]
        and "*" not in domains["grant"]
    )
