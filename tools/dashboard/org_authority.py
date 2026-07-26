"""Fold-based dashboard authorization — authority from the signed ledger.

Every dashboard authority decision is computed from the verified event
chain, never from a mutable projection (register D18). ``authorize``
opens the organization's authority ledger (hydration content-verifies
every stored event; a forged event never entered — ``Ledger.add``
verifies signatures at append), folds it at the requested head, reads
the persona's roles, and answers whether the union of those roles'
scope sets covers the required scope under the attenuation order
(exact, ``*``, and ``prefix:*`` covering).

Fail closed: a missing or genesis-less ledger raises (``GenesisError``
or the store's own errors) — the caller treats any exception as not
authorized. Folds carry no ``now``, so authority is a pure function of
the head set and the head-keyed cache stays sound: a head advance
changes the key and recomputes; pinning ``at_head`` reproduces a past
decision exactly.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from tools.network.ledger import LedgerStore, org_ledger_db_path, set_covers

#: (org, head_key) -> FoldState. One entry per org — the most recent
#: head wins; a miss evicts the org's older entries.
_fold_cache: Dict[Tuple[str, tuple], object] = {}


def _fold_at(org: str, at_head) -> object:
    store = LedgerStore(org_ledger_db_path(org))
    try:
        if at_head is not None:
            head_key = tuple(sorted(set(at_head)))
        else:
            head_key = tuple(store.heads())
        cached = _fold_cache.get((org, head_key))
        if cached is not None:
            return cached
        state = store.fold(heads=list(head_key))
        for key in [k for k in _fold_cache if k[0] == org]:
            del _fold_cache[key]
        _fold_cache[(org, head_key)] = state
        return state
    finally:
        store.close()


def authorize(org: str, persona_pub: str, required_scope: str, at_head=None) -> bool:
    """True iff *persona_pub*'s roles cover *required_scope* at the head.

    *at_head* pins an explicit head set (an iterable of event ids);
    ``None`` folds at the ledger's current heads.
    """
    state = _fold_at(org, at_head)
    patterns = set()
    for role in state.roles(persona_pub):
        view = state.role_defs.get(role)
        if view is not None:
            patterns.update(view.scope_set)
    return set_covers(patterns, required_scope)
