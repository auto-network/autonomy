"""Host-side OAuth refresh poller for ``dashboard.codex.credentials`` rows.

The launcher materializes each container's ``~/.codex/auth.json`` from a
``dashboard.codex.credentials`` substrate row and mounts it read-only, so
the in-container Codex CLI physically cannot write a refreshed token back.
Left alone, the row's stored refresh_token is never exercised host-side and
eventually ages past whatever opaque lifetime the endpoint enforces, at
which point every running container fails to authenticate at once. The host
runs this tick to keep every row's refresh_token rolling forward, in the
spirit of ``claude_credentials_refresh`` — but on a MEASURED cadence rather
than a token-exp threshold (see "what governs a refresh decision" below).

STEP 2 (the retire step) of the Codex credential end-state — bead
auto-l1h3f. Spec / parity reference: ``graph://73c4e9ef-bbc``
(``claude_credentials_refresh``). Proven refresh recipe: session
``df69566d-979`` (2026-05-31, headless, zero-browser).

Failure handling (mirrors the Claude poller):

* HTTP 401 / ``invalid_grant`` / ``refresh_token_not_found`` /
  ``refresh_token_expired`` → the refresh_token chain is broken and only
  an interactive ``codex login`` (then ``graph credentials import``) can
  fix it. We stamp ``last_refresh_error = "invalid_grant: …"`` on the row,
  leave the existing tokens untouched, and log a clear remediation line.
  Subsequent ticks skip the row until an operator-initiated re-import
  rotates it.
* ``refresh_token_reused`` / ``refresh_token_already_used`` (or an
  ``invalid_grant`` whose text says the token was already used) → the
  SUPERSEDED-TOKEN CANARY. See the rotation assumption below; this is a
  distinct, loudly-alarmed state, not a generic revocation.
* HTTP 429 / network / timeout / a 2xx we can't parse → transient. Stamp
  a short ``last_refresh_error`` and try again next tick.

On success we always rotate the access/id/refresh triple (the endpoint
returns a fresh refresh_token because we request ``offline_access``),
recompute ``expires_at_ms`` from the new id_token's ``exp`` claim — the
same derivation ``credential_import`` uses — and clear
``last_refresh_error``.

WHAT GOVERNS A REFRESH DECISION (and what does NOT). The id_token whose
``exp`` feeds ``expires_at_ms`` lives only ~60 MINUTES — measured from the
real token, not the ~10-day figure an earlier record assumed. Comparing
that 60-minute clock against any multi-day "needs refresh" threshold is
always true, so a threshold-on-``expires_at_ms`` design refreshes every
row on every tick for a reason that has nothing to do with the value that
actually predicts a launch failure. The number that matters — the
refresh_token's true lifetime — is opaque and unknowable. So this poller
decides from MEASURED STATE instead: the age since the last SUCCESSFUL
refresh (``last_refresh_at``) drives a deliberate rotation cadence, an
outstanding ``last_refresh_error`` drives an immediate retry, and the
FAILURE AGE (time since the last success while a row is erroring) is what
we alarm on — because that is the only measurable proxy for "this row is
drifting toward the cliff". ``expires_at_ms`` is still stored for
reference but is never the basis of a decision. See :func:`_decide_refresh`.

THE ROTATION ASSUMPTION THIS DESIGN LOAD-BEARS ON. Every successful
refresh rotates the refresh_token, and the host poller and every running
container refresh the SAME stored token independently. That only works
because OpenAI's rotation is currently GRACEFUL: the predecessor
refresh_token stays valid after a successor is minted (verified
2026-05-31 — refresh once, replay the old token, it still works). If
OpenAI moved to STRICT single-use rotation, the first party to refresh
would invalidate everyone else's copy and the fleet would fail
confusingly and all at once. That behaviour change would surface as the
token endpoint rejecting our stored token as already-used — which is
exactly the SUPERSEDED-TOKEN CANARY: a distinct :class:`RefreshResult`
kind, an ``ERROR`` alarm, and a named incident, so a vendor change reads
as one line in the log rather than a mysterious fleet-wide outage. See
:data:`_SUPERSEDED_ERROR_CODES` and :func:`_looks_superseded`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from tools.graph import ops as graph_ops
from tools.graph.codex_oauth import (
    CODEX_CLIENT_ID,
    CODEX_REFRESH_SCOPE,
    CODEX_TOKEN_URL,
    CODEX_USER_AGENT,
    id_token_exp_ms,
)
from tools.graph.schemas.codex_credentials import (
    CODEX_CREDENTIALS_REVISION,
    CODEX_CREDENTIALS_SET_ID,
)


logger = logging.getLogger(__name__)


# 6h between ticks. The cadence below is expressed in wall-clock age since
# the last SUCCESSFUL refresh, not in token lifetime (the id_token lives
# ~60 minutes and tells us nothing about when a refresh is due).
CODEX_CREDENTIAL_REFRESH_POLL_INTERVAL = 6 * 60 * 60.0

# Deliberate rotation cadence: refresh a healthy row once its last
# successful refresh is at least this old. Chosen well above one tick so a
# recently-refreshed row is genuinely skipped (the decision is measured,
# not the accidental every-tick churn the old expires_at threshold caused)
# yet far below any plausible refresh_token lifetime, keeping the chain
# rolling with margin. A row that is currently erroring ignores this and
# retries every tick — see :func:`_decide_refresh`.
CODEX_CREDENTIAL_REFRESH_INTERVAL_MS = 12 * 60 * 60 * 1000

# Failure-age alarm: when a row has been unable to refresh for at least
# this long (measured as now − last successful refresh, while an error is
# outstanding) we escalate to a loud log line. This is the only measurable
# proxy for "this credential is drifting toward a launch failure", since
# the refresh_token's true lifetime is unknowable.
CODEX_CREDENTIAL_FAILURE_AGE_ALARM_MS = 24 * 60 * 60 * 1000

# Sentinel prefix on ``last_refresh_error`` that means the refresh_token
# is gone. An operator-initiated ``graph credentials import`` overwrites
# the row through ``build_codex_payload`` (which writes clean, without
# ``last_refresh_error``), naturally clearing this state.
INVALID_GRANT_PREFIX = "invalid_grant"

# Sentinel prefix on ``last_refresh_error`` for the superseded-token
# canary — the stored refresh_token was rejected as already-used. Kept
# distinct from ``invalid_grant`` so the vendor-behaviour-change incident
# is never quietly folded into ordinary revocation.
SUPERSEDED_PREFIX = "superseded"

# Error codes the token endpoint returns when the refresh chain is dead
# and only an interactive re-login can recover it.
_REVOKED_ERROR_CODES = frozenset({
    "invalid_grant",
    "refresh_token_not_found",
    "refresh_token_expired",
})

# Error codes / phrases that mean our stored refresh_token was already
# consumed — the signature of strict single-use rotation. Under the
# graceful rotation this design assumes, we should NEVER see these; if we
# do, the rotation assumption has broken. See the module docstring.
_SUPERSEDED_ERROR_CODES = frozenset({
    "refresh_token_reused",
    "refresh_token_already_used",
})
_SUPERSEDED_PHRASES = ("reused", "already used", "already been used", "superseded")


@dataclass
class RefreshResult:
    """Outcome of one refresh attempt for a single credentials row."""

    kind: str  # "ok" | "revoked" | "superseded" | "transient"
    access_token: str | None = None
    refresh_token: str | None = None
    id_token: str | None = None
    expires_at_ms: int | None = None
    error: str | None = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_iso_ms(value: Any) -> int | None:
    """Parse an ISO-8601 stamp (``...Z`` or offset) into epoch ms, or ``None``.

    Used to age the fleet's standing failures for the per-tick log and to
    drive the measured refresh decision (:func:`_decide_refresh`). Anything
    unparseable collapses to ``None`` so the caller degrades gracefully —
    the log line reads "age unknown" and the decision treats the row as
    never successfully refreshed — rather than raising inside a poller tick.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _short_error(err: Any, *, limit: int = 160) -> str | None:
    """A single-line, length-capped form of ``last_refresh_error`` for logs."""
    if not isinstance(err, str) or not err:
        return None
    err = err.strip().replace("\n", " ")
    return err if len(err) <= limit else err[: limit - 1] + "…"


