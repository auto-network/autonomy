"""What the dashboard's sign-in gate will accept.

Signing in, reaching the root, and opening a vault secret are three separate
permissions. This set describes ONLY the first: which factors may open the
dashboard, and how many of them it takes. Nothing here is cryptographic — the
gate is ordinary code protecting keys in its own process, so it can enforce any
rule at all, and the only requirement is that the rule be writable down.

## Home, per the settings rubric (``graph://4d88c2ad-625``)

Asked of each FIELD, not of the setting. "Which methods do I want to require"
is mine on every machine I own, so: ``personal``.

The rubric's "does the value have one home at all?" test does fire here, and the
answer is already correct by accident. A second fact hides nearby — *what this
installation can actually do*, since a headless box has no authenticator and a
passkey requirement is unsatisfiable there. That fact is machine-local, and it
already lives in the ``DASHBOARD_AUTH`` environment variable rather than in a
row. Two facts, two homes, and the split predates this file. Do not fold the
escape hatch in here.

Banded at ``max=raw``. It carries no secret, but nothing outside this operator
has any business reading which factors open their dashboard, and a band is
enforced at write, at promote and at the federated read — three points that fail
independently — where "everybody remembers to pass raw" is a convention.

## The reader must pin to personal, and this is a known way to fail

The rubric names it as a bug that has already happened twice: the dashboard runs
under ``GRAPH_ORG=autonomy`` and read operator-local rows from ``autonomy``,
where they do not live. Here that mistake has a worse ending than usual — a
policy that reads as absent is a policy that is not enforced, so the gate fails
OPEN on itself. Read this through ``read_owned_set(..., org=None)`` and assert
it in a test rather than trusting a comment.

## Why a policy nothing headless can satisfy is refused at write

Raised by the Installation & Packaging pillar before this was written, and it
follows from the HOME rather than being a special case: this row is
``personal``, so it applies to every machine the operator owns — including
every headless one. A policy requiring a passkey is therefore not a strict
setting on a server, it is an unsatisfiable one, and the node can never unlock.

It is refused at WRITE, where one caller is stopped and somebody is watching,
rather than discovered at a read on a box nobody is looking at. That is the
rubric's own blast-radius argument.

The context cannot be trusted to distinguish them. A headless unlock and a
browser unlock arrive at the same route, and anything claiming "I am headless"
to skip a factor is a claim an attacker makes too. So the constraint is
unconditional.

**Consequence, stated rather than discovered: require-two is currently
unrepresentable.** The only headless-satisfiable method is the password, so a
policy demanding two distinct methods cannot be met on a server. Three things
would each release it — a machine-homed override (the escape hatch already is
one), a software authenticator letting a headless node satisfy a passkey
requirement, or accepting that two-factor is desktop-only and modelling the
machine dimension explicitly. That is a decision above this schema.

## Root-signed, always

Operator ruling: this row is root-signed. That is the security model the
surrounding flows were built around, not a hardening option — so ``signer`` and
``signature`` are REQUIRED, and there is no unsigned form to fall back to.

The gate must verify before applying, and ``signer`` must be checked against the
personal identity's ``root_pub`` rather than taken from the row. A policy that
vouches for itself is the tautology ``idkit.enrollment.verify`` warns about, one
line away and indistinguishable from a real check.

``policy_binding`` below defines exactly what the signature covers, so the
signed payload is a property of the schema rather than something the first
writer invents and every later reader has to reverse-engineer.

**What this ruling does NOT answer, and somebody must.** A tampered row fails
verification and is therefore treated as absent — at which point the gate falls
back to its built-in default. If the operator's real policy was stricter than
that default, an attacker who merely CORRUPTS the row has achieved a downgrade
without forging anything. Refusing all sign-in instead is worse: that is a
lockout obtainable by writing garbage.

Closing it needs the *existence* of a policy to be authenticated somewhere the
row cannot reach — the personal identity row is the natural candidate, being
root-authored, protected, and refusing overwrite. Not built here; recorded so it
is not discovered later as a surprise.
"""

from __future__ import annotations

import re
from typing import Any

from .registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    publication_band,
    singleton,
)

SYNOPSIS = {
    "summary": (
        "The operator's rule for what opens the dashboard: which factor kinds "
        "the sign-in gate accepts and how many it takes. Sign-in only — "
        "reaching the personal root and opening a vault secret are separate "
        "permissions with separate mechanisms. Not cryptographic: the gate is "
        "ordinary middleware, so this row only has to be writable down. A "
        "policy no headless node could satisfy is refused at write."
    ),
    "nouns": [
        "dashboard auth", "sign-in policy", "unlock gate", "two-factor",
        "passkey required", "password floor", "headless sign-in",
    ],
    "related_set_ids": [
        "autonomy.identity.personal#1",
        "autonomy.identity.passkey#1",
    ],
}

DASHBOARD_AUTH_SET_ID = "autonomy.identity.dashboard-auth"
DASHBOARD_AUTH_REVISION = 1

#: The factor kinds the gate knows how to evaluate. A method it cannot evaluate
#: is refused at write: a policy naming something unenforceable reads as
#: configured while doing nothing.
KNOWN_METHODS = ("passkey", "password")

#: Methods that need a physical or platform authenticator plus a browser
#: ceremony. A headless node — every server install, and the SJC demo — has
#: neither and can never enrol OR present one.
AUTHENTICATOR_METHODS = ("passkey",)

#: Methods a headless node can satisfy. Today this is the password floor that
#: ``unlock_routes.human_auth_enrolled`` already relies on.
HEADLESS_SATISFIABLE = ("password",)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX128 = re.compile(r"^[0-9a-f]{128}$")


