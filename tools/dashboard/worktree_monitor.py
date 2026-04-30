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


def _degraded_snapshot(*, state: str, reason: str | None) -> dict:
    return {
        "state": state,
        "implementation": "autonomy/github",
        "reason": reason,
        "review": None,
    }


async def _fetch_source_control(
    row: WorktreeState, all_rows: list[WorktreeState],
) -> dict:
    """Probe + read the row's source_control snapshot for one live row."""
    probe_result = await github_probe.probe_v1(
        row.session_name,
        timeout=int(_GITHUB_PROBE_TIMEOUT),
    )
    if probe_result["state"] != github_probe.STATE_READY:
        return _degraded_snapshot(
            state=probe_result["state"],
            reason=probe_result["reason"],
        )

    if not row.branch:
        # Probe ready but the worktree has no checked-out branch — we
        # can't ask gh for review state. Surface as degraded with a
        # specific reason rather than pretending review is null.
        return _degraded_snapshot(state="degraded", reason="no_branch")

    op_result: WorktreeGithubExecResult = await source_control_review_read_v1(
        row.session_name,
        row.repo_name,
        rows=all_rows,
        timeout=int(_GITHUB_REVIEW_TIMEOUT),
    )
    if not op_result.ok:
        return _degraded_snapshot(state="degraded", reason=op_result.failure)

    review = normalize_review_payload(op_result.stdout)
    return {
        "state": "ready",
        "implementation": "autonomy/github",
        "reason": None,
        "review": review,
    }


class WorktreeMonitor:
    """Polling cache for session worktree state + per-row capability data."""

    def __init__(self, *, interval_seconds: float = 30.0) -> None:
        self._interval_seconds = interval_seconds
        self._cache: list[WorktreeState] = []
        self._source_control_cache: dict[tuple[str, str], dict] = {}
        self._task: asyncio.Task | None = None
        self._lock: asyncio.Lock | None = None
        self._started = False

    def get_all(self) -> list[WorktreeState]:
        """Return a snapshot of cached worktree state."""
        return list(self._cache)

    def get_source_control(self, session_name: str, repo_name: str) -> dict | None:
        """Return the cached source_control snapshot for a row, if any."""
        return self._source_control_cache.get((session_name, repo_name))

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
            *(_fetch_source_control(row, rows) for row in live_rows),
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
                    state="degraded", reason="probe_failed",
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