def _credentials_org() -> str:
    # Host-local, per-instance credentials live in personal.db, read with
    # peers=[] (never shared across orgs). Pinned to personal so the refresh
    # poller, launcher, and importer all converge on the same rows — mirror
    # of ``session_launcher._credentials_org`` and
    # ``credential_import.CREDENTIALS_ORG``.
    return "personal"


# ── HTTP shim ────────────────────────────────────────────────


def _post_refresh(refresh_token: str) -> tuple[int, dict[str, Any]]:
    """POST the refresh grant to the token endpoint; return ``(status, body)``.

    The ChatGPT OAuth token endpoint is a standard OAuth 2.0 token
    endpoint (RFC 6749 §6): the refresh grant is
    ``application/x-www-form-urlencoded``. On a captured ``HTTPError`` we
    still parse the error body so the caller can classify
    ``error`` / ``error_description``. Network errors propagate so the
    caller routes them as transient.

    Logs the request boundary with HTTP code + duration on every call.
    The refresh_token itself is never logged.
    """
    encoded = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CODEX_CLIENT_ID,
        "scope": CODEX_REFRESH_SCOPE,
    }).encode("utf-8")
    req = urllib.request.Request(
        CODEX_TOKEN_URL,
        data=encoded,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": CODEX_USER_AGENT,
        },
        method="POST",
    )
    logger.info(
        "codex credentials refresh: POST %s grant_type=refresh_token",
        CODEX_TOKEN_URL,
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body_bytes = resp.read()
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as exc:
        body_bytes = exc.read() or b""
        status = exc.code
    elapsed_ms = (time.monotonic() - started) * 1000
    try:
        body = json.loads(body_bytes.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    if 200 <= status < 300:
        logger.info(
            "codex credentials refresh: POST %s OK HTTP %d in %.1fms",
            CODEX_TOKEN_URL, status, elapsed_ms,
        )
    else:
        err_code = body.get("error") or ""
        err_desc = body.get("error_description") or ""
        logger.error(
            "codex credentials refresh: POST %s FAILED HTTP %d in %.1fms: %s",
            CODEX_TOKEN_URL, status, elapsed_ms,
            err_desc or err_code or repr(body_bytes[:200]),
        )
    return status, body


# ── Pure classification helpers ──────────────────────────────


def refresh_one(refresh_token: str) -> RefreshResult:
    """Hit the token endpoint with ``refresh_token``; classify the result.

    Returns a :class:`RefreshResult` with ``kind`` ∈ {``ok``, ``revoked``,
    ``superseded``, ``transient``}. A 2xx body that omits any of the
    rotated tokens, or
    whose new id_token carries no usable ``exp``, falls under ``transient``
    — we don't have enough to write a coherent row, but the next tick may
    succeed.
    """
    try:
        status, body = _post_refresh(refresh_token)
    except urllib.error.URLError as exc:
        return RefreshResult(kind="transient", error=f"network: {exc.reason}")
    except Exception as exc:  # noqa: BLE001 — surface unexpected as transient
        return RefreshResult(
            kind="transient",
            error=f"{type(exc).__name__}: {exc}",
        )
    if 200 <= status < 300:
        access = body.get("access_token")
        refresh = body.get("refresh_token")
        id_tok = body.get("id_token")
        if not isinstance(access, str) or not access:
            return RefreshResult(
                kind="transient", error="response missing access_token"
            )
        if not isinstance(refresh, str) or not refresh:
            # No rotated refresh_token means we requested (or were granted)
            # no offline_access — writing the old refresh_token back with a
            # new access_token would work once, but the chain is one refresh
            # from death. Treat as transient so we don't silently persist it.
            return RefreshResult(
                kind="transient", error="response missing refresh_token"
            )
        if not isinstance(id_tok, str) or not id_tok:
            return RefreshResult(
                kind="transient", error="response missing id_token"
            )
        exp_ms = id_token_exp_ms(id_tok)
        if exp_ms is None:
            return RefreshResult(
                kind="transient",
                error="response id_token missing/invalid exp",
            )
        return RefreshResult(
            kind="ok",
            access_token=access,
            refresh_token=refresh,
            id_token=id_tok,
            expires_at_ms=exp_ms,
        )
    err_code = str(body.get("error") or "")
    err_desc = str(body.get("error_description") or "")
    short = (err_desc or err_code or f"HTTP {status}").strip()
    if _looks_superseded(err_code, err_desc):
        # The canary: our stored refresh_token was already consumed. Prefix
        # distinctly so the row's stamped error routes to the superseded
        # skip/alarm path and never reads as an ordinary revocation.
        prefix = SUPERSEDED_PREFIX
        msg = f"{prefix}: {short}" if short and short != prefix else prefix
        return RefreshResult(kind="superseded", error=msg)
    if status == 401 or err_code in _REVOKED_ERROR_CODES:
        prefix = INVALID_GRANT_PREFIX
        msg = f"{prefix}: {short}" if short and short != prefix else prefix
        return RefreshResult(kind="revoked", error=msg)
    return RefreshResult(kind="transient", error=f"HTTP {status}: {short}")


def _looks_superseded(err_code: str, err_desc: str) -> bool:
    """``True`` when a token-endpoint error means our token was already used.

    Matches the explicit single-use error codes and, defensively, any
    ``error``/``error_description`` text that reads as "already used" —
    OpenAI could switch to strict rotation without minting a brand-new
    error code, and we would rather over-detect the canary than miss it.
    """
    if err_code in _SUPERSEDED_ERROR_CODES:
        return True
    text = f"{err_code} {err_desc}".lower()
    return any(phrase in text for phrase in _SUPERSEDED_PHRASES)


def _row_payload(row: Any) -> dict[str, Any]:
    payload = getattr(row, "payload", None)
    return payload if isinstance(payload, dict) else {}


def _is_revoked(payload: dict[str, Any]) -> bool:
    err = payload.get("last_refresh_error")
    return isinstance(err, str) and err.startswith(INVALID_GRANT_PREFIX)


def _is_superseded(payload: dict[str, Any]) -> bool:
    err = payload.get("last_refresh_error")
    return isinstance(err, str) and err.startswith(SUPERSEDED_PREFIX)


def _has_error(payload: dict[str, Any]) -> bool:
    err = payload.get("last_refresh_error")
    return isinstance(err, str) and bool(err)


def _age_since_refresh_ms(payload: dict[str, Any], *, now_ms: int) -> int | None:
    """Wall-clock age since the last successful refresh, or ``None``.

    ``None`` means the row has never recorded a successful refresh. Clamped
    at zero so a clock skew never reads as a negative age.
    """
    last_ms = _parse_iso_ms(payload.get("last_refresh_at"))
    if last_ms is None:
        return None
    return max(0, now_ms - last_ms)


def _failure_age_ms(payload: dict[str, Any], *, now_ms: int) -> int | None:
    """The failure age: age since last success while a row is erroring.

    Returns ``None`` when the row is not currently erroring (no failure to
    age) or when it has never refreshed successfully (age unknowable). This
    is the value :func:`_maybe_alarm_failure_age` escalates on — the only
    measurable proxy for "drifting toward a launch failure".
    """
    if not _has_error(payload):
        return None
    return _age_since_refresh_ms(payload, now_ms=now_ms)


@dataclass
class RefreshDecision:
    """Whether to refresh a row this tick, and the measured basis for it."""

    refresh: bool
    basis: str  # never_refreshed|retry_after_error|stale|recently_refreshed|
    #            revoked|superseded|no_refresh_token
    age_since_refresh_ms: int | None


def _decide_refresh(payload: dict[str, Any], *, now_ms: int) -> RefreshDecision:
    """Decide from MEASURED state — last successful refresh + error — not exp.

    Order matters: a dead or missing chain is skipped before cadence is
    considered; a row that is actively erroring (but not dead) retries every
    tick; an otherwise-healthy row rotates on the deliberate cadence and is
    genuinely skipped in between. ``expires_at_ms`` deliberately plays no
    part — the id_token clock it derives from is a ~60-minute value that
    never reflects when a refresh is actually due.
    """
    age = _age_since_refresh_ms(payload, now_ms=now_ms)
    refresh_tok = payload.get("refresh_token")
    if not isinstance(refresh_tok, str) or not refresh_tok:
        return RefreshDecision(False, "no_refresh_token", age)
    if _is_superseded(payload):
        return RefreshDecision(False, "superseded", age)
    if _is_revoked(payload):
        return RefreshDecision(False, "revoked", age)
    if age is None:
        return RefreshDecision(True, "never_refreshed", age)
    if _has_error(payload):
        return RefreshDecision(True, "retry_after_error", age)
    if age >= CODEX_CREDENTIAL_REFRESH_INTERVAL_MS:
        return RefreshDecision(True, "stale", age)
    return RefreshDecision(False, "recently_refreshed", age)


def _build_payload_after_refresh(
    *, base: dict[str, Any], result: RefreshResult, now_iso: str,
) -> dict[str, Any]:
    """Produce the upsert payload for a row given a refresh ``result``.

    Always starts from ``base`` (the existing row payload) so required
    fields (``auth_mode``, ``email``) survive. On success we rotate the
    access/id/refresh triple, advance ``expires_at_ms`` (from the new
    id_token) and ``last_refresh_at``, and drop ``last_refresh_error``. On
    failure we leave the tokens untouched and stamp the error.
    """
    payload = dict(base)
    if result.kind == "ok":
        assert (
            result.access_token
            and result.refresh_token
            and result.id_token
            and result.expires_at_ms is not None
        )
        payload["access_token"] = result.access_token
        payload["refresh_token"] = result.refresh_token
        payload["id_token"] = result.id_token
        payload["expires_at_ms"] = result.expires_at_ms
        payload["last_refresh_at"] = now_iso
        payload.pop("last_refresh_error", None)
    else:
        payload["last_refresh_error"] = result.error or "unknown"
    # Optional None fields round-trip, but prefer omission so rows stay tidy.
    if payload.get("email") is None:
        payload.pop("email", None)
    if payload.get("last_refresh_at") is None:
        payload.pop("last_refresh_at", None)
    if payload.get("last_refresh_error") is None:
        payload.pop("last_refresh_error", None)
    return payload


# ── Per-row + tick orchestration ─────────────────────────────


def _maybe_alarm_failure_age(
    row: Any, payload: dict[str, Any], *, now_ms: int, who: Any,
) -> None:
    """Escalate when a row has been unable to refresh for too long.

    The failure age — time since the last SUCCESSFUL refresh while an error
    is outstanding — is the only measurable predictor of a launch failure,
    so this alarm is what an operator watches, not the token exp. A row
    erroring but never yet refreshed (age unknowable) still alarms, since it
    is equally at risk once the imported refresh_token dies.
    """
    if not _has_error(payload):
        return
    err = _short_error(payload.get("last_refresh_error"))
    failure_age = _failure_age_ms(payload, now_ms=now_ms)
    if failure_age is None:
        logger.warning(
            "codex credentials refresh: ALARM account=%s who=%r has never "
            "refreshed successfully and is in error state (%s) — this row "
            "will fail at launch once its imported refresh token expires",
            row.key, who, err,
        )
    elif failure_age >= CODEX_CREDENTIAL_FAILURE_AGE_ALARM_MS:
        logger.error(
            "codex credentials refresh: ALARM account=%s who=%r has not "
            "refreshed successfully in %.1fh (error=%s) — the refresh token's "
            "true lifetime is unknowable; a launch failure is approaching",
            row.key, who, failure_age / 3_600_000.0, err,
        )


def refresh_credential_row(
    row: Any, *, org: str, now_ms: int, now_iso: str,
) -> RefreshResult | None:
    """Refresh ``row`` if measured state says it's due; ``None`` when skipped.

    The decision derives from :func:`_decide_refresh` (last successful
    refresh age + outstanding error), never from ``expires_at_ms``. Every
    tick logs the decision basis for the row, and the failure-age alarm
    fires whether or not we attempt a refresh — a dead chain we skip is
    exactly the row an operator most needs to hear about.
    """
    payload = _row_payload(row)
    who = payload.get("email") or row.key
    decision = _decide_refresh(payload, now_ms=now_ms)
    failure_age = _failure_age_ms(payload, now_ms=now_ms)

    # Per-tick decision basis — MEASURED state, greppable, one line per row.
    logger.info(
        "codex credentials refresh: account=%s who=%r decision=%s basis=%s "
        "last_refresh_age_ms=%s failure_age_ms=%s last_error=%r",
        row.key, who,
        "refresh" if decision.refresh else "skip",
        decision.basis,
        decision.age_since_refresh_ms, failure_age,
        _short_error(payload.get("last_refresh_error")),
    )

    # Alarm on failure age regardless of whether we act this tick.
    _maybe_alarm_failure_age(row, payload, now_ms=now_ms, who=who)

    if not decision.refresh:
        return None

    refresh_tok = payload.get("refresh_token")
    result = refresh_one(refresh_tok)
    if result.kind == "ok":
        logger.info(
            "codex credentials refresh: account=%s OK (new expires_at_ms=%s)",
            row.key, result.expires_at_ms,
        )
    new_payload = _build_payload_after_refresh(
        base=payload, result=result, now_iso=now_iso,
    )
    graph_ops.upsert_by_key(
        CODEX_CREDENTIALS_SET_ID,
        CODEX_CREDENTIALS_REVISION,
        row.key,
        new_payload,
        org=org,
    )
    if result.kind == "superseded":
        # THE CANARY. Our stored refresh_token was rejected as already-used,
        # which the graceful-rotation assumption says can't happen. Log it as
        # a named, ERROR-level incident so a vendor switch to strict
        # single-use rotation is one legible line, not a mystery outage.
        logger.error(
            "codex credentials refresh: SUPERSEDED-TOKEN CANARY account=%s "
            "who=%r — the token endpoint rejected our stored refresh token as "
            "already-used (%s). This is the graceful-rotation assumption "
            "failing: OpenAI appears to have moved to strict single-use "
            "refresh-token rotation, so the host poller and every running "
            "container now invalidate each other's tokens. This needs a "
            "design change, not a re-login.",
            row.key, who, result.error,
        )
    elif result.kind == "revoked":
        # upsert_by_key already wrote the error onto the row so
        # `graph set members dashboard.codex.credentials` surfaces it; the
        # WARNING gives an operator tailing the dashboard log the actionable
        # remediation in one line.
        logger.warning(
            "codex credentials refresh: invalid_grant for account=%s who=%r — "
            "re-run `codex login` then `graph credentials import`",
            row.key, who,
        )
    elif result.kind == "transient":
        logger.warning(
            "codex credentials refresh: transient failure for account=%s: %s",
            row.key, result.error,
        )
    return result


def refresh_all_credentials() -> dict[str, int]:
    """Iterate every ``dashboard.codex.credentials`` row and refresh as needed.

    Returns counters keyed by ``ok`` / ``revoked`` / ``superseded`` /
    ``transient`` / ``skipped``. Caller usually just logs the dict.
    """
    org = _credentials_org()
    counters: dict[str, int] = {
        "ok": 0, "revoked": 0, "superseded": 0, "transient": 0, "skipped": 0,
    }
    try:
        members = graph_ops.read_set(
            CODEX_CREDENTIALS_SET_ID, org=org, peers=[],
        )
    except Exception:
        logger.exception(
            "codex credentials refresh: read_set failed; tick aborted",
        )
        return counters
    rows = list(getattr(members, "members", []) or [])
    if not rows:
        # A Codex-enabled fleet whose credential surface is empty is NOT
        # healthy — every session falls back to an interactive sign-in with
        # no mounted auth. WARN (not INFO) so it stands out against the
        # steady tick, and name the remedy inline: a missed import must read
        # as an operator-actionable error, not a silent zero-row tick.
        logger.warning(
            "codex credentials refresh: Codex is enabled but the credentials "
            "surface holds ZERO rows — sessions will get sign-in prompts with "
            "no mounted auth. Remedy: run `graph credentials import`.",
        )
        return counters
    now_ms = _now_ms()
    now_iso = _now_iso()
    for row in rows:
        try:
            result = refresh_credential_row(
                row, org=org, now_ms=now_ms, now_iso=now_iso,
            )
        except Exception:
            logger.exception(
                "codex credentials refresh: row %r failed",
                getattr(row, "key", "?"),
            )
            counters["transient"] += 1
            continue
        if result is None:
            counters["skipped"] += 1
        else:
            counters[result.kind] = counters.get(result.kind, 0) + 1
    _log_tick_decision(rows=rows, counters=counters, now_ms=now_ms)
    return counters


def _log_tick_decision(
    *, rows: list[Any], counters: dict[str, int], now_ms: int,
) -> None:
    """Log the decision basis for one tick, not just the raw counters.

    States rows found, how many refreshed, and the age of the oldest
    standing failure (a row still carrying ``last_refresh_error``) so an
    operator tailing ``data/dashboard.log`` can read fleet health at a
    glance instead of guessing what four bare integers meant.
    """
    failing_ages_ms: list[int] = []
    failing = 0
    for row in rows:
        payload = _row_payload(row)
        if not payload.get("last_refresh_error"):
            continue
        failing += 1
        last_ok_ms = _parse_iso_ms(payload.get("last_refresh_at"))
        if last_ok_ms is not None:
            failing_ages_ms.append(now_ms - last_ok_ms)
    if not failing:
        failure_desc = "no standing failures"
    elif failing_ages_ms:
        oldest_h = max(failing_ages_ms) / 3_600_000
        failure_desc = (
            f"{failing} standing failure(s), oldest {oldest_h:.1f}h since "
            "last success"
        )
    else:
        failure_desc = f"{failing} standing failure(s), age unknown"
    logger.info(
        "codex credentials refresh tick: %d row(s) found, %d refreshed, "
        "%d revoked, %d superseded, %d transient, %d skipped; %s",
        len(rows), counters["ok"], counters["revoked"],
        counters.get("superseded", 0),
        counters["transient"], counters["skipped"], failure_desc,
    )


# ── Background poller ────────────────────────────────────────


def should_run_codex_credentials_refresh_poller() -> bool:
    """Mirror the Claude refresh poller's environment gate.

    Mock mode and pytest both skip — the write paths run inline in those
    environments via direct calls to :func:`refresh_all_credentials`.
    """
    if os.environ.get("DASHBOARD_MOCK"):
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return True


async def codex_credentials_refresh_poller() -> None:
    """Background asyncio task — fire :func:`refresh_all_credentials` every
    :data:`CODEX_CREDENTIAL_REFRESH_POLL_INTERVAL` seconds.

    Logs the first tick on entry and each tick's counters so operators can
    see the loop is alive in ``data/dashboard.log``.
    """
    logger.info(
        "codex credentials refresh poller started (interval=%ss)",
        CODEX_CREDENTIAL_REFRESH_POLL_INTERVAL,
    )
    while True:
        try:
            # refresh_all_credentials logs the per-tick decision basis
            # itself (rows found / refreshed / failure age), or WARNs on a
            # zero-row surface — no bare-counter echo here.
            await asyncio.to_thread(refresh_all_credentials)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("codex credentials refresh poller tick failed")
        await asyncio.sleep(CODEX_CREDENTIAL_REFRESH_POLL_INTERVAL)
