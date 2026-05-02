"""Background scanner for session worktrees.

Caches ``agents.workspace_manager.scan_all_worktrees()`` results for the
dashboard and refreshes them periodically. Git operations are blocking, so
refreshes run in a worker thread.

For live rows, refresh also fetches the ``autonomy/github`` capability's
``source_control`` snapshot in parallel and caches it so HTTP handlers
can compose row + capability data without fanning out N gh calls per
GET. Capability fetches are best-effort: a row's local state survives
even when its capability lookup fails.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from agents.capabilities.github import probe as github_probe
from agents.capabilities.github.service import (
    FAILURE_NOT_MODIFIED,
    FAILURE_RATE_LIMITED,
    WorktreeGithubExecResult,
    derive_repo_slug,
    normalize_review_payload,
    parse_check_runs_response,
    parse_pull_response,
    source_control_check_runs_read_for_sha_v1,
    source_control_review_read_by_id_v1,
    source_control_review_read_v1,
)
from agents.workspace_manager import WorktreeState, scan_all_worktrees
from tools.graph import settings_ops
from tools.graph.schemas.source_control_review_state import (
    SCHEMA_REVISION as REVIEW_STATE_REVISION,
    SET_ID as REVIEW_STATE_SET_ID,
    SourceControlReviewStateV1,
)
from tools.graph.schemas.worktree_review_binding import (
    SET_ID as REVIEW_BINDING_SET_ID,
    parse_binding_key,
)

logger = logging.getLogger(__name__)


# Per-row source_control snapshot. Shape mirrors the row composition spec
# (auto-4ze9o §source_control): a ready row carries ``review`` (which may
# itself be ``None`` when the branch has no PR); a degraded/unavailable
# row carries ``reason`` and ``None`` review so the UI can render a
# stable card without faking PR data.
_GITHUB_PROBE_TIMEOUT = 3.0
_GITHUB_REVIEW_TIMEOUT = 10.0

# Background fetch policy for source_control snapshots.
#
# The 30s scan loop is right for derivable local-git signals (graph://
# c64d0f5d-480 § Why we must become stateful) but should NOT make any
# external-network calls in the background. Per Jeremy's directive
# (2026-04-30): the only conceivable polling is for an active running
# PR in non-terminal state, and even that requires a safety valve.
#
# So the rules below are deliberately conservative:
#   - Silent (NAG_SILENT) rows: never fetch in background. Period.
#   - Watch-active rows: only fetch when the cached snapshot's
#     review.running flag is True AND the row is within its hourly
#     poll budget AND its watch TTL has elapsed.
#   - Force-refresh (POST /api/worktrees/refresh): always fetches,
#     bypasses TTL and budget. This is the operator-explicit path.
#
# Rate-limit backoff still applies as a circuit breaker on top of the
# above — when gh reports rate-limited, we suspend ALL fetches
# (including operator-triggered) for a window so we don't deepen the
# hole. Force-refresh that returns clean clears the backoff.
SOURCE_CONTROL_WATCH_TTL_SECONDS = 60.0
RATE_LIMIT_BACKOFF_SECONDS = 600.0
# Hard ceiling on background polls per (session, repo) per rolling
# hour. ~1/min average upper bound; in practice the running-gate
# means most rows poll far less. Stuck PRs (e.g. CI hangs) hit this
# cap and stop polling instead of draining quota indefinitely.
MAX_POLLS_PER_HOUR_PER_ROW = 60
_POLL_WINDOW_SECONDS = 3600.0


# Nag modes for the source_control.watch block. The UI maps these onto
# Silent / Nag All Changes / Nag When Done buttons (settled design
# 3435e03f). Persistence is in-memory on the monitor for v1; CrossTalk
# delivery (auto-f2quy-style) is a separate follow-up bead.
NAG_SILENT = "silent"
NAG_ALL_CHANGES = "nag_all"
NAG_WHEN_DONE = "nag_done"
NAG_MODES = frozenset({NAG_SILENT, NAG_ALL_CHANGES, NAG_WHEN_DONE})
NAG_DEFAULT = NAG_SILENT
# Hard time-limit on every nag request. Per Jeremy (2026-04-30): "A
# request to watch / nag should always be time limited. You can never
# request infinite nags." Default is 1 hour; the API can shorten it
# but cannot extend past NAG_MAX_DURATION_SECONDS so a stuck or
# forgotten nag mode auto-reverts to silent.
NAG_DEFAULT_DURATION_SECONDS = 3600.0   # 1 hour by default
NAG_MAX_DURATION_SECONDS = 14400.0      # 4-hour absolute ceiling


def _degraded_snapshot(
    *,
    state: str,
    reason: str | None,
    watch_mode: str = NAG_DEFAULT,
    details: dict | None = None,
) -> dict:
    snapshot = {
        "state": state,
        "implementation": "autonomy/github",
        "reason": reason,
        # Always plural — UI renders zero, one, or many reviews per row.
        # Keeping ``reviews`` (list) and ``review`` (first-or-None
        # back-compat alias) avoids a flag day for every consumer that
        # still reads the singular field; the alias drops in a follow-up.
        "reviews": [],
        "review": None,
        "watch": {"mode": watch_mode},
    }
    if details:
        # Only emit non-empty details so the snapshot stays compact when
        # the failure mode doesn't carry extra context.
        snapshot["details"] = details
    return snapshot


def _ready_snapshot(
    reviews: list[dict],
    *,
    watch_mode: str,
    stale: bool = False,
) -> dict:
    """Compose a ``state=ready`` snapshot with a (possibly empty) list of reviews.

    ``stale=True`` flags the whole snapshot when the resolver couldn't
    confirm freshness against the live container (e.g. the bindings
    were never refreshed and the cache is empty). The UI surfaces this
    as a "stale, refresh required" indicator.
    """
    snapshot: dict = {
        "state": "ready",
        "implementation": "autonomy/github",
        "reason": None,
        "reviews": list(reviews),
        # Back-compat alias — the first review (or None) under the
        # singular ``review`` field so legacy callers keep rendering
        # one PR while the UI migration to ``reviews`` plural rolls out.
        "review": reviews[0] if reviews else None,
        "watch": {"mode": watch_mode},
    }
    if stale:
        snapshot["stale"] = True
    return snapshot


# ── Binding-driven composition ───────────────────────────────────────


def _read_bindings(row: WorktreeState) -> list:
    """Read bindings for a row by ``<session>:<repo>:<branch>`` prefix.

    Returns the raw ``ResolvedSetting`` list — caller owns key parsing
    and payload composition. Always returns an empty list (never None)
    so the call site can branch on truthiness.
    """
    if not row.branch:
        return []
    prefix = f"{row.session_name}:{row.repo_name}:{row.branch}"
    members = settings_ops.read_set(
        REVIEW_BINDING_SET_ID,
        prefix=prefix,
        org="autonomy",
    )
    # Deterministic per-review ordering matters for stacked rows because
    # the UI falls back to the first review when no explicit review_id is
    # supplied (e.g. legacy single-PR overlay entry points).
    return sorted(members.members, key=lambda member: member.key)


def _read_review_state_cache(repo_slug: str | None) -> dict[str, dict]:
    """Read the per-repo cache map ``{key: payload}`` for ``repo_slug``.

    ``repo_slug`` is the ``<owner>/<repo>`` form derived from the
    worktree's managed clone. Empty / None returns an empty dict so
    callers don't have to special-case the boundary.
    """
    if not repo_slug:
        return {}
    members = settings_ops.read_set(
        REVIEW_STATE_SET_ID,
        prefix=repo_slug,
        org="autonomy",
    )
    return {m.key: m.payload for m in members.members}


def _compose_review_payload(
    *,
    repo_slug: str,
    review_id: str,
    binding_payload: dict,
    cache: dict | None,
) -> dict:
    """Synthesize the per-review block the UI renders.

    Mirrors :class:`agents.capabilities.github.service.ReviewPayload`
    so the JS consumer doesn't need a second adapter. When the cache is
    missing for this binding, returns a stub flagged ``stale=True`` so
    the UI shows "refresh required" rather than crashing.

    ``commit_shas`` is intentionally empty — the cache is provider
    state, not local git state. Per-PR diff scoping uses
    ``binding.base_sha..cache.head_sha`` directly.
    """
    base_sha = binding_payload.get("base_sha", "") or ""
    if cache is None:
        # Operator declared the binding but no fetch has populated the
        # cache yet. Stub with the binding's base_sha + a stale flag so
        # the UI knows to nudge the operator toward Refresh.
        return {
            "number": int(review_id) if review_id.isdigit() else None,
            "review_id": review_id,
            "url": "",
            "title": "",
            "body": "",
            "head_sha": "",
            "base_sha": base_sha,
            "base_branch": "",
            "state": "open",
            "is_draft": False,
            "aggregate_state": "yellow",
            "running": False,
            "checks": [],
            "commit_shas": [],
            "stale": True,
        }

    checks = list(cache.get("checks") or [])
    # The cache deliberately omits per-check ``icon``; render-time
    # derivation lives in JS (``_iconFromLabel``). We surface the same
    # vocabulary the legacy normalizer does, so the existing tooltip
    # disc styling keeps working.
    running = any(
        (c.get("status") in ("running", "pending"))
        for c in checks
    )
    has_failure = any((c.get("status") == "fail") for c in checks)
    aggregate_state = "yellow" if has_failure else "green"

    return {
        "number": int(review_id) if review_id.isdigit() else None,
        "review_id": review_id,
        "url": cache.get("url") or "",
        "title": cache.get("title") or "",
        "body": cache.get("body") or "",
        "head_sha": cache.get("head_sha") or "",
        # Per-PR scoping — the binding's base_sha overrides the cache's
        # base_sha so stacked PRs diff correctly even when the
        # provider's reported ``base.sha`` reflects the integration
        # branch instead of the previous PR's head.
        "base_sha": base_sha or (cache.get("base_sha") or ""),
        "base_branch": cache.get("base_branch") or "",
        "state": cache.get("state") or "open",
        "is_draft": bool(cache.get("is_draft")),
        "aggregate_state": aggregate_state,
        "running": running,
        "checks": checks,
        "commit_shas": [],
    }


def _compose_bound_snapshot(
    row: WorktreeState,
    *,
    bindings: list,
    watch_mode: str,
) -> dict:
    """Compose a ``ready`` snapshot from bindings + cache. Zero gh calls.

    The list order matches the bindings' key order
    (sorted by ``read_set``), which gives deterministic output when
    the operator declared a stack in order.
    """
    repo_slug = derive_repo_slug(row.managed_clone) or ""
    cache_map = _read_review_state_cache(repo_slug)
    reviews: list[dict] = []
    any_stale = False
    for binding in bindings:
        try:
            _, _, _, review_id = parse_binding_key(binding.key)
        except ValueError:
            # Drop malformed binding keys — log so an operator can spot
            # the drift, but don't poison the snapshot.
            logger.warning(
                "worktree_monitor: malformed binding key %r — skipping",
                binding.key,
            )
            continue
        cache_key = f"{repo_slug}:{review_id}" if repo_slug else None
        cache = cache_map.get(cache_key) if cache_key else None
        payload = _compose_review_payload(
            repo_slug=repo_slug,
            review_id=review_id,
            binding_payload=binding.payload or {},
            cache=cache,
        )
        if payload.get("stale"):
            any_stale = True
        reviews.append(payload)
    return _ready_snapshot(reviews, watch_mode=watch_mode, stale=any_stale)


def _details_from_op_result(op_result: WorktreeGithubExecResult) -> dict:
    """Pull operator-actionable diagnostics out of a failed gh op.

    ``exec_failed`` and similar non-auth failures are otherwise opaque
    via ``/api/worktrees`` — the operator gets the canonical reason
    code but no clue *why* gh didn't like the call. Surfacing the
    truncated stderr (and the gh argv tail) closes the loop without
    forcing host-side docker access for diagnosis.
    """
    details: dict = {}
    if op_result.error_message:
        details["error"] = op_result.error_message
    stderr = (op_result.stderr or "").strip()
    if stderr:
        details["stderr"] = stderr[:600]
    if op_result.exit_code:
        details["exit_code"] = op_result.exit_code
    if op_result.command:
        # Drop the docker exec prefix so the relevant gh argv reads
        # cleanly. Keep paths/refs intact for debug.
        cmd = op_result.command
        for marker in ("gh", "docker"):
            if marker in cmd:
                cmd = cmd[cmd.index(marker):]
                break
        details["command"] = cmd
    return details


async def _fetch_source_control(
    row: WorktreeState,
    all_rows: list[WorktreeState],
    *,
    watch_mode: str = NAG_DEFAULT,
) -> dict:
    """Probe + read the row's source_control snapshot for one live row.

    Binding-driven first: if the operator/agent declared one or more
    review bindings for this row, the snapshot is composed from the
    cached review_state + bindings with **zero** gh calls. Falls back
    to the legacy ``gh pr list --head <branch>`` auto-detect path when
    no bindings exist — preserves the no-config experience for
    operators who haven't adopted the binding flow yet.
    """
    bindings = _read_bindings(row)
    if bindings:
        # Cache-only path — never touches gh on a background tick.
        # The probe is intentionally skipped: cache reads don't depend
        # on the live container being up. ``refresh_one`` does the
        # network work and arms the cache.
        return _compose_bound_snapshot(
            row, bindings=bindings, watch_mode=watch_mode,
        )

    probe_result = await github_probe.probe_v1(
        row.session_name,
        timeout=int(_GITHUB_PROBE_TIMEOUT),
    )
    if probe_result.state != github_probe.STATE_READY:
        return _degraded_snapshot(
            state=probe_result.state,
            reason=probe_result.reason,
            watch_mode=watch_mode,
        )

    if not row.branch:
        # Probe ready but the worktree has no checked-out branch — we
        # can't ask gh for review state. Surface as degraded with a
        # specific reason rather than pretending review is null.
        return _degraded_snapshot(state="degraded", reason="no_branch", watch_mode=watch_mode)

    op_result: WorktreeGithubExecResult = await source_control_review_read_v1(
        row.session_name,
        row.repo_name,
        rows=all_rows,
        timeout=int(_GITHUB_REVIEW_TIMEOUT),
    )
    if not op_result.ok:
        return _degraded_snapshot(
            state="degraded",
            reason=op_result.failure,
            watch_mode=watch_mode,
            details=_details_from_op_result(op_result),
        )

    review = normalize_review_payload(op_result.stdout)
    review_dict = review.to_dict() if review is not None else None
    reviews = [review_dict] if review_dict is not None else []
    return _ready_snapshot(reviews, watch_mode=watch_mode)


# ── Operator-explicit force-fetch over bindings ──────────────────────


async def _refresh_bindings_via_rest(
    row: WorktreeState,
    all_rows: list[WorktreeState],
    bindings: list,
    *,
    watch_mode: str,
) -> tuple[dict, bool]:
    """Walk bindings, fetch each via REST, write the cache, and recompose.

    Returns ``(snapshot, rate_limited_seen)``. The caller arms the
    rate-limit backoff on True and recomposes from cache regardless.
    """
    repo_slug = derive_repo_slug(row.managed_clone) or ""
    cache_map = _read_review_state_cache(repo_slug)
    rate_limited = False

    for binding in bindings:
        try:
            _, _, _, review_id = parse_binding_key(binding.key)
        except ValueError:
            logger.warning(
                "worktree_monitor: malformed binding key %r — skipping",
                binding.key,
            )
            continue
        cache_key = f"{repo_slug}:{review_id}"
        cached = cache_map.get(cache_key)
        cached_etag = (cached or {}).get("etag")

        rest = await source_control_review_read_by_id_v1(
            row.session_name,
            row.repo_name,
            review_id=review_id,
            rows=all_rows,
            repo_slug=repo_slug or None,
            etag=cached_etag,
            timeout=int(_GITHUB_REVIEW_TIMEOUT),
        )
        if rest.failure == FAILURE_NOT_MODIFIED:
            # Cache still fresh — touch ``updated_at`` by re-writing the
            # same payload. Skip when there's no cache to refresh.
            if cached is not None:
                try:
                    settings_ops.add_setting(
                        REVIEW_STATE_SET_ID, REVIEW_STATE_REVISION,
                        cache_key, cached, org="autonomy",
                    )
                except Exception:
                    logger.warning(
                        "worktree_monitor: cache touch failed for %s",
                        cache_key, exc_info=True,
                    )
            continue
        if rest.failure == FAILURE_RATE_LIMITED:
            rate_limited = True
            # Leave the cached row alone — caller will surface degraded
            # state in the snapshot if everything was rate-limited.
            continue
        if not rest.ok:
            # Other failures: keep cache, log, move on.
            logger.warning(
                "worktree_monitor: REST review fetch failed for %s/%s "
                "review=%s failure=%s",
                row.session_name, row.repo_name, review_id, rest.failure,
            )
            continue

        parsed = parse_pull_response(rest.stdout, etag=cached_etag)
        head_sha = parsed.get("head_sha") or ""
        checks: list[dict] = []
        if head_sha:
            checks_rest = await source_control_check_runs_read_for_sha_v1(
                row.session_name,
                row.repo_name,
                head_sha=head_sha,
                rows=all_rows,
                repo_slug=repo_slug or None,
                timeout=int(_GITHUB_REVIEW_TIMEOUT),
            )
            if checks_rest.ok:
                checks = parse_check_runs_response(checks_rest.stdout)
            else:
                # Carry old checks forward when the check-runs sub-fetch
                # fails — partial freshness beats no checks at all.
                checks = list((cached or {}).get("checks") or [])

        payload: dict = {
            "title": parsed["title"],
            "body": parsed["body"],
            "state": parsed["state"],
            "head_sha": parsed["head_sha"],
            "base_sha": parsed["base_sha"],
            "base_branch": parsed["base_branch"],
            "is_draft": parsed["is_draft"],
            "provider": "github",
            "checks": checks,
        }
        if parsed.get("etag"):
            payload["etag"] = parsed["etag"]
        if parsed.get("url"):
            payload["url"] = parsed["url"]
        try:
            settings_ops.add_setting(
                REVIEW_STATE_SET_ID, REVIEW_STATE_REVISION,
                cache_key, payload, org="autonomy",
            )
        except Exception:
            logger.warning(
                "worktree_monitor: cache write failed for %s",
                cache_key, exc_info=True,
            )

    snapshot = _compose_bound_snapshot(
        row, bindings=bindings, watch_mode=watch_mode,
    )
    return snapshot, rate_limited


class WorktreeMonitor:
    """Polling cache for session worktree state + per-row capability data."""

    def __init__(self, *, interval_seconds: float = 30.0) -> None:
        self._interval_seconds = interval_seconds
        self._cache: list[WorktreeState] = []
        self._source_control_cache: dict[tuple[str, str], dict] = {}
        # Per-row last-fetch monotonic timestamp (used for the
        # watch-active TTL gate).
        self._source_control_fetched_at: dict[tuple[str, str], float] = {}
        # Per-row deque of recent poll timestamps (sliding-window
        # budget — graph://c64d0f5d-480 § safety valve discussion).
        self._poll_history: dict[tuple[str, str], deque[float]] = {}
        # Monotonic-clock instant before which we skip every capability
        # fetch — set when gh reports rate-limited, cleared on the next
        # successful operator-forced refresh that actually probes.
        self._capability_backoff_until: float = 0.0
        # Per-row (mode, expiry_monotonic) — silent has no entry.
        self._nag_modes: dict[tuple[str, str], tuple[str, float]] = {}
        self._task: asyncio.Task | None = None
        self._lock: asyncio.Lock | None = None
        self._started = False

    def get_all(self) -> list[WorktreeState]:
        """Return a snapshot of cached worktree state."""
        return list(self._cache)

    def get_source_control(self, session_name: str, repo_name: str) -> dict | None:
        """Return the cached source_control snapshot for a row, if any."""
        return self._source_control_cache.get((session_name, repo_name))

    def get_nag_mode(self, session_name: str, repo_name: str) -> str:
        """Return the live nag mode for a row.

        ``silent`` is returned both when no row entry exists and when
        the row's nag mode has expired — there's no concept of an
        "infinite" nag request (per Jeremy 2026-04-30). Callers don't
        have to know about expiry; the timer is invisible to the
        polling decision tree.
        """
        entry = self._nag_modes.get((session_name, repo_name))
        if entry is None:
            return NAG_DEFAULT
        mode, expiry = entry
        if time.monotonic() >= expiry:
            return NAG_DEFAULT
        return mode

    def get_nag_expiry_remaining(self, session_name: str, repo_name: str) -> float:
        """Seconds remaining on the row's nag mode (0.0 if silent/expired).

        UI surfaces this as a countdown so the operator knows when the
        nag will lapse and can re-arm if they still want to be paged.
        """
        entry = self._nag_modes.get((session_name, repo_name))
        if entry is None:
            return 0.0
        _mode, expiry = entry
        return max(0.0, expiry - time.monotonic())

    def set_nag_mode(
        self,
        session_name: str,
        repo_name: str,
        mode: str,
        *,
        duration_seconds: float | None = None,
    ) -> str:
        """Persist a nag mode for a row with a hard time limit.

        Per Jeremy (2026-04-30) every watch request is time-limited —
        an operator can never request infinite nags. ``mode=silent``
        clears the entry. Other modes record an expiry monotonic
        instant; ``duration_seconds`` defaults to
        :data:`NAG_DEFAULT_DURATION_SECONDS` and is clamped down to
        :data:`NAG_MAX_DURATION_SECONDS`. The poll decision tree then
        treats an expired entry exactly as silent — no further fetches.

        Updates the cached source_control snapshot in place when
        present so the next ``GET /api/worktrees`` reflects the new
        mode + remaining duration without waiting on the 30s loop.
        """
        if mode not in NAG_MODES:
            raise ValueError(
                f"invalid nag mode: {mode!r}; expected one of {sorted(NAG_MODES)}"
            )
        key = (session_name, repo_name)

        if mode == NAG_SILENT:
            # Silent doesn't need a timer — clear the entry so
            # get_nag_mode + the watch block both reflect plain silent.
            self._nag_modes.pop(key, None)
            cached = self._source_control_cache.get(key)
            if cached is not None:
                cached["watch"] = {"mode": NAG_SILENT}
            return mode

        if duration_seconds is None:
            duration_seconds = NAG_DEFAULT_DURATION_SECONDS
        if duration_seconds <= 0:
            raise ValueError(
                f"invalid nag duration: {duration_seconds!r}; must be > 0"
            )
        # Clamp to the absolute ceiling — even an explicit longer
        # request gets capped here so the safety guarantee holds.
        duration_seconds = min(float(duration_seconds), NAG_MAX_DURATION_SECONDS)
        expiry = time.monotonic() + duration_seconds
        self._nag_modes[key] = (mode, expiry)

        cached = self._source_control_cache.get(key)
        if cached is not None:
            cached["watch"] = {
                "mode": mode,
                "expires_in_seconds": int(duration_seconds),
            }
        return mode

    async def refresh(self, *, force_capabilities: bool = False) -> list[WorktreeState]:
        """Force a scan and replace the cache.

        ``force_capabilities=True`` bypasses the per-row TTL and any
        active rate-limit back-off for EVERY live row — fan-out
        capability fetch. Today's only callers are tests; the
        operator-facing top-level refresh now passes the default
        (local-git only) and uses :meth:`refresh_one` for scoped
        operator-explicit fetches.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            rows = await asyncio.to_thread(scan_all_worktrees)
            self._cache = rows
            await self._refresh_source_control(
                rows, force_capabilities=force_capabilities,
            )
            return list(rows)

    async def refresh_one(
        self, session_name: str, repo_name: str,
    ) -> list[WorktreeState]:
        """Force-refresh a single row's source_control snapshot.

        The operator-explicit force-GET path: scoped to one
        ``(session, repo)``. Re-runs the local-git scan (for
        consistency with the top-level refresh) and then fetches
        fresh capability data for ONLY the target row, bypassing TTL
        + per-row poll budget. The rate-limit backoff is still
        respected — refusing to re-fetch while the limit is hot
        would be the wrong move only if the operator could parse it
        themselves; the backoff already protects against deepening
        the hole.

        When the row isn't found / isn't live, the local-git scan
        still runs and the cache is updated, but no capability
        fetch happens. Returns the full row list so the caller can
        serve the updated snapshot.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            rows = await asyncio.to_thread(scan_all_worktrees)
            self._cache = rows
            target = next(
                (
                    r for r in rows
                    if r.session_name == session_name
                    and r.repo_name == repo_name
                    and r.session_live
                ),
                None,
            )
            if target is not None:
                await self._refresh_one_source_control(target, rows)
            return list(rows)

    async def _refresh_one_source_control(
        self, row: WorktreeState, all_rows: list[WorktreeState],
    ) -> None:
        """Single-row source_control fetch for the operator-explicit path.

        Bypasses TTL + poll budget but still respects rate-limit
        backoff. Updates the cache + fetched_at + poll_history in
        place; arms or clears the backoff based on the response.

        When the row has one or more declared bindings, REST-by-id
        ops drive the refresh (separate quota from GraphQL, ETag 304s
        are free). When no bindings exist, falls back to the legacy
        ``gh pr list`` auto-detect path so unbound rows still refresh.
        """
        key = (row.session_name, row.repo_name)
        now = time.monotonic()
        if now < self._capability_backoff_until:
            # Backoff is hot — refuse to deepen the hole. Surface
            # the back-off explicitly to the caller via the cache.
            self._source_control_cache[key] = _degraded_snapshot(
                state="degraded",
                reason=FAILURE_RATE_LIMITED,
                watch_mode=self.get_nag_mode(*key),
                details={
                    "backoff_seconds_remaining": max(
                        0, int(self._capability_backoff_until - now),
                    ),
                },
            )
            return

        bindings = _read_bindings(row)
        rate_limited = False
        try:
            if bindings:
                snapshot, rate_limited = await _refresh_bindings_via_rest(
                    row, all_rows, bindings,
                    watch_mode=self.get_nag_mode(*key),
                )
            else:
                snapshot = await _fetch_source_control(
                    row, all_rows,
                    watch_mode=self.get_nag_mode(*key),
                )
        except Exception as exc:
            logger.warning(
                "worktree_monitor: refresh_one failed for %s/%s: %s",
                row.session_name, row.repo_name, exc,
            )
            self._source_control_cache[key] = _degraded_snapshot(
                state="degraded",
                reason="probe_failed",
                watch_mode=self.get_nag_mode(*key),
            )
            return

        self._record_poll(key, now)
        self._source_control_cache[key] = snapshot
        self._source_control_fetched_at[key] = now

        if rate_limited or snapshot.get("reason") == FAILURE_RATE_LIMITED:
            self._capability_backoff_until = now + RATE_LIMIT_BACKOFF_SECONDS
            logger.warning(
                "worktree_monitor: refresh_one hit rate-limit on %s/%s; "
                "backing off capability fetches for %ds",
                row.session_name, row.repo_name, int(RATE_LIMIT_BACKOFF_SECONDS),
            )
        elif self._capability_backoff_until > 0:
            # Clean response — clear any stale back-off.
            self._capability_backoff_until = 0.0

    def _watch_ttl_elapsed(self, key: tuple[str, str], now: float) -> bool:
        """True when the watch-active row's TTL window has lapsed."""
        last = self._source_control_fetched_at.get(key)
        if last is None:
            return True
        return (now - last) >= SOURCE_CONTROL_WATCH_TTL_SECONDS

    def _record_poll(self, key: tuple[str, str], now: float) -> None:
        """Stamp a poll timestamp into the row's sliding-window budget."""
        history = self._poll_history.setdefault(key, deque())
        history.append(now)
        # Trim entries outside the rolling-hour window so the deque
        # length is the count of recent polls.
        cutoff = now - _POLL_WINDOW_SECONDS
        while history and history[0] < cutoff:
            history.popleft()

    def _poll_budget_remaining(self, key: tuple[str, str], now: float) -> int:
        """Polls left in the row's sliding-hour budget (≥ 0)."""
        history = self._poll_history.get(key)
        if history is None:
            return MAX_POLLS_PER_HOUR_PER_ROW
        cutoff = now - _POLL_WINDOW_SECONDS
        recent = sum(1 for t in history if t >= cutoff)
        return max(0, MAX_POLLS_PER_HOUR_PER_ROW - recent)

    def _should_poll_in_background(
        self, key: tuple[str, str], cached: dict | None, now: float,
    ) -> bool:
        """Decide whether to fetch this row in a background tick.

        Returns ``True`` only when every gate clears:
          - row is watch-active (operator opted in),
          - cached snapshot has at least one running review
            (something is actually transitioning — no point polling
            otherwise),
          - the watch TTL has elapsed since the last fetch,
          - the per-row sliding-hour poll budget has not been spent.

        Force-refresh callers do not consult this — they always fetch.
        """
        if self.get_nag_mode(*key) == NAG_SILENT:
            return False
        if cached is None:
            return False  # never had a snapshot; an operator refresh has to seed it
        # Plural ``reviews`` is the canonical shape post-binding migration;
        # legacy ``review`` (singular) is read as fallback for older cache
        # entries seeded before the migration.
        reviews = cached.get("reviews")
        if not reviews:
            single = cached.get("review")
            reviews = [single] if single else []
        if not any((r and r.get("running")) for r in reviews):
            return False
        if not self._watch_ttl_elapsed(key, now):
            return False
        if self._poll_budget_remaining(key, now) <= 0:
            return False
        return True

    async def _refresh_source_control(
        self,
        rows: list[WorktreeState],
        *,
        force_capabilities: bool = False,
    ) -> None:
        """Fan out source_control fetches for live rows, concurrently.

        Skips a per-row fetch when:
          - the row's cached snapshot is within TTL (and not forced), OR
          - the rate-limit back-off is active (and not forced).

        In both cases the existing snapshot is preserved so the UI
        stays stable; rows that have never been fetched stamp a
        synthesized degraded snapshot so the operator sees the back-off
        explicitly rather than a missing block.
        """
        live_rows = [r for r in rows if r.session_live]
        if not live_rows:
            self._source_control_cache = {}
            self._source_control_fetched_at = {}
            return

        now = time.monotonic()
        backoff_active = (
            not force_capabilities and now < self._capability_backoff_until
        )
        backoff_remaining = max(0, int(self._capability_backoff_until - now))

        # Decide per row: fetch or reuse the cached snapshot.
        #
        # Background ticks (force_capabilities=False) NEVER fetch by
        # default. The only exception is a watch-active row whose
        # cached snapshot shows running checks AND has poll budget
        # left — caught by ``_should_poll_in_background``. Every
        # other path carries the cached snapshot forward unchanged.
        # Operator-forced refreshes ignore all gates (subject only
        # to the rate-limit backoff above).
        rows_to_fetch: list[WorktreeState] = []
        carried: dict[tuple[str, str], dict] = {}
        for row in live_rows:
            key = (row.session_name, row.repo_name)
            cached = self._source_control_cache.get(key)
            if backoff_active:
                if cached is not None:
                    carried[key] = cached
                else:
                    carried[key] = _degraded_snapshot(
                        state="degraded",
                        reason=FAILURE_RATE_LIMITED,
                        watch_mode=self.get_nag_mode(*key),
                        details={"backoff_seconds_remaining": backoff_remaining},
                    )
                continue
            if force_capabilities:
                rows_to_fetch.append(row)
                continue
            if self._should_poll_in_background(key, cached, now):
                rows_to_fetch.append(row)
                continue
            # Default background path: keep the cached snapshot
            # untouched. Rows with no cache stay absent from the
            # snapshot map — UI shows no source_control block for
            # them until an operator refresh seeds it.
            if cached is not None:
                carried[key] = cached

        # Fan out only the rows that actually need a fresh fetch.
        results: list = []
        if rows_to_fetch:
            results = await asyncio.gather(
                *(
                    _fetch_source_control(
                        row, rows,
                        watch_mode=self.get_nag_mode(row.session_name, row.repo_name),
                    )
                    for row in rows_to_fetch
                ),
                return_exceptions=True,
            )

        new_cache: dict[tuple[str, str], dict] = dict(carried)
        new_fetched_at: dict[tuple[str, str], float] = {
            k: v for k, v in self._source_control_fetched_at.items() if k in carried
        }
        rate_limit_seen = False
        for row, snapshot in zip(rows_to_fetch, results):
            key = (row.session_name, row.repo_name)
            # Stamp the poll history regardless of outcome so a stuck
            # row that errors repeatedly still consumes its budget
            # rather than retrying forever.
            self._record_poll(key, now)
            if isinstance(snapshot, Exception):
                logger.warning(
                    "worktree_monitor: source_control fetch failed for %s/%s: %s",
                    row.session_name, row.repo_name, snapshot,
                )
                new_cache[key] = _degraded_snapshot(
                    state="degraded",
                    reason="probe_failed",
                    watch_mode=self.get_nag_mode(*key),
                )
                continue
            new_cache[key] = snapshot
            new_fetched_at[key] = now
            # Detect rate-limited responses and arm the back-off so
            # subsequent rows in the same refresh cycle (and the next
            # 30s tick) skip gh entirely.
            if snapshot.get("reason") == FAILURE_RATE_LIMITED:
                rate_limit_seen = True

        if rate_limit_seen:
            self._capability_backoff_until = now + RATE_LIMIT_BACKOFF_SECONDS
            logger.warning(
                "worktree_monitor: gh rate-limited; backing off capability "
                "fetches for %ds",
                int(RATE_LIMIT_BACKOFF_SECONDS),
            )
        elif force_capabilities and rows_to_fetch:
            # An operator-forced refresh that ran without seeing
            # rate-limit clears any stale back-off — auth/quota
            # recovered.
            self._capability_backoff_until = 0.0

        self._source_control_cache = new_cache
        self._source_control_fetched_at = new_fetched_at

    async def start(self) -> None:
        """Start the background polling loop."""
        if self._started:
            return
        self._started = True
        self._lock = asyncio.Lock()
        try:
            await self.refresh()
        except Exception:
            logger.exception("worktree_monitor: initial refresh failed")
            self._cache = []
        self._task = asyncio.create_task(self._loop())
        logger.info("worktree_monitor: background task started")

    async def stop(self) -> None:
        """Cancel the polling loop and allow later restart."""
        if not self._started:
            return
        task = self._task
        if task and not task.done():
            try:
                task.cancel()
            except RuntimeError:
                pass
            try:
                await task
            except asyncio.CancelledError:
                pass
            except RuntimeError:
                pass
        self._task = None
        self._lock = None
        self._started = False
        logger.info("worktree_monitor: background task stopped")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worktree_monitor: refresh failed")


worktree_monitor = WorktreeMonitor()
