"""Look-alike refusal for operator-chosen serving labels (F6 of graph://c9d72ea4-feb).

A custom label becomes part of a public hostname (`<app>.<label>.serve.auto.network`),
bound once at the registry and never changed. Labels are lowercase DNS labels, so
the confusables that matter are the ASCII ones a reader glosses over: digits that
read as letters (0/o, 1/l, 5/s), letter pairs that fuse (rn/m, vv/w, cl/d) and
hyphens. A candidate is refused when its skeleton equals the skeleton of a
protected name, contains the platform's own name, or equals the skeleton of a
label the operator already publishes under (two of their labels must not read
alike either).

Pure functions, no I/O: the caller supplies the operator's existing labels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tools.graph.schemas.namespace_reservation import RESERVED_APP_LABELS

#: Names nobody may read as: the platform and its infrastructure words.
PLATFORM_NAMES = frozenset({
    "autonomy", "autonomynetwork", "auto", "network", "dashboard", "relay",
    "registry", "serve", "www", "api", "admin", "login", "auth", "support",
})

_SINGLE = str.maketrans({
    "0": "o", "1": "l", "i": "l", "5": "s", "2": "z", "8": "b", "6": "b",
    "9": "g", "3": "e", "4": "a", "7": "t",
})
# Letter pairs that fuse at a glance. "cl"/"d" and "nn"/"m" are left out on
# purpose: they turn ordinary words (clock, anna) into other ordinary words.
_PAIRS = (("rn", "m"), ("vv", "w"))
# Same shape as the reservation validator, tagged labels (`xn--`) refused.
_LABEL_RE = re.compile(r"^(?![a-z0-9]{2}--)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def skeleton(label: str) -> str:
    """The shape a reader perceives: confusables folded, hyphens dropped."""
    folded = label.lower().replace("-", "").translate(_SINGLE)
    for pair, single in _PAIRS:
        folded = folded.replace(pair, single)
    return folded


@dataclass(frozen=True)
class LookalikeRefusal:
    code: str        # platform_name | reserved_name | existing_label
    against: str     # the protected name the candidate reads as


def lookalike_conflict(candidate: str, existing_labels=()) -> LookalikeRefusal | None:
    """None when *candidate* is distinct, else why it is refused.

    A malformed candidate is not this function's business (the label
    validators refuse it); it is compared as given.
    """
    shape = skeleton(candidate)
    if not shape:
        return None
    for code, names in (("platform_name", PLATFORM_NAMES), ("reserved_name", RESERVED_APP_LABELS)):
        for name in sorted(names):
            if shape == skeleton(name.lstrip("_")):
                return LookalikeRefusal(code, name.lstrip("_"))
    # The platform's own name inside a label reads as endorsement
    # ("autonomy-support", "my-autonomy"); an exact platform word alone was
    # caught above, so this is the substring case for the two brand names.
    for brand in ("autonomy", "autonetwork"):
        if skeleton(brand) in shape:
            return LookalikeRefusal("platform_name", brand)
    for label in existing_labels:
        if not isinstance(label, str) or not label:
            continue
        if label == candidate:
            continue  # the operator's own label is not a look-alike of itself
        if skeleton(label) == shape:
            return LookalikeRefusal("existing_label", label)
    return None


def is_well_formed(label: str) -> bool:
    return isinstance(label, str) and _LABEL_RE.fullmatch(label) is not None
