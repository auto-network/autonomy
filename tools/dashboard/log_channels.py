"""Dashboard log channels: one INFO catch-all, buckets for the spammers.

Operator's rule (2026-09-07): "one file is fine as long as nothing is
duplicated; buckets for the spammers". So:

* ``<log dir>/dashboard.log`` — INFO+ from every logger NOT routed below.
  Still the single answer to "what is the dashboard doing".
* routed channels, each its own file, NOT duplicated into ``dashboard.log``:
  ``http`` (one line per request), ``stalls`` (event-loop stall diagnostics),
  ``monitors`` (session / worktree / resource monitors, worktree scans),
  ``fleet`` (fleet sync, enrollment, serving connectors, tools.network.*),
  ``voice`` (dictation websocket + WhisperLive client).
* stdout — WARNING+ from everything, so ``docker logs`` / the launcher's
  ``data/dashboard.log`` capture reads as "what's wrong". Uvicorn's own
  lifecycle lines (``uvicorn.error``) stay on stderr as today and are ALSO
  written to the ``dashboard`` channel so the file has the restart timeline.
* rotation: size-based, ``DASHBOARD_LOG_MAX_BYTES`` × ``DASHBOARD_LOG_BACKUPS``
  per channel (default 50 MB × 5 — days at post-hygiene rates).

Installed programmatically and idempotently (handlers are tagged and replaced
on re-configure) rather than through ``dictConfig``: ``dictConfig`` closes
every existing handler, which under pytest kills the capture handlers of the
test that imported the server, and the server module IS re-imported by the
``test_app`` fixture.

Environment:
``DASHBOARD_LOG_DIR``       where the channel files live (default ``<DATA_ROOT>/logs``)
``DASHBOARD_LOG_CHANNELS``  ``on`` (default) or ``off`` → plain stream logging at
                            INFO, exactly the old ``basicConfig`` behaviour; also
                            the default under ``DASHBOARD_MOCK``.
``DASHBOARD_LOG_STDOUT_LEVEL``  default ``WARNING``
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
CATCH_ALL = "dashboard"

#: channel -> logger-name prefixes routed to it (children inherit the route).
CHANNELS: dict[str, tuple[str, ...]] = {
    "http": ("tools.dashboard.server.http",),
    "stalls": ("tools.dashboard.server.stall",),
    "voice": (
        "tools.dashboard.server.voice",
        "tools.dashboard.voice_whisperlive",
        "tools.dashboard.voice_session",
        "tools.dashboard.voice_buffer",
        "tools.dashboard.voice_transcription_settings",
        "tools.dashboard.voiceover",
    ),
    "monitors": (
        "tools.dashboard.session_monitor",
        "tools.dashboard.worktree_monitor",
        "tools.dashboard.resource_monitor",
        "tools.dashboard.attention_index_service",
        "agents.workspace_manager",
    ),
    "fleet": (
        "tools.network",
        "tools.dashboard.link_serving",
        "tools.dashboard.link_serving_supervisor",
        "tools.dashboard.fleet_enrollment_routes",
        "tools.dashboard.fleet_enrollment_service",
        "tools.dashboard.fleet_enrollment_approvals",
    ),
}

_TAG = "_dashboard_log_channel"
_DEFAULT_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_BACKUPS = 5

logger = logging.getLogger(__name__)


def channel_for(logger_name: str) -> str:
    """Which channel a logger's records land in."""
    for channel, prefixes in CHANNELS.items():
        for prefix in prefixes:
            if logger_name == prefix or logger_name.startswith(prefix + "."):
                return channel
    return CATCH_ALL


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _level(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip().upper()
    if raw and isinstance(logging.getLevelName(raw), int):
        return logging.getLevelName(raw)
    return default


def enabled(environ=None) -> bool:
    env = os.environ if environ is None else environ
    mode = env.get("DASHBOARD_LOG_CHANNELS", "").strip().lower()
    if mode in ("off", "0", "false", "no"):
        return False
    if mode in ("on", "1", "true", "yes"):
        return True
    return not env.get("DASHBOARD_MOCK")


def log_dir(environ=None) -> Path:
    env = os.environ if environ is None else environ
    raw = env.get("DASHBOARD_LOG_DIR")
    if raw:
        return Path(raw)
    from tools.data_paths import DATA_ROOT
    return Path(DATA_ROOT) / "logs"


def _tag(handler: logging.Handler, channel: str) -> logging.Handler:
    setattr(handler, _TAG, channel)
    return handler


def _remove_tagged(target: logging.Logger) -> None:
    for handler in list(target.handlers):
        if getattr(handler, _TAG, None) is not None:
            target.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass


def _file_handler(path: Path, channel: str, max_bytes: int, backups: int,
                  formatter: logging.Formatter) -> logging.Handler:
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(formatter)
    return _tag(handler, channel)


class _StdoutHandler(logging.StreamHandler):
    """A stream handler that writes to whatever ``sys.stdout`` is NOW.

    ``logging.StreamHandler(sys.stdout)`` pins the stream object at install
    time; anything that later replaces ``sys.stdout`` (pytest's capture, a
    supervisor re-plumbing the child's fds) would silently keep writing to
    the old one.
    """

    def __init__(self) -> None:
        super().__init__(stream=None)

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, _value):  # StreamHandler.__init__ assigns; ignore it
        pass


