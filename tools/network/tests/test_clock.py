"""One clock, named tolerances, and gates that fail in the right direction.

Acceptance for auto-e8gcn (design of record graph://21a0da9e-1c2, driver D5;
attested time graph://a8ae94f4-059). The settings store/refuse/resolve items
are proven here at FUNCTION level — the boundary (auto-wah16) and resolution
(auto-y2ubq) prove them end to end when they land on these gates.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from tools.network import clock

REPO_ROOT = Path(__file__).resolve().parents[3]

MIN = 60_000  # one minute, milliseconds
T = 1_755_500_000  # an attestation timestamp, seconds
NOW_MS = T * 1000


# -- accessors ----------------------------------------------------------------

def test_accessors_supply_the_wall_clock_only_when_nothing_is_injected():
    before = time.time()
    s = clock.now_s()
    ms = clock.now_ms()
    after = time.time()
    assert int(before) <= s <= int(after) + 1
    assert int(before * 1000) <= ms <= int(after * 1000) + 1


def test_accessors_return_the_injected_now_verbatim():
    assert clock.now_s(12345) == 12345
    assert clock.now_ms(12345) == 12345


def test_the_two_units_are_distinct():
    s = clock.now_s()
    ms = clock.now_ms()
    assert abs(ms - s * 1000) < 2000, "now_ms is milliseconds, now_s seconds"


# -- constants are defined once and imported ----------------------------------

def test_every_tolerance_is_owned_by_the_clock_module():
    from tools.network.ledger import store as ledger_store
    from tools.network.registry import (
        app,
        assertion,
        listings,
        relay,
        signing,
        turn_credentials,
    )
    from tools.network.relaykit import peer
    from tools.network.storagekit import delegate

    assert signing.MAX_CLOCK_SKEW is clock.MAX_CLOCK_SKEW
    assert peer.MAX_RELAY_SKEW is clock.MAX_RELAY_SKEW
    assert assertion.MAX_ASSERTION_TTL is clock.MAX_ASSERTION_TTL
    assert app.MAX_ATTESTATION_FUTURE_TS is clock.MAX_ATTESTATION_FUTURE_TS
    assert ledger_store.PENDING_CLAIM_TTL_MS is clock.PENDING_CLAIM_TTL_MS
    assert listings.MAX_ATTESTATION_TTL is clock.MAX_ATTESTATION_TTL
    assert relay.STREAM_EXPIRY_SECONDS is clock.STREAM_EXPIRY_SECONDS
    assert delegate.DEFAULT_DELEGATE_TTL_MS is clock.DEFAULT_DELEGATE_TTL_MS
    assert (turn_credentials.TURN_CREDENTIAL_TTL_SECONDS
            is clock.TURN_CREDENTIAL_TTL_SECONDS)
    assert (turn_credentials.TURN_ISSUANCE_WINDOW_SECONDS
            is clock.TURN_ISSUANCE_WINDOW_SECONDS)
    for name in ("DEFAULT_BINDING_TTL", "MIN_BINDING_TTL", "MAX_BINDING_TTL",
                 "DEFAULT_HINT_TTL", "MIN_HINT_TTL", "MAX_HINT_TTL",
                 "SESSION_TTL", "ANON_SESSION_TTL", "CHALLENGE_TTL"):
        assert getattr(app, name) is getattr(clock, name), name


TIME_GATE_NAME = re.compile(
    r"^(_?[A-Z][A-Z0-9_]*(?:TTL|SKEW|EXPIRY|WINDOW|ALLOWANCE)[A-Z0-9_]*)\s*=",
    re.M,
)


def test_no_module_outside_clock_defines_a_time_gate_constant():
    """The property, not the enumeration: 'every named tolerance lives in
    clock.py' is an ABSENCE claim, and a test listing the constants that
    moved certifies the claim while it silently rots. This scans the tree
    for any assignment whose name matches a time-gate pattern; the only
    permitted survivors are clock.py itself and the explicit, named
    exclusions clock.py declares — so a new gate constant added anywhere
    else fails here until it is either moved or visibly excluded."""
    offenders = []
    for path in sorted((REPO_ROOT / "tools" / "network").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if "/tests/" in rel or rel == "tools/network/clock.py":
            continue
        for match in TIME_GATE_NAME.finditer(path.read_text()):
            if (rel, match.group(1)) not in clock.DOMAIN_OWNED_EXCLUSIONS:
                offenders.append((rel, match.group(1)))
    assert offenders == [], (
        "time-gate constants defined outside tools/network/clock.py and not "
        f"in its DOMAIN_OWNED_EXCLUSIONS: {offenders}"
    )


TESTS_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+[\w.]*\btests\b", re.M,
)


def test_no_production_module_imports_from_a_tests_package():
    """Companion to the scan above, closing its one blind spot: the scan
    skips /tests/ paths (fixtures legitimately define constants), so a
    production module importing a gate constant FROM a tests package would
    keep the property green. This forbids the import edge itself — which is
    the stronger invariant anyway: production code depending on test code
    is a defect whatever name it imports."""
    offenders = []
    for path in sorted((REPO_ROOT / "tools" / "network").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if "/tests/" in rel:
            continue
        for match in TESTS_IMPORT.finditer(path.read_text()):
            offenders.append((rel, match.group(0).strip()))
    assert offenders == [], (
        f"production modules importing from a tests package: {offenders}"
    )


def test_registry_hello_freshness_is_unchanged():
    """Still ±300s, still two-sided — the consolidation moved no behavior."""
    assert clock.MAX_CLOCK_SKEW == 300
    assert clock.MAX_RELAY_SKEW == 300


def test_the_settings_constants_have_their_stated_values():
    assert clock.SETTINGS_PLAUSIBILITY_WINDOW_S == 30 * 60
    assert clock.SETTINGS_OFFLINE_WRITE_ALLOWANCE_S == 7 * 24 * 3600


# -- the offline-write allowance: a bound over signed bytes -------------------

def test_the_allowance_verdict_is_a_function_of_the_signed_bytes_alone():
    """Stores whose clocks differ by ten minutes return the same verdict —
    the check takes no clock at all, which is what makes refusing at the
    boundary safe."""
    at_limit = (T + clock.SETTINGS_OFFLINE_WRITE_ALLOWANCE_S) * 1000
    beyond = at_limit + 1
    # There is no `now` parameter to disagree about; assert the API shape
    # stays clock-free and the verdicts are stable across wall-clock changes.
    import inspect

    params = inspect.signature(clock.settings_claim_within_allowance).parameters
    assert "now" not in params
    for _ in range(2):
        assert clock.settings_claim_within_allowance(at_limit, T) is True
        assert clock.settings_claim_within_allowance(beyond, T) is False
        time.sleep(0.01)  # wall clock moved; verdicts cannot


def test_a_claim_within_the_allowance_stands_and_one_beyond_it_does_not():
    honest_offline = (T + 6 * 24 * 3600) * 1000  # six days past the citation
    too_far = (T + 8 * 24 * 3600) * 1000  # eight days: beyond W
    assert clock.settings_claim_within_allowance(honest_offline, T)
    assert not clock.settings_claim_within_allowance(too_far, T)


def test_recovery_does_not_depend_on_the_allowance_size():
    """An attacker citing t reaches t+W; a member citing any strictly newer
    attestation reaches further. Both grow by the same constant."""
    attacker_max = (T + clock.SETTINGS_OFFLINE_WRITE_ALLOWANCE_S) * 1000
    member_reach = ((T + 1) + clock.SETTINGS_OFFLINE_WRITE_ALLOWANCE_S) * 1000
    assert clock.settings_claim_within_allowance(attacker_max, T)
    assert member_reach > attacker_max
    assert clock.settings_claim_within_allowance(member_reach, T + 1)


# -- the plausibility window: deferred eligibility, not freshness -------------

def test_the_window_is_one_sided_and_the_past_is_always_plausible():
    an_hour_ago = NOW_MS - 60 * MIN
    a_year_ago = NOW_MS - 365 * 24 * 60 * MIN
    assert clock.settings_signed_at_is_plausible(an_hour_ago, now=NOW_MS)
    assert clock.settings_signed_at_is_plausible(a_year_ago, now=NOW_MS)


def test_29_minutes_ahead_resolves_and_31_does_not_yet():
    assert clock.settings_signed_at_is_plausible(NOW_MS + 29 * MIN, now=NOW_MS)
    assert not clock.settings_signed_at_is_plausible(NOW_MS + 31 * MIN, now=NOW_MS)


def test_an_implausible_row_becomes_plausible_by_the_clock_advancing():
    """Deferral, not refusal: nothing is re-ingested and nothing rewritten —
    the same value, asked again later, changes answer."""
    signed_at = NOW_MS + 31 * MIN
    assert not clock.settings_signed_at_is_plausible(signed_at, now=NOW_MS)
    assert clock.settings_signed_at_is_plausible(signed_at, now=NOW_MS + 2 * MIN)


def test_over_claiming_inside_the_allowance_gains_a_writer_nothing():
    """The interaction that puts both constants in one module: a writer may
    claim up to W beyond their attestation and the BOUNDARY will store it,
    but the RESOLUTION window refuses to resolve anything more than 30
    minutes ahead of the reader — so the over-claimed row waits, the honest
    concurrent write resolves now, and over-claiming buys standing only when
    real time reaches the claim (by which point the honest writer can simply
    write again, later than the leader)."""
    over_claim = (T + clock.SETTINGS_OFFLINE_WRITE_ALLOWANCE_S) * 1000
    honest = NOW_MS

    # Both survive the boundary: neither exceeds its citation's allowance.
    assert clock.settings_claim_within_allowance(over_claim, T)
    assert clock.settings_claim_within_allowance(honest, T)

    # At resolution, only the honest row is eligible now.
    plausible_now = [
        s for s in (over_claim, honest)
        if clock.settings_signed_at_is_plausible(s, now=NOW_MS)
    ]
    assert plausible_now == [honest]


# -- discipline: pure layers still never read the clock -----------------------

def _source(path: str) -> str:
    return (REPO_ROOT / path).read_text()


def test_fold_performs_no_wall_clock_read():
    src = _source("tools/network/ledger/fold.py")
    assert not re.search(r"time\.time\(|datetime\.(utc)?now", src)


def test_the_hlc_paths_use_the_named_milliseconds_accessor():
    src = _source("tools/network/ledger/store.py")
    assert "int(time.time() * 1000)" not in src
    assert "clock.now_ms(" in src


def test_no_call_site_redefines_a_moved_tolerance():
    for path, name in (
        ("tools/network/registry/signing.py", "MAX_CLOCK_SKEW"),
        ("tools/network/relaykit/peer.py", "MAX_RELAY_SKEW"),
        ("tools/network/registry/assertion.py", "MAX_ASSERTION_TTL"),
        ("tools/network/registry/app.py", "MAX_ATTESTATION_FUTURE_TS"),
        ("tools/network/ledger/store.py", "PENDING_CLAIM_TTL_MS"),
    ):
        assert not re.search(rf"^{name} = \d", _source(path), re.M), (
            f"{path} defines {name} inline; it must import it from clock"
        )
