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
    # ``key`` must match as a WORD PART, not a whole word: ``\bkey\b`` cannot
    # match inside ``private_key`` because ``_`` is a word character, so the
    # audit reported CLEAN on the two sets holding the operator's actual root
    # keys — armored_private_key and sealed_root_key. Found by reading the
    # final-form identity schemas, not by the audit.
    r"(token|secret|password|passwd|api[_-]?key|key|credential|auth|armor|seal)",
    re.IGNORECASE,
)

#: Names that match the pattern above but are not credentials. Kept explicit
#: rather than tightened into the regex: each is a real field that WOULD be
#: reported, and naming them is how the next person knows they were
#: considered rather than missed.
#:
#: ``_path`` is here on the rubric's own logic. A path is not a credential —
#: "store the credential, not the path to the file containing it" — so a field
#: holding one is a DIFFERENT finding: a secret that has not left the
#: filesystem, which is auto-xhb74's class, not this audit's. Naming it
#: matters because the value can still LOOK secret: serve-cert's key_path is a
#: portable basename shaped ``serve-<uuid>``, with no slash to give it away.
FIELD_IS_NOT_SECRET = re.compile(
    r"(_sha|_hash|_id$|fingerprint|key_id|_path|rel_path|filename|lastRowId)",
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


#: Findings that are KNOWN, TRACKED and not yet fixable. Each entry is
#: ``(set_id, "store/key", field_path)`` and must cite the bead holding it.
#:
#: A baseline is a liability, so it lives in the source rather than a data
#: file: adding a line is a code change somebody reviews, and an entry with no
#: bead is the one to reject. The gate exists to catch the NEXT one, and it
#: cannot do that while it is red for the ones already tracked.
KNOWN_OUTSTANDING = {
    # auto-ehyoh (P0) — the operator's own GitHub credentials in Anchore's
    # shared org store. They cannot move until the vault releases a secret
    # with no human present (auto-a1pub). These entries are DELETED, not
    # edited, when that migration lands.
    ("autonomy.workspace", "anchore/enterprise-data-feeds", "env.GH_TOKEN"),
    ("autonomy.workspace", "anchore/enterprise-data-feeds", "env.GITHUB_RELEASE_PULL_TOKEN"),
    ("autonomy.workspace", "anchore/enterprise-ng", "env.GH_TOKEN"),
    ("autonomy.workspace", "anchore/enterprise-ng", "env.GITHUB_RELEASE_PULL_TOKEN"),
    ("autonomy.workspace", "anchore/insights-data-service", "env.GH_TOKEN"),
    ("autonomy.workspace", "anchore/insights-data-service", "env.GITHUB_RELEASE_PULL_TOKEN"),
    ("autonomy.workspace", "anchore/on-prem-ui", "env.GH_TOKEN"),
    ("autonomy.workspace", "anchore/on-prem-ui", "env.GITHUB_RELEASE_PULL_TOKEN"),
    ("autonomy.workspace", "anchore/scale-harness", "env.GH_TOKEN"),
    ("autonomy.workspace", "anchore/scale-harness", "env.GITHUB_RELEASE_PULL_TOKEN"),

    # auto-ehyoh item 4 — the invitation tokens. Org-homed with no band, in
    # anchore, autonomy and dynbench. Org-homing is PLAUSIBLY right here,
    # unlike the rows above: an invitation belongs to the organization that
    # issued it, not to the operator personally. What is missing is a band,
    # so "these do not reach a peer's read surface" is structural rather
    # than true-by-accident. A whole-set entry rather than twenty literals
    # because the question is about the SET, and enumerating rows would
    # churn every time one is minted.
    ("autonomy.network.link-grant", "*", "*"),
}


def baseline_key(finding: "Finding") -> tuple[str, str, str]:
    return (finding.set_id, f"{finding.store}/{finding.key}", finding.field_path)


def _baselined(finding: "Finding") -> bool:
    """Whether this finding is accepted, by exact row or by whole set.

    A whole-set entry is for a question that is about the SET rather than any
    row — enumerating rows there would churn every time one is minted, and a
    baseline that has to be edited during normal operation stops being read.
    """
    return (
        baseline_key(finding) in KNOWN_OUTSTANDING
        or (finding.set_id, "*", "*") in KNOWN_OUTSTANDING
    )


def unbaselined(findings) -> list["Finding"]:
    """Unprotected findings nobody has accepted — what the gate fails on."""
    return [f for f in findings if not f.protected and not _baselined(f)]


class NothingRead(RuntimeError):
    """No store could be read, so there is no result — clean or otherwise."""


def _live_rows():
    """Every stored row reachable from here, read over the dashboard API.

    Deliberately the plain HTTP read rather than the in-process settings
    path. In a container the databases are not on disk, so an in-process
    read raises for every store and the audit reports a confident ZERO
    FINDINGS having looked at nothing — the exact failure this audit exists
    to catch, occurring inside the audit.

    Each member reports the store it CAME FROM. Attribution uses that, never
    the store that was queried: a read scoped to one org still returns rows
    from its peers, and labelling those by the query names the wrong place.
    """
    import json as _json
    import os as _os
    import ssl as _ssl
    import urllib.request as _u

    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    api = _os.environ.get("GRAPH_API", "https://localhost:8080")

    def get(path, org=None):
        req = _u.Request(f"{api}{path}")
        if org:
            req.add_header("X-Graph-Org", org)
        with _u.urlopen(req, context=ctx, timeout=30) as r:
            return _json.loads(r.read())

    orgs = [
        (row.get("org") or {}).get("slug")
        for row in get("/api/orgs").get("orgs", [])
    ]
    orgs = [o for o in orgs if o]
    if not orgs:
        raise NothingRead("the dashboard reported no stores at all")

    rows, read_ok = [], 0
    for org in orgs:
        # Enumerate over HTTP too. Using the in-process lister here was the
        # same frame error one level up: the read worked and the ENUMERATION
        # raised, so every store was skipped and the result was empty.
        try:
            set_ids = get("/api/graph/sets", org).get("set_ids", [])
        except Exception:
            set_ids = []
        if not set_ids:
            continue
        for set_id in set_ids:
            try:
                members = get(f"/api/graph/settings/{set_id}", org=org).get(
                    "members", [])
            except Exception:
                continue
            read_ok += 1
            for member in members:
                came_from = member.get("org") or org
                if came_from != org:
                    continue          # counted once, under its own store
                rows.append(
                    (set_id, came_from, member.get("key", ""),
                     member.get("payload") or {})
                )
    if not read_ok:
        raise NothingRead(
            f"read none of {len(orgs)} store(s). Reporting zero findings "
            f"from here would be a clean result produced by looking at "
            f"nothing."
        )
    return rows


def main() -> int:
    try:
        rows = _live_rows()
    except NothingRead as exc:
        print(f"REFUSING TO REPORT: {exc}")
        return 2
    findings = audit(rows)
    unprotected = [f for f in findings if not f.protected]
    new = unbaselined(findings)

    for finding in sorted(findings, key=lambda f: (f.protected, f.set_id)):
        mark = "ok " if finding.protected else "!! "
        print(
            f"{mark}{finding.set_id:<34} {finding.store}/{finding.key} "
            f"-> {finding.field_path}   home={finding.home} band={finding.band}"
        )

    print(
        f"\n{len(findings)} credential-shaped value(s), "
        f"{len(unprotected)} in a set that is neither personal-homed nor "
        f"band-pinned, {len(new)} of those NOT baselined."
    )
    if new:
        print("\nNEW, and this is what the gate exists for:")
        for f in new:
            print(f"  {f.set_id} {f.store}/{f.key} -> {f.field_path}")
        print(
            "\nEither give the set a personal home or a publication band, or "
            "add it to KNOWN_OUTSTANDING WITH THE BEAD that holds it. An "
            "entry with no bead is the one to reject."
        )
    present = {baseline_key(f) for f in unprotected}
    present |= {(f.set_id, "*", "*") for f in unprotected}
    stale = KNOWN_OUTSTANDING - present
    if stale:
        print(f"\n{len(stale)} baselined finding(s) no longer present — "
              f"delete them, a baseline that outlives its cause hides the "
              f"next one:")
        for entry in sorted(stale):
            print(f"  {entry[0]} {entry[1]} -> {entry[2]}")
    return 1 if new else 0


if __name__ == "__main__":
    raise SystemExit(main())
