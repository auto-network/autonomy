"""Which DAG an ``ancestry`` callable closes over, checked rather than assumed.

There are two graphs in this system and they are indistinguishable to the type
system and to the eye. Both are one-argument callables conventionally named
``ancestry``; both return a frozenset; both are usually in scope together.

    AUTHORITY  ``Ledger.ancestry``           over signed ledger EVENT ids
    STORAGE    ``KeyControlStore.ancestry``  over storage ``state_id``s

They close over DISJOINT identifier spaces, so a value from one means nothing
in the other. Passing the wrong one is silent: it reaches ``state_covers``,
which asks whether a closure contains a set of loss heads, and the answer is
"no, never" or "yes, vacuously" depending on how the closure treats
identifiers it has never seen. Nothing raises, and the write either always
advances or never does.

## Why a runtime tag rather than a comment or a type

Comments did not hold: this exact confusion survived three review rounds in
one afternoon on the vault sealer, with the correct answer already written in
the crib (§1b) and in ``keycontrol``'s own module docstring.

Static types would not have held either, because the mistake is not a type
error — both really are ``Callable[[Iterable[str]], frozenset]``. The
difference is the MEANING of the strings, which is exactly what a nominal tag
can carry and a structural type cannot.

## Tag positively, not negatively

:func:`require_dag` asserts what a seam DOES take, never what it does not.
That catches an untagged callable, which is the case that matters most —
because the reason this stayed green in CI is that the test double's ancestry
is untagged and happens to be wired to the ledger's. Under a positive check
the double fails until somebody tags it, and tagging it forces the one
question whose absence hid the bug: which DAG is this?
"""

from __future__ import annotations

AUTHORITY = "authority"
STORAGE = "storage"

#: Attribute a tagged callable carries. Set on the FUNCTION, so it survives
#: being looked up as a bound method.
DAG_ATTR = "__autonomy_dag__"


def tag_dag(kind: str):
    """Mark a function as closing over *kind*'s identifier space."""

    def _wrap(fn):
        setattr(fn, DAG_ATTR, kind)
        return fn

    return _wrap


def dag_of(ancestry) -> "str | None":
    """The DAG *ancestry* declares, or ``None`` if it declares nothing."""
    return getattr(getattr(ancestry, "__func__", ancestry), DAG_ATTR, None)


def require_dag(ancestry, expected: str, where: str) -> None:
    """Refuse an ``ancestry`` that does not declare *expected*.

    ``where`` names the seam, because the message is read by someone holding
    two plausible callables who needs to know which one this argument wants.
    """
    got = dag_of(ancestry)
    if got == expected:
        return
    if got is None:
        raise TypeError(
            f"{where} takes the {expected!r} DAG's ancestry, and this callable "
            f"declares no DAG. Tag it with dag_tag.tag_dag(...) at "
            f"its definition — including in test doubles, where an untagged "
            f"stand-in is how the wrong graph passes CI."
        )
    raise TypeError(
        f"{where} takes the {expected!r} DAG's ancestry; this is the {got!r} "
        f"one. They close over disjoint identifier spaces — ledger event ids "
        f"versus storage state ids — so the wrong one does not fail, it "
        f"answers a question about identifiers it has never seen."
    )
