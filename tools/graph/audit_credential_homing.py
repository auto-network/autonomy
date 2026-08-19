"""Which stored rows hold credential-shaped values, and are they protected.

auto-ehyoh. The structural guarantee everyone leans on — "secret-bearing sets
are pinned ``max=raw``, so secrets never leak" — protects the sets somebody
LABELLED secret. It does not protect a set that merely CONTAINS secrets.
``autonomy.workspace`` proved it: no declared home, no band, and the
operator's own GitHub tokens in plaintext in an organization's shared store,
served by the generic Settings readers.

Every audit that missed it classified STRUCTURE. ``homes.json`` is a per-set
row census; the home classifier reads DECLARATIONS. Neither looks at payload
VALUES, so a set with no declared home holding literal credentials was
invisible to both. This one looks at values.

NO VALUE IS EVER RETURNED OR PRINTED. A finding names the set, the store, the
key and the FIELD PATH. That is deliberate: an audit that has to be trusted
with secrets in order to find them is not one anybody will run.

Run it:

    python3 -m tools.graph.audit_credential_homing

A row is UNPROTECTED when its set is neither personal-homed nor band-pinned.
Personal-homed keeps it off other people's machines; a band keeps it off the
federated read surface. Either is a real protection; neither is a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: A value that LOOKS like credential material. Deliberately loose on its own
#: — paired with :data:`FIELD_SAYS_SECRET` below, because shape alone matches
#: every SHA, UUID and content hash in the estate and drowns the signal.
VALUE_LOOKS_SECRET = re.compile(
    r"^(?:[A-Za-z0-9_\-]{32,}|gh[pousr]_[A-Za-z0-9]{20,}|-----BEGIN)"
)

#: A field NAME that says the value is credential material.
FIELD_SAYS_SECRET = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|\bkey\b|credential|auth)",
    re.IGNORECASE,
)

#: Names that match the pattern above but are not credentials. Kept explicit
#: rather than tightened into the regex: each is a real field that WOULD be
#: reported, and naming them is how the next person knows they were
#: considered rather than missed.
FIELD_IS_NOT_SECRET = re.compile(
    r"(_sha|_hash|_id$|fingerprint|key_id|rel_path|filename|lastRowId)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    """One credential-shaped value, located but never carried."""

    set_id: str
    store: str
    key: str
    field_path: str
    home: str | None
    band: tuple[str, str] | None

    @property
    def protected(self) -> bool:
        """Personal-homed OR band-pinned. Either is enough; neither is a guess."""
        return self.home == "personal" or self.band is not None


def credential_shaped_fields(payload, _path: str = ""):
    """Yield the FIELD PATH of every credential-shaped value in ``payload``.

    Yields paths, never values. Walks nested dicts and lists so a credential
    inside ``env`` or an array element is not missed — the live finding was
    ``env.GH_TOKEN``, one level down.
    """
    if isinstance(payload, dict):
        for name, value in payload.items():
            child = f"{_path}.{name}" if _path else name
            yield from credential_shaped_fields(value, child)
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            yield from credential_shaped_fields(value, f"{_path}[{index}]")
    elif isinstance(payload, str) and VALUE_LOOKS_SECRET.match(payload.strip()):
        if FIELD_SAYS_SECRET.search(_path) and not FIELD_IS_NOT_SECRET.search(_path):
            yield _path


def declared_protection(set_id: str):
    """``(home, band)`` for ``set_id``, scanning revisions for the band."""
    from tools.graph import schemas

    home = schemas.declared_home(set_id)
    band = next(
        (
            b
            for revision in range(1, 8)
            if (b := schemas.declared_band(set_id, revision)) is not None
        ),
        None,
    )
    return home, band


def audit(rows) -> list[Finding]:
    """Findings for ``rows`` — an iterable of ``(set_id, store, key, payload)``."""
    out: list[Finding] = []
    for set_id, store, key, payload in rows:
        home, band = declared_protection(set_id)
        for field_path in credential_shaped_fields(payload):
            out.append(Finding(set_id, store, key, field_path, home, band))
    return out


def _live_rows():
    """Every stored row reachable from here, via the settings read path."""
    from tools.graph import cross_org, settings_ops

    for store in cross_org.all_store_slugs() + cross_org.list_org_slugs():
        for set_id in settings_ops.list_set_ids():
            try:
                members = settings_ops.read_set(set_id, org=store, peers=[]).members
            except Exception:
                continue
            for member in members:
                yield set_id, store, member.key, (member.payload or {})


def main() -> int:
    findings = audit(_live_rows())
    unprotected = [f for f in findings if not f.protected]

    for finding in sorted(findings, key=lambda f: (f.protected, f.set_id)):
        mark = "ok " if finding.protected else "!! "
        print(
            f"{mark}{finding.set_id:<34} {finding.store}/{finding.key} "
            f"-> {finding.field_path}   home={finding.home} band={finding.band}"
        )

    print(
        f"\n{len(findings)} credential-shaped value(s), "
        f"{len(unprotected)} in a set that is neither personal-homed nor "
        f"band-pinned."
    )
    return 1 if unprotected else 0


if __name__ == "__main__":
    raise SystemExit(main())
