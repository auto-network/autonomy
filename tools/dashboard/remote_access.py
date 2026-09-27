"""Remote access to the dashboard: the label question (F6 of graph://c9d72ea4-feb).

Onboarding asks one optional free-text question: the serving label the
dashboard's relay origin is published under, ``<app>.<label>.serve.auto.network``.
The default is the persona label the node derives on its own; a custom string
becomes the label's slug, ``<slug>-<20 hex>`` (service_publication
.normalize_persona_label), and the registry binds it once and never changes it.

Availability needs no registry call: the twenty-hex suffix is the persona's own
digest, so two personas can never own the same label. What remains is local:
the slug must be well formed, must not read as the platform, an infrastructure
word or a reserved app label, and must not read as a label the operator already
publishes under. That is ``check_label``.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from tools.dashboard import label_lookalike

#: The slug part of a persona label: normalize_persona_label keeps at most 42
#: characters before the digest.
SLUG_MAX = 42
_SLUG_RE = re.compile(r"^(?![a-z0-9]{2}--)[a-z0-9](?:[a-z0-9-]{0,40}[a-z0-9])?$")
_PERSONA_SUFFIX_RE = re.compile(r"-[0-9a-f]{20}$")


@dataclass(frozen=True)
class LabelCheck:
    ok: bool
    label: str
    code: str = ""        # malformed | platform_name | reserved_name | existing_label
    against: str = ""     # the protected name the candidate reads as
    reason: str = ""      # one sentence for the screen

    def as_dict(self) -> dict:
        return asdict(self)


def existing_labels(org: str) -> list[str]:
    """Labels the operator already publishes under, from their reservations in
    *org* and in the personal scope: app labels and persona-label slugs."""
    from tools.dashboard import service_publication

    seen: list[str] = []
    for scope in dict.fromkeys(("personal", org)):
        try:
            rows = service_publication.list_reservations(scope)
        except Exception:
            continue
        for row in rows:
            if not isinstance(row, dict) or row.get("state") == "released":
                continue
            app = row.get("app_label")
            if isinstance(app, str) and app and app not in seen:
                seen.append(app)
            persona_label = row.get("persona_label")
            if isinstance(persona_label, str):
                slug = _PERSONA_SUFFIX_RE.sub("", persona_label)
                if slug and slug not in seen:
                    seen.append(slug)
    return seen


def check_label(org: str, candidate: object, *, existing: list[str] | None = None) -> LabelCheck:
    """Decide whether *candidate* may become the operator's serving-label slug."""
    if not isinstance(candidate, str):
        return LabelCheck(False, "", "malformed", "", "The label must be text.")
    label = candidate.strip().lower()
    if not label or not _SLUG_RE.fullmatch(label) or not label_lookalike.is_well_formed(label):
        return LabelCheck(
            False, label, "malformed", "",
            f"Use 1 to {SLUG_MAX} lowercase letters, digits and hyphens, starting and ending "
            "with a letter or digit.",
        )
    conflict = label_lookalike.lookalike_conflict(
        label, existing if existing is not None else existing_labels(org))
    if conflict is None:
        return LabelCheck(True, label)
    reasons = {
        "platform_name": f"That reads as \"{conflict.against}\", which belongs to the platform.",
        "reserved_name": f"That reads as \"{conflict.against}\", which is reserved.",
        "existing_label": f"That reads like your existing label \"{conflict.against}\".",
    }
    return LabelCheck(False, label, conflict.code, conflict.against, reasons[conflict.code])
