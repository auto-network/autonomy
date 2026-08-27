"""Uvicorn reload launcher that warns connected dashboard clients first.

This changes only Uvicorn's ``BaseReload.restart`` seam. File watching,
process management, and worker startup remain Uvicorn's implementation.

This module is itself watched so a committed probe can exercise the complete
preflight path without changing dashboard behaviour.
"""

from __future__ import annotations

import logging
import os
import ssl
import time
from types import SimpleNamespace
from urllib import error, request

from uvicorn.main import main as uvicorn_main
from uvicorn.supervisors.basereload import BaseReload

logger = logging.getLogger("uvicorn.error")

_NOTICE_PATH = "/api/internal/restart-notice"
_TOKEN_HEADER = "X-Dashboard-Restart-Token"
_COUNTDOWN_SECONDS = 3
_REQUEST_TIMEOUT_SECONDS = 1
_original_restart = BaseReload.restart


def _restart_notice_url(config: SimpleNamespace) -> str:
    scheme = "https" if getattr(config, "ssl_certfile", None) else "http"
    return f"{scheme}://127.0.0.1:{config.port}{_NOTICE_PATH}"


def _notify_dashboard(config: SimpleNamespace) -> bool:
    """Tell the still-running worker to emit SSE. Failure must not block reload."""
    token = os.environ.get("DASHBOARD_RESTART_TOKEN")
    if not token:
        logger.warning("reload warning skipped: DASHBOARD_RESTART_TOKEN is unset")
        return False
    req = request.Request(
        _restart_notice_url(config),
        data=b"{}",
        headers={_TOKEN_HEADER: token, "Content-Type": "application/json"},
        method="POST",
    )
    context = ssl._create_unverified_context() if getattr(config, "ssl_certfile", None) else None
    try:
        with request.urlopen(req, timeout=_REQUEST_TIMEOUT_SECONDS, context=context) as response:
            if 200 <= response.status < 300:
                return True
            logger.warning("reload warning refused with HTTP %s", response.status)
    except (OSError, error.URLError, error.HTTPError):
        logger.warning("reload warning request failed; reloading without a countdown")
    return False


def _restart_with_notice(self: BaseReload) -> None:
    if _notify_dashboard(self.config):
        time.sleep(_COUNTDOWN_SECONDS)
    _original_restart(self)


BaseReload.restart = _restart_with_notice


if __name__ == "__main__":
    uvicorn_main(prog_name="python -m tools.dashboard.reload_with_notice")
