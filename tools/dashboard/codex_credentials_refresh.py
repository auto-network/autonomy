"""Host-side OAuth refresh poller for ``dashboard.codex.credentials`` rows.

The launcher materializes each container's ``~/.codex/auth.json`` from a
``dashboard.codex.credentials`` substrate row and mounts it read-only, so
the in-container Codex CLI physically cannot write a refreshed token back.
Left alone, the ~10-day access/id tokens ride out their validity and then
expire for every running container at once. The host runs this tick to
keep every row's tokens ahead of expiry, exactly the way
``claude_credentials_refresh`` keeps the Claude consumer bundles fresh.

STEP 2 (the retire step) of the Codex credential end-state — bead
auto-l1h3f. Spec / parity reference: ``graph://73c4e9ef-bbc``
(``claude_credentials_refresh``). Proven refresh recipe: session
``df69566d-979`` (2026-05-31, headless, zero-browser).

Failure handling (mirrors the Claude poller):

* HTTP 401 / ``invalid_grant`` / ``refresh_token_reused`` /
  ``refresh_token_not_found`` → the refresh_token chain is broken and only
  an interactive ``codex login`` (then ``graph credentials import``) can
  fix it. We stamp ``last_refresh_error = "invalid_grant: …"`` on the row,
  leave the existing tokens untouched, and log a clear remediation line.
  Subsequent ticks skip the row until an operator-initiated re-import
  rotates it.
* HTTP 429 / network / timeout / a 2xx we can't parse → transient. Stamp
  a short ``last_refresh_error`` and try again next tick.

On success we always rotate the access/id/refresh triple (the endpoint
returns a fresh refresh_token because we request ``offline_access``),
recompute ``expires_at_ms`` from the new id_token's ``exp`` claim — the
same derivation ``credential_import`` uses — and clear
``last_refresh_error``.
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


# 6h between ticks. Token TTL is ~10 days; with a 3-day "needs refresh"
# threshold every active row lands in the refresh window and rotates
# roughly weekly — minimal churn, always well ahead of expiry.
CODEX_CREDENTIAL_REFRESH_POLL_INTERVAL = 6 * 60 * 60.0

# Refresh when remaining lifetime drops to or below 3 days.
CODEX_CREDENTIAL_FRESH_THRESHOLD_MS = 3 * 24 * 60 * 60 * 1000

# Sentinel prefix on ``last_refresh_error`` that means the refresh_token
# is gone. An operator-initiated ``graph credentials import`` overwrites
# the row through ``build_codex_payload`` (which writes clean, without
# ``last_refresh_error``), naturally clearing this state.
INVALID_GRANT_PREFIX = "invalid_grant"

# Error codes the token endpoint returns when the refresh chain is dead.
_REVOKED_ERROR_CODES = frozenset({
    "invalid_grant",
    "refresh_token_reused",
    "refresh_token_not_found",
    "refresh_token_expired",
})


@dataclass
class RefreshResult:
    """Outcome of one refresh attempt for a single credentials row."""

    kind: str  # "ok" | "revoked" | "transient"
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
    ``transient``}. A 2xx body that omits any of the rotated tokens, or
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
    if status == 401 or err_code in _REVOKED_ERROR_CODES:
        prefix = INVALID_GRANT_PREFIX
        msg = f"{prefix}: {short}" if short and short != prefix else prefix
        return RefreshResult(kind="revoked", error=msg)
    return RefreshResult(kind="transient", error=f"HTTP {status}: {short}")


def _row_payload(row: Any) -> dict[str, Any]:
    payload = getattr(row, "payload", None)
    return payload if isinstance(payload, dict) else {}


def _is_revoked(payload: dict[str, Any]) -> bool:
    err = payload.get("last_refresh_error")
    return isinstance(err, str) and err.startswith(INVALID_GRANT_PREFIX)


def _needs_refresh(payload: dict[str, Any], *, now_ms: int) -> bool:
    """``True`` when the row's tokens are within the refresh window.

    Missing / non-int ``expires_at_ms`` is treated as "needs refresh" so a
    row written by an older code path doesn't silently never refresh.
    """
    expires = payload.get("expires_at_ms")
    if isinstance(expires, bool) or not isinstance(expires, int):
        return True
    return (expires - now_ms) <= CODEX_CREDENTIAL_FRESH_THRESHOLD_MS


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


def refresh_credential_row(
    row: Any, *, org: str, now_ms: int, now_iso: str,
) -> RefreshResult | None:
    """Refresh ``row`` if it's stale; return the result or ``None`` when skipped.

    Skips when:
    * the row's ``last_refresh_error`` already marks it as
      ``invalid_grant`` (operator must re-import),
    * the row has plenty of TTL left (``expires_at_ms - now_ms > 3d``), or
    * the row is missing a usable ``refresh_token``.
    """
    payload = _row_payload(row)
    if _is_revoked(payload):
        return None
    if not _needs_refresh(payload, now_ms=now_ms):
        return None
    refresh_tok = payload.get("refresh_token")
    if not isinstance(refresh_tok, str) or not refresh_tok:
        return None
    who = payload.get("email") or row.key
    expires_at = payload.get("expires_at_ms")
    remaining_ms = (expires_at - now_ms) if isinstance(expires_at, int) else None
    logger.info(
        "codex credentials refresh: starting account=%s who=%r remaining_ms=%s",
        row.key, who, remaining_ms,
    )
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
    if result.kind == "revoked":
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

    Returns counters keyed by ``ok`` / ``revoked`` / ``transient`` /
    ``skipped``. Caller usually just logs the dict.
    """
    org = _credentials_org()
    counters: dict[str, int] = {
        "ok": 0, "revoked": 0, "transient": 0, "skipped": 0,
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
    return counters


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
            counters = await asyncio.to_thread(refresh_all_credentials)
            logger.info("codex credentials refresh tick: %s", counters)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("codex credentials refresh poller tick failed")
        await asyncio.sleep(CODEX_CREDENTIAL_REFRESH_POLL_INTERVAL)
