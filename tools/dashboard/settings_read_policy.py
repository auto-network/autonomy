"""Who may read a Setting through the GENERIC readers, and how that is decided.

auto-1wwpf.10. The generic Settings readers accept an arbitrary ``set_id``, so
a guard on a secret's dedicated endpoint does not contain the secret: an
unauthenticated ``GET /api/graph/settings/autonomy.commit.signing-key/default``
returned the raw payload including ``armored_private_key``.

FAIL-CLOSED, NOT A DENYLIST — and the polarity is the whole design. A
secret-only denylist treats OMISSION AS PUBLIC: a newly registered secret set
leaks from the moment it is declared until somebody remembers to add it. That
is not a hypothetical. ``autonomy.vault.secret#1`` is specified and unbuilt;
under a denylist it would have arrived unlisted and leaked on the day it
shipped. Under this polarity it is refused on arrival.

So: an explicit PUBLIC allowlist names the sets these readers may serve without
global operator authority. Everything else — including a set nobody has
classified, including one that does not exist — requires it. Unknown denies.

ONE CONSTANT, CONSULTED BY EVERY READER. Not a per-handler copy. A second copy
drifts, and the synthetic-set test still passes while the drifted reader
serves.

This module decides POLICY only. It performs no authentication: callers pair
``requires_global_authority`` with the existing
``api_auth.require_global_api_authority``, which classifies the caller through
``ApiIdentityMiddleware``. ``X-Graph-Org``, query parameters, ambient graph
scope, publication state and federated composition grant no authority.
"""

from __future__ import annotations

#: Sets these generic readers may serve WITHOUT global operator authority.
#:
#: Adding to this list is a deliberate act that needs a positive test proving
#: the set still serves unauthenticated — over-denial is then caught here
#: rather than in production. Removing from it is always safe.
#:
#: Everything absent is refused, which is the point: a set that nobody has
#: classified yet is refused rather than served.
PUBLIC_SETTING_SET_IDS: frozenset[str] = frozenset({
    "autonomy.identity.passkey",
    "autonomy.network.binding",
    "autonomy.network.persona",
    # The serving check reads this one.
    "autonomy.network.serve-cert",
    "autonomy.network.ledger-projection",
    "autonomy.network.ledger-state",
})


def canonical_set_id(raw: str | None) -> str | None:
    """The canonical set id, or ``None`` if it is not in canonical form.

    Deliberately strict: this REJECTS a non-canonical form rather than
    normalizing it into a match. Normalizing would make "rejected" and
    "normalized then matched" indistinguishable from outside, and an
    allowlist that quietly accepts variants is an allowlist with unbounded
    membership — ``Autonomy.Identity.Passkey`` or a trailing-dot prefix would
    admit whatever the normalizer decided they meant.

    A caller holding a legitimately canonical id is unaffected. A caller
    holding a variant gets a refusal, which is the correct answer for a
    surface where omission must be safe.
    """
    if not raw or not isinstance(raw, str):
        return None
    trimmed = raw.strip()
    if trimmed != raw or not trimmed:
        return None
    # A revision suffix is a display form, not the set identity.
    if "#" in trimmed:
        return None
    if trimmed != trimmed.lower():
        return None
    if trimmed.startswith(".") or trimmed.endswith(".") or ".." in trimmed:
        return None
    if "/" in trimmed or "\\" in trimmed:
        return None
    return trimmed


def requires_global_authority(raw_set_id: str | None) -> bool:
    """Whether reading ``raw_set_id`` needs global operator authority.

    True for everything not on the public allowlist in canonical form —
    including an unrecognised set, a malformed one, and ``None``. The default
    answer to "may an anonymous caller read this?" is no.
    """
    canonical = canonical_set_id(raw_set_id)
    if canonical is None:
        return True
    return canonical not in PUBLIC_SETTING_SET_IDS
