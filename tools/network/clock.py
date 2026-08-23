"""The system's clock and every time tolerance, in one place.

Time gates in this system fall into kinds with OPPOSING adversarial
directions, so a single shared tolerance would be wrong in at least two
directions. Every named tolerance lives here, grouped by kind; call sites
import them and never define one inline. The injection discipline is
unchanged: a pure function never reads the wall clock — the caller passes
``now``, and the accessors here are where a default comes from when no
caller supplied one.

The kinds
---------

**Freshness** — refuse on arrival. The adversary gains by claiming a time
they are not at. Registry envelope ``ts``, relay hello ``ts``, the witness
mint future bound. Two-sided where both parties are live.

**Deferred eligibility** — store now, defer to the reader's clock. Same
adversary as freshness, but the answer is to wait rather than refuse,
because refusing on arrival makes the outcome depend on one receiver's
clock at one instant and partitions the data permanently. A settings row's
``signed_at`` is the instance: stored whatever it claims (the boundary
separately bounds the claim against signed bytes — see the allowance
below), ignored at resolution while it is further ahead of the reader's
clock than the window, resolving once real time advances into range.

**Validity intervals** — certificate ``not_before``/``not_after``,
delegation TTL, the assertion window. A fast clock gains nothing; a SLOW
one keeps expired credentials alive.

**Expiry sweeps** — settings ``expires_at``, the pending-claim TTL. Late
is harmless. Per-record data; no shared constant exists today.

**Deliberate delays** — an action that must not take effect before a
stated instant. The adversary gains by making time appear to have PASSED —
the inverse of freshness. Nothing in the tree implements one today; the
first one defines its constant HERE rather than inventing its own home.

Not gates at all, and deliberately not here: elapsed-time measurement
(progress deadlines, rate limiting) belongs on the MONOTONIC clock, which
bounds no one's claim about wall time — one live site currently violates
this (``TurnCredentialIssuer`` ages its issuance quota against the same
wall clock that stamps ``expires_at``; defect ``auto-wsnvj``, not licence)
— and timestamp records (``created_at`` and friends) write the clock down
without gating on it.

Named exclusions — constants that MATCH a time-gate name pattern and stay
where they are, each for a stated reason (`DOMAIN_OWNED_EXCLUSIONS` below
is the machine-readable copy the property test enforces):

- ``relaykit/attachment_download.WINDOW`` / ``LAST_IN_WINDOW`` — flow
  control, measured in BYTES and a flag bit; not time.
- ``relaykit/connector._LIFECYCLE_LOG_WINDOW_S`` — diagnostic log
  bucketing; no adversary, nothing refused.
- ``tools/graph`` metrics windows (``_STATS_*_WINDOW_SECONDS``) — operator
  telemetry arithmetic; and ``CLAUDE_SETUP_TOKEN_TTL`` — a Setting-schema
  payload default owned by its schema, expressed as a ``timedelta`` in the
  schema's own vocabulary. Graph-side ADVERSARIAL gates (the settings
  window and allowance) consume this module's helpers instead.

Units
-----

Integer seconds everywhere, except the HLC-based ledger paths, which are
integer milliseconds because HLC is millisecond-based — and settings
``signed_at``, which is milliseconds for the same reason the HLC is: a
per-signer freshness floor that refuses "not strictly newer" needs
sub-second ordering. The two accessors below name the unit at the call
site instead of leaving a bare ``* 1000`` to say it.
"""

from __future__ import annotations

import time

# -- Accessors ----------------------------------------------------------------


def now_s(now: int | None = None) -> int:
    """Unix seconds — the supplied ``now`` if any, else the wall clock."""
    return int(time.time()) if now is None else int(now)


def now_ms(now: int | None = None) -> int:
    """Unix milliseconds (the HLC and settings ``signed_at`` unit)."""
    return int(time.time() * 1000) if now is None else int(now)


# -- Freshness gates ----------------------------------------------------------