@home("personal")
@publication_band(max="raw")
@singleton(key="default")
class DashboardAuthPolicyV1(SettingSchema):
    """The operator's own rule for what opens their dashboard.

    Key: ``default`` — one policy per operator, matching the shape of the
    personal identity row it sits beside.
    """

    set_id = DASHBOARD_AUTH_SET_ID
    schema_revision = DASHBOARD_AUTH_REVISION

    methods: list = field(
        required=True,
        element=str,
        description=(
            "Which factor kinds may satisfy the gate, from "
            "'passkey' and 'password'. Empty is refused: a policy nothing can "
            "satisfy locks the operator out of their own dashboard."
        ),
    )
    require_count: int = field(
        required=True,
        description=(
            "How many DISTINCT accepted methods a sign-in must present — 1 or "
            "2. Never more than the number of accepted methods, or the policy "
            "is unsatisfiable by construction."
        ),
    )
    excluded_credential_ids: list = field(
        required=False,
        default_factory=list,
        element=str,
        description=(
            "Passkeys that may not satisfy the gate, by credential id. Lets a "
            "device's sign-in rights be retired without deleting the "
            "credential, which is a separate decision with separate "
            "consequences."
        ),
    )
    updated_at: str = field(
        required=True,
        description="ISO-8601 UTC timestamp the policy was last written.",
    )
    signer: str = field(
        required=True,
        description=(
            "The personal root public key that signed this policy, 64 "
            "lowercase hex. An identifier for key selection after a root "
            "rotation — NEVER the reason to believe the policy. The gate "
            "compares it against the personal identity's root_pub and refuses "
            "on mismatch."
        ),
    )
    signature: str = field(
        required=True,
        description=(
            "Ed25519 over policy_binding(), 128 lowercase hex. The gate "
            "verifies before applying; an unverifiable policy is not a policy."
        ),
    )

    @staticmethod
    def policy_binding(payload: dict) -> dict:
        """Exactly what the signature covers.

        Everything that decides who gets in, and nothing else. ``signature`` is
        excluded because it cannot cover itself; ``signer`` IS covered, so a
        policy cannot be re-attributed to a different root key without breaking.

        Absent optional fields are omitted rather than nulled, on the same
        discipline as the enrollment statement: absence is part of what the root
        committed to, so a row that grows an exclusion list later no longer
        matches anything that was signed.
        """
        binding = {
            "methods": sorted(payload["methods"]),
            "require_count": payload["require_count"],
            "updated_at": payload["updated_at"],
            "signer": payload["signer"],
        }
        excluded = payload.get("excluded_credential_ids")
        if excluded:
            binding["excluded_credential_ids"] = sorted(excluded)
        return binding

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return

        methods = payload.get("methods")
        if not isinstance(methods, list) or not methods:
            raise SchemaValidationError(
                f"{cls.__name__}: 'methods' must list at least one factor kind "
                f"— a policy nothing can satisfy is a lockout, not a hardening"
            )
        seen = set()
        for i, m in enumerate(methods):
            if m not in KNOWN_METHODS:
                raise SchemaValidationError(
                    f"{cls.__name__}: methods[{i}] is {m!r}; the gate can only "
                    f"evaluate {list(KNOWN_METHODS)}"
                )
            if m in seen:
                raise SchemaValidationError(
                    f"{cls.__name__}: methods lists {m!r} twice; a duplicate "
                    f"would inflate the count a two-factor policy compares to"
                )
            seen.add(m)

        count = payload.get("require_count")
        # bool is excluded deliberately: True == 1 in Python, so a naive check
        # accepts a policy whose wire form is `true` where a number belongs.
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise SchemaValidationError(
                f"{cls.__name__}: 'require_count' must be a positive integer, "
                f"not a boolean"
            )
        if count > len(methods):
            raise SchemaValidationError(
                f"{cls.__name__}: 'require_count' is {count} but only "
                f"{len(methods)} method(s) are accepted — unsatisfiable by "
                f"construction, so nobody could sign in"
            )

        # A policy no headless node can meet bricks every server install. Refuse
        # it here rather than at a read, where nobody is watching.
        headless = [m for m in methods if m in HEADLESS_SATISFIABLE]
        if not headless:
            raise SchemaValidationError(
                f"{cls.__name__}: every accepted method needs an authenticator, "
                f"which no headless node has — this policy would lock every "
                f"server install out permanently"
            )
        if count > len(headless):
            raise SchemaValidationError(
                f"{cls.__name__}: 'require_count' is {count} but only "
                f"{len(headless)} accepted method(s) can be satisfied without an "
                f"authenticator, so no headless node could ever sign in"
            )

        excluded = payload.get("excluded_credential_ids")
        if excluded is not None:
            if not isinstance(excluded, list):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'excluded_credential_ids' must be a list"
                )
            for i, c in enumerate(excluded):
                if not isinstance(c, str) or not c:
                    raise SchemaValidationError(
                        f"{cls.__name__}: excluded_credential_ids[{i}] must be "
                        f"a non-empty credential id"
                    )

        signer, signature = payload.get("signer"), payload.get("signature")
        if not _HEX64.match(signer or ""):
            raise SchemaValidationError(
                f"{cls.__name__}: 'signer' must be 64 lowercase hex characters"
            )
        if not _HEX128.match(signature or ""):
            raise SchemaValidationError(
                f"{cls.__name__}: 'signature' must be 128 lowercase hex "
                f"characters"
            )
