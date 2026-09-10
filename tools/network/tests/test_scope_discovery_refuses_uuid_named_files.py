"""A uuid-named database in orgs/ must never become a sync scope.

`discover_org_sync_scopes` now filters by the shared `db.is_org_slug`. It used
to carry its OWN `^[a-z][a-z0-9-]{0,62}$`, with a comment claiming that excluded
"a uuid-named file beside the slug-named one". It did so only by accident, and by
TWO different accidents. Measured 2026-09-10 against the fleet's real
identifiers, running that regex:

    ORG_UUIDS (36 chars) — `^[a-z][a-z0-9-]{0,62}$` needs a leading LETTER:
        2d4b90cb-…  leading '2'  rejected   <- home's stray file, excluded by
                                               luck of its first character
        33112ee7-…  leading '3'  rejected
        02d833fd-…  leading '0'  rejected
        c8e5cd04-…  leading 'c'  ADMITTED   <- anchore's own org_uuid

    GENESIS IDS (64 hex, no hyphens) — rejected on LENGTH, not characters:
        a0d22266b6ff…  leading 'a'  rejected (64 > 63)
        b1416c0ba774…  leading 'b'  rejected (64 > 63)
        00a5bc303ced…  leading '0'  rejected

Six of sixteen first-character classes admit a 36-char uuid, so one of the
fleet's four org_uuids gets through today. The genesis ids are excluded only
because the bound is 62-plus-one and they are 64: a 63-character
leading-letter string passes and a 64-character one does not. THE LENGTH BOUND
IS NOT A GUARD — it was chosen for slug length, and widening it to 64 for any
unrelated reason silently opens that door. Corrected by host-0906-222509, who
caught that an earlier version of this file labelled a hyphenated 36-char
string as "autonomy's genesis id"; I had reformatted a real 64-hex identifier
into uuid shape and kept the real one's name on it.

Home really does carry such a file — 700 KB beside a 2.7 GB autonomy.db, with
seventy replicated settings and catalog rows — so this is the amplification
path, not a hypothetical (auto-kou68).
"""

from __future__ import annotations

import pytest

from tools.network import fleet_sync_scheduler as fss

#: The fleet's REAL org_uuids, spanning both first-character cases: leading
#: digit (the old regex rejected these too) and leading letter (admitted).
UUID_STEMS = [
    "2d4b90cb-1e89-452b-82cb-68ca44fd8e52",   # autonomy  — leading digit
    "c8e5cd04-8f19-4bc2-8951-a6b6b80b2699",   # anchore   — leading letter
    "33112ee7-b56d-4fa9-9922-8f8ce0769efd",   # dynbench  — leading digit
    "02d833fd-a664-5b08-86ca-86615db52f6f",   # personal  — leading digit
]

#: The fleet's REAL genesis ids: 64 hex, NO hyphens. A different identifier
#: from the org_uuid above, and the one the serving key is derived from.
GENESIS_IDS = [
    "a0d22266b6ffda8c631d95974ce6d4df61c83b95ace08ad62b4341174bcd915a",  # autonomy
    "b1416c0ba77478ce5694a86ef6e7c8024baf9a1e1df6f06c34870747642c32bc",  # anchore
    "00a5bc303ced851b3b2a47dd73dc23280146b1e41d79dbb22eb11f204efd9c0c",  # dynbench
]


@pytest.fixture
def orgs_dir(tmp_path, monkeypatch):
    """Point scope discovery at a temp orgs/ directory."""
    root = tmp_path / "data"
    orgs = root / "orgs"
    orgs.mkdir(parents=True)
    monkeypatch.setattr(
        "tools.graph.db._org_db_path",
        lambda slug, r=None: root / f"{slug}.db",
    )
    return orgs


def test_a_real_slug_is_still_discovered(orgs_dir):
    for slug in ("anchore", "autonomy", "blindhash", "dynbench"):
        (orgs_dir / f"{slug}.db").write_text("")
    assert sorted(fss.discover_org_sync_scopes()) == [
        "anchore", "autonomy", "blindhash", "dynbench",
    ]


@pytest.mark.parametrize("stem", UUID_STEMS)
def test_a_uuid_named_database_is_not_a_scope(stem, orgs_dir):
    (orgs_dir / "anchore.db").write_text("")
    (orgs_dir / f"{stem}.db").write_text("")
    scopes = fss.discover_org_sync_scopes()
    assert stem not in scopes, (
        f"{stem} was surfaced as an organization to synchronize; a uuid is not "
        "a slug and replicating into it creates a parallel database"
    )
    assert sorted(scopes) == ["anchore"], "the real org is unaffected"


def test_exactly_one_real_org_uuid_gets_through_the_old_slug_regex():
    """Pins WHY the explicit check was needed, so nobody reintroduces a
    leading-letter-only regex believing it covers uuids. One of four today —
    and which one is an accident of anchore's uuid beginning with a letter.

    The regex is written out here rather than imported because the module no
    longer has one: the site now uses `db.is_org_slug`, which is the whole
    point. This test documents what the replaced check did.
    """
    import re

    old_slug_re = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
    admitted = [stem for stem in UUID_STEMS if old_slug_re.match(stem)]
    assert admitted == ["c8e5cd04-8f19-4bc2-8951-a6b6b80b2699"], (
        "a leading-letter-only slug regex admits a uuid beginning with a "
        "letter; six of sixteen first-character classes do"
    )
    # And the predicate that replaced it rejects every one of them.
    from tools.graph.db import is_org_slug

    assert not any(is_org_slug(stem) for stem in UUID_STEMS)


@pytest.mark.parametrize("genesis", GENESIS_IDS)
def test_a_genesis_id_is_excluded_by_LENGTH_which_is_not_a_guard(genesis):
    """The second accident, asserted with its reason attached.

    A genesis id is 64 hex and the bound is 63, so it fails on length rather
    than on anything meaningful. Asserting only "rejected" would let the next
    person widen the bound to 64 for an unrelated reason and silently open the
    door — so the cause is pinned too.
    """
    import re

    from tools.graph.db import is_org_slug

    old_slug_re = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
    assert len(genesis) == 64
    assert not old_slug_re.match(genesis)
    # Not the character class: one character shorter and it would have passed.
    if genesis[0].isalpha():
        assert old_slug_re.match(genesis[:63])
    # The bound was the only thing doing the work — 63 in, 64 out.
    assert old_slug_re.match("a" * 63) and not old_slug_re.match("a" * 64)
    # The predicate that replaced it rejects a genesis id for its SHAPE, so the
    # exclusion no longer rests on that accident.
    assert not is_org_slug(genesis)


def test_the_none_stem_stays_excluded(orgs_dir):
    """The other ghost, still covered by _NON_SCOPE_STEMS."""
    for stem in ("None", "none", ""):
        (orgs_dir / f"{stem}.db").write_text("")
    (orgs_dir / "anchore.db").write_text("")
    assert sorted(fss.discover_org_sync_scopes()) == ["anchore"]