#: Maximum tolerated |now - ts| between two live parties, seconds. Two-sided,
#: refused on arrival: it bounds replay of a signed request envelope or hello,
#: where both parties are present and a stale or future stamp buys the sender
#: nothing legitimate.
MAX_CLOCK_SKEW = 300

#: The relay hello's copy of the same bound. Named separately because relaykit
#: deliberately never imports registry; both import this module instead, which
#: is what makes the two values one value.
MAX_RELAY_SKEW = MAX_CLOCK_SKEW

#: An attestation minted further in the future than this is junk, not skew.
MAX_ATTESTATION_FUTURE_TS = MAX_CLOCK_SKEW

# -- Deferred-eligibility gates -----------------------------------------------

#: The settings resolution plausibility window, seconds. ONE-SIDED, applied at
#: RESOLUTION only, against the reader's own clock: a row claiming more than
#: this ahead is stored and ignored, and begins resolving once the clock
#: reaches it. A row from the past is always eligible — refusing the past
#: would refuse the offline member the design deliberately accommodates, and
#: refusing the future AT INGEST would leave stores holding different rows
#: according to their clock at the moment of arrival (graph://21a0da9e-1c2,
#: "The clock").
SETTINGS_PLAUSIBILITY_WINDOW_S = 30 * 60

#: The offline-write allowance, seconds: how far a settings ``signed_at`` may
#: exceed the ``t`` of the witness attestation it cites. NOT a clock gate —
#: the check reads signed bytes only, so every store returns the same verdict
#: and refusal at the boundary introduces no divergence (graph://a8ae94f4-059).
#: Sized to cover the longest legitimate offline writing period; no
#: adversarial pressure pulls it smaller, because an attacker's reach and the
#: member's grow by the same constant and recovery depends only on citing a
#: newer attestation. Provisional product value — retuning it is editing this
#: line. Coincidentally equal to PENDING_CLAIM_TTL_MS below; they are
#: unrelated, and retuning one must not touch the other.
SETTINGS_OFFLINE_WRITE_ALLOWANCE_S = 7 * 24 * 3600

# -- Validity intervals -------------------------------------------------------

#: Hard ceiling on the identity-ASSERTION validity window, seconds. Spec §7A:
#: the TTL is "seconds," and the QR challenge (§4.8) is ~60s; 120s leaves room
#: for clock skew between the minting dashboard and the registry while keeping
#: a stolen assertion useless within a breath. Not MAX_ATTESTATION_TTL below —
#: near-namesakes, unrelated instruments.
MAX_ASSERTION_TTL = 120

#: Hard ceiling on a listing ATTESTATION's declared validity, seconds (one
#: year). Not MAX_ASSERTION_TTL above.
MAX_ATTESTATION_TTL = 365 * 86_400

#: TURN relay credential lifetime, seconds. Long enough to carry an ICE
#: session, short enough that a leaked credential dies on its own.
TURN_CREDENTIAL_TTL_SECONDS = 15 * 60

#: Lifetime of the browser-minted, machine-signed Fleet process delegation.
#: Matched to the serve-cert at 30 days: the credential is PERSISTED (so it
#: survives a process restart) and opportunistically re-minted at unlock once
#: it is older than ~10 days, exactly like the tunnel serve-cert's renew path
#: (SERVE_CERT_RENEW_BELOW_DAYS). The old 12h value only made sense while it
#: lived solely in memory and died on every restart; persistence makes the
#: 30-day TTL both safe and the correct cadence (one unlock every ~20 days
#: keeps it fresh, and a restart re-loads the still-valid persisted cert
#: instead of demanding a fresh unlock).
FLEET_RUNTIME_DELEGATION_TTL_SECONDS = 30 * 24 * 60 * 60

#: Default lifetime of a storage delegate grant, milliseconds (the
#: key-control plane is HLC/millisecond-based).
DEFAULT_DELEGATE_TTL_MS = 12 * 60 * 60 * 1000