def _stream_handler(level: int, channel: str, formatter: logging.Formatter) -> logging.Handler:
    handler = _StdoutHandler()
    handler.setLevel(level)
    handler.setFormatter(formatter)
    return _tag(handler, channel)


def _routed_loggers() -> list[tuple[str, str]]:
    return [(channel, prefix) for channel, prefixes in CHANNELS.items() for prefix in prefixes]


def describe(directory: Path, stdout_level: int, max_bytes: int, backups: int) -> str:
    parts = [f"{CATCH_ALL}={directory / (CATCH_ALL + '.log')} (INFO catch-all)"]
    parts += [f"{channel}={directory / (channel + '.log')}" for channel in CHANNELS]
    return (
        "log channels: " + " ".join(parts)
        + f"; stdout={logging.getLevelName(stdout_level)}+ (uvicorn.error INFO+)"
        + f"; rotation {max_bytes // (1024 * 1024)}MB x {backups} per channel"
    )


def configure_plain(level: int = logging.INFO) -> None:
    """The pre-channel behaviour: one stream handler on root at INFO."""
    root = logging.getLogger()
    _remove_tagged(root)
    for channel, prefix in _routed_loggers():
        target = logging.getLogger(prefix)
        _remove_tagged(target)
        target.propagate = True
    _remove_tagged(logging.getLogger("uvicorn.error"))
    _remove_tagged(logger)
    root.setLevel(level)
    if not root.handlers:
        root.addHandler(_tag(_stream_handler(level, "plain", logging.Formatter(FORMAT)), "plain"))


def configure(environ=None) -> str | None:
    """Install the channels. Returns the channel-map line, or None when off."""
    env = os.environ if environ is None else environ
    if not enabled(env):
        configure_plain()
        return None
    directory = log_dir(env)
    directory.mkdir(parents=True, exist_ok=True)
    stdout_level = _level("DASHBOARD_LOG_STDOUT_LEVEL", logging.WARNING)
    max_bytes = _env_int("DASHBOARD_LOG_MAX_BYTES", _DEFAULT_MAX_BYTES)
    backups = _env_int("DASHBOARD_LOG_BACKUPS", _DEFAULT_BACKUPS)
    formatter = logging.Formatter(FORMAT)

    root = logging.getLogger()
    _remove_tagged(root)
    root.setLevel(logging.INFO)
    catch_all = _file_handler(directory / f"{CATCH_ALL}.log", CATCH_ALL, max_bytes, backups, formatter)
    root.addHandler(catch_all)
    root.addHandler(_stream_handler(stdout_level, CATCH_ALL, formatter))

    files: dict[str, logging.Handler] = {}
    for channel, prefix in _routed_loggers():
        handler = files.get(channel)
        if handler is None:
            handler = files[channel] = _file_handler(
                directory / f"{channel}.log", channel, max_bytes, backups, formatter,
            )
        target = logging.getLogger(prefix)
        _remove_tagged(target)
        target.propagate = False           # routed: never duplicated into the catch-all
        target.setLevel(logging.NOTSET)
        target.addHandler(handler)
        target.addHandler(_stream_handler(stdout_level, channel, formatter))

    # Uvicorn's lifecycle lines keep their own stderr handler (uvicorn sets
    # propagate=False) and are ALSO written to the catch-all file so the
    # restart timeline is in the same file as the app's own startup phases.
    uv = logging.getLogger("uvicorn.error")
    _remove_tagged(uv)
    uv.addHandler(catch_all)

    # The map line must be discoverable from the stdout capture too, so this
    # module's logger gets one INFO stdout handler of its own.
    _remove_tagged(logger)
    logger.addHandler(_stream_handler(logging.INFO, "map", formatter))
    line = describe(directory, stdout_level, max_bytes, backups)
    logger.info(line)
    return line
