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
    FAILURE_RATE_LIMITED,
    WorktreeGithubExecResult,
    normalize_review_payload,
    source_control_review_read_v1,
)
from agents.workspace_manager import WorktreeState, scan_all_worktrees

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
        "review": None,
        "watch": {"mode": watch_mode},
    }
    if details:
        # Only emit non-empty details so the snapshot stays compact when
        # the failure mode doesn't carry extra context.
        snapshot["details"] = details
    return snapshot


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
    """Probe + read the row's source_control snapshot for one live row."""
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
    return {
        "state": "ready",
        "implementation": "autonomy/github",
        "reason": None,
        "review": review.to_dict() if review is not None else None,
        "watch": {"mode": watch_mode},
    }


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
        active rate-limit back-off — operator-triggered refresh
        (``POST /api/worktrees/refresh``) wants fresh data on demand.
        The background polling loop calls with the default
        ``False`` so it respects the TTL and backs off when gh is
        rate-limited.
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
          - cached snapshot exists and shows ``review.running == True``
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
        review = cached.get("review")
        if not review:
            return False
        if not review.get("running"):
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
