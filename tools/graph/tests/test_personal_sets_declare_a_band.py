"""A personal-homed set must SAY what it publishes, rather than defaulting.

Proposed by the Mission Control infrastructure pillar after the same omission
was found twice in one evening. The failure it closes is silent by construction:
``@publication_band`` is a decorator, and a decorator that is not written
produces no error, no warning, and a set that resolves to the full range.

That default is worse than it first looks, because in this system the outbound
declaration and the inbound one are the same declaration. ``read_set`` drops
peers before opening any peer database:

    if not set(schemas.states_allowed(set_id, 1)) & set(PEER_VISIBLE_STATES):
        resolved_peers = []

So a missing band does two things nobody chose: the row can be promoted to a
peer-visible state, AND peer databases are opened for that set at all. An
omission is enough; no widening is required.

Three sets reached master this way — a harness's OAuth token triple, the
operator's device inventory, and the vault's sealed key-control wraps — while
their siblings were pinned. That is not a pattern of decisions; it is a pattern
of defaults.

**This test demands a DECLARATION, not a particular value.** ``max="canonical"``
passes. The rubric has a real use for a published personal row — an individual's
public identity, profile and presence — and this must not stand in its way. What
it refuses is arriving at federation by not having written anything down.
"""

from __future__ import annotations

import tools.graph.schemas as schemas
from tools.graph.schemas.registry import SCHEMAS


def _personal_sets():
    """Every personal-homed set, with the band its schema declares (or None)."""
    found: dict[str, object] = {}
    for key, cls in SCHEMAS.items():
        set_id = getattr(cls, "set_id", None) or key.split("#")[0]
        if schemas.declared_home(set_id) != "personal":
            continue
        # Any revision declaring one is enough; the decorator is per-class.
        band = getattr(cls, "_publication_band", None)
        if band is not None or set_id not in found:
            found[set_id] = band
    return found


def test_every_personal_homed_set_declares_a_publication_band():
    """THE ONE THAT MATTERS.

    An undeclared band is not a neutral default — it is the widest possible
    exposure, chosen by nobody, on the store that holds the operator's own
    material.
    """
    undeclared = sorted(sid for sid, band in _personal_sets().items() if band is None)
    assert not undeclared, (
        "these personal-homed sets declare no publication band, so they resolve "
        "to the full range and peer databases are opened for them: "
        f"{undeclared}. Declare @publication_band explicitly — max='raw' for "
        "anything that should stay the operator's own, or a wider band if the "
        "set really is meant to be read through by peers."
    )


def test_the_secret_bearing_personal_sets_are_pinned_to_raw():
    """The stronger claim, for the sets where a wider band could not be right.

    Named individually rather than derived, because a rule clever enough to
    infer 'this one carries key material' is a rule that will be wrong about a
    set nobody thought of.
    """
    must_be_raw = [
        "autonomy.identity.personal",       # the armored root
        "autonomy.identity.passkey",        # the device inventory
        "autonomy.vault.policy-class",      # sealed key-control wraps
        "autonomy.vault.audited",
        "autonomy.vault.secured",
        "autonomy.commit.signing-key",
        "dashboard.claude.credentials",     # OAuth token triple
        "dashboard.codex.credentials",      # its sibling, missed until 2026-08-20
        "dashboard.claude.setup_tokens",
        "autonomy.identity.dashboard-auth",
    ]
    wider = {
        set_id: schemas.states_allowed(set_id, 1)
        for set_id in must_be_raw
        if tuple(schemas.states_allowed(set_id, 1)) != ("raw",)
    }
    assert not wider, (
        f"these must never reach a peer-visible state, and do not resolve to "
        f"raw alone: {wider}"
    )


def test_a_declared_wide_band_is_allowed():
    """The rule is 'say what you publish', not 'publish nothing'. If this ever
    fails it means the first test has been tightened into a prohibition, which
    would block the rubric's legitimate published-identity case."""
    from tools.graph.schemas.registry import publication_band

    decorated = publication_band(max="canonical")(type("Probe", (), {}))
    assert getattr(decorated, "_publication_band", None) is not None