#: Registry binding TTLs, seconds (spec §4.2: default 30d).
DEFAULT_BINDING_TTL = 30 * 86_400
MIN_BINDING_TTL = 3_600
MAX_BINDING_TTL = DEFAULT_BINDING_TTL

#: Node reachability hint TTLs, seconds (spec §8). Short is the point: a
#: hint is a live-address claim, not a record.
DEFAULT_HINT_TTL = 3_600
MIN_HINT_TTL = 60
MAX_HINT_TTL = 86_400

#: Registry session lifetimes, seconds (§6.3), and the ~60s cross-device
#: challenge (§4.8).
SESSION_TTL = 24 * 3600
ANON_SESSION_TTL = 3600
CHALLENGE_TTL = 60

#: TURN issuance quota window, seconds — an ELAPSED-TIME quota, which by the
#: taxonomy above belongs on the monotonic clock; the issuer currently ages
#: it against the wall clock it also stamps expires_at with (auto-wsnvj).
#: The constant is a tolerance either way and lives here; the clock-source
#: fix belongs to that bead.
TURN_ISSUANCE_WINDOW_SECONDS = 60

# -- Expiry sweeps ------------------------------------------------------------

#: How long a staged member claim stays finalizable, measured from staging,
#: milliseconds (auto-cz4fb). Late enforcement is harmless — an expired row
#: surfaces as a distinct terminal state, never a silent pending. The origin
#: is the server wall clock at staging, not the client's HLC. Coincidentally
#: equal to SETTINGS_OFFLINE_WRITE_ALLOWANCE_S above; they are unrelated
#: (different kind, different unit), and retuning one must not touch the
#: other.
PENDING_CLAIM_TTL_MS = 7 * 24 * 60 * 60 * 1000

#: How long a relay stream offer waits unclaimed before the server sweeps
#: it, seconds. Late sweeping is harmless; the stream just lingers.
STREAM_EXPIRY_SECONDS = 60

# Settings ``expires_at`` and invite expiry are per-record data of this same
# kind; they carry their own values and take no shared constant.


# -- Named exclusions ---------------------------------------------------------

#: Constants whose NAMES match a time-gate pattern but which stay in their
#: own modules, each for the reason in the module docstring above. The
#: property test in tools/network/tests/test_clock.py enforces that any
#: OTHER match outside this module is a defect — this tuple is the single
#: allowlist, so an exclusion cannot be silent.
DOMAIN_OWNED_EXCLUSIONS = (
    ("tools/network/relaykit/attachment_download.py", "WINDOW"),
    ("tools/network/relaykit/attachment_download.py", "LAST_IN_WINDOW"),
    ("tools/network/relaykit/connector.py", "_LIFECYCLE_LOG_WINDOW_S"),
)

# -- Deliberate delays --------------------------------------------------------
# None implemented. The first one belongs here.


# -- Settings gate helpers ----------------------------------------------------


def settings_claim_within_allowance(signed_at_ms: int, attestation_t_s: int) -> bool:
    """Boundary bound: may ``signed_at`` stand, given the attestation it cites?

    A function of signed bytes alone — it reads no clock, so stores whose
    clocks disagree by any amount return the same verdict, which is what lets
    this refusal live at the boundary without partitioning the data. The
    attestation's ``t`` is seconds (the witness unit); ``signed_at`` is
    milliseconds. Callers verify the attestation itself separately; an
    organization that has never published cites none and takes no bound.
    """
    return signed_at_ms <= (attestation_t_s + SETTINGS_OFFLINE_WRITE_ALLOWANCE_S) * 1000


def settings_signed_at_is_plausible(signed_at_ms: int, now: int | None = None) -> bool:
    """Resolution filter: is this row's claim within the reader's window?

    One-sided: the past is always plausible. ``now`` is milliseconds,
    injected; a row failing this is SKIPPED at resolution, never refused,
    never rewritten, and becomes plausible by the clock advancing.
    """
    return signed_at_ms <= now_ms(now) + SETTINGS_PLAUSIBILITY_WINDOW_S * 1000
