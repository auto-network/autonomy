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

from agents.capabilities.github import probe as github_probe
from agents.capabilities.github.service import (
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


# Nag modes for the source_control.watch block. The UI maps these onto
# Silent / Nag All Changes / Nag When Done buttons (settled design
# 3435e03f). Persistence is in-memory on the monitor for v1; CrossTalk
# delivery (auto-f2quy-style) is a separate follow-up bead.
NAG_SILENT = "silent"
NAG_ALL_CHANGES = "nag_all"
NAG_WHEN_DONE = "nag_done"
NAG_MODES = frozenset({NAG_SILENT, NAG_ALL_CHANGES, NAG_WHEN_DONE})
NAG_DEFAULT = NAG_SILENT


def _degraded_snapshot(*, state: str, reason: str | None, watch_mode: str = NAG_DEFAULT) -> dict:
    return {
        "state": state,
        "implementation": "autonomy/github",
        "reason": reason,
        "review": None,
        "watch": {"mode": watch_mode},
    }


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
            state="degraded", reason=op_result.failure, watch_mode=watch_mode,
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
        self._nag_modes: dict[tuple[str, str], str] = {}
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
        """Return the persisted nag mode for a row (defaults to ``silent``)."""
        return self._nag_modes.get((session_name, repo_name), NAG_DEFAULT)

    def set_nag_mode(self, session_name: str, repo_name: str, mode: str) -> str:
        """Persist a nag mode for a row. Raises ValueError on invalid mode.

        Updates the cached source_control snapshot in place when present
        so the next ``GET /api/worktrees`` reflects the new mode without
        waiting for the 30s background refresh.
        """
        if mode not in NAG_MODES:
            raise ValueError(
                f"invalid nag mode: {mode!r}; expected one of {sorted(NAG_MODES)}"
            )
        key = (session_name, repo_name)
        self._nag_modes[key] = mode
        cached = self._source_control_cache.get(key)
        if cached is not None:
            cached["watch"] = {"mode": mode}
        return mode

    async def refresh(self) -> list[WorktreeState]:
        """Force a scan and replace the cache."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            rows = await asyncio.to_thread(scan_all_worktrees)
            self._cache = rows
            await self._refresh_source_control(rows)
            return list(rows)

    async def _refresh_source_control(self, rows: list[WorktreeState]) -> None:
        """Fan out source_control fetches for live rows, concurrently."""
        live_rows = [r for r in rows if r.session_live]
        if not live_rows:
            self._source_control_cache = {}
            return

        results = await asyncio.gather(
            *(
                _fetch_source_control(
                    row, rows,
                    watch_mode=self.get_nag_mode(row.session_name, row.repo_name),
                )
                for row in live_rows
            ),
            return_exceptions=True,
        )

        new_cache: dict[tuple[str, str], dict] = {}
        for row, snapshot in zip(live_rows, results):
            key = (row.session_name, row.repo_name)
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
        self._source_control_cache = new_cache

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
