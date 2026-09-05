"""Autonomy Dashboard — Starlette server.

Thin rendering layer over the bd and graph CLI tools.
Every view the dashboard shows, an agent can also produce via CLI.
"""

import asyncio
import contextlib
import fcntl
import functools
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import pty
import re
import shlex
import signal
import sqlite3
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Callable
from urllib import error as urllib_error, request as urllib_request
from urllib.parse import quote as url_quote

logger = logging.getLogger(__name__)
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow importing from agents/ (sibling of tools/)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.data_paths import DATA_ROOT

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route, Mount, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.websockets import WebSocket, WebSocketDisconnect
from sse_starlette.sse import EventSourceResponse

from agents.dispatch_db import (
    list_runs, get_run, get_runs_for_bead, get_currently_running, DB_PATH,
    clear_paused, is_paused, get_pause_reason,
    get_consecutive_failures, reset_circuit_breaker,
    record_worktree_merge_run,
)
from agents.session_launcher import launch_session
from agents import workspace_settings
from agents.primer_renderer import render_workspace_primer
from agents.workspace_manager import (
    GitFileChange,
    RebaseRequiredError,
    WORKTREES_DIR,
    WorktreeCommit,
    WorktreeDirtyDetail,
    WorktreeState,
    WorkspaceError,
    cleanup_session_worktree,
    cleanup_session_worktrees,
    get_repo_commit_detail,
    get_session_worktree_commit_detail,
    get_session_worktree_dirty_detail,
    get_session_worktree_integrated_diff,
    cherry_pick_session_worktree,
    ensure_local_workspace_repository,
    local_workspace_repo_path,
    managed_clone_path,
    merge_session_worktree,
    merge_session_worktree_commit,
    load_row_cache,
    prepare_session_mounts,
    save_row_cache,
    sync_session_worktree_base,
)
from agents.design_db import DuplicateDesignTitleError
if os.environ.get("DASHBOARD_MOCK"):
    from tools.dashboard.dao.mock import (
        create_design, get_design, submit_results,
        list_pending_designs, dismiss_design, resolve_design_prefix,
    )
else:
    from agents.design_db import (
        create_design, get_design, submit_results, list_pending as list_pending_designs,
        dismiss_design, resolve_design_prefix,
    )
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

from tools.dashboard.event_bus import event_bus, current_server_epoch
from tools.dashboard import session_harness
from tools.dashboard.session_harness import (
    CLAUDE_HARNESS,
    dedup_claude_entries,
    enrich_claude_entries,
    parse_claude_log_line,
    postprocess_claude_entries,
    resolve_harness_for_path,
    resolve_harness_for_session_row,
)
from tools.dashboard import session_monitor as session_monitor_mod
from tools.dashboard.session_monitor import count_tool_uses, session_monitor, TaskStateTracker
from tools.dashboard.resource_monitor import resource_monitor
from tools.dashboard.session_lifecycle_worker import (
    derive_lifecycle_state,
    LifecycleJob,
    SessionLifecycleStateWriter,
    SessionLifecycleWorker,
)
from tools.dashboard.worktree_monitor import worktree_monitor
from tools.dashboard import session_trace
from tools.dashboard import turn_corrections as turn_corrections_mod
from tools.dashboard.dao import auth_db, dashboard_db, mcp_relay_db
from tools.dashboard import approvals_routes
from tools.dashboard import attention_routes
from tools.dashboard import mcp_relay_routes
from tools.dashboard import dropbox_routes
from tools.dashboard import jira_routes
from tools.dashboard import identity_routes
from tools.dashboard import fleet_enrollment_routes
from tools.dashboard import unlock_routes
from tools.dashboard import vault_routes
from tools.dashboard import api_auth, route_policy
from tools.dashboard import network_routes
from tools.dashboard import org_membership_routes
from tools.dashboard import web_push, web_push_proof, web_push_routes, web_push_worker
from tools.dashboard import image_build_worker
from tools.dashboard import web_gateway_supervisor
from tools.dashboard import service_certificate_manager
from agents import image_builder
if os.environ.get("DASHBOARD_MOCK"):
    from tools.dashboard.dao import mock as dao_beads
    from tools.dashboard.dao import mock as dao_dispatch
    from tools.dashboard.dao import mock as dao_sessions
else:
    from tools.dashboard.dao import beads as dao_beads
    from tools.dashboard.dao import dispatch as dao_dispatch
    from tools.dashboard.dao import sessions as dao_sessions
from tools.graph.schemas.agent_actions import AGENT_ACTION_TEMPLATE_ROOTS

from tools.dashboard.tmux_send import (
    tmux_enter_checked_sync,
    tmux_paste_checked_sync,
    tmux_send,
    tmux_send_awaited,
    tmux_send_sync,
)
from tools.graph import ops as graph_ops
from tools.graph.settings_ops import VaultSealerMissing
from tools.vault.key_sealer import VaultSealerNotReady


STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"
PLUGINS_DIR = Path(__file__).parent / "plugins"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


# ── Plugin substrate ──────────────────────────────────────────
# See bead auto-a79f6 + design note graph://f77a5415-04f.

from tools.dashboard.plugin_api import loader as plugin_loader  # noqa: E402
from tools.dashboard.plugin_api.session_contributions import (  # noqa: E402
    SESSION_CONTRIBUTIONS_TOPIC,
    normalize_descriptor as normalize_session_contribution,
)

# Settings-mediator substrate (bead auto-f93wj) — imported eagerly so
# its cursor + state schemas are in the registry before
# ``flush_schema_meta_machine_store`` runs at lifespan startup. The dispatch
# loop itself starts inside the lifespan hook.
from tools.dashboard import settings_mediator as _settings_mediator  # noqa: E402, F401
from tools.dashboard import harness_usage_settings as _harness_usage_settings  # noqa: E402, F401
from tools.dashboard import harness_bootstrap as _harness_bootstrap  # noqa: E402, F401
from tools.dashboard import session_upload_settings as _session_upload  # noqa: E402, F401
from tools.dashboard import session_orientation_settings as _session_orientation_settings  # noqa: E402, F401
from tools.dashboard import voice_transcription_settings as _voice_transcription_settings  # noqa: E402
from tools.dashboard import worktree_directives as _worktree_directives  # noqa: E402, F401
from tools.dashboard import claude_credentials_refresh as _claude_credentials_refresh  # noqa: E402
from tools.dashboard import codex_credentials_refresh as _codex_credentials_refresh  # noqa: E402
from tools.graph import settings_ops  # noqa: E402

# Activity tab notifications substrate (bead auto-5u8zb) — imported
# eagerly so the four ``dashboard.activity.*`` SettingSchema classes
# (ask, ask_vote, ask_refresh, operator_dismissed) are in the registry
# before ``flush_schema_meta_machine_store`` runs at lifespan startup.
# See pitfall ``graph://3fe60c25-fab``.
from tools.dashboard import notifications_settings as _notifications_settings  # noqa: E402, F401
from tools.dashboard import agent_test_leases as _agent_test_leases  # noqa: E402

# Surface Presence + OperatorActivity substrate (bead auto-i3tki) —
# imported eagerly for the same reason: its three SettingSchema classes
# (dashboard.surface.presence, dashboard.surface.ping,
# dashboard.operator.activity) must be in the registry before
# ``flush_schema_meta_machine_store`` runs at lifespan startup.
# See pitfall ``graph://3fe60c25-fab``.
from tools.graph import surface as _surface  # noqa: E402, F401

# Surface ping CrossTalk delivery (bead auto-9gxo8, substrate.C) —
# imported eagerly so the ``surface.ping.deliver`` mediator action is
# registered before ``start_action_loop`` ticks. The handler routes
# pings by explicit ``to_participant_id`` (per pitfall
# ``graph://1ba4d2e0-c5f``).
from tools.dashboard import surface_actions as _surface_actions  # noqa: E402, F401

# Notifications-tab refresh-request → CrossTalk source-session ping
# (beads auto-r92kc → auto-tdlhq). Imported eagerly so the
# ``@register_action_decorator`` runs at module-load time, registering
# the handler on the settings_mediator's registry before
# :func:`settings_mediator.start_action_loop` begins ticking. No
# lifespan plumbing — registration is the only side effect of import.
from tools.dashboard import notifications_actions as _notifications_actions  # noqa: E402, F401

# CrosstalkDirective family base (bead auto-ixzdz) — imported eagerly so
# the ``dashboard.session.crosstalk`` namespace root is established and
# any concrete subclass (request-rebase, request-identity-refresh, …)
# composing under it via ``set_id_suffix`` registers before
# ``flush_schema_meta_machine_store`` runs at lifespan startup.
from tools.dashboard import crosstalk_directive as _crosstalk_directive  # noqa: E402, F401

# Settings Nexus plugin schemas (bead auto-ct3ey) — imported eagerly so
# ``dashboard.nexus.scene#1`` + ``dashboard.nexus.tile#1`` are in the
# registry before ``flush_schema_meta_machine_store`` runs at lifespan
# startup. The plugin loader also imports the module via the
# manifest's ``entrypoints.schemas`` list, but that import runs *after*
# this top-level reference; the explicit import is the contract per
# pitfall ``graph://3fe60c25-fab``.
from tools.dashboard.plugins.nexus.entrypoints import schemas as _nexus_schemas  # noqa: E402, F401

# Load every valid plugin (regardless of Setting state) so route
# registration covers plugins that operators may flip on at runtime.
# Per-request handlers gate via ``_plugin_enabled_map``; plugins
# disabled at request time return 404 and are excluded from
# ``/api/plugins``.
PLUGIN_REGISTRY: list[plugin_loader.LoadedPlugin] = plugin_loader.load_all(
    plugins_dir=PLUGINS_DIR,
)


def _build_plugin_jinja_loader(registry):
    """Resolve `plugins/<id>/<file>` template paths to each plugin's dir.

    Plugin directory names may not match the plugin id (e.g. shipped
    sample lives under `plugins/_example/` but its id is `example`), so
    a custom loader is needed instead of bolting `PLUGINS_DIR` onto the
    Jinja search path.
    """
    from jinja2 import BaseLoader, TemplateNotFound

    class _PluginTemplateLoader(BaseLoader):
        def __init__(self, registry_):
            self._registry = list(registry_)

        def get_source(self, environment, template):
            if not template.startswith("plugins/"):
                raise TemplateNotFound(template)
            rest = template[len("plugins/"):]
            plugin_id, _, rel_path = rest.partition("/")
            if not rel_path:
                raise TemplateNotFound(template)
            plugin = next(
                (p for p in self._registry if p.id == plugin_id), None,
            )
            if plugin is None:
                raise TemplateNotFound(template)
            file_path = plugin.plugin_dir / rel_path
            if not file_path.is_file():
                raise TemplateNotFound(template)
            mtime = file_path.stat().st_mtime
            source = file_path.read_text(encoding="utf-8")
            return source, str(file_path), lambda: file_path.stat().st_mtime == mtime

    return _PluginTemplateLoader(registry)


if PLUGIN_REGISTRY:
    from jinja2 import ChoiceLoader as _PluginChoiceLoader
    templates.env.loader = _PluginChoiceLoader([
        templates.env.loader,
        _build_plugin_jinja_loader(PLUGIN_REGISTRY),
    ])


# Substrate picks sidebar badge colors round-robin from a fixed palette.
# Plugins do not get a say in their badge color (design note: "Dashboard
# theming concern; substrate picks").
_PLUGIN_BADGE_PALETTE = (
    "indigo", "green", "purple", "amber", "blue", "pink", "gray",
)


def _plugin_badge_color(idx: int) -> str:
    return _PLUGIN_BADGE_PALETTE[idx % len(_PLUGIN_BADGE_PALETTE)]


def _plugin_enabled_map() -> dict[str, bool]:
    """Resolve current enable state for every loaded plugin.

    Each plugin's toggle row lives in *its own* ``manifest.org``'s DB
    — substrate v1.1 scopes per-plugin internally so unscoped browser
    requests still see the canonical state. Reads are batched per-org
    so two plugins sharing an install scope make one DB call.

    Per-request rather than cached: lets operators flip
    ``dashboard.plugin#1: {enabled: ...}`` without restart, and the
    L2.B sweep tests rely on the same path to drive plugin state via
    fixture toggles.
    """
    cache: dict[str, dict[str, dict]] = {}
    out: dict[str, bool] = {}
    for p in PLUGIN_REGISTRY:
        manifest_org = p.manifest.org
        if manifest_org not in cache:
            cache[manifest_org] = plugin_loader._read_plugin_settings(
                org=manifest_org,
            )
        settings = cache[manifest_org]
        out[p.id] = plugin_loader.is_enabled(
            p.id, p.plugin_dir, settings, manifest=p.manifest,
        )
    return out


def _plugin_effective_org(plugin: plugin_loader.LoadedPlugin) -> str:
    """Resolve the runtime org for *plugin* — payload override or manifest."""
    settings = plugin_loader._read_plugin_settings(org=plugin.manifest.org)
    payload = settings.get(plugin.id) or {}
    override = payload.get("org")
    if isinstance(override, str) and override:
        return override
    return plugin.manifest.org


class _VersionedStatic(StaticFiles):
    """Static files that a browser may keep, when the URL says which version.

    Nothing under /static carried a cache-control header, so a browser had no
    instruction to reuse anything and revalidated on every navigation. The
    shell asks for nineteen files before it can paint, so that was nineteen
    conditional requests per page change, each a round trip, six at a time
    over HTTP/1.1 and sharing that budget with the live event streams the
    pages hold open. They all answer 304 with an empty body: nothing is
    downloaded and the reader waits anyway.

    A request carrying a ``?v=`` is asking for one specific build -- the shell
    stamps every asset it references with the newest modification time under
    static/, so the URL changes whenever the file does. That request can be
    answered once and kept, because a changed file is a different URL.

    A request without one cannot. Five references have no stamp, among them
    the encryption library and the network-join page, and pinning those for a
    year would strand a browser on whichever build it happened to see first.
    Those keep revalidating, which is what they do today.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        scope = args[2] if len(args) > 2 else kwargs.get("scope") or {}
        query = scope.get("query_string") or b""
        stamped = b"v=" in query
        response.headers["cache-control"] = (
            "public, max-age=31536000, immutable" if stamped
            else "no-cache"
        )
        return response


def _static_version() -> str:
    import time as _time
    t0 = _time.monotonic()
    try:
        mtimes = [p.stat().st_mtime for p in (Path(__file__).parent / "static").rglob("*") if p.is_file()]
        version = str(int(max(mtimes))) if mtimes else str(int(_time.time()))
    except Exception:
        version = str(int(_time.time()))
    elapsed_ms = (_time.monotonic() - t0) * 1000
    logger.warning("[static_version] computed in %.1fms → %s", elapsed_ms, version)
    return version

DISPATCH_STATE_PATH = DATA_ROOT / "dispatch.state"
# Tests override this via the DASHBOARD_EVENT_BUS_STATE env var to avoid
# polluting the real repo path when TestClient drives the lifespan.
EVENT_BUS_STATE_PATH = Path(
    os.environ.get("DASHBOARD_EVENT_BUS_STATE")
    or str(DATA_ROOT / "event_bus.state")
)
# A tiny hand-off record for the user-visible reload notice.  It deliberately
# lives beside the EventBus snapshot rather than inside it: the state needs to
# survive even if snapshotting the (much larger) replay buffer fails.
RESTART_NOTICE_STATE_PATH = Path(
    os.environ.get("DASHBOARD_RESTART_NOTICE_STATE")
    or str(DATA_ROOT / "restart_notice.state")
)
_RESTART_WARNING_SECONDS = 3
_RESTART_EXPECTED_MS = 30_000
_RESTART_TOKEN_HEADER = "x-dashboard-restart-token"
_restart_notice_lock = asyncio.Lock()
_restart_notice_payload: dict[str, int] | None = None


def _write_restart_notice(payload: dict[str, int]) -> None:
    """Atomically persist the one datum the next process needs for timing."""
    try:
        RESTART_NOTICE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = RESTART_NOTICE_STATE_PATH.with_suffix(
            RESTART_NOTICE_STATE_PATH.suffix + ".tmp"
        )
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(RESTART_NOTICE_STATE_PATH)
    except OSError:
        logger.warning("could not persist restart notice state", exc_info=True)


def _read_restart_notice() -> dict[str, int] | None:
    try:
        payload = json.loads(RESTART_NOTICE_STATE_PATH.read_text(encoding="utf-8"))
        started_at_ms = int(payload["started_at_ms"])
        if started_at_ms <= 0:
            raise ValueError("non-positive started_at_ms")
        return {"started_at_ms": started_at_ms}
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        logger.warning("invalid restart notice state; ignoring it", exc_info=True)
        return None


def _current_headline_context() -> dict[str, str]:
    """Return the head SHA and subject without allowing a git hiccup to delay boot."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H%x00%s"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=1,
            check=True,
        )
        commit_hash, headline = result.stdout.rstrip("\n").split("\x00", 1)
        return {"commit_hash": commit_hash, "commit_headline": headline}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def _discard_restart_event_cache() -> None:
    """Restart messages are for clients present at the time, never new tabs."""
    discard = getattr(event_bus, "discard_cached", None)
    if callable(discard):
        discard(lambda topic, _data, _decoded_ok: topic == "server:restart")


async def _emit_restart_complete() -> None:
    """Publish a durable restart completion only after this process is ready."""
    restart_notice = _read_restart_notice()
    if restart_notice is None:
        return
    completed_at_ms = int(time.time() * 1000)
    started_at_ms = restart_notice["started_at_ms"]
    payload: dict[str, Any] = {
        "phase": "complete",
        "started_at_ms": started_at_ms,
        "completed_at_ms": completed_at_ms,
        "duration_ms": max(0, completed_at_ms - started_at_ms),
        "expected_ms": _RESTART_EXPECTED_MS,
    }
    payload.update(_current_headline_context())
    await event_bus.broadcast("server:restart", payload, dedup=False)
    _discard_restart_event_cache()
    try:
        RESTART_NOTICE_STATE_PATH.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("could not clear restart notice state", exc_info=True)


async def _announce_restart() -> dict[str, int]:
    """Persist and broadcast the restart countdown exactly once per worker."""
    global _restart_notice_payload
    async with _restart_notice_lock:
        if _restart_notice_payload is not None:
            return _restart_notice_payload
        started_at_ms = int(time.time() * 1000)
        payload = {
            "phase": "countdown",
            "started_at_ms": started_at_ms,
            "countdown_ends_at_ms": started_at_ms + _RESTART_WARNING_SECONDS * 1000,
            "expected_ms": _RESTART_EXPECTED_MS,
        }
        _write_restart_notice({"started_at_ms": started_at_ms})
        await event_bus.broadcast("server:restart", payload, dedup=False)
        _discard_restart_event_cache()
        _restart_notice_payload = payload
        return payload
# Resource collector ring buffers survive hot reloads the same way the
# event bus does: snapshot on shutdown, restore on boot. Env-overridable
# for tests, mirroring DASHBOARD_EVENT_BUS_STATE.
RESOURCE_MONITOR_STATE_PATH = Path(
    os.environ.get("DASHBOARD_RESOURCE_MONITOR_STATE")
    or str(DATA_ROOT / "resource_monitor.state")
)
# The worktree monitor validates every restored row against current git-file
# fingerprints before use; this snapshot merely avoids rebuilding unchanged
# rows with hundreds of git subprocesses after a dashboard hot reload.
WORKTREE_ROW_CACHE_PATH = Path(
    os.environ.get("DASHBOARD_WORKTREE_ROW_CACHE_STATE")
    or str(DATA_ROOT / "worktree_row_cache.state")
)
# Labels always shown in pause UI even if not in dispatch.state
_KNOWN_PAUSE_LABELS = ["dashboard"]


# ── CLI Subprocess Helper ─────────────────────────────────────

async def run_cli(cmd: list[str], timeout: int = 30, stdin_data: str | None = None,
                  beads_dir=None) -> tuple[str, str, int]:
    """Run a CLI command async and return (stdout, stderr, returncode).

    A missing binary (e.g. no ``bd`` on a fresh deployment without the
    beads toolchain, DEPLOY.md) degrades to the same soft-error shape as
    a nonzero exit instead of 500ing every endpoint that shells out.

    ``BEADS_DIR`` is defaulted to ``DATA_ROOT / ".beads"`` (the same
    default ``agents/dispatcher.py`` resolves) so ``bd`` never falls back
    to cwd-walk discovery from this process's working directory — that
    walk found ``<repo>/.beads`` only while beads state lived on the code
    volume, and 8e3c3486 moved it to the state volume. An explicit
    ``BEADS_DIR`` in the environment still wins.
    """
    from tools.data_paths import beads_client_env
    env = dict(os.environ)
    env.update(beads_client_env(beads_dir))
    env.setdefault("BEADS_DIR", str(DATA_ROOT / ".beads"))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except (FileNotFoundError, PermissionError) as exc:
        return "", f"{cmd[0]}: {exc}", 127
    try:
        input_bytes = stdin_data.encode() if stdin_data is not None else None
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=input_bytes), timeout=timeout)
        return stdout.decode(), stderr.decode(), proc.returncode
    except asyncio.TimeoutError:
        proc.kill()
        return "", "timeout", -1


async def run_cli_json(
    cmd: list[str], timeout: int = 30, *, empty: list | dict | None = None,
    beads_dir=None,
) -> list | dict:
    """Run CLI command and parse JSON output.

    ``empty`` is returned when the binary itself is missing (rc 127 from
    ``run_cli``) — deployments without the beads toolchain get a real
    empty collection from list-shaped endpoints instead of an error
    object the frontend has to special-case (DEPLOY.md, clean-machine
    degradation). Left ``None``, the error shape passes through.

    ``beads_dir`` forwards to ``run_cli`` so a bead read can target a
    specific org's tracker (per-org databases); left None, bd uses the
    shared default.
    """
    stdout, stderr, rc = await run_cli(cmd, timeout, beads_dir=beads_dir)
    if rc == 127 and empty is not None:
        return empty
    if rc != 0 or not stdout.strip():
        return {"error": stderr or "no output", "returncode": rc}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"error": "invalid JSON", "raw": stdout[:500]}


# ── API Endpoints ─────────────────────────────────────────────

async def api_ping(request):
    """Trivial liveness probe — zero work: no DB, no subprocess, no I/O.

    Its round-trip latency is a direct measure of event-loop
    responsiveness. Fast-polling /api/ping during a session launch is the
    proof that the create/startup path isn't blocking the loop: if latency
    stays flat while an NG container boots, the dashboard API is properly
    async; if it spikes, something synchronous is hogging the loop.
    """
    return JSONResponse({"ok": True})

async def api_health(request):
    """Degradation snapshot (W6) — reconciliation-loop failure streak.

    Unlike /api/ping this does real work (reads in-process monitor state)
    but no I/O; safe to poll for a dashboard banner.
    """
    return JSONResponse(session_monitor.get_health())

async def api_beads_ready(request):
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse(dao_beads.get_open_beads())
    return JSONResponse(await run_cli_json(["bd", "ready", "--json"], empty=[]))

def _beads_request_org(request):
    """Return one selected tracker org without letting a bearer widen scope.

    Tracker existence is checked by each route so an explicit unknown slug can
    never degrade to ``org_beads_dir(None)`` and expose the default database.
    """
    requested_org = (request.query_params.get("org") or "").strip() or None
    pinned_org = api_auth.organization_scope_from_request(request)
    if pinned_org is not None:
        if requested_org is not None and requested_org != pinned_org:
            return None, JSONResponse(
                {"error": "cross-org access to another org's resource is not permitted"},
                status_code=403,
            )
        return pinned_org, None
    return requested_org, None


async def api_beads_list(request):
    org, refused = _beads_request_org(request)
    if refused is not None:
        return refused
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse(dao_beads.get_open_beads(limit=100))
    from tools.data_paths import org_beads_dir
    bd_dir = org_beads_dir(org)
    if org is not None and bd_dir is None:
        return JSONResponse([])
    kwargs = {"beads_dir": bd_dir} if bd_dir is not None else {}
    return JSONResponse(await run_cli_json(
        ["bd", "list", "--json", "-n", "100", "--sort", "updated"],
        empty=[], **kwargs,
    ))

async def api_bead_show(request):
    bead_id = request.path_params["id"]
    if os.environ.get("DASHBOARD_MOCK"):
        bead = dao_beads.get_bead(bead_id)
        if not bead:
            return JSONResponse({"error": "bead not found"}, status_code=404)
        return JSONResponse(bead)
    return JSONResponse(await run_cli_json(["bd", "show", bead_id, "--json"]))

async def api_bead_tree(request):
    bead_id = request.path_params["id"]
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse(dao_beads.get_bead_deps(bead_id))
    return JSONResponse(await run_cli_json(["bd", "dep", "tree", bead_id, "--json"], empty=[]))


async def api_bead_deps(request):
    """Return both blockers (down) and dependents (up) for a bead."""
    bead_id = request.path_params["id"]
    org, refused = _beads_request_org(request)
    if refused is not None:
        return refused
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse(dao_beads.get_bead_deps(bead_id))
    from tools.data_paths import org_beads_dir
    bd_dir = org_beads_dir(org)
    if org is not None and bd_dir is None:
        return JSONResponse({"blockers": [], "dependents": []})
    down, up = await asyncio.gather(
        run_cli_json(["bd", "dep", "list", bead_id, "--json"], empty=[], beads_dir=bd_dir),
        run_cli_json(["bd", "dep", "list", bead_id, "--direction=up", "--json"], empty=[], beads_dir=bd_dir),
    )
    blockers = down if isinstance(down, list) else []
    dependents = up if isinstance(up, list) else []
    return JSONResponse({"blockers": blockers, "dependents": dependents})


async def api_beads_search(request):
    """Search beads by title and description. Falls back to issues.jsonl if bd unavailable."""
    q = request.query_params.get("q", "").strip().lower()
    if not q:
        return JSONResponse({"error": "missing q parameter"})

    if os.environ.get("DASHBOARD_MOCK"):
        # Search mock beads by title/description
        beads = dao_beads.get_open_beads(limit=500)
        terms = q.split()
        results = [
            b for b in beads
            if all(t in f"{b.get('title', '')} {b.get('description', '')}".lower() for t in terms)
        ]
        return JSONResponse(results)

    # Try bd search first
    stdout, stderr, rc = await run_cli(["bd", "search", q, "--json"], timeout=10)
    if rc == 0 and stdout.strip():
        try:
            results = json.loads(stdout)
            if isinstance(results, list):
                return JSONResponse(results)
        except json.JSONDecodeError:
            pass

    # Fallback: read issues.jsonl directly and filter
    # Beads state lives on the STATE volume (auto-qk4ip), not beside the code.
    from tools.data_paths import DATA_ROOT as _DATA_ROOT
    issues_path = _DATA_ROOT / ".beads" / "issues.jsonl"
    if not issues_path.exists():
        return JSONResponse({"error": "no beads data found"})

    terms = q.split()
    results = []
    with open(issues_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                issue = json.loads(line)
            except json.JSONDecodeError:
                continue
            searchable = f"{issue.get('title', '')} {issue.get('description', '')}".lower()
            if all(term in searchable for term in terms):
                results.append(issue)
    return JSONResponse(results)


_TERMINAL_CREATED_BY_PREFIX = "terminal:"


def _author_session_for_bead(bead: dict | None) -> str | None:
    """Extract a tmux session id from ``bead.created_by`` if it is one.

    The dispatcher writes ``terminal:<session_id>`` for beads authored from
    a terminal session; everything else (manual entries, automation handles,
    e-mail addresses) is stripped. Returns the trimmed session id or None.
    """
    created_by = (bead or {}).get("created_by")
    if not isinstance(created_by, str):
        return None
    if not created_by.startswith(_TERMINAL_CREATED_BY_PREFIX):
        return None
    session_id = created_by[len(_TERMINAL_CREATED_BY_PREFIX):].strip()
    return session_id or None


def _build_dashboard_approval_envelope(bead_id: str, title: str) -> str:
    """Build the CrossTalk envelope delivered for a dashboard-approved bead.

    Sender is the synthetic ``dashboard`` system identity — there is no
    authenticated session, only the proxy "approval came via dashboard ⇒
    a human did it." Source / turn / harness / model are intentionally
    blank; the envelope still validates against the receive-side parser.
    """
    iso_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    safe_title = (title or bead_id).replace('"', "'")
    body = (
        f"Bead {bead_id} ({safe_title}) approved for dispatch via dashboard."
    )
    return (
        f'<crosstalk from="dashboard"\n'
        f'           label="dashboard approval"\n'
        f'           source="" turn=""\n'
        f'           harness="" model=""\n'
        f'           timestamp="{iso_now}">\n'
        f'{body}\n'
        f'</crosstalk>'
    )


async def _send_dashboard_approval_nag(
    bead_id: str, title: str, *, session_id: str
) -> None:
    """Targeted launch nag for a dashboard-approved bead.

    Bypasses the global ``dispatch_nag`` flag — this is per-bead targeting
    aimed at the authoring session only. CLI approvals never call this; the
    dashboard endpoint is the sole entry point.
    """
    envelope = _build_dashboard_approval_envelope(bead_id, title)
    await tmux_send(session_id, envelope)


async def _maybe_send_dashboard_approval_nag(bead_id: str, org: str | None = None) -> None:
    """If ``bead_id`` was authored by a live terminal session, ping it.

    Best-effort: any DAO failure or unexpected exception is swallowed so a
    nag-side hiccup never fails the underlying approval. The approval has
    already succeeded by the time this runs.
    """
    try:
        if org is None:
            bead = await asyncio.to_thread(dao_beads.get_bead, bead_id)
        else:
            bead = await asyncio.to_thread(dao_beads.get_bead, bead_id, org)
    except Exception:
        logger.exception(
            "dashboard approval nag: get_bead failed for %s (best-effort)",
            bead_id,
        )
        return

    session_id = _author_session_for_bead(bead)
    if not session_id:
        return

    try:
        is_live = await asyncio.to_thread(
            dashboard_db.is_session_live, session_id
        )
    except Exception:
        logger.exception(
            "dashboard approval nag: liveness check failed for %s "
            "(best-effort)", session_id,
        )
        return
    if not is_live:
        return

    title = (bead or {}).get("title") or bead_id
    try:
        await _send_dashboard_approval_nag(
            bead_id, title, session_id=session_id,
        )
    except Exception:
        logger.exception(
            "dashboard approval nag: send failed for %s -> %s "
            "(best-effort)", bead_id, session_id,
        )


async def api_bead_approve(request):
    """Set readiness=approved on a bead, releasing it for dispatch.

    On success, ping the authoring session via CrossTalk if it was created
    from a live terminal session (``created_by="terminal:<session_id>"``).
    The CLI approval path (``cmd_dispatch_approve``) does NOT trigger this
    targeted nag — only this dashboard endpoint does, on the proxy that
    "approval came via dashboard ⇒ a human did it."
    """
    bead_id = request.path_params["id"]
    org, refused = _beads_request_org(request)
    if refused is not None:
        return refused
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"ok": True, "bead_id": bead_id})
    from tools.data_paths import org_beads_dir
    bd_dir = org_beads_dir(org)
    if org is not None and bd_dir is None:
        return JSONResponse(
            {"error": "organization has no bead tracker", "ok": False},
            status_code=404,
        )
    kwargs = {"beads_dir": bd_dir} if bd_dir is not None else {}
    stdout, stderr, rc = await run_cli(
        ["bd", "set-state", bead_id, "readiness=approved",
         "--reason", "dashboard: approved for dispatch"], **kwargs,
    )
    if rc != 0:
        return JSONResponse({"error": stderr.strip(), "ok": False}, status_code=400)
    await _maybe_send_dashboard_approval_nag(bead_id, org)
    return JSONResponse({"ok": True, "bead_id": bead_id})

async def api_pinned_beads(request):
    """Return beads with the 'pinned' label."""
    beads = await asyncio.to_thread(dao_beads.get_beads_by_label, "pinned")
    return JSONResponse(beads)

# ── Dispatch pause state helpers ──────────────────────────────

def _read_dispatch_state() -> dict:
    """Read dispatch.state file. Returns {} if missing or invalid."""
    try:
        return json.loads(DISPATCH_STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_dispatch_state(state: dict) -> None:
    """Write dispatch.state atomically via rename."""
    DISPATCH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DISPATCH_STATE_PATH.with_suffix(".state.tmp")
    tmp.write_text(json.dumps(state))
    tmp.rename(DISPATCH_STATE_PATH)


def _get_pause_state() -> dict:
    """Return pause state for all known labels (always includes _KNOWN_PAUSE_LABELS)."""
    raw = _read_dispatch_state()
    result = {label: bool(raw.get(label, False)) for label in _KNOWN_PAUSE_LABELS}
    for label, paused in raw.items():
        if label.endswith("_reason"):
            continue  # Skip reason keys — handled by _get_pause_reasons
        if label not in result:
            result[label] = bool(paused)
    return result


def _get_pause_reasons() -> dict:
    """Return pause reasons for labels that have them.

    Reads {label}_reason keys from dispatch.state and returns {label: reason_string}.
    Only includes labels that are currently paused AND have a reason stored.
    """
    raw = _read_dispatch_state()
    reasons = {}
    for key, value in raw.items():
        if key.endswith("_reason") and isinstance(value, str) and value:
            label = key[:-len("_reason")]
            if raw.get(label):  # Only include if label is actually paused
                reasons[label] = value
    return reasons


async def api_librarian_enqueue(request):
    """POST /api/librarians/jobs {job_type, payload} -> {job_id}.

    The API door onto the librarian queue. The prompt is built HERE, at
    enqueue time, purely for validation — a typo'd mission id or a missing
    report_to fails this request loudly instead of launching a container
    aimed at nothing. The dispatcher rebuilds it at launch from the stored
    payload, so what runs reflects the payload, not this preview.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    job_type = body.get("job_type")
    payload = body.get("payload") or {}
    if not isinstance(job_type, str) or not job_type or not isinstance(payload, dict):
        return JSONResponse(
            {"error": "job_type (string) and payload (object) are required"},
            status_code=400)
    try:
        from agents.dispatcher import _build_librarian_prompt
        _build_librarian_prompt(job_type, payload)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:
        logger.warning("librarian enqueue preflight failed; enqueueing anyway",
                       exc_info=True)
    from agents.librarian_db import enqueue as _enqueue_librarian_job
    job_id = _enqueue_librarian_job(job_type, payload=json.dumps(payload))
    return JSONResponse({"job_id": job_id, "job_type": job_type,
                         "status": "pending"}, status_code=201)


async def api_dispatch_pause_get(request):
    """GET /api/dispatch/pause — return current pause state for all label queues."""
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"paused": {}, "reasons": {}})
    return JSONResponse({"paused": _get_pause_state(), "reasons": _get_pause_reasons()})


async def api_dispatch_pause_post(request):
    """POST /api/dispatch/pause — set pause state for a label queue.

    Body: {"label": "dashboard", "paused": true}
    Returns updated full pause state with reasons.
    """
    body = await request.json()
    label = body.get("label")
    paused = bool(body.get("paused", False))
    if not label:
        return JSONResponse({"error": "label required"}, status_code=400)
    state = _read_dispatch_state()
    if paused:
        state[label] = True
    else:
        state.pop(label, None)
        state.pop(f"{label}_reason", None)  # Clear reason on unpause
    _write_dispatch_state(state)
    new_pause = _get_pause_state()
    new_reasons = _get_pause_reasons()
    # Broadcast updated pause state + reasons via SSE
    await event_bus.broadcast("dispatch_pause", {"paused": new_pause, "reasons": new_reasons})
    return JSONResponse({"paused": new_pause, "reasons": new_reasons})


async def api_dispatch_limits_get(request):
    """GET /api/dispatch/limits — effective dispatch concurrency limits."""
    limits = await asyncio.to_thread(_resolved_dispatch_limits)
    return JSONResponse(limits)


async def api_dispatch_limits_post(request):
    """POST /api/dispatch/limits — operator-tunable, settings-backed.

    Body: {"bead_max_concurrent": int?, "agentic_max_concurrent": int?}.
    Persists to the machine-homed autonomy.dispatch.limits#1 singleton;
    the dispatcher picks the bead limit up next cycle, the agentic cap
    applies on the next dispatch request.
    """
    from tools.graph.schemas import dispatch_limits as _dl
    from tools.graph.schemas.registry import (
        SchemaValidationError, validate_payload,
    )
    body = await request.json()
    current = await asyncio.to_thread(_resolved_dispatch_limits)
    payload = dict(current)
    for name in ("bead_max_concurrent", "agentic_max_concurrent"):
        if name in body:
            payload[name] = body[name]
    try:
        validate_payload(_dl.SET_ID, _dl.SCHEMA_REVISION, payload)
    except SchemaValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    await asyncio.to_thread(
        graph_ops.upsert_by_key,
        _dl.SET_ID, _dl.SCHEMA_REVISION, "default", payload, org="machine",
    )
    await event_bus.broadcast("dispatch_limits", payload, dedup=False)
    return JSONResponse(payload)


async def api_dispatch_resume(request):
    """POST /api/dispatch/resume — clear auth-failure pause so dispatcher resumes launching."""
    if os.environ.get("DASHBOARD_MOCK"):
        await event_bus.broadcast("dispatcher_state", {"paused": False, "reason": None})
        return JSONResponse({"ok": True, "was_paused": False, "cleared_reason": None})
    was_paused = is_paused()
    reason = get_pause_reason() if was_paused else None
    clear_paused()
    # Broadcast cleared state so all clients update immediately
    await event_bus.broadcast("dispatcher_state", {"paused": False, "reason": None})
    return JSONResponse({"ok": True, "was_paused": was_paused, "cleared_reason": reason})


async def api_dispatch_resume_bead(request):
    """POST /api/dispatch/resume/{bead_id} — revive a dead dispatch interactively.

    Finds the most recent dead dispatch row for ``bead_id`` and registers a
    NEW interactive container session with the monitor that points at the
    original JSONL. The dead row stays dead (history intact); the new row
    shows up on the Active list because its type is ``container``.
    """
    bead_id = request.path_params["bead_id"]
    from tools.dashboard.dao.dashboard_db import get_conn as _get_conn
    import uuid as _uuid
    import time as _time

    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM tmux_sessions"
        " WHERE bead_id=? AND type='dispatch' AND state IN ('ENDED','FAILED')"
        " ORDER BY created_at DESC LIMIT 1",
        (bead_id,),
    ).fetchone()
    if row is None:
        return JSONResponse(
            {"error": "No dead dispatch session found for bead",
             "bead_id": bead_id},
            status_code=404,
        )

    orig_tmux = row["tmux_name"]
    new_tmux = f"{bead_id}-resume-{int(_time.time())}"
    new_uuid = str(_uuid.uuid4())
    project = row["project"]
    jsonl_path = row["jsonl_path"]
    resolution_dir = row["resolution_dir"] or (
        str(Path(jsonl_path).parent) if jsonl_path else None
    )

    await session_monitor.register_session(
        tmux_name=new_tmux,
        type="container",
        jsonl_path=Path(jsonl_path) if jsonl_path else None,
        run_dir=Path(resolution_dir).parent if resolution_dir else None,
        bead_id=bead_id,
        project=project,
    )
    # register() inserts the session with the UUID derived from jsonl_path.
    # For resume, we want a NEW session_uuid so the Active card does not
    # collide with the dead row's identity.
    conn.execute(
        "UPDATE tmux_sessions SET session_uuid=? WHERE tmux_name=?",
        (new_uuid, new_tmux),
    )
    conn.commit()

    return JSONResponse({
        "ok": True,
        "resumed_from": orig_tmux,
        "tmux_session": new_tmux,
        "session_uuid": new_uuid,
        "bead_id": bead_id,
    }, status_code=201)


async def api_dispatch_pause_state(request):
    """GET /api/dispatch/pause-state — return dispatcher pause state from SQLite."""
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"paused": False, "reason": None})
    paused = is_paused()
    reason = get_pause_reason() if paused else None
    return JSONResponse({"paused": paused, "reason": reason})


def _get_dispatcher_state() -> dict:
    """Read dispatcher pause state and merge health from SQLite/git for SSE broadcast."""
    paused = is_paused()
    reason = get_pause_reason() if paused else None

    # Check for UU (unmerged) files that block all merges
    merge_health: dict = {"status": "ok"}
    try:
        porcelain = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, timeout=5,
            cwd=str(_REPO_ROOT),
        ).stdout
        uu_files = [line for line in porcelain.splitlines() if line[:2] == "UU"]
        if uu_files:
            merge_health = {
                "status": "blocked",
                "reason": f"UU: {uu_files[0][3:].strip()}",
                "count": len(uu_files),
            }
    except Exception:
        pass  # Non-critical — don't break SSE on git failure

    return {"paused": paused, "reason": reason, "merge_health": merge_health}


async def api_dispatch_status(request):
    """Show dispatched beads with their dispatch state.

    Reads the dispatch dimension (queued/launching/running/collecting/merging/done/failed)
    from bd labels + docker ps for containers. Adds currently-running runs from SQLite.
    """
    if os.environ.get("DASHBOARD_MOCK"):
        running = dao_dispatch.get_running_with_stats()
        return JSONResponse({
            "claimed": [],
            "dispatching": [],
            "containers": [],
            "running_runs": running,
        })
    claimed = await run_cli_json(["bd", "query", 'label="work:claimed"', "--json"], empty=[])
    # Also query beads with active dispatch states for richer status
    dispatching = await run_cli_json(["bd", "query", 'label="dispatch:running" OR label="dispatch:launching" OR label="dispatch:collecting" OR label="dispatch:merging" OR label="dispatch:queued"', "--json"], empty=[])
    # Containers are still useful for runtime info (uptime, image)
    stdout, _, _ = await run_cli(["docker", "ps", "--filter", "name=agent-", "--format", '{"name":"{{.Names}}","status":"{{.Status}}","image":"{{.Image}}"}'])
    containers = []
    for line in stdout.strip().splitlines():
        if line:
            try:
                containers.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    # Currently running runs from SQLite (started but no status yet)
    running_runs = await asyncio.to_thread(get_currently_running)
    return JSONResponse({
        "claimed": claimed if isinstance(claimed, list) else [],
        "dispatching": dispatching if isinstance(dispatching, list) else [],
        "containers": containers,
        "running_runs": running_runs,
    })


async def api_dispatch_approved(request):
    """Return approved beads split into waiting (unblocked) vs blocked.

    For each approved bead, checks dependencies via `bd dep list`.
    Blocked beads include their open blockers so the frontend can link to them.
    """
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse(dao_beads.get_dispatch_beads())
    all_beads = await run_cli_json(["bd", "list", "--json", "-n", "100"], empty=[])
    bead_list = all_beads if isinstance(all_beads, list) else []

    # Filter to open, approved beads not currently being dispatched
    dispatch_labels = {
        "dispatch:queued", "dispatch:launching", "dispatch:running",
        "dispatch:collecting", "dispatch:merging",
    }
    approved = []
    for b in bead_list:
        if b.get("status") != "open":
            continue
        labels = set(b.get("labels") or [])
        if "readiness:approved" not in labels:
            continue
        if labels & dispatch_labels:
            continue
        approved.append(b)

    # Check dependencies for each approved bead in parallel
    async def check_deps(bead):
        dep_data = await run_cli_json(["bd", "dep", "list", bead["id"], "--json"], empty=[])
        if not isinstance(dep_data, list):
            return bead, []
        open_blockers = []
        for dep in dep_data:
            if not isinstance(dep, dict):
                continue
            if dep.get("dependency_type") == "parent-child":
                continue
            if dep.get("status") != "closed":
                open_blockers.append({
                    "id": dep.get("id", ""),
                    "title": dep.get("title", ""),
                    "status": dep.get("status", ""),
                    "priority": dep.get("priority"),
                })
        return bead, open_blockers

    results = await asyncio.gather(*(check_deps(b) for b in approved))

    waiting = []
    blocked = []
    for bead, blockers in results:
        if blockers:
            blocked.append({**bead, "blockers": blockers})
        else:
            waiting.append(bead)

    return JSONResponse({"waiting": waiting, "blocked": blocked})


AGENT_RUNS_DIR = Path(os.environ.get(
    "DASHBOARD_AGENT_RUNS_DIR",
    str(Path(__file__).parent.parent.parent / "data" / "agent-runs"),
))

# Host (terminal) sessions run on the host filesystem, not in a container,
# so they have no data/agent-runs/<name>-* run dir to drop uploads into.
# Their attachments land here, one subdir per session, where the host agent
# can read them directly and api_session_output can serve them back to the
# viewer tile — the host-session counterpart of a container run dir's
# ``.uploads`` folder.
HOST_UPLOADS_DIR = Path(__file__).parent.parent.parent / "data" / "host-uploads"


def _resolve_agentic_identity(agentic_source_id: str | None) -> dict:
    """Resolve agentic identity fields from the agentic source row.

    The agentic source's own title is the action's display label
    (e.g. "Update Title & Summary"); the target asset metadata stores a
    single ``target_source_id`` plus ``target_kind`` to interpret it.
    Returns a dict with: ``action_label``, ``member_key``,
    ``target_kind``, ``target_source_id``, ``target_org``,
    ``dispatched_by_session``, ``harness``, ``model``, and ``title``
    (target asset's title, or action_label as fallback).

    Single source of truth used by ``_enrich_dispatch_runs``,
    ``_enrich_timeline_agentic``, and the live-active list builder so
    every code path that surfaces an agentic dispatch resolves
    identity the same way.
    """
    out: dict = {
        "action_label": None,
        "member_key": None,
        "target_kind": None,
        "target_source_id": None,
        "target_org": None,
        "dispatched_by_session": None,
        "harness": None,
        "model": None,
        "title": None,
    }
    if not agentic_source_id:
        return out
    try:
        src = graph_ops.get_source(agentic_source_id)
    except Exception:  # noqa: BLE001
        src = None
    if not src:
        return out
    out["action_label"] = src.get("title") or None
    meta = src.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    out["member_key"] = meta.get("member_key") or None
    out["target_kind"] = meta.get("target_kind") or None
    out["target_source_id"] = meta.get("target_source_id") or None
    out["target_org"] = meta.get("target_org") or None
    out["dispatched_by_session"] = meta.get("dispatched_by_session") or None
    out["harness"] = meta.get("harness") or None
    out["model"] = meta.get("model") or None
    if out["target_kind"] == "bead" and out["target_source_id"]:
        try:
            bead = dao_beads.get_bead(out["target_source_id"])
        except Exception:  # noqa: BLE001
            bead = None
        if bead and bead.get("title"):
            out["title"] = str(bead["title"])
    elif out["target_source_id"]:
        try:
            tsrc = graph_ops.get_source(out["target_source_id"])
        except Exception:  # noqa: BLE001
            tsrc = None
        if tsrc and tsrc.get("title"):
            out["title"] = str(tsrc["title"])
    if not out["title"]:
        out["title"] = out["action_label"]
    return out


def _enrich_dispatch_runs(runs: list[dict]) -> None:
    """Add smoke_result and librarian_review to dispatch run dicts in-place."""
    # Smoke results — read from each run's output directory
    for run in runs:
        run["smoke_result"] = _read_smoke_result(run.get("_output_dir") or None)

    # Agentic identity: resolve action_label / target_* / sender / title
    # via the shared helper so all dispatch-surfacing endpoints agree.
    for run in runs:
        if run.get("kind") != "agentic" or not run.get("agentic_source_id"):
            continue
        ident = _resolve_agentic_identity(run["agentic_source_id"])
        run["action_label"] = ident["action_label"]
        run["member_key"] = ident["member_key"]
        run["target_kind"] = ident["target_kind"]
        run["target_source_id"] = ident["target_source_id"]
        run["target_org"] = ident["target_org"]
        run["dispatched_by_session"] = ident["dispatched_by_session"]
        if ident["title"]:
            run["title"] = ident["title"]

    # Librarian entries — read review from their own output_dir
    for run in runs:
        if run.get("_librarian_type"):
            run["librarian_review"] = _read_librarian_results(
                run.get("_output_dir") or None
            )

    # Regular dispatch entries — bulk-query librarian_jobs for review results
    regular_runs = [
        r for r in runs
        if not r.get("_librarian_type") and r.get("_run_id")
    ]
    if not regular_runs:
        return

    run_ids = [r["_run_id"] for r in regular_runs]
    placeholders = ",".join("?" * len(run_ids))
    sql = f"""
        SELECT
            json_extract(lj.payload, '$.run_id') AS dispatch_run_id,
            lj.status AS job_status,
            dr.output_dir AS lib_output_dir
        FROM librarian_jobs lj
        LEFT JOIN dispatch_runs dr ON (
            dr.librarian_type = lj.job_type
            AND dr.id LIKE 'librarian-' || lj.job_type || '-' || substr(lj.id, 1, 8) || '-%'
        )
        WHERE lj.job_type = 'review_report'
        AND json_extract(lj.payload, '$.run_id') IN ({placeholders})
    """
    try:
        conn = _timeline_conn()
        rows = conn.execute(sql, run_ids).fetchall()
        conn.close()
    except Exception:
        return

    reviews: dict[str, dict] = {}
    for row in rows:
        run_id = row["dispatch_run_id"]
        if run_id in reviews:
            continue
        if row["job_status"] == "running":
            reviews[run_id] = {"status": "running"}
        elif row["job_status"] == "done":
            results = _read_librarian_results(row["lib_output_dir"])
            reviews[run_id] = results if results is not None else {"status": "done"}

    for run in regular_runs:
        run["librarian_review"] = reviews.get(run["_run_id"])

    # ── Experience report summary ────────────────────────────
    for run in runs:
        output_dir = run.get("_output_dir")
        if output_dir:
            exp_path = Path(output_dir) / "experience_report.md"
            try:
                if exp_path.exists():
                    lines = exp_path.read_text().strip().split("\n")[:5]
                    run["experience_summary"] = "\n".join(lines)
            except OSError:
                pass

    # ── Validation + pitfall notes (batched graph query) ─────
    try:

        # Collect bead IDs from regular runs
        bead_ids = {r["bead_id"] for r in regular_runs if r.get("bead_id")}

        # Batch query: validation notes
        if bead_ids:
            val_notes = graph_ops.list_sources(source_type="note", tags=["validation"], limit=200)
            val_by_bead: dict[str, dict] = {}
            for n in val_notes:
                title = n.get("title") or ""
                for bid in bead_ids:
                    if bid in title and bid not in val_by_bead:
                        val_by_bead[bid] = {"source_id": n["id"][:12], "title": title[:80]}
            for run in regular_runs:
                bid = run.get("bead_id")
                if bid and bid in val_by_bead:
                    run["validation"] = val_by_bead[bid]

        # Batch query: pitfall notes — single query covering all run time windows
        earliest_start = min(
            (r["_started_at"] for r in regular_runs if r.get("_started_at")),
            default=None,
        )
        if earliest_start:
            pitfall_notes = graph_ops.list_sources(
                source_type="note", tags=["pitfall"], since=earliest_start, limit=500,
            )
            for run in regular_runs:
                started = run.get("_started_at")
                completed = run.get("_completed_at")
                if not started or not completed:
                    continue
                matched = [
                    p for p in pitfall_notes
                    if started <= (p.get("created_at") or "") <= completed
                ]
                if matched:
                    run["pitfalls"] = [
                        {"id": p["id"][:12], "title": (p.get("title") or "")[:60]}
                        for p in matched
                    ]
    except Exception:
        pass


def _enrich_librarian_fields(runs: list[dict]) -> None:
    """Populate librarian-specific fields: synthetic title, fallback bead_id."""
    for run in runs:
        librarian_type = run.get("librarian_type")
        if not librarian_type:
            continue
        if not run.get("bead_id"):
            run["bead_id"] = run.get("dir") or run.get("id") or ""
        if not run.get("title"):
            run["title"] = f"Librarian: {librarian_type}"


async def api_dispatch_runs(request):
    """List dispatch runs from SQLite (includes RUNNING rows)."""
    if os.environ.get("DASHBOARD_MOCK"):
        all_runs = dao_dispatch.get_recent_runs(limit=50)
        running = dao_dispatch.get_running_with_stats()
        combined = [*running, *all_runs]
        _enrich_librarian_fields(combined)
        return JSONResponse(combined)
    db_rows = await asyncio.to_thread(list_runs)
    runs = []
    for row in db_rows:
        # Reconstruct decision dict from flat columns for backward compat
        decision = None
        if row.get("status") and row["status"] != "RUNNING":
            decision = {"status": row["status"], "reason": row.get("reason")}
            scores = {}
            for key in ("tooling", "clarity", "confidence"):
                val = row.get(f"score_{key}")
                if val is not None:
                    scores[key] = val
            if scores:
                decision["scores"] = scores
            time_breakdown = {}
            for db_key, dec_key in [
                ("time_research_pct", "research_pct"),
                ("time_coding_pct", "coding_pct"),
                ("time_debugging_pct", "debugging_pct"),
                ("time_tooling_pct", "tooling_workaround_pct"),
            ]:
                val = row.get(db_key)
                if val is not None:
                    time_breakdown[dec_key] = val
            if time_breakdown:
                decision["time_breakdown"] = time_breakdown
            if row.get("failure_category"):
                decision["failure_category"] = row["failure_category"]
            if row.get("discovered_beads_count"):
                decision["discovered_beads_count"] = row["discovered_beads_count"]

        # Derive timestamp from completed_at, started_at, or dir name
        timestamp = ""
        if row.get("completed_at"):
            # completed_at is "YYYY-MM-DD HH:MM:SS" — convert to YYYYMMDD-HHMMSS
            ts = row["completed_at"].replace("-", "").replace(":", "").replace(" ", "-")
            timestamp = ts[:8] + "-" + ts[8:]  # YYYYMMDD-HHMMSS
        elif row.get("started_at"):
            ts = row["started_at"].replace("-", "").replace(":", "").replace(" ", "-")
            timestamp = ts[:8] + "-" + ts[8:]
        elif row.get("id"):
            parts = row["id"].rsplit("-", 2)
            if len(parts) >= 3:
                timestamp = f"{parts[1]}-{parts[2]}"

        librarian_type = row.get("librarian_type") or None
        # Coalesce legacy NULL kind → 'bead'. The DAO already does this for
        # rows it returns, but list_runs() bypasses the DAO and reads
        # dispatch.db directly via the dispatcher's writer connection — so
        # NULL can leak through here.
        kind = row.get("kind") or "bead"
        dir_name = row.get("id", "")
        bead_id = row.get("bead_id", "")
        agentic_source_id = row.get("agentic_source_id") or None
        # Librarian runs have empty bead_id — use dir name as identifier so
        # the librarian review URL still resolves to the run's output dir.
        if not bead_id and librarian_type:
            bead_id = dir_name
        # ``kind='agentic'`` rows are addressed by ``agentic_source_id``
        # in the UI (the click routes to ``/graph/<asset_id>``), so we
        # leave ``bead_id`` empty here. Stuffing the run-id into bead_id
        # used to make the timeline route to ``/bead/<run-id>`` (404).
        # Synthetic title for librarian runs; agentic rows get their
        # title from the agentic source's row in step _enrich_dispatch_runs.
        title = None
        if librarian_type:
            title = f"Librarian: {librarian_type}"

        # auto-wvdhs: optional journal entry the agent wrote during wrap-up.
        # Reconstruct the {source_id, compact} dict only when the row carries
        # a non-empty source id; absent is the common case.
        journal_entry = None
        j_sid = row.get("journal_source_id")
        if j_sid:
            journal_entry = {
                "source_id": j_sid,
                "compact": row.get("journal_compact") or "",
            }

        runs.append({
            "bead_id": bead_id,
            "timestamp": timestamp,
            "dir": dir_name,
            "decision": decision,
            "status": row.get("status") or "",
            "has_experience_report": bool(row.get("has_experience_report")),
            "commit_hash": row.get("commit_hash") or "",
            "branch": row.get("branch") or "",
            "duration_secs": row.get("duration_secs"),
            "lines_added": row.get("lines_added"),
            "lines_removed": row.get("lines_removed"),
            "files_changed": row.get("files_changed"),
            "commit_message": row.get("commit_message") or "",
            "smoke_result": None,
            "librarian_review": None,
            "librarian_type": librarian_type,
            "kind": kind,
            "title": title,
            # ``agentic_source_id`` is the typed pointer for kind='agentic'
            # rows. Front-end ``routeForRun`` reads this to compose
            # ``/graph/<asset_id>``. None for bead/librarian rows.
            "agentic_source_id": agentic_source_id,
            "journal_entry": journal_entry,
            # internal fields for enrichment — stripped before response
            "_run_id": row.get("id", ""),
            "_output_dir": row.get("output_dir") or "",
            "_librarian_type": librarian_type,
            "_started_at": row.get("started_at") or "",
            "_completed_at": row.get("completed_at") or "",
        })

    await asyncio.to_thread(_enrich_dispatch_runs, runs)

    for run in runs:
        run.pop("_run_id", None)
        run.pop("_output_dir", None)
        run.pop("_librarian_type", None)
        run.pop("_started_at", None)
        run.pop("_completed_at", None)

    return JSONResponse(runs)


def _normalize_bead_show_payload(payload):
    """Normalize ``bd show --json`` / mock bead payloads to one dict or None."""
    if isinstance(payload, list):
        return payload[0] if payload else None
    return payload if isinstance(payload, dict) else None


def _bead_readiness(bead: dict | None) -> str:
    if not bead:
        return "idea"
    labels = bead.get("labels") or []
    for label in labels:
        if isinstance(label, str) and label.startswith("readiness:"):
            return label.split(":", 1)[1]
    return "idea"


async def api_dispatch_wait(request):
    """Return poll status for ``graph wait``."""
    bead_id = request.path_params["bead_id"]

    if os.environ.get("DASHBOARD_MOCK"):
        bead = dao_beads.get_bead(bead_id)
        runs = dao_dispatch.get_runs_for_bead(bead_id)
    else:
        bead = _normalize_bead_show_payload(await run_cli_json(["bd", "show", bead_id, "--json"]))
        runs = await asyncio.to_thread(get_runs_for_bead, bead_id)

    if not bead or bead.get("error"):
        return JSONResponse({"error": "bead not found"}, status_code=404)

    readiness = _bead_readiness(bead)
    if readiness != "approved":
        return JSONResponse(
            {
                "bead_id": bead_id,
                "readiness": readiness,
                "state": "unapproved",
            }
        )

    running = next((run for run in runs if run.get("status") == "RUNNING"), None)
    if running is not None:
        return JSONResponse(
            {
                "bead_id": bead_id,
                "readiness": readiness,
                "state": "running",
            }
        )

    completed = next((run for run in runs if run.get("completed_at")), None)
    if completed is not None:
        return JSONResponse(
            {
                "bead_id": bead_id,
                "readiness": readiness,
                "state": "completed",
                "run": completed,
            }
        )

    return JSONResponse(
        {
            "bead_id": bead_id,
            "readiness": readiness,
            "state": "waiting",
        }
    )


async def api_dispatch_reset(request):
    """Reset a bead's dispatch circuit breaker on the host dispatcher DB.

    Containerized ``graph dispatch reset`` cannot consult its local
    ``data/dispatch.db`` because that file is not the host dispatcher's source
    of truth. This API surfaces the reset operation against the live host DB so
    container sessions can clear failure streaks correctly.
    """
    bead_id = request.path_params["bead_id"]
    agent_fails, merge_fails = await asyncio.to_thread(
        get_consecutive_failures, bead_id,
    )
    if agent_fails == 0 and merge_fails == 0:
        return JSONResponse({
            "bead_id": bead_id,
            "reset": False,
            "agent_failures": 0,
            "merge_failures": 0,
        })

    run_id = await asyncio.to_thread(reset_circuit_breaker, bead_id)
    agent_after, merge_after = await asyncio.to_thread(
        get_consecutive_failures, bead_id,
    )
    return JSONResponse({
        "bead_id": bead_id,
        "reset": True,
        "agent_failures": agent_fails,
        "merge_failures": merge_fails,
        "run_id": run_id,
        "agent_failures_after": agent_after,
        "merge_failures_after": merge_after,
    })


# ── Timeline API ─────────────────────────────────────────────

_RANGE_MAP = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
    "3d": timedelta(days=3),
    "7d": timedelta(days=7),
    "14d": timedelta(days=14),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}


def _timeline_conn() -> sqlite3.Connection:
    """Get a read-only connection with row_factory for timeline queries."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _parse_range(range_str: str) -> str | None:
    """Convert range param to a UTC datetime cutoff string, or None for 'all'."""
    if not range_str or range_str == "all":
        return None
    td = _RANGE_MAP.get(range_str)
    if td is None:
        # Try parsing Nd or Nh patterns
        m = re.match(r"^(\d+)([dhm])$", range_str)
        if not m:
            return None
        val, unit = int(m.group(1)), m.group(2)
        if unit == "d":
            td = timedelta(days=val)
        elif unit == "h":
            td = timedelta(hours=val)
        elif unit == "m":
            td = timedelta(minutes=val)
    cutoff = datetime.now(timezone.utc) - td
    return cutoff.strftime("%Y-%m-%d %H:%M:%S")


def _build_timeline_where(
    range_str: str | None, project: str | None, q: str | None
) -> tuple[str, list]:
    """Build WHERE clause and params for timeline queries.

    Always excludes RUNNING rows — timeline shows completed work only.
    """
    clauses = ["status != 'RUNNING'"]
    params = []

    # Time range filter
    cutoff = _parse_range(range_str) if range_str else None
    if cutoff:
        clauses.append("completed_at >= ?")
        params.append(cutoff)

    # Project filter — match against bead_id prefix or image name
    if project:
        clauses.append("(bead_id LIKE ? OR image LIKE ?)")
        params.append(f"{project}%")
        params.append(f"%{project}%")

    # Text search — LIKE against bead_id, reason, commit_message
    if q:
        terms = q.strip().split()
        for term in terms:
            like = f"%{term}%"
            clauses.append(
                "(bead_id LIKE ? OR reason LIKE ? OR commit_message LIKE ?)"
            )
            params.extend([like, like, like])

    where = " AND ".join(clauses) if clauses else "1=1"
    return where, params


def _row_to_timeline_entry(row: sqlite3.Row) -> dict:
    """Convert a dispatch_runs row to a timeline entry dict."""
    scores = {}
    for key in ("tooling", "clarity", "confidence"):
        val = row[f"score_{key}"]
        if val is not None:
            scores[key] = val

    time_breakdown = {}
    for db_key, out_key in [
        ("time_research_pct", "research_pct"),
        ("time_coding_pct", "coding_pct"),
        ("time_debugging_pct", "debugging_pct"),
        ("time_tooling_pct", "tooling_workaround_pct"),
    ]:
        val = row[db_key]
        if val is not None:
            time_breakdown[out_key] = val

    librarian_type = row["librarian_type"] or None
    # Coalesce legacy NULL kind → 'bead'. Direct timeline reads bypass the
    # dispatch DAO's _coerce_kind, so we normalize here.
    try:
        kind = row["kind"] or "bead"
    except (IndexError, KeyError):
        kind = "bead"
    try:
        agentic_source_id = row["agentic_source_id"] or None
    except (IndexError, KeyError):
        agentic_source_id = None
    # auto-wvdhs: journal_entry is None unless the agent wrote one during
    # wrap-up. Pre-migration schemas may not have these columns; tolerate.
    journal_entry = None
    try:
        j_sid = row["journal_source_id"]
    except (IndexError, KeyError):
        j_sid = None
    if j_sid:
        try:
            j_compact = row["journal_compact"] or ""
        except (IndexError, KeyError):
            j_compact = ""
        journal_entry = {"source_id": j_sid, "compact": j_compact}
    return {
        "run_id": row["id"] or "",
        "bead_id": row["bead_id"] or "",
        "title": librarian_type or row["bead_id"] or "",  # librarian type or bead_id
        "priority": None,  # not stored in dispatch_runs
        "status": row["status"] or "",
        "reason": row["reason"] or "",
        "duration_secs": row["duration_secs"],
        "commit_hash": row["commit_hash"] or "",
        "commit_message": row["commit_message"] or "",
        # auto-ecmss: branch + container_name surface the source worktree
        # for ``kind='worktree-merge'`` rows (used by the timeline title
        # "Worktree merge — from auto-XXXXX"). Bead/agentic rows
        # populate these too, but the timeline doesn't read them — so
        # this is additive, not a behavior change for existing cards.
        "branch": row["branch"] or "",
        "container_name": row["container_name"] or "",
        "lines_added": row["lines_added"],
        "lines_removed": row["lines_removed"],
        "files_changed": row["files_changed"],
        "scores": scores or None,
        "time_breakdown": time_breakdown or None,
        "failure_category": row["failure_category"] or None,
        "discovered_beads_count": row["discovered_beads_count"],
        "started_at": (row["started_at"] + "Z") if row["started_at"] else None,
        "completed_at": (row["completed_at"] + "Z") if row["completed_at"] else None,
        "has_experience_report": bool(row["has_experience_report"]),
        "token_count": row["token_count"],
        "librarian_type": librarian_type,
        "kind": kind,
        # Agentic identity — populated below in _enrich_timeline_agentic
        # (which fetches the agentic source row + target asset title).
        # Bead/librarian rows leave these as None.
        "agentic_source_id": agentic_source_id,
        "journal_entry": journal_entry,
        "target_kind": None,
        "target_source_id": None,
        "target_org": None,
        "member_key": None,
        "action_label": None,
        # auto-ngis4: dispatching session identity surfaced on every row so
        # the timeline card chrome can render a uniform harness badge.
        "sender_harness": None,
        "sender_model": None,
        "librarian_review": None,  # populated by _enrich_with_librarian_data
        "smoke_result": _read_smoke_result(row["output_dir"]),
        "_output_dir": row["output_dir"] or "",  # internal field, stripped before response
    }


def _walk_decision_path(decision: dict | None, path: str):
    """Walk a dotted ``path`` into ``decision``; return None if any hop misses.

    Used by :func:`_resolve_card_summary_slots` to look up declarative
    paths from an action's ``card_summary`` Setting field. Both list
    indices (``foo.0.bar``) and dict keys are supported; non-dict/list
    intermediates short-circuit to None rather than raising.
    """
    cur: object = decision
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _resolve_card_summary_slots(slots, decision: dict | None) -> list[dict]:
    """Resolve a card_summary slot list against ``decision``.

    Each input slot is ``{label, path, format?}``; output slots are
    ``{label, value, format}`` with ``value`` already walked from the
    decision dict. Slots whose path doesn't resolve are dropped (silent
    no-op) so the rendered card matches what's actually known. Returns
    an empty list if either side is missing.
    """
    if not slots or not isinstance(slots, list):
        return []
    out: list[dict] = []
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        label = slot.get("label")
        path = slot.get("path")
        if not isinstance(label, str) or not label:
            continue
        if not isinstance(path, str) or not path:
            continue
        value = _walk_decision_path(decision, path)
        if value is None or value == "" or value == [] or value == {}:
            continue
        out.append({
            "label": label,
            "value": value,
            "format": slot.get("format") or "text",
        })
    return out


def _load_action_card_summaries(
    member_keys: set[tuple[str, str]],
) -> dict[tuple[str, str], list]:
    """Bulk-load ``card_summary`` slots for a set of (member_key, org) pairs.

    Returns a dict keyed by ``(member_key, target_org)``. Missing or
    invalid lookups yield empty lists so callers don't have to guard.
    """
    out: dict[tuple[str, str], list] = {}
    by_org: dict[str, set[str]] = {}
    for mk, org in member_keys:
        if not mk or not org:
            continue
        by_org.setdefault(org, set()).add(mk)
    for org, keys in by_org.items():
        try:
            members = graph_ops.read_set(
                "dashboard.agent-actions", org=org, peers=[],
            ).members
        except Exception:  # noqa: BLE001 — agentic UI must not crash
            logger.exception(
                "card_summary: read_set failed org=%s", org,
            )
            continue
        for m in members:
            if m.key not in keys:
                continue
            payload = m.payload if isinstance(m.payload, dict) else {}
            cs = payload.get("card_summary")
            if isinstance(cs, list) and cs:
                out[(m.key, org)] = cs
    return out


def _load_decision_for_run(output_dir: str | None) -> dict | None:
    """Read ``decision.json`` from a dispatch run's output dir, if present."""
    if not output_dir:
        return None
    path = Path(output_dir) / "decision.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _agentic_decision_artifacts(
    row: dict | sqlite3.Row | None,
    decision: dict | None = None,
) -> dict:
    """Return the durable branch artifact identity for an agentic run.

    Agentic comparison actions write their deliverable to ``decision.json``.
    Older dispatcher processes completed those runs with empty commit/branch
    columns even though the decision file and pushed branch were intact.  Read
    both sources so historical runs remain reviewable after their disposable
    worktree directory is cleaned up.
    """
    row = dict(row) if row is not None else {}
    if decision is None:
        decision = _load_decision_for_run(row.get("output_dir")) or {}
    return {
        "commit_hash": (
            row.get("commit_hash")
            or decision.get("commit_hash")
            or decision.get("commit")
            or ""
        ),
        "branch": row.get("branch") or decision.get("branch") or "",
        "branch_base": (
            row.get("branch_base")
            or decision.get("branch_base")
            or decision.get("base_commit")
            or ""
        ),
    }


def _enrich_timeline_agentic(entries: list[dict]) -> None:
    """Populate the agentic-only timeline fields in-place via the
    shared identity resolver. See :func:`_resolve_agentic_identity`."""
    for entry in entries:
        if entry.get("kind") != "agentic" or not entry.get("agentic_source_id"):
            continue
        ident = _resolve_agentic_identity(entry["agentic_source_id"])
        entry["action_label"] = ident["action_label"]
        entry["member_key"] = ident["member_key"]
        entry["target_kind"] = ident["target_kind"]
        entry["target_source_id"] = ident["target_source_id"]
        entry["target_org"] = ident["target_org"]
        entry["dispatched_by_session"] = ident["dispatched_by_session"]
        # auto-ngis4: attach the dispatching session's harness + model so
        # the timeline card can render an icon-rail badge for the "From:"
        # link without a separate lookup. Sentinel sessions ("dashboard")
        # leave both fields None.
        sender = ident["dispatched_by_session"] or ""
        if sender and sender != "dashboard":
            sender_row = dashboard_db.get_session(sender) or {}
            entry["sender_harness"] = sender_row.get("harness") or None
            entry["sender_model"] = sender_row.get("model") or None
        else:
            entry["sender_harness"] = None
            entry["sender_model"] = None
        # Always overwrite title for agentic entries so live and historical
        # views agree even if the upstream row had a stale or wrong value.
        if ident["title"]:
            entry["title"] = ident["title"]
        elif not entry.get("title"):
            entry["title"] = ""

    # Per-action card_summary slots (auto-56o4m). The action's Setting
    # member declares which decision-dict paths matter for its card; we
    # walk them here, server-side, so the timeline JS just iterates a
    # flat list of resolved {label, value, format} slots.
    pairs = {
        (e.get("member_key") or "", e.get("target_org") or "")
        for e in entries
        if e.get("kind") == "agentic"
    }
    summaries = _load_action_card_summaries(pairs - {("", "")})
    for entry in entries:
        if entry.get("kind") != "agentic":
            continue
        saved_decision = _load_decision_for_run(entry.get("_output_dir")) or {}
        artifacts = _agentic_decision_artifacts(entry, saved_decision)
        if artifacts["commit_hash"]:
            entry.update(artifacts)
        entry["diff_target"] = _agentic_trace_diff_target(
            entry.get("run_id") or entry.get("container_name") or "",
            row=entry,
            decision=saved_decision,
        )
        slots = summaries.get(
            (entry.get("member_key") or "", entry.get("target_org") or ""),
        )
        if not slots:
            entry["card_summary"] = []
            continue
        entry["card_summary"] = _resolve_card_summary_slots(slots, saved_decision)


def _parse_review_summary(text: str) -> dict | None:
    """Parse experience reviewer markdown summary into structured data.

    Expected format (from experience_reviewer/prompt.md):
        ### Extracted
        - [pitfall] description → graph note created (note-id)
        - [bug] description → bead created (bead-id)
        ### Skipped
        - description — reason: why skipped

    Returns dict with extracted/skipped lists, or None if no parseable content.
    """
    ext_match = re.search(
        r"###\s*Extracted\s*\n(.*?)(?=\n###|\Z)", text, re.DOTALL
    )
    skip_match = re.search(
        r"###\s*Skipped\s*\n(.*?)(?=\n###|\Z)", text, re.DOTALL
    )
    if not ext_match and not skip_match:
        return None

    extracted: list[dict] = []
    if ext_match:
        for line in ext_match.group(1).strip().splitlines():
            line = line.strip()
            if not line.startswith("- "):
                continue
            line = line[2:]
            m = re.match(r"\[(\w+)\]\s+(.*?)(?:\s*→\s*(.*))?$", line)
            if not m:
                continue
            item: dict = {"type": m.group(1), "description": m.group(2).strip()}
            prov = m.group(3) or ""
            bead_m = re.search(r"bead created \(([^)]+)\)", prov)
            if bead_m:
                item["bead_id"] = bead_m.group(1)
            note_m = re.search(r"note created \(([^)]+)\)", prov)
            if note_m:
                item["source_id"] = note_m.group(1)
            extracted.append(item)

    skipped: list[dict] = []
    if skip_match:
        for line in skip_match.group(1).strip().splitlines():
            line = line.strip()
            if not line.startswith("- "):
                continue
            line = line[2:]
            parts = line.split(" — reason: ", 1)
            item = {"description": parts[0].strip()}
            if len(parts) > 1:
                item["reason"] = parts[1].strip()
            skipped.append(item)

    return {"status": "done", "extracted": extracted, "skipped": skipped}


def _read_review_from_session(output_dir: str) -> dict | None:
    """Extract review summary from librarian session JSONL.

    Scans assistant messages for the experience reviewer's markdown summary
    (### Extracted / ### Skipped sections) and parses into structured data.
    """
    sessions_dir = Path(output_dir) / "sessions"
    if not sessions_dir.is_dir():
        return None

    jsonl_files = sorted(
        sessions_dir.glob("**/*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not jsonl_files:
        return None

    last_summary: str | None = None
    try:
        with open(jsonl_files[0]) as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                content = entry.get("message", {}).get("content", "")
                if isinstance(content, list):
                    content = "\n".join(
                        p.get("text", "")
                        for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                if not isinstance(content, str):
                    continue
                if "### Extracted" in content or "### Skipped" in content:
                    last_summary = content
    except OSError:
        return None

    if not last_summary:
        return None
    return _parse_review_summary(last_summary)


def _read_librarian_results(output_dir: str | None) -> dict | None:
    """Read results from a librarian's output directory.

    Tries in order:
    1. results.json — structured output (written by future experience_reviewer enhancement)
    2. Session JSONL — parse review summary from assistant messages
    3. decision.json — fallback for basic status
    """
    if not output_dir:
        return None
    base = Path(output_dir)

    # 1. Structured results.json (preferred)
    try:
        with open(base / "results.json") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass

    # 2. Parse review summary from session JSONL
    session_results = _read_review_from_session(output_dir)
    if session_results is not None:
        return session_results

    # 3. Fallback to decision.json
    try:
        with open(base / "decision.json") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass

    return None


def _read_smoke_result(output_dir: str | None) -> dict | None:
    """Read smoke_result.json from a run's output directory. Returns None if absent."""
    if not output_dir:
        return None
    smoke_path = Path(output_dir) / "smoke_result.json"
    try:
        return json.loads(smoke_path.read_text()) if smoke_path.exists() else None
    except (OSError, json.JSONDecodeError):
        return None


def _enrich_with_librarian_data(
    conn: sqlite3.Connection, entries: list[dict]
) -> None:
    """Enrich timeline entries with librarian review data in-place.

    For regular dispatch entries (librarian_type=None): queries librarian_jobs
    for any review_report job whose payload.run_id matches the dispatch run,
    then reads results JSON from the librarian's output directory.

    For standalone librarian entries (librarian_type set): reads results JSON
    from the entry's own output_dir (stored in _output_dir).
    """
    # Standalone librarian entries — read results from their own output_dir
    for entry in entries:
        if entry.get("librarian_type"):
            entry["librarian_review"] = _read_librarian_results(entry.get("_output_dir"))

    # Populate parent_run_id on librarian entries (for timeline hiding logic)
    lib_run_ids = [
        e["run_id"] for e in entries
        if e.get("librarian_type") and e.get("run_id")
    ]
    if lib_run_ids:
        lib_ph = ",".join("?" * len(lib_run_ids))
        parent_sql = f"""
            SELECT dr.id AS lib_run_id,
                   json_extract(lj.payload, '$.run_id') AS parent_run_id
            FROM dispatch_runs dr
            JOIN librarian_jobs lj ON (
                dr.librarian_type = lj.job_type
                AND dr.id LIKE 'librarian-' || lj.job_type || '-' || substr(lj.id, 1, 8) || '-%'
            )
            WHERE dr.id IN ({lib_ph})
        """
        try:
            parent_rows = conn.execute(parent_sql, lib_run_ids).fetchall()
            parent_map = {r["lib_run_id"]: r["parent_run_id"] for r in parent_rows}
            for entry in entries:
                if entry.get("librarian_type"):
                    entry["parent_run_id"] = parent_map.get(entry["run_id"])
        except Exception:
            pass

    # Regular dispatch entries — bulk-query librarian_jobs for review results
    regular_run_ids = [
        e["run_id"] for e in entries
        if not e.get("librarian_type") and e.get("run_id")
    ]
    if not regular_run_ids:
        return

    placeholders = ",".join("?" * len(regular_run_ids))
    sql = f"""
        SELECT
            json_extract(lj.payload, '$.run_id') AS dispatch_run_id,
            lj.status AS job_status,
            dr.output_dir AS lib_output_dir
        FROM librarian_jobs lj
        LEFT JOIN dispatch_runs dr ON (
            dr.librarian_type = lj.job_type
            AND dr.id LIKE 'librarian-' || lj.job_type || '-' || substr(lj.id, 1, 8) || '-%'
        )
        WHERE lj.job_type = 'review_report'
        AND json_extract(lj.payload, '$.run_id') IN ({placeholders})
    """
    try:
        rows = conn.execute(sql, regular_run_ids).fetchall()
    except Exception:
        return

    reviews: dict[str, dict] = {}
    for row in rows:
        run_id = row["dispatch_run_id"]
        if run_id in reviews:
            continue  # keep first match
        if row["job_status"] == "running":
            reviews[run_id] = {"status": "running"}
        elif row["job_status"] == "done":
            results = _read_librarian_results(row["lib_output_dir"])
            reviews[run_id] = results if results is not None else {"status": "done"}

    for entry in entries:
        if not entry.get("librarian_type"):
            entry["librarian_review"] = reviews.get(entry["run_id"])


async def api_timeline(request):
    """Timeline entries from dispatch_runs.

    GET /api/timeline?range=1d&project=autonomy&q=search+terms

    Returns reverse-chronological array of timeline entries.
    """
    if os.environ.get("DASHBOARD_MOCK"):
        range_str = request.query_params.get("range")
        limit = min(int(request.query_params.get("limit", "200")), 1000)
        entries = dao_dispatch.get_timeline_entries(range_str, limit)
        # Enrich with bead title/priority from mock beads
        bead_ids = [e["bead_id"] for e in entries if e.get("bead_id")]
        if bead_ids:
            meta = dao_beads.get_bead_title_priority(bead_ids)
            for entry in entries:
                bid = entry.get("bead_id")
                if bid and bid in meta:
                    entry["title"] = meta[bid].get("title") or entry["bead_id"]
                    entry["priority"] = meta[bid].get("priority")
        return JSONResponse(entries)

    range_str = request.query_params.get("range")
    project = request.query_params.get("project")
    q = request.query_params.get("q")
    limit = min(int(request.query_params.get("limit", "200")), 1000)
    offset = int(request.query_params.get("offset", "0"))

    where, params = _build_timeline_where(range_str, project, q)
    sql = f"""
        SELECT * FROM dispatch_runs
        WHERE {where}
        ORDER BY completed_at DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])

    def _query():
        conn = _timeline_conn()
        try:
            rows = conn.execute(sql, params).fetchall()
            entries = [_row_to_timeline_entry(r) for r in rows]
            _enrich_with_librarian_data(conn, entries)
            return entries
        finally:
            conn.close()

    try:
        entries = await asyncio.to_thread(_query)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    # Agentic identity enrichment: fills in member_key, action_label,
    # target_source_id, target_org, and a card-friendly title for every
    # ``kind='agentic'`` entry. Bead/librarian entries are unaffected.
    # Reads decision.json from _output_dir for card_summary resolution,
    # so the pop happens *after* this step (auto-56o4m).
    await asyncio.to_thread(_enrich_timeline_agentic, entries)
    for e in entries:
        e.pop("_output_dir", None)

    # Enrich with bead title and priority from Dolt
    bead_ids = [e["bead_id"] for e in entries if e.get("bead_id")]
    if bead_ids:
        try:
            meta = await asyncio.to_thread(dao_beads.get_bead_title_priority, bead_ids)
            for entry in entries:
                bid = entry.get("bead_id")
                if bid and bid in meta:
                    entry["title"] = meta[bid].get("title") or entry["bead_id"]
                    entry["priority"] = meta[bid].get("priority")
        except Exception:
            pass  # fall back to bead_id as title

    return JSONResponse(entries)


async def api_dispatch_run_commit_detail(request):
    """Return commit detail (subject, body, files, patch) for a dispatch run.

    Worktree-merge rows resolve from the integrated repository. Agentic rows
    resolve the commit recorded in their durable decision artifact from the
    target workspace's managed clone, so a pushed comparison branch remains
    reviewable after its disposable worktree is cleaned up. Both return the
    shared Worktrees overlay shape.
    """
    run_id = request.path_params["run_id"]

    if os.environ.get("DASHBOARD_MOCK"):
        commit = dao_dispatch.get_dispatch_run_commit_detail(run_id)
        if not commit:
            return JSONResponse(
                {"error": "commit detail not found for run"}, status_code=404,
            )
        return JSONResponse(commit)

    conn = _timeline_conn()
    try:
        row = conn.execute(
            "SELECT id, kind, commit_hash, branch, branch_base, output_dir, "
            "agentic_source_id FROM dispatch_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return JSONResponse({"error": "run not found"}, status_code=404)
    kind = row["kind"] or "bead"
    if kind not in {"worktree-merge", "agentic"}:
        return JSONResponse(
            {"error": "commit-detail is only available for merge or agentic runs"},
            status_code=400,
        )
    artifacts = _agentic_decision_artifacts(row) if kind == "agentic" else {
        "commit_hash": row["commit_hash"] or "",
        "branch": row["branch"] or "",
        "branch_base": row["branch_base"] or "",
    }
    sha = artifacts["commit_hash"]
    if not sha:
        return JSONResponse(
            {"error": "run has no commit_hash"}, status_code=404,
        )

    repo_paths: list[Path] = []
    if kind == "agentic":
        identity = _resolve_agentic_identity(row["agentic_source_id"] or "")
        workspace = _resolve_workspace_for_org(identity["target_org"] or "")
        # Historical action rows do not carry the explicit workspace override
        # selected at dispatch time. Try that org's default first, then the
        # finite configured-workspace set. Paths are deterministic from the
        # Settings repo declarations; this is click-time commit lookup, not a
        # filesystem scan or a timeline-load git sweep.
        candidates = ([workspace] if workspace is not None else []) + list(
            workspace_settings.load_workspaces().values()
        )
        for candidate in candidates:
            for repo in candidate.repos:
                path = managed_clone_path(repo.url)
                if path not in repo_paths:
                    repo_paths.append(path)
    if _REPO_ROOT not in repo_paths:
        repo_paths.append(_REPO_ROOT)

    commit = None
    errors: list[str] = []
    for repo_path in repo_paths:
        try:
            commit = await asyncio.to_thread(
                get_repo_commit_detail, repo_path, sha,
            )
            break
        except WorkspaceError as exc:
            errors.append(str(exc))
    if commit is None:
        return JSONResponse(
            {"error": errors[-1] if errors else f"commit could not be read: {sha}"},
            status_code=404,
        )

    return JSONResponse(_worktree_commit_json(commit, include_patch=True))


async def api_timeline_stats(request):
    """Aggregate stats from dispatch_runs.

    GET /api/timeline/stats?range=1d&project=autonomy

    Returns: completed_count, success_rate, failed_count, blocked_count,
             avg_duration, avg_tooling_score, avg_confidence_score
    """
    if os.environ.get("DASHBOARD_MOCK"):
        range_str = request.query_params.get("range")
        return JSONResponse(dao_dispatch.get_timeline_stats(range_str))

    range_str = request.query_params.get("range")
    project = request.query_params.get("project")

    where, params = _build_timeline_where(range_str, project, None)
    sql = f"""
        SELECT
            COUNT(*) as total_count,
            COUNT(CASE WHEN status = 'DONE' THEN 1 END) as completed_count,
            COUNT(CASE WHEN status = 'FAILED' THEN 1 END) as failed_count,
            COUNT(CASE WHEN status = 'BLOCKED' THEN 1 END) as blocked_count,
            AVG(duration_secs) as avg_duration,
            AVG(score_tooling) as avg_tooling_score,
            AVG(score_confidence) as avg_confidence_score,
            AVG(score_clarity) as avg_clarity_score
        FROM dispatch_runs
        WHERE {where}
    """

    def _query():
        conn = _timeline_conn()
        try:
            row = conn.execute(sql, params).fetchone()
            if not row or row["total_count"] == 0:
                return {
                    "completed_count": 0,
                    "success_rate": 0.0,
                    "failed_count": 0,
                    "blocked_count": 0,
                    "avg_duration": None,
                    "avg_tooling_score": None,
                    "avg_confidence_score": None,
                    "avg_clarity_score": None,
                }
            total = row["total_count"]
            completed = row["completed_count"]
            return {
                "completed_count": completed,
                "success_rate": round(completed / total, 4) if total > 0 else 0.0,
                "failed_count": row["failed_count"],
                "blocked_count": row["blocked_count"],
                "avg_duration": round(row["avg_duration"], 1) if row["avg_duration"] is not None else None,
                "avg_tooling_score": round(row["avg_tooling_score"], 2) if row["avg_tooling_score"] is not None else None,
                "avg_confidence_score": round(row["avg_confidence_score"], 2) if row["avg_confidence_score"] is not None else None,
                "avg_clarity_score": round(row["avg_clarity_score"], 2) if row["avg_clarity_score"] is not None else None,
            }
        finally:
            conn.close()

    try:
        stats = await asyncio.to_thread(_query)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse(stats)


def _agentic_trace_diff_target(
    run_name: str,
    *,
    row: dict | sqlite3.Row | None = None,
    decision: dict | None = None,
) -> dict | None:
    """Resolve the best currently-viewable diff for an agentic run.

    Resolution is intentionally cheap and ordered by fidelity:

    1. The worktree monitor's in-memory rows, when the retained worktree has
       commits or dirty files. This is the same state the Worktrees page uses.
    2. The run's persisted decision artifact, which names the pushed branch
       commit even after the disposable worktree is cleaned up.
    3. A recorded host-side merge row for this session, which opens the
       existing immutable commit overlay after the worktree is gone.
    4. The deterministic session directory as a cold-cache fallback. The
       Worktrees deep link refreshes/resolves the concrete repository row.

    No git command runs on the Trace request path.
    """
    worktree_href = f"/worktrees?session={url_quote(run_name, safe='')}"
    matching_rows = [
        row for row in worktree_monitor.get_all()
        if row.session_name == run_name
    ]
    changed_rows = [
        row for row in matching_rows
        if row.is_dirty or row.commits_ahead > 0
    ]
    if changed_rows:
        return {
            "kind": "worktree",
            "session_name": run_name,
            "href": worktree_href,
            "repo_count": len(changed_rows),
        }

    if row is None and decision is None:
        try:
            row = get_run(run_name)
        except Exception:  # noqa: BLE001 — an absent legacy row is normal
            row = None
    artifacts = _agentic_decision_artifacts(row, decision)
    if artifacts["commit_hash"]:
        return {
            "kind": "commit",
            "run_id": run_name,
            **artifacts,
        }

    try:
        conn = _timeline_conn()
        try:
            merged = conn.execute(
                "SELECT id, commit_hash, branch, branch_base "
                "FROM dispatch_runs "
                "WHERE COALESCE(kind, 'bead') = 'worktree-merge' "
                "AND container_name = ? AND COALESCE(commit_hash, '') != '' "
                "ORDER BY completed_at DESC LIMIT 1",
                (run_name,),
            ).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        merged = None
    if merged is not None:
        return {
            "kind": "commit",
            "run_id": merged["id"],
            "commit_hash": merged["commit_hash"],
            "branch": merged["branch"] or "",
            "branch_base": merged["branch_base"] or "",
        }

    # The background monitor may not have completed its first sweep after a
    # dashboard restart. Folder existence is enough to offer the stable deep
    # link; that page performs its own fresh row resolution.
    if (WORKTREES_DIR / run_name).is_dir():
        return {
            "kind": "worktree",
            "session_name": run_name,
            "href": worktree_href,
            "repo_count": len(matching_rows),
        }
    return None


async def api_dispatch_trace(request):
    """Full trace for a completed dispatch run.

    Metadata (status, reason, scores, commit info, diff stats, duration)
    comes from SQLite. Large artifacts (experience_report.md, session JSONL,
    git diff) are still read from disk on demand.
    """
    run_name = request.path_params["run"]

    if os.environ.get("DASHBOARD_MOCK"):
        trace = dao_dispatch.get_trace(run_name)
        if not trace:
            return JSONResponse({"error": "run not found"}, status_code=404)
        return JSONResponse({
            "run": trace.get("id", run_name),
            "kind": trace.get("kind") or "bead",
            "bead_id": trace.get("bead_id", ""),
            "bead": dao_beads.get_bead(trace.get("bead_id", "")) if trace.get("bead_id") else None,
            "decision": trace.get("decision"),
            "experience_report": trace.get("experience_report", ""),
            "commit_hash": trace.get("commit_hash", ""),
            "branch": trace.get("branch", ""),
            "diff": trace.get("diff", ""),
            "has_session": False,
            "is_live": False,
            "duration_secs": trace.get("duration_secs"),
            "commit_message": trace.get("commit_message", ""),
            "lines_added": trace.get("lines_added"),
            "lines_removed": trace.get("lines_removed"),
            "files_changed": trace.get("files_changed"),
            # Mock fixtures may pre-populate agentic identity for trace fixtures
            "agentic_source_id": trace.get("agentic_source_id") or "",
            "action_label": trace.get("action_label"),
            "member_key": trace.get("member_key"),
            "target_source_id": trace.get("target_source_id"),
            "target_org": trace.get("target_org"),
            "target_title": trace.get("target_title"),
            "dispatched_by_session": trace.get("dispatched_by_session"),
            "dispatched_by_project": trace.get("dispatched_by_project") or "",
            "diff_target": trace.get("diff_target"),
        })

    # Get structured metadata from SQLite
    row = await asyncio.to_thread(get_run, run_name)

    # Fall back to filesystem if not in DB yet
    run_dir = AGENT_RUNS_DIR / run_name
    if not row and not run_dir.exists():
        # Try as bead ID — resolve to most recent run
        runs = await asyncio.to_thread(get_runs_for_bead, run_name)
        if runs:
            row = runs[0]
            run_name = row["id"]
            run_dir = AGENT_RUNS_DIR / run_name
        else:
            return JSONResponse({"error": "run not found"}, status_code=404)

    # Extract fields from DB row (or fall back to filesystem)
    if row:
        bead_id = row.get("bead_id") or ""
        commit_hash = row.get("commit_hash") or ""
        branch = row.get("branch") or ""
        branch_base = row.get("branch_base") or ""
        status = row.get("status")
        reason = row.get("reason")
        duration_secs = row.get("duration_secs")
        commit_message = row.get("commit_message") or ""
        lines_added = row.get("lines_added")
        lines_removed = row.get("lines_removed")
        files_changed = row.get("files_changed")

        # Reconstruct decision dict from flat DB columns
        decision = None
        if status:
            decision = {"status": status, "reason": reason}
            scores = {}
            for key in ("tooling", "clarity", "confidence"):
                val = row.get(f"score_{key}")
                if val is not None:
                    scores[key] = val
            if scores:
                decision["scores"] = scores
            if row.get("failure_category"):
                decision["failure_category"] = row["failure_category"]
    else:
        # Filesystem fallback for runs not yet in DB
        parts = run_name.rsplit("-", 2)
        bead_id = parts[0] if len(parts) >= 3 else run_name
        commit_hash = ""
        commit_path = run_dir / ".commit_hash"
        if commit_path.exists():
            commit_hash = commit_path.read_text().strip()
        branch = ""
        branch_path = run_dir / ".branch"
        if branch_path.exists():
            branch = branch_path.read_text().strip()
        branch_base = ""
        base_path = run_dir / ".branch_base"
        if base_path.exists():
            branch_base = base_path.read_text().strip()
        decision = None
        decision_path = run_dir / "decision.json"
        if decision_path.exists():
            try:
                decision = json.loads(decision_path.read_text())
            except json.JSONDecodeError:
                pass
        duration_secs = None
        commit_message = ""
        lines_added = None
        lines_removed = None
        files_changed = None

    kind = (row.get("kind") if row else None) or "bead"
    agentic_source_id = (row.get("agentic_source_id") if row else None) or ""
    is_live = row.get("status") == "RUNNING" if row else False

    # Agentic runs follow a different shape than beads: no commit, no
    # diff, no experience_report, no bead lookup. The interesting
    # context lives on the agentic source row (action label, target
    # asset, sender) and in the session JSONL the agent wrote at
    # ``dispatch_runs.output_dir``. Resolve those and short-circuit.
    if kind == "agentic":
        agentic_run_dir = Path(row["output_dir"]) if row and row.get("output_dir") else run_dir
        saved_decision = _load_decision_for_run(str(agentic_run_dir)) or {}
        if saved_decision:
            decision = {**saved_decision, **(decision or {})}
        artifacts = _agentic_decision_artifacts(row, saved_decision)
        has_session = bool(_find_session_files(run_name, run_dir=agentic_run_dir))
        identity = _resolve_agentic_identity(agentic_source_id)
        # Resolve the dispatching session's project so the front-end
        # can build a real /session/<project>/<tmux> link. The "dashboard"
        # sentinel is browser-initiated and has no session to link back to.
        sender = identity["dispatched_by_session"] or ""
        sender_project = ""
        if sender and sender != "dashboard":
            sender_project = _session_meta_for_tmux(sender).get("project") or ""
        # Per-action card_summary (auto-56o4m): same shape as on the
        # timeline so the trace card reuses the timeline's slot template.
        card_summary: list[dict] = []
        if identity["member_key"] and identity["target_org"]:
            slots = _load_action_card_summaries(
                {(identity["member_key"], identity["target_org"])},
            ).get((identity["member_key"], identity["target_org"]))
            if slots:
                card_summary = _resolve_card_summary_slots(slots, decision)
        return JSONResponse({
            "run": run_name,
            "kind": "agentic",
            "bead_id": "",
            "bead": None,
            "decision": decision,
            "card_summary": card_summary,
            "experience_report": "",
            "commit_hash": artifacts["commit_hash"],
            "branch": artifacts["branch"],
            "branch_base": artifacts["branch_base"],
            "diff": "",
            "has_session": has_session,
            "is_live": is_live,
            "duration_secs": duration_secs,
            "commit_message": "",
            "lines_added": None,
            "lines_removed": None,
            "files_changed": None,
            # Agentic identity — what action ran, on what asset, for whom.
            "agentic_source_id": agentic_source_id,
            "action_label": identity["action_label"],
            "member_key": identity["member_key"],
            "target_kind": identity["target_kind"],
            "target_source_id": identity["target_source_id"],
            "target_org": identity["target_org"],
            "target_title": identity["title"],
            "dispatched_by_session": sender,
            "dispatched_by_project": sender_project,
            "diff_target": (
                None if is_live else _agentic_trace_diff_target(
                    run_name,
                    row=row,
                    decision=saved_decision,
                )
            ),
        })

    # Large artifacts still from disk (beads / librarian only)
    experience = ""
    if run_dir.exists():
        exp_path = run_dir / "experience_report.md"
        if exp_path.exists():
            experience = exp_path.read_text()

    # Git diff (computed on demand)
    diff = ""
    if commit_hash and branch_base:
        stdout, _, rc = await run_cli(["git", "diff", f"{branch_base}..{commit_hash}"], timeout=10)
        if rc == 0:
            diff = stdout

    # Bead info — only for kind='bead' rows. ``bd show`` with an empty
    # id returns an "ambiguous ID" error that crowds the UI.
    bead = None
    if bead_id:
        bead = await run_cli_json(["bd", "show", bead_id, "--json"])

    # Session log availability
    has_session = bool(_find_session_files(run_name))

    return JSONResponse({
        "run": run_name,
        "kind": kind,
        "bead_id": bead_id,
        "bead": bead,
        "decision": decision,
        "experience_report": experience,
        "commit_hash": commit_hash,
        "branch": branch,
        "diff": diff,
        "has_session": has_session,
        "is_live": is_live,
        "duration_secs": duration_secs,
        "commit_message": commit_message,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
        "files_changed": files_changed,
    })

_SEARCH_VALID_ORDERS = ("relevance", "recent")
_SEARCH_VALID_RANKERS = ("legacy", "smart")
_SEARCH_VALID_SESSION_TYPES = (
    "terminal", "chatwith", "dispatch", "librarian", "agentic",
)


async def api_search(request):
    q = request.query_params.get("q", "")
    if not q:
        return JSONResponse({"error": "missing q parameter"})

    # ``order`` — relevance (default) or recent. Strict allowlist so a
    # typo (?order=date, ?order=desc) fails loud rather than silently
    # passing through to db.search and surprising someone reading logs.
    order = request.query_params.get("order", "relevance")
    if order not in _SEARCH_VALID_ORDERS:
        return JSONResponse(
            {"error": f"invalid order {order!r}; "
                      f"expected one of {list(_SEARCH_VALID_ORDERS)}"},
            status_code=400,
        )

    # ``ranker`` selects the relevance algorithm. It is still parsed for
    # recent-order requests so malformed/bookmarked values fail consistently;
    # GraphDB treats recency as authoritative and ignores relevance ranking.
    ranker = request.query_params.get("ranker", "legacy")
    if ranker not in _SEARCH_VALID_RANKERS:
        return JSONResponse(
            {"error": f"invalid ranker {ranker!r}; "
                      f"expected one of {list(_SEARCH_VALID_RANKERS)}"},
            status_code=400,
        )

    # ``session_type`` — comma-separated list, strictly one of the known
    # values. ``None`` (param absent) disables the filter; an empty/all-
    # invalid list returns 400 to avoid the "I sent a typo and got every
    # row" footgun. Round 7k pins NULL invisibility — see db.search docs.
    session_type_param = request.query_params.get("session_type")
    session_type: list[str] | None
    if session_type_param is None:
        session_type = None
    else:
        raw = [s.strip() for s in session_type_param.split(",") if s.strip()]
        bad = [s for s in raw if s not in _SEARCH_VALID_SESSION_TYPES]
        if bad:
            return JSONResponse(
                {"error": f"invalid session_type values {bad!r}; "
                          f"expected subset of "
                          f"{list(_SEARCH_VALID_SESSION_TYPES)}"},
                status_code=400,
            )
        session_type = raw

    if os.environ.get("DASHBOARD_MOCK"):
        limit = int(request.query_params.get("limit", "20"))
        project = request.query_params.get("project")
        results = dao_beads.search(
            q, limit=limit, project=project,
            order=order, session_type=session_type,
        )
        if request.query_params.get("group"):
            results = _group_search_results(results, order=order)
        _enrich_search_results(results)
        return JSONResponse(results)
    limit = int(request.query_params.get("limit", "20"))
    or_mode = bool(request.query_params.get("or"))
    tag = request.query_params.get("tag")
    states_param = request.query_params.get("states")
    states = [s for s in states_param.split(",") if s] if states_param else None
    include_raw = bool(request.query_params.get("include_raw"))
    only_org = request.query_params.get("only_org")
    peers_param = request.query_params.get("peers")
    peers = [p for p in peers_param.split(",") if p] if peers_param is not None else None
    org = api_auth.organization_scope_from_request(request)
    # Auxiliary source types (currently 'agentic' agent-action runs) are
    # excluded from /api/search by default — the global surface should not
    # be polluted by short-lived dashboard-spawned agents. Pass
    # ``?include_aux=1`` to surface them (future "Auxiliary runs" tab).
    excluded_source_types: list[str] | None = None
    if request.query_params.get("include_aux"):
        excluded_source_types = []
    results = await asyncio.to_thread(
        graph_ops.search,
        q, org=org, peers=peers, only_org=only_org,
        limit=limit, or_mode=or_mode, tag=tag,
        states=states, include_raw=include_raw,
        excluded_source_types=excluded_source_types,
        order=order, session_type=session_type, ranker=ranker,
    )
    if request.query_params.get("group"):
        results = _group_search_results(results, order=order)
    _enrich_search_results(results)
    return JSONResponse(results)


def _group_search_results(rows: list, *, order: str = "relevance") -> list:
    """Collapse FTS rows into one entry per source while preserving per-turn excerpts.

    Output shape per group: source-level fields (source_id, source_title,
    source_type, project, platform, org, source_created_at, rrf_score) plus
    a best-rank ``rank`` and a sorted ``excerpts`` array of per-turn hits.

    ``order`` controls the post-group sort: ``'relevance'`` (default)
    sorts by best ``rank`` ascending — the legacy behaviour; ``'recent'``
    sorts by ``source_created_at`` DESC so Round 7k's recency toggle
    survives the groupby step. Per-source excerpts always sort by rank
    so the strongest excerpt leads each card.
    """
    SOURCE_FIELDS = (
        "source_id", "source_title", "source_type", "project", "platform",
        "org", "source_created_at", "source_metadata", "rrf_score",
        "short_description", "keywords",
        # Round 7k: surface ``session_type`` so the search page's chip
        # rail (Sessions vs Dispatch) and per-row badge can read it
        # directly. The mock DAO sets the field at top level; the live
        # path stores it inside source_metadata JSON, where
        # _enrich_search_results promotes it.
        "session_type",
    )
    EXCERPT_FIELDS = ("turn_number", "content", "result_type", "rank")
    groups: dict = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        sid = r.get("source_id") or r.get("id")
        if sid is None:
            continue
        excerpt = {k: r.get(k) for k in EXCERPT_FIELDS}
        g = groups.get(sid)
        if g is None:
            g = {k: r.get(k) for k in SOURCE_FIELDS if k in r}
            g["source_id"] = sid
            g["rank"] = r.get("rank")
            g["excerpts"] = [excerpt]
            g["match_count"] = 1
            groups[sid] = g
        else:
            g["excerpts"].append(excerpt)
            g["match_count"] += 1
            cur = g.get("rank")
            new = r.get("rank")
            if new is not None and (cur is None or new < cur):
                g["rank"] = new
        # Round 7l: db.search caps emitted excerpts at
        # SEARCH_EXCERPTS_PER_SOURCE per source but stamps the true
        # ``hit_count`` on every row of that source. Promote it to the
        # group-level match_count so a 30-hit session card shows
        # "30 matches" even though only ~10 excerpts surface in the
        # API payload. Falls back to the count-as-we-go when hit_count
        # is absent (legacy callers / tests that don't go through the
        # source-aware db.search path).
        hit_count = r.get("hit_count")
        if isinstance(hit_count, int) and hit_count > g.get("match_count", 0):
            g["match_count"] = hit_count
    for g in groups.values():
        g["excerpts"].sort(
            key=lambda e: (e.get("rank") if e.get("rank") is not None else 0)
        )
    if order == "recent":
        return sorted(
            groups.values(),
            key=lambda g: g.get("source_created_at") or "",
            reverse=True,
        )
    return sorted(
        groups.values(),
        key=lambda g: (g.get("rank") if g.get("rank") is not None else 0),
    )


def _enrich_search_results(results: list) -> None:
    """Attach resolved ``org``, ``is_peer``, and 24hr ``date`` to each row in-place.

    ``is_peer`` is True when the row's resolved org slug differs from the
    caller-org bound by ``ApiIdentityMiddleware`` — used by the search UI to
    paint a "peer" pill on cross-org rows. A scopeless caller (no contextvar /
    no env) treats every row as own-org since "peer" only makes sense relative
    to a known caller seat.
    """
    from tools.dashboard.org_identity import resolve_org_identity, session_org_slug
    from tools.graph.ops import _resolve_org as _resolve_caller_org
    caller = _resolve_caller_org(None)
    for r in results:
        if not isinstance(r, dict):
            continue
        # The cross-org search path annotates ``r["org"]`` with a bare slug
        # string; older callers leave it absent. Capture the slug either way
        # so the peer comparison sees a real value.
        existing_org = r.get("org")
        if isinstance(existing_org, dict):
            org_slug = existing_org.get("slug")
        elif isinstance(existing_org, str) and existing_org:
            org_slug = existing_org
        else:
            org_slug = session_org_slug(r)
        if "org" not in r:
            r["org"] = resolve_org_identity(org_slug)
        r["is_peer"] = bool(caller) and bool(org_slug) and org_slug != caller
        # Round 7k: promote ``metadata.session_type`` to the row's top
        # level so the chip rail can read ``r.session_type`` without
        # parsing the metadata JSON in the browser. Production rows
        # carry it nested in ``source_metadata``; mock rows may set it
        # at the top level directly. Skip if already set (don't clobber
        # an explicit fixture value with a missing metadata key).
        if r.get("session_type") is None:
            meta = r.get("source_metadata")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    meta = None
            if isinstance(meta, dict):
                st = meta.get("session_type")
                if isinstance(st, str) and st:
                    r["session_type"] = st
        # Normalize date to "YYYY-MM-DD HH:MM" (24hr). Source may supply
        # created_at / date / last_activity_at in various forms.
        if "date" not in r or not r.get("date"):
            r["date"] = _format_search_date(
                r.get("source_created_at")
                or r.get("created_at")
                or r.get("date")
                or r.get("last_activity_at")
                or ""
            )
        else:
            r["date"] = _format_search_date(r["date"])


def _format_search_date(raw: str) -> str:
    """Normalize a timestamp string to ``YYYY-MM-DD HH:MM:SS`` (24hr).

    Second-resolution disambiguates rows that collide on minute (e.g. two
    rollouts of the same tmux session ingested seconds apart) — see bead
    auto-kvka6 §6.
    """
    if not raw:
        return ""
    # Strip trailing Z / fractional seconds, support ISO or space-delimited.
    s = raw.replace("T", " ").replace("Z", "")
    # Drop microseconds / timezone offset if present.
    if "+" in s:
        s = s.split("+", 1)[0]
    if "." in s:
        s = s.split(".", 1)[0]
    s = s.strip()
    # Accept "YYYY-MM-DD" alone; otherwise keep to second resolution
    # (19 chars: "YYYY-MM-DD HH:MM:SS").
    if len(s) >= 19:
        return s[:19]
    if len(s) >= 16:
        return s[:16]
    return s[:10]

async def api_sources(request):
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"results": "", "error": None})
    org = api_auth.organization_scope_from_request(request)
    stype = request.query_params.get("type")
    limit = int(request.query_params.get("limit", "30"))
    rows = await asyncio.to_thread(
        graph_ops.list_sources,
        org=org, source_type=stype, limit=limit,
    )
    # Render a CLI-equivalent text body so existing UI consumers that parse
    # ``results`` as a pre-formatted list keep working. Structured clients
    # can switch to the list payload via the new ``sources`` field.
    lines = []
    for r in rows:
        title = (r.get("title") or "")[:80]
        created = (r.get("created_at") or "")[:16]
        lines.append(
            f"[{r.get('org','')}] {r['id'][:12]} {r.get('type',''):12s} "
            f"{created}  {title}"
        )
    return JSONResponse({
        "results": "\n".join(lines),
        "sources": rows,
        "error": None,
    })

async def api_source_read(request):
    source_id = request.path_params["id"]
    if os.environ.get("DASHBOARD_MOCK"):
        source = dao_beads.get_source(source_id)
        if not source:
            return JSONResponse({"error": "source not found"}, status_code=404)
        _attach_source_org(source)
        return JSONResponse(source)
    # Asset/full-read surface — unbounded by default. Callers may pass
    # ``?max_chars=N`` to take an explicit slice; the sole frontend caller
    # (agent-actions.js) wants the whole thing.
    try:
        max_chars = int(request.query_params.get("max_chars", "0"))
    except ValueError:
        return JSONResponse({"error": "invalid max_chars"}, status_code=400)
    turn_raw = request.query_params.get("turn")
    around_turn: int | None = None
    if turn_raw is not None:
        try:
            around_turn = int(turn_raw)
        except ValueError:
            return JSONResponse({"error": "invalid turn"}, status_code=400)
    try:
        window = int(request.query_params.get("window", "5"))
    except ValueError:
        return JSONResponse({"error": "invalid window"}, status_code=400)
    org = api_auth.organization_scope_from_request(request)
    result = await asyncio.to_thread(
        graph_ops.read_source_full, source_id, max_chars=max_chars, org=org,
        around_turn=around_turn, window=window,
    )
    if result is None:
        return JSONResponse({"error": "source not found"}, status_code=404)
    _attach_source_org(result)
    return JSONResponse(result)


def _note_version_count(source_id: str, org: str | None) -> int:
    """How many stored versions a note has. 1 when it has never been revised."""
    try:
        from tools.graph.db import GraphDB
        db = GraphDB(org=org, mode="ro")
        try:
            versions = db.list_note_versions(source_id)
        finally:
            db.close()
    except Exception:
        return 1
    return max(1, len(versions or []))


def _attach_source_org(result: dict | None) -> None:
    """Attach resolved ``org`` (and tmux session, when applicable) to the
    nested ``source`` of a graph-read response.

    ``graph read --json --first`` returns ``{source: {...}, entries: [...], ...}``.
    The mock DAO returns the source dict directly. Handle both shapes.
    """
    if not isinstance(result, dict):
        return
    from tools.dashboard.org_identity import resolve_org_identity, session_org_slug
    src = result.get("source") if isinstance(result.get("source"), dict) else result
    if not isinstance(src, dict):
        return
    # The stored row already carries ``org`` as a SLUG string, so a
    # presence check never resolves it and the viewer receives a bare
    # string where it renders an identity object -- no colour, no
    # initial, no name. Resolve unless it is already resolved.
    org_value = src.get("org")
    if not isinstance(org_value, dict):
        src["org"] = resolve_org_identity(
            org_value if isinstance(org_value, str) and org_value
            else session_org_slug(src)
        )
    _attach_source_session_chip(src)


def _attach_source_session_chip(src: dict) -> None:
    """Attach ``tmux_session`` to a source dict for type=session entries.

    The viewer (and ``graph read`` over the API) needs the tmux name to
    show a session-name chip and a link back to ``/session/<tmux>``.
    Looks up dashboard.db by graph_source_id, falling back to
    metadata.session_uuid for sources the linker hasn't reconciled yet.
    Silently skips on lookup error so the response still ships.
    """
    if not isinstance(src, dict) or src.get("type") != "session":
        return
    if "tmux_session" in src:
        return
    meta = src.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            meta = {}
    session_uuid = meta.get("session_uuid") or meta.get("session_id")
    try:
        tmux = dashboard_db.get_tmux_name_for_source(
            src.get("id") or "", session_uuid=session_uuid,
        )
    except Exception:
        tmux = None
    if tmux:
        src["tmux_session"] = tmux

async def api_context(request):
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"content": "", "error": None})
    source_id = request.path_params["id"]
    try:
        turn = int(request.path_params["turn"])
        window = int(request.query_params.get("window", "3"))
    except ValueError:
        return JSONResponse({"error": "turn/window must be integers"}, status_code=400)
    org = api_auth.organization_scope_from_request(request)
    result = await asyncio.to_thread(
        graph_ops.get_context, source_id, turn, window=window, org=org,
    )
    if result is None:
        return JSONResponse({"content": "", "error": "source not found"})
    lines = []
    for t in result["turns"]:
        role = t.get("role") or "?"
        marker = "→ " if t["turn_number"] == turn else "  "
        lines.append(f"{marker}[{t['turn_number']}] {role}: {t['content']}")
    return JSONResponse({
        "content": "\n".join(lines),
        "turns": result["turns"],
        "center_turn": result["center_turn"],
        "source_id": result["source"]["id"],
        "error": None,
    })

async def api_projects(request):
    """Return the workspace registry — one entry per containerized project.

    Each entry carries the fields the frontend needs for grouping, display,
    and routing: ``id``, ``name``, ``description``, ``graph_project`` (the
    org the workspace belongs to), ``needs_nested_docker`` and
    ``session_runtime``. The deprecated ``dind`` alias remains for older
    dashboard clients.
    """
    try:
        projects = workspace_settings.load_workspaces()
    except workspace_settings.WorkspaceSettingsError as e:
        return JSONResponse({"projects": [], "error": str(e)}, status_code=500)
    from tools.dashboard.org_identity import resolve_org_identity
    entries = []
    org_cache: dict[str, dict] = {}
    for p in projects.values():
        slug = p.graph_project
        if slug not in org_cache:
            org_cache[slug] = resolve_org_identity(slug)
        entries.append({
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "graph_project": slug,
            "org": org_cache[slug],
            "needs_nested_docker": p.needs_nested_docker,
            "session_runtime": p.session_runtime,
            "dind": p.needs_nested_docker,
        })
    return JSONResponse({"projects": entries})


async def api_workspace_local_create(request):
    """Create or attach an API-managed local Git repo to a workspace.

    ``POST /api/workspaces/local`` with an explicit ``X-Graph-Org`` header and
    body ``{id, name?, description?, image?, harness?, model?}`` is the
    repository-less counterpart to configuring a remote Git URL.  New
    workspaces and existing repo-less workspaces both end up with a durable
    bare backing repo plus isolated writable session worktrees at a dedicated
    ``/workspace/<id>`` path.  The platform checkout remains visible at
    ``/workspace/repo`` so built-in CLIs such as ``graph`` keep working.
    """
    org = api_auth.organization_scope_from_request(request)
    if not org:
        return JSONResponse(
            {"error": "X-Graph-Org header is required"}, status_code=400,
        )
    # The four Settings calls below must stay off a bare ``_caller_org``
    # value: a missing org has to route through the CALLER_ORG resolver, not
    # silently land scopeless (graph://53f7412f-51e). The guard above already
    # rejects a missing org, so this only ever resolves to the concrete slug —
    # it pins the required-org contract uniformly across every call site.
    org = org or graph_ops.CALLER_ORG
    try:
        body = await request.json()
    except Exception:
        body = {}
    workspace_id = body.get("id")
    if not isinstance(workspace_id, str) or not workspace_id:
        return JSONResponse({"error": "id is required"}, status_code=400)

    harness = body.get("harness", "codex")
    if harness not in {"claude", "codex"}:
        return JSONResponse(
            {"error": "harness must be 'claude' or 'codex'"}, status_code=400,
        )
    mount = f"/workspace/{workspace_id}"
    try:
        expected_repo_path = local_workspace_repo_path(org, workspace_id)
    except WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    members = graph_ops.read_set(
        workspace_settings.WORKSPACE_SET_ID,
        org=org,
        peers=[],
    ).members
    existing = next((m for m in members if m.key == workspace_id), None)
    # The managed local repository is declared with the portable
    # ``local: true`` — the consuming node derives
    # data/workspace-repos/<org>/<id> itself. Never an absolute
    # ``local_path``: that is true on one machine and false on the next,
    # and must not enter a replicating org row.
    repo_spec = {
        "local": True,
        "mount": mount,
        "writable": True,
    }

    def _same_local_repo(r: dict) -> bool:
        # Tolerant on the way in: rows written before the shape changed carry
        # the same repository as ``local_path`` (this machine's resolution of
        # it) or the even older ``url``. Reading only the current field would
        # decide the existing configuration disagreed, and refuse with a 409
        # that named no difference.
        return (
            r.get("local") is True
            or (r.get("local_path") or r.get("url")) == str(expected_repo_path)
        )

    if existing is not None:
        existing_repos = existing.payload.get("repos") or []
        if existing_repos and not (
            len(existing_repos) == 1 and _same_local_repo(existing_repos[0])
        ):
            return JSONResponse(
                {
                    "error": (
                        f"workspace {workspace_id!r} already has repository configuration"
                    )
                },
                status_code=409,
            )

    try:
        repo_path, repo_created = await asyncio.to_thread(
            ensure_local_workspace_repository,
            org,
            workspace_id,
            name=body.get("name") or workspace_id,
        )
    except WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    setting_created = False
    setting_overridden = False
    try:
        if existing is None:
            payload = {
                "name": str(body.get("name") or workspace_id),
                "description": str(body.get("description") or ""),
                "image": str(body.get("image") or "autonomy-session-platform"),
                "harness": harness,
                "working_dir": mount,
                "repos": [repo_spec],
                "dind": False,
            }
            model = body.get("model")
            if isinstance(model, str) and model:
                payload["model"] = model
            setting_id = graph_ops.add_setting(
                workspace_settings.WORKSPACE_SET_ID,
                workspace_settings.WORKSPACE_REVISION_2,
                workspace_id,
                payload,
                state="raw",
                org=org,
            )
            setting_created = True
        else:
            existing_repos = existing.payload.get("repos") or []
            # A legacy row pointing at this machine's resolution of the same
            # repository is already configured — rewriting it to the portable
            # shape is the voluntary per-row migration, not this route's job.
            already_configured = (
                len(existing_repos) == 1
                and _same_local_repo(existing_repos[0])
                and existing.payload.get("working_dir") == mount
            )
            if already_configured:
                setting_id = existing.id
            else:
                chain = graph_ops.chain_setting(
                    workspace_settings.WORKSPACE_SET_ID,
                    workspace_id,
                    org=org,
                    peers=[],
                )
                layers = chain.get("layers", []) if chain else []
                if not layers:
                    raise WorkspaceError(
                        f"could not resolve base Setting for workspace {workspace_id!r}"
                    )
                setting_id = graph_ops.override_setting(
                    layers[0]["id"],
                    {"working_dir": mount, "repos": [repo_spec]},
                    state="raw",
                    org=org,
                )
                setting_overridden = True
    except WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except (LookupError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    workspace_settings.invalidate_caches()
    return JSONResponse(
        {
            "id": workspace_id,
            "org": org,
            "setting_id": setting_id,
            "repo": str(repo_path),
            "mount": mount,
            "repo_created": repo_created,
            "workspace_created": setting_created,
            "workspace_overridden": setting_overridden,
        },
        status_code=201 if setting_created or setting_overridden else 200,
    )

async def api_stats(request):
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"results": "", "error": None})
    org = api_auth.organization_scope_from_request(request)
    data = await asyncio.to_thread(graph_ops.stats, org=org)
    lines = ["Knowledge Graph Stats:"]
    for table, count in data.items():
        lines.append(f"  {table:20s}  {count:6d}")
    return JSONResponse({"results": "\n".join(lines), "stats": data, "error": None})


def _existing_usage_payload(row_key: str) -> dict | None:
    """The usage row currently stored under ``row_key``, or None.

    Read so a failed poll can tell whether it would be overwriting a reading
    that is still true. Best-effort: if the read fails, the caller falls back
    to the behaviour it had before, which is to record the failure.
    """
    try:
        row = graph_ops.read_set_key(
            _harness_usage_settings.HARNESS_USAGE_SET_ID,
            row_key, org="personal", peers=[],
        )
    except Exception:
        return None
    return (row or {}).get("payload") if isinstance(row, dict) else None


async def api_harness_usage(request):
    _ = request
    data = await asyncio.to_thread(_collect_harness_usage)
    return JSONResponse(data)


async def api_attention(request):
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"results": "", "error": None})
    org = api_auth.organization_scope_from_request(request)
    last = request.query_params.get("last")
    search = request.query_params.get("search")
    try:
        last_int = int(last) if last else None
    except ValueError:
        return JSONResponse({"error": f"invalid last: {last!r}"}, status_code=400)
    items = await asyncio.to_thread(
        graph_ops.list_attention,
        org=org, search=search, last=last_int,
    )
    lines = []
    for it in items:
        created = (it.get("created_at") or "")[:19]
        sid = (it.get("source_id") or "")[:12]
        sess = it.get("session_name") or ""
        content = (it.get("content") or "").replace("\n", " ")[:200]
        lines.append(f"{created}  [{sess}] src:{sid}  {content}")
    return JSONResponse({
        "results": "\n".join(lines),
        "attention": items,
        "error": None,
    })

async def api_active_sessions(request):
    """Find currently active Claude Code sessions (JSONL files still being written)."""
    import time
    from pathlib import Path

    threshold = int(request.query_params.get("threshold", "300"))  # seconds
    # Operator's home, not this process's: containerized, the host's
    # ~/.claude/projects is mounted at its host path and AUTONOMY_HOST_HOME
    # names it; natively the two are the same.
    projects_dir = (
        Path(os.environ.get("AUTONOMY_HOST_HOME") or Path.home())
        / ".claude" / "projects"
    )
    now = time.time()
    sessions = []

    if projects_dir.exists():
        for jsonl in projects_dir.rglob("*.jsonl"):
            try:
                stat = jsonl.stat()
                age = now - stat.st_mtime
                if age < threshold:
                    # Get last line for latest activity
                    last_line = ""
                    with open(jsonl, "rb") as f:
                        f.seek(max(0, stat.st_size - 2000))
                        last_line = f.read().decode("utf-8", errors="replace")

                    # Extract latest user or assistant text
                    latest = ""
                    import json as _json
                    for line in reversed(last_line.strip().split("\n")):
                        try:
                            e = _json.loads(line)
                            if e.get("type") in ("user", "assistant") and not e.get("isSidechain"):
                                msg = e.get("message", {})
                                content = msg.get("content", "")
                                if isinstance(content, str) and len(content) > 5:
                                    latest = content[:150]
                                    break
                                elif isinstance(content, list):
                                    for c in content:
                                        if isinstance(c, dict) and c.get("type") == "text":
                                            latest = c["text"][:150]
                                            break
                                    if latest:
                                        break
                        except _json.JSONDecodeError:
                            continue

                    sessions.append({
                        "session_id": jsonl.stem,
                        "project": jsonl.parent.name,
                        "size_bytes": stat.st_size,
                        "age_seconds": round(age),
                        "active": age < 60,
                        "latest": latest,
                    })
            except OSError:
                continue

    sessions.sort(key=lambda s: s["age_seconds"])
    return JSONResponse(sessions)


async def api_terminals(request):
    """List active terminal sessions (tmux-backed, DB-sourced)."""
    live_tmux = set(_list_dashboard_tmux())
    db_sessions = dashboard_db.get_live_sessions()
    result = []
    for row in db_sessions:
        name = row["tmux_name"]
        alive = name in live_tmux
        if not alive:
            # Mark dead in DB if tmux is gone
            dashboard_db.mark_dead(name)
            continue
        result.append({
            "id": name,
            "alive": True,
            "cmd": "",
            "env": "container" if row["type"] == "container" else "host",
            "started": row["created_at"],
        })
    # Also include live tmux sessions not in DB yet (e.g. pre-existing)
    db_names = {r["tmux_name"] for r in db_sessions}
    for name in live_tmux:
        if name not in db_names:
            info = _detect_terminal_type(name)
            info["started"] = asyncio.get_event_loop().time()
            result.append({"id": name, "alive": True, **info})
    return JSONResponse(result)

def _host_form(s: str) -> str:
    """Render a string containing this process's paths in HOST paths.

    A containerized node sees the repo at /app and its home at
    /home/autonomy; the host tmux server that forks host sessions sees
    neither. The entrypoint exports AUTONOMY_HOST_ROOT (=<data root>/code)
    and AUTONOMY_HOST_HOME for exactly this translation — the launch-side
    twin of tools/graph/ingest.py's ingest-side rewrites. Native runs have
    neither var set and this is the identity function.
    """
    for env_key, src in (("AUTONOMY_HOST_ROOT", str(_REPO_ROOT)),
                         ("AUTONOMY_HOST_HOME", str(Path.home()))):
        dst = os.environ.get(env_key)
        if dst and dst != src:
            s = s.replace(src, dst)
    return s


async def api_terminal_kill(request):
    """Stop a terminal session via the lifecycle worker.

    Dashboard-tracked sessions go through the worker's STOP handler
    (stopping → cleaning → dead, bounded steps, chatwith ingest on
    completion) and return 202 immediately — even when their tmux session
    is already gone, because the workload can outlive tmux: proven
    2026-08-31, a container session whose tmux died returned "not found"
    here while its container kept running, making close a dead button.
    Only a name that neither tmux nor dashboard.db knows is not_found.
    Unknown-to-the-DB tmux sessions fall back to a direct kill.
    """
    name = request.path_params["id"]
    if not _tmux_session_exists(name) and not dashboard_db.session_exists(name):
        return JSONResponse({"status": "not_found", "id": name})

    if dashboard_db.session_exists(name):
        job = LifecycleJob("stop", name, {"event_loop": asyncio.get_running_loop()})
        if _SESSION_LIFECYCLE_WORKER.try_enqueue(job):
            return JSONResponse(
                {"status": "stopping", "id": name}, status_code=202,
            )
        logger.warning(
            "api_terminal_kill: lifecycle queue full; stopping %s inline", name,
        )

    # Non-dashboard tmux session, or queue-full fallback: direct kill of
    # both halves — the tmux session and any same-named container (each a
    # no-op when absent, including for host terminals).
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    await session_monitor.deregister(name)
    await asyncio.to_thread(auth_db.revoke_token, name)
    if name.startswith("chatwith-"):
        asyncio.create_task(asyncio.to_thread(
            subprocess.run,
            ["graph", "sessions", "--all"],
            capture_output=True, timeout=30,
            cwd=str(Path(__file__).parents[2]),
        ))
    return JSONResponse({"status": "killed", "id": name})


async def api_terminal_rename(request):
    """Rename a terminal session's display name."""
    name = request.path_params["id"]
    body = await request.json()
    new_name = body.get("name", "").strip()
    if dashboard_db.session_exists(name):
        # Display name is UI-only — stored in DB as last_message prefix if needed
        return JSONResponse({"ok": True, "id": name, "name": new_name})
    return JSONResponse({"error": "not found"}, status_code=404)


# ── CrossTalk API ────────────────────────────────────────────────────────────

#: The only session type that identifies a local/operator caller. Everything
#: else the launcher can mint (``container``, ``dispatch``, ``librarian``,
#: ``chatwith``, ``agentic``, ``terminal``, …) is a docker-launched agent
#: subject to the fail-closed org stamp.
_LOCAL_SESSION_TYPE = "host"


def _is_local_caller(session: str) -> bool:
    """True ONLY for a positively-asserted local/operator session: an existing
    session row whose ``type`` is ``host``.

    Used to classify an org-less token. Locality is a POSITIVE assertion, never
    an absence: no row, an unknown type, an unreadable row, or a lookup error
    all mean "not local" — an org-less token in any of those states is refused.
    Rowless is the NORMAL end state for aged-out dispatch/librarian containers
    (their tokens outlive their session rows), so admitting a rowless token as
    local would hand every one of them dashboard authority once settings routes
    consume the org (h4kzx). Discriminates on the launcher-written ``type``
    field only, so it stays cache-independent (never the b4lbv-stale workspace
    map).
    """
    try:
        from tools.dashboard.dao import dashboard_db
        row = dashboard_db.get_session(session)
    except Exception:
        return False
    if not row:
        return False
    return (row.get("type") or "").strip() == _LOCAL_SESSION_TYPE


def authenticate_session_request(
    request,
) -> tuple[tuple[str, str | None] | None, JSONResponse | None]:
    """Authenticate a request's bearer session token — the shared request-auth
    primitive for every restricted dashboard route (CrossTalk, settings-org
    scoping, turn-correction, …).

    Returns ``((session, org), None)`` on success, or ``(None, error_response)``.
    ``org`` is the organization stamped on the token at mint: a slug for a
    container, ``None`` for a local/host caller.

    Fail-closed guard: a token whose org is absent but whose session maps to a
    workspace is a container token minted before the org column existed (there
    is no backfill) or by a future mint site that forgot to stamp it. It is
    REFUSED, never treated as a local caller — a forgotten org locks a session
    out rather than escalating it to full dashboard authority. Callers may then
    trust ``org is None`` to mean "genuine local caller."
    """
    auth = request.headers.get("authorization", "")
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return None, JSONResponse(
            {"error": "missing or invalid Authorization header"}, status_code=401)
    raw_token = parts[1]
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    resolved = auth_db.resolve_token(token_hash)
    if resolved is None:
        return None, JSONResponse(
            {"error": "invalid or revoked token"}, status_code=401)
    session, org = resolved
    if org is None and not _is_local_caller(session):
        return None, JSONResponse(
            {"error": (
                "session token carries no organization; relaunch the session "
                "to mint an org-stamped token"
            )},
            status_code=403)
    return (session, org), None


def _crosstalk_auth(request) -> tuple[str | None, JSONResponse | None]:
    """Extract and verify a bearer session token for CrossTalk routes.

    Thin wrapper over :func:`authenticate_session_request`; CrossTalk needs only
    the sender identity, so it discards org. Returns (sender_tmux_name, None) on
    success, or (None, error_response) on failure.
    """
    identity, err = authenticate_session_request(request)
    if err is not None:
        return None, err
    session, _org = identity
    return session, None


#: The route prefix the MCP relay service token is scoped to. Its whole authority
#: is reaching these protocol routes — resolving/binding a chat session and
#: relaying crosstalk. Everything organization-scoped is decided downstream, per
#: chat session, through the approval protocol; the token itself carries no org
#: and no dashboard authority (see auth_db.insert_service_token).
_MCP_SERVICE_ROUTE_PREFIX = "/api/mcp/"


def authenticate_mcp_service(request) -> "api_auth.ApiPrincipal | None":
    """Classify the machine-scoped MCP relay service token, ONLY on MCP routes.

    Returns an ``MCP_SERVICE`` principal when the request targets an
    ``/api/mcp/*`` route and carries a valid, non-revoked service token; ``None``
    otherwise. The route scoping lives here: off these routes the function
    returns None, so the token never authenticates anywhere else — and because a
    service token is not a session token (``auth_db.resolve_token`` cannot see
    it), the ordinary bearer path rejects it there too. The per-handler
    ``_relay_auth`` check stays on the handlers as defense in depth.
    """
    path = request.url.path
    if not path.startswith(_MCP_SERVICE_ROUTE_PREFIX):
        return None
    auth = request.headers.get("authorization", "")
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return None
    token_hash = hashlib.sha256(parts[1].encode()).hexdigest()
    name = auth_db.resolve_service_token(token_hash)
    if name is None:
        return None
    return api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.MCP_SERVICE, subject=name,
    )


DISPATCHER_TOKEN_FILE = DATA_ROOT / ".dispatch_token"


def _ensure_dispatcher_service_token() -> None:
    """Provision the dispatcher's route-scoped bearer (handoff item 4).

    Commit 8066651 stripped monitor auth as "localhost, no trust
    boundary"; the authenticated-API work then made every unadorned call
    401 and no credential was ever handed back — so the dispatcher's
    register/deregister silently failed on every dispatch. This mints a
    scoped service token good for EXACTLY the two monitor routes, stores
    only its hash (machine-local auth.db), and writes the secret to a
    0600 file under data/ that the host-side dispatcher reads per call.
    Re-minted on every dashboard start with a 7-day expiry, so restarts
    rotate it and superseded tokens age out.
    """
    import secrets as _secrets
    token = _secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    auth_db.insert_scoped_service_token(
        token_hash,
        "dispatcher-monitor",
        capabilities=[
            {"method": "POST", "path": "/api/monitor/register"},
            {"method": "POST", "path": "/api/monitor/deregister"},
        ],
        application_scope="dispatcher-monitor",
        resource_audience="dashboard-local",
        source_approval_id="dashboard-startup-provisioned",
        expires_at=time.time() + 7 * 24 * 3600,
    )
    DISPATCHER_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Created 0600 atomically (house pattern, unlock_routes) — never a
    # window where the plaintext exists with default-umask permissions.
    fd = os.open(
        DISPATCHER_TOKEN_FILE,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        os.write(fd, token.encode())
    finally:
        os.close(fd)
    os.chmod(DISPATCHER_TOKEN_FILE, 0o600)
    logger.info("dispatcher monitor token provisioned at %s",
                DISPATCHER_TOKEN_FILE)


def authenticate_service(request) -> "api_auth.ApiPrincipal | None":
    """Classify a fixed-purpose or generically scoped machine credential.

    The existing MCP verifier remains unchanged and independently testable.
    Operator-approved external credentials carry immutable exact method/path
    capabilities in the hashed auth store. A token classifies only when the
    current request matches one of those stored pairs; off-scope it falls
    through and cannot resolve as a session token.
    """
    mcp = authenticate_mcp_service(request)
    if mcp is not None:
        return mcp
    auth = request.headers.get("authorization", "")
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return None
    token_hash = hashlib.sha256(parts[1].encode()).hexdigest()
    scope = auth_db.resolve_scoped_service_token(
        token_hash, method=request.method, path=request.url.path,
    )
    if scope is None:
        return None
    capabilities = tuple(
        (capability["method"], capability["path"])
        for capability in scope["capabilities"]
    )
    return api_auth.ApiPrincipal(
        api_auth.ApiPrincipalKind.EXTERNAL_SERVICE,
        subject=scope["name"],
        api_capabilities=capabilities,
        application_scope=scope.get("application_scope"),
        resource_audience=scope.get("resource_audience"),
        source_approval_id=scope.get("sourceApprovalId"),
    )


def _session_hidden_cross_org(request, session_row) -> bool:
    """True when an organization-stamped API caller must not see this session
    because it belongs to a different organization (or its org cannot be
    resolved).

    The caller then returns its OWN route-native "not found" so a cross-org
    session is byte-indistinguishable from a nonexistent one: a 403 would confirm
    the session exists in another org, an existence oracle the org boundary is
    meant to deny. The guard must run BEFORE any reconcile, approval lookup,
    UUID/path/fallback resolution, or file read, so none of those observe a
    session the caller may not.

    Global-authority callers (the operator cookie, a local host token) see every
    session; a same-org org-bound caller passes. Compatibility traffic is
    governed by the default-deny gate, not here, so it is never judged cross-org.
    ``session_row`` is any payload ``session_org_slug`` understands (a
    dashboard_db row or a monitor registry row); ``None`` means the session could
    not be resolved, which an org-stamped caller may not distinguish from
    cross-org and so is hidden.
    """
    principal = api_auth.principal_from_request(request)
    if not principal.org_bound:
        return False
    from tools.dashboard.org_identity import session_org_slug
    session_org = session_org_slug(session_row) if session_row else None
    return not (session_org and principal.org and session_org == principal.org)


async def api_resources(request):
    """GET /api/resources — per-session CPU/RAM/disk samples + collector health.

    ``?history=1`` includes each session's ring buffer of (ts, cpu_pct,
    mem_bytes) samples for sparklines. The ``health`` block carries the
    collector's own cost profile (tick/disk-measure timing stats) so the
    overhead of monitoring is always inspectable.
    """
    include_history = request.query_params.get("history") in ("1", "true")
    return JSONResponse(resource_monitor.snapshot(include_history=include_history))


async def api_resources_refresh(request):
    """POST /api/resources/{tmux_name}/refresh — force a full disk re-measure.

    Backs the UI refresh affordance under the disk stat: bypasses the
    cadence clocks and the idle skip, so the baseline cadence can stay slow.
    Returns the fresh merged disk dict.
    """
    tmux_name = request.path_params["tmux_name"]
    disk = await resource_monitor.refresh_disk(tmux_name)
    if disk is None:
        return JSONResponse(
            {"error": f"no live session: {tmux_name}"}, status_code=404)
    return JSONResponse({"tmux_name": tmux_name, "disk": disk})


async def api_monitor_register(request):
    """POST /api/monitor/register — register a session with the in-process monitor.

    Body: {tmux_name, type, jsonl_path, bead_id, project, run_dir?, harness?, model?}

    Calls session_monitor.register_session() in-process so that the DB row
    AND the inotify watch AND the SSE registry broadcast all happen. This is
    the IPC bridge between the dispatcher (separate process) and the
    dashboard — a direct dashboard_db.upsert_session() from the dispatcher
    would bypass watch setup and the SSE broadcast, leaving the overlay
    frozen (the auto-ylj6r gap, pitfall graph://f4b1bb26-a1).

    Idempotent on re-register: existing rows are left in place and the
    in-process watch is refreshed.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    tmux_name = body.get("tmux_name")
    session_type = body.get("type")
    jsonl_path = body.get("jsonl_path")
    if not tmux_name or not session_type:
        return JSONResponse(
            {"error": "tmux_name and type are required"}, status_code=400,
        )

    bead_id = body.get("bead_id")
    project = body.get("project")
    run_dir = body.get("run_dir")
    requested_harness = body.get("harness")
    if requested_harness is not None and not isinstance(requested_harness, str):
        return JSONResponse({"error": "harness must be a string"}, status_code=400)
    model = body.get("model")
    if model is not None and not isinstance(model, str):
        return JSONResponse({"error": "model must be a string"}, status_code=400)

    existing = dashboard_db.get_session(tmux_name)
    if existing is not None:
        identity_changed = bool(
            (requested_harness and requested_harness != existing.get("harness"))
            or (model and model != existing.get("model"))
        )
        if requested_harness or model:
            dashboard_db.update_session_provider_identity(
                tmux_name,
                harness=requested_harness or None,
                model=model or None,
            )
        # Idempotent re-register: refresh the in-process watch without
        # rebuilding the tail state/parser context on every dispatcher poll.
        if jsonl_path and session_monitor._use_inotify:
            try:
                session_monitor._add_file_watch(tmux_name, jsonl_path)
            except Exception:
                logger.exception(
                    "api_monitor_register: watch refresh failed for %s",
                    tmux_name,
                )
        if identity_changed:
            await session_monitor._broadcast_registry()
        return JSONResponse({"ok": True, "tmux_name": tmux_name})

    harness = requested_harness or "claude"

    try:
        await session_monitor.register_session(
            tmux_name=tmux_name,
            type=session_type,
            jsonl_path=jsonl_path,
            run_dir=run_dir,
            bead_id=bead_id,
            project=project,
            harness=harness,
            model=model or None,
        )
    except Exception as exc:
        logger.exception("api_monitor_register failed for %s", tmux_name)
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"ok": True, "tmux_name": tmux_name})


async def api_monitor_deregister(request):
    """POST /api/monitor/deregister — mark a session dead in the monitor.

    Body: {tmux_name}

    Calls session_monitor.deregister_session() in-process so the inotify
    watch is removed, the DB row is flipped to is_live=0, and the registry
    SSE broadcast fires. Idempotent — missing sessions return 200.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    tmux_name = body.get("tmux_name")
    if not tmux_name:
        return JSONResponse({"error": "tmux_name is required"}, status_code=400)

    try:
        await session_monitor.deregister_session(tmux_name)
    except Exception as exc:
        logger.exception("api_monitor_deregister failed for %s", tmux_name)
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({"ok": True})


async def api_crosstalk_send(request):
    """POST /api/crosstalk/send — deliver a plain-text message to a peer session."""
    sender, err = _crosstalk_auth(request)
    if err:
        return err

    # Parse body
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    target = body.get("target", "")
    message = body.get("message", "")

    error = _validate_crosstalk_message(message)
    if error:
        return JSONResponse({"error": error}, status_code=400)

    # Validate target. A live tmux session delivers normally (below). A target
    # that is a known chat handle (ChatGPT-<datetime>) has no live pane; queue the
    # message for that chat to collect — but only while an approved crosstalk grant
    # links this sender to that chat (the same grant the chat's own send created;
    # no new approval for the reply). Any other target is unknown → 404.
    if not target or not _tmux_session_exists(target):
        chat = mcp_relay_db.get_session_by_handle(target) if target else None
        if chat is None:
            return JSONResponse({"error": f"target session not found: {target}"},
                                status_code=404)
        if not mcp_relay_db.crosstalk_allowed(chat["openai_session"], sender):
            return JSONResponse(
                {"error": f"no approved crosstalk channel with {target}"},
                status_code=403)
        reply_row = dashboard_db.get_session(sender)
        reply_label = (reply_row or {}).get("label", "") or sender
        await asyncio.to_thread(
            auth_db.insert_message, sender, reply_label, target,
            None, None, message, time.time(), 0)
        return JSONResponse({"delivered": False, "queued": True, "from": sender,
                             "label": reply_label, "target": target})

    # Resolve sender metadata from dashboard_db. Reconcile-on-read so a
    # drifted graph_source_id never makes it into the envelope (auto-4nr14
    # §A); use graph MAX(turn_number) for the envelope's turn= attribute,
    # NOT entry_count (auto-4nr14 §B — entry_count is the JSONL/viewer-tail
    # line count and lives on a different scale).
    sender_row = dashboard_db.get_session(sender)
    sender_label = (sender_row or {}).get("label", "") or sender
    sender_source_id = dashboard_db.reconcile_session_graph_source_id(sender_row)
    sender_turn = dashboard_db.get_source_max_turn_number(sender_source_id)
    sender_harness = (sender_row or {}).get("harness") or "claude"
    sender_model = (sender_row or {}).get("model") or ""

    # Build envelope. When MAX(turn_number) is unavailable (source not yet
    # ingested) emit an empty turn="" rather than fall back to a
    # different-scale counter (auto-4nr14 §B). The CrossTalk parser regex
    # requires the attribute to be present; empty string is valid.
    iso_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    turn_str = str(sender_turn) if sender_turn is not None else ""
    envelope = (
        f'<crosstalk from="{sender}"\n'
        f'           label="{sender_label}"\n'
        f'           source="{sender_source_id}" turn="{turn_str}"\n'
        f'           harness="{sender_harness}" model="{sender_model}"\n'
        f'           timestamp="{iso_now}">\n'
        f'{message}\n'
        f'</crosstalk>'
    )

    # Inject via unified tmux_send (per-session lock + double-Enter retry)
    await tmux_send(target, envelope)

    # Store in crosstalk_messages
    await asyncio.to_thread(
        auth_db.insert_message,
        sender, sender_label, target,
        sender_source_id or None, sender_turn,
        message, time.time(),
    )

    return JSONResponse({
        "delivered": True,
        "from": sender,
        "label": sender_label,
        "source_id": sender_source_id or None,
        "turn": sender_turn,
        "target": target,
        "harness": sender_harness,
        "model": sender_model or None,
    })


_MAX_BROADCAST_IDLE_SECS = 21600  # 6 hours
_MAX_CROSSTALK_MESSAGE_CHARS = 8000


def _validate_crosstalk_message(message: str) -> str | None:
    """Return a validation error for an outbound CrossTalk body, if any."""
    if not message or len(message) > _MAX_CROSSTALK_MESSAGE_CHARS:
        return f"message must be 1-{_MAX_CROSSTALK_MESSAGE_CHARS} characters"
    if "</crosstalk>" in message:
        return "message must not contain </crosstalk>"
    return None


async def api_crosstalk_broadcast(request):
    """POST /api/crosstalk/broadcast — send message to all recently-active peers.

    Query params:
        max_idle: int — max idle seconds (default 3600, capped at 21600)
    Body JSON: {"message": "..."}
    """
    sender, err = _crosstalk_auth(request)
    if err:
        return err

    # Parse max_idle from query params
    try:
        max_idle = int(request.query_params.get("max_idle", "3600"))
    except (ValueError, TypeError):
        return JSONResponse({"error": "max_idle must be an integer (seconds)"}, status_code=400)
    if max_idle < 0:
        return JSONResponse({"error": "max_idle must be non-negative"}, status_code=400)
    if max_idle > _MAX_BROADCAST_IDLE_SECS:
        return JSONResponse(
            {"error": f"max_idle exceeds maximum of {_MAX_BROADCAST_IDLE_SECS}s (6h)"},
            status_code=400,
        )

    # Parse body
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    message = body.get("message", "")
    error = _validate_crosstalk_message(message)
    if error:
        return JSONResponse({"error": error}, status_code=400)

    # Get live sessions, filter by idle time
    conn = dashboard_db.get_conn()
    rows = conn.execute(
        "SELECT tmux_name, last_activity, created_at FROM tmux_sessions"
        " WHERE state NOT IN ('ENDED','FAILED') AND tmux_name != ?",
        (sender,),
    ).fetchall()

    now = time.time()
    targets = []
    skipped = 0
    for r in rows:
        la = r["last_activity"]
        if la is None:
            # Fall back to created_at (ISO string)
            ca = r["created_at"]
            if isinstance(ca, str):
                try:
                    la = datetime.fromisoformat(ca).replace(tzinfo=timezone.utc).timestamp()
                except (ValueError, TypeError):
                    la = None
        if la is None or (now - la) >= max_idle:
            skipped += 1
            continue
        targets.append(r["tmux_name"])

    # Resolve sender metadata. Reconcile-on-read + graph MAX(turn_number)
    # (auto-4nr14 §A/§B) — see api_crosstalk_send for full rationale.
    sender_row = dashboard_db.get_session(sender)
    sender_label = (sender_row or {}).get("label", "") or sender
    sender_source_id = dashboard_db.reconcile_session_graph_source_id(sender_row)
    sender_turn = dashboard_db.get_source_max_turn_number(sender_source_id)
    sender_harness = (sender_row or {}).get("harness") or "claude"
    sender_model = (sender_row or {}).get("model") or ""

    # Build envelope. Empty turn="" when graph turn number is unavailable.
    iso_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    turn_str = str(sender_turn) if sender_turn is not None else ""
    envelope = (
        f'<crosstalk from="{sender}"\n'
        f'           label="{sender_label}"\n'
        f'           source="{sender_source_id}" turn="{turn_str}"\n'
        f'           harness="{sender_harness}" model="{sender_model}"\n'
        f'           timestamp="{iso_now}">\n'
        f'{message}\n'
        f'</crosstalk>'
    )

    sent = 0
    failed = 0
    for target in targets:
        try:
            await tmux_send(target, envelope)
            await asyncio.to_thread(
                auth_db.insert_message,
                sender, sender_label, target,
                sender_source_id or None, sender_turn,
                message, time.time(),
            )
            sent += 1
        except Exception:
            failed += 1

    return JSONResponse({
        "sent": sent,
        "skipped_idle": skipped,
        "failed": failed,
        "total_live": len(rows),
        "max_idle": max_idle,
    })


async def api_crosstalk_peers(request):
    """GET /api/crosstalk/peers — list live sessions excluding the caller."""
    sender, err = _crosstalk_auth(request)
    if err:
        return err

    conn = dashboard_db.get_conn()
    rows = conn.execute(
        "SELECT tmux_name, type, label, created_at FROM tmux_sessions"
        " WHERE state NOT IN ('ENDED','FAILED') AND tmux_name != ?",
        (sender,),
    ).fetchall()
    return JSONResponse({"peers": [dict(r) for r in rows]})


async def api_crosstalk_log(request):
    """GET /api/crosstalk/log — list recent CrossTalk messages."""
    from tools.graph.duration import parse_duration

    _sender, err = _crosstalk_auth(request)
    if err:
        return err

    try:
        limit = int(request.query_params.get("limit", "30"))
    except (ValueError, TypeError):
        return JSONResponse({"error": "limit must be an integer"}, status_code=400)
    if limit <= 0:
        return JSONResponse({"error": "limit must be positive"}, status_code=400)

    session = request.query_params.get("session") or None
    since = request.query_params.get("since")
    since_epoch = None
    if since:
        try:
            since_epoch = time.time() - parse_duration(since)
        except ValueError:
            return JSONResponse(
                {"error": "since must be a duration like 30m, 1h, or 2d"},
                status_code=400,
            )

    messages = await asyncio.to_thread(
        auth_db.get_messages,
        limit=limit,
        since=since_epoch,
        session=session,
    )
    return JSONResponse({"messages": messages})


async def api_session_startup_trace(request):
    """GET /api/session/{tmux_name}/startup-trace — the persisted, structured
    per-session startup timeline (data/session-traces/<tmux_name>.jsonl).

    The reliable replacement for grepping phase-trace lines out of the
    rotating dashboard.log: one query returns every phase with its dt_ms and
    the terminal outcome (tracked / zombie_202 / prep error).
    """
    tmux_name = request.path_params["tmux_name"]
    # Cross-org guard BEFORE reading the trace (auto-49esb): a cross-org session
    # looks the same as one with no trace — an empty timeline, no existence leak.
    if _session_hidden_cross_org(request, dashboard_db.get_session(tmux_name)):
        return JSONResponse({
            "tmux_name": tmux_name, "event_count": 0, "outcome": None, "events": [],
        })
    events = session_trace.read_trace(tmux_name)
    outcome = next(
        (e.get("outcome") for e in reversed(events) if e.get("outcome")), None,
    )
    return JSONResponse({
        "tmux_name": tmux_name,
        "event_count": len(events),
        "outcome": outcome,
        "events": events,
    })


async def api_primer(request):
    bead_id = request.path_params["id"]
    org, refused = _beads_request_org(request)
    if refused is not None:
        return refused
    if os.environ.get("DASHBOARD_MOCK"):
        primer = dao_beads.get_primer(bead_id)
        if not primer:
            return JSONResponse({"error": "bead not found"}, status_code=404)
        return JSONResponse(primer)
    from tools.graph.primer import collect_primer_data, format_for_dashboard
    try:
        def _collect():
            # Open the DB for the caller's org so the primer sees the right
            # provenance edges + pitfall notes.
            db = graph_ops._open(org)
            try:
                return collect_primer_data(
                    bead_id, db=db,
                    include_provenance=True, include_pitfalls=True,
                )
            finally:
                db.close()
        data = await asyncio.to_thread(_collect)
    except Exception as e:
        return JSONResponse({"error": str(e) or "primer generation failed"}, status_code=500)
    return JSONResponse(format_for_dashboard(data))


async def api_chatwith_primer(request):
    """Return a Chat With primer for a specific page type and context ID.

    GET /api/chatwith/primer/{page_type}?context={id}

    Returns {primer_text: str}.
    Returns 400 for unknown page_type or missing context param.
    Returns 404 if the context resource is not found.
    """
    from tools.dashboard.chatwith_primers import get_primer, VALID_PAGE_TYPES

    page_type = request.path_params["page_type"]
    context_id = request.query_params.get("context", "").strip()

    if not context_id:
        return JSONResponse(
            {"error": "Missing required query parameter: context"},
            status_code=400,
        )

    try:
        result = await asyncio.to_thread(get_primer, page_type, context_id)
        return JSONResponse(result)
    except ValueError as exc:
        msg = str(exc)
        if "Unknown page type" in msg:
            return JSONResponse(
                {"error": msg, "valid_types": VALID_PAGE_TYPES},
                status_code=400,
            )
        # Resource not found (design missing, etc.)
        return JSONResponse({"error": msg}, status_code=404)


async def api_chatwith_check(request):
    """Check if a Chat With tmux session exists.

    GET /api/chatwith/check?session={session_name}
    Returns {exists: bool, session_name: str}.
    """
    session_name = request.query_params.get("session", "").strip()
    if not session_name:
        return JSONResponse({"error": "Missing session parameter"}, status_code=400)
    exists = await asyncio.to_thread(_tmux_session_exists, session_name)
    return JSONResponse({"exists": exists, "session_name": session_name})


async def api_chatwith_sessions(request):
    """List all active Chat With tmux sessions.

    GET /api/chatwith/sessions
    Returns {sessions: [session_name, ...]} for all tmux sessions prefixed 'chatwith-'.
    """
    result = await asyncio.to_thread(
        subprocess.run,
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return JSONResponse({"sessions": []})
    sessions = [s for s in result.stdout.strip().split("\n")
                 if s.startswith("chatwith-") or s.startswith("chat-")]
    return JSONResponse({"sessions": sessions})



# ── Live Session Tailing ──────────────────────────────────────


# auto-ngis4: harness + model are optional (additive) so legacy envelopes
# without those attrs continue to parse and pre-existing callers see the
# same group set. Named groups insulate consumers from positional shifts.
# ANY well-formed envelope, not one attribute inventory. The old pattern
# hardcoded the operator-send attribute set in order, so every envelope a
# substrate feature minted with its own attributes (mission-question's
# kind/mission/entry_id, surface pings) failed the fullmatch and rendered
# as raw XML in a user bubble. The attributes are data; the parser's only
# job is the envelope shape.
_CROSSTALK_RE = re.compile(
    r'<crosstalk\s+(?P<attrs>[\w-]+="[^"]*"(?:\s+[\w-]+="[^"]*")*)\s*>\n'
    r'(?P<body>.*)\n</crosstalk>',
    re.DOTALL,
)
_CROSSTALK_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')


def _classify_crosstalk(text: str) -> dict | None:
    """Detect CrossTalk peer messages in user entries.

    Returns a dict with sender info and message body, or None if the text
    is not a valid crosstalk envelope. Body may contain ordinary angle
    brackets; only a literal closing envelope tag inside the body is
    rejected.
    """
    stripped = text.strip()
    m = _CROSSTALK_RE.fullmatch(stripped)
    if not m:
        return None
    body = m.group("body")
    if "</crosstalk>" in body:
        return None
    attrs = dict(_CROSSTALK_ATTR_RE.findall(m.group("attrs")))
    sender = attrs.get("from", "")
    if not sender:
        return None
    return {
        "from": sender,
        # A substrate envelope has no label; its kind is the honest one.
        "label": attrs.get("label") or attrs.get("kind") or sender,
        "source": attrs.get("source", ""),
        "turn": attrs.get("turn", ""),
        "timestamp": attrs.get("timestamp", ""),
        "harness": attrs.get("harness", ""),
        "model": attrs.get("model", ""),
        "kind": attrs.get("kind", ""),
        # An envelope may carry its own destination — a mission question
        # deep-links to the exact conversation it opened. Data, not trust:
        # the renderer treats it as an ordinary same-origin path.
        "href": attrs.get("href", ""),
        "message": body,
    }


def _parse_crosstalk_send(command: str, timestamp: str) -> dict | None:
    """Detect outbound CrossTalk send in a Bash command."""
    if "crosstalk/send" not in command:
        return None
    import shlex
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None  # malformed shell quoting
    # Find the token after -d
    payload = None
    for i, tok in enumerate(tokens):
        if tok == "-d" and i + 1 < len(tokens):
            try:
                parsed = json.loads(tokens[i + 1])
                if isinstance(parsed, dict) and "target" in parsed and "message" in parsed:
                    payload = parsed
                    break
            except (json.JSONDecodeError, ValueError):
                continue
    if not payload:
        return None
    return {
        "type": "crosstalk",
        "role": "crosstalk",
        "content": payload.get("message", ""),
        "sender": "self",
        "sender_label": "",
        "source_id": "",
        "turn": "",
        "target": payload.get("target", ""),
        "direction": "sent",
        "timestamp": timestamp,
    }


def _parse_graph_comment_cmd(command: str, timestamp: str) -> dict | None:
    m = re.search(r'graph comment\s+(\S+)', command)
    if not m:
        return None
    return {
        "type": "semantic_bash",
        "semantic_type": "comment-added",
        "role": "assistant",
        "source_id": m.group(1),
        "content": "Added comment",
        "timestamp": timestamp,
    }


def _parse_dispatch_approve_cmd(command: str, timestamp: str) -> dict | None:
    m = re.search(r'graph dispatch approve\s+(\S+)', command)
    if not m:
        return None
    return {
        "type": "semantic_bash",
        "semantic_type": "dispatch-approved",
        "role": "assistant",
        "bead_id": m.group(1),
        "content": f"Approved {m.group(1)} for dispatch",
        "timestamp": timestamp,
    }


def _parse_bd_setstate_cmd(command: str, timestamp: str) -> dict | None:
    m = re.search(r'bd set-state\s+(\S+)\s+(\S+=\S+)', command)
    if not m:
        return None
    return {
        "type": "semantic_bash",
        "semantic_type": "state-changed",
        "role": "assistant",
        "bead_id": m.group(1),
        "state": m.group(2),
        "content": f"Set {m.group(2)} on {m.group(1)}",
        "timestamp": timestamp,
    }


def _upconvert_graph_result(content: str, timestamp: str, tool_id: str = "") -> dict | None:
    """Upconvert graph CLI tool_result output to semantic tiles.

    Detects note creation, thought capture, and comment addition confirmations
    in tool_result text and returns a semantic_bash entry with extracted IDs.
    Preserves tool_id so activity_state tracking can match tool_use → result.
    """
    if not isinstance(content, str):
        return None
    # Note saved (src:abc123-456)
    base = {"type": "semantic_bash", "role": "tool", "timestamp": timestamp}
    if tool_id:
        base["tool_id"] = tool_id
    # Match only actual graph CLI output.  Real output looks like:
    #   "  ✓ Note saved (src:abc123-def) — 25 lines, 1234 chars"
    # The ✓ must appear at the start of a line (after optional whitespace)
    # to avoid false positives when Bash prints repr/test output that
    # happens to contain ✓ embedded in a larger string.
    m = re.search(r"^\s*\u2713 Note saved \(src:([a-f0-9-]+)\)", content, re.MULTILINE)
    if m:
        return {**base, "semantic_type": "note-created",
                "source_id": m.group(1), "content": content.strip()[:100]}
    m = re.search(r"^\s*\u2713 Captured:\s*([a-f0-9-]+)", content, re.MULTILINE)
    if m:
        return {**base, "semantic_type": "thought-captured",
                "source_id": m.group(1), "content": content.strip()[:100]}
    m = re.search(r"^\s*\u2713 Comment added.*?id:([a-f0-9-]+)", content, re.MULTILINE)
    if m:
        return {**base, "semantic_type": "comment-added",
                "comment_id": m.group(1), "content": content.strip()[:100]}
    return None


def _enrich_semantic_tile(entry: dict) -> None:
    """Enrich note-created/thought-captured/comment-added tiles with graph.db data.

    One SQLite read per semantic tile. Mutates entry in place, adding title,
    preview, and tags fields. Graceful fallback: if graph.db unavailable or
    source not found, the entry is left unchanged (raw CLI output).
    """
    source_id = entry.get("source_id") or entry.get("comment_id")
    if not source_id:
        return

    db_path = _graph_db_path()
    try:
        if db_path:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        else:
            # Default path
            default = Path(__file__).resolve().parents[2] / "data" / "graph.db"
            if not default.exists():
                return
            conn = sqlite3.connect(f"file:{default}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except (sqlite3.OperationalError, OSError):
        return

    try:
        # For comments, look up the parent note's metadata
        lookup_id = source_id
        row = conn.execute(
            "SELECT id, title, metadata FROM sources WHERE id = ?", (lookup_id,)
        ).fetchone()
        if not row:
            # Try prefix match
            row = conn.execute(
                "SELECT id, title, metadata FROM sources WHERE id LIKE ? LIMIT 1",
                (f"{lookup_id}%",),
            ).fetchone()
        if not row:
            return

        meta = {}
        try:
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
        except (json.JSONDecodeError, TypeError):
            pass

        # For comment-added, use the parent note's data
        if entry.get("semantic_type") == "comment-added" and meta.get("parent_source_id"):
            parent_row = conn.execute(
                "SELECT id, title, metadata FROM sources WHERE id = ?",
                (meta["parent_source_id"],),
            ).fetchone()
            if not parent_row:
                parent_row = conn.execute(
                    "SELECT id, title, metadata FROM sources WHERE id LIKE ? LIMIT 1",
                    (f"{meta['parent_source_id']}%",),
                ).fetchone()
            if parent_row:
                row = parent_row
                try:
                    meta = json.loads(parent_row["metadata"]) if parent_row["metadata"] else {}
                except (json.JSONDecodeError, TypeError):
                    meta = {}

        # Extract title (strip leading # headings)
        title = (row["title"] or "").lstrip("#").strip()

        # Extract tags from metadata
        tags = meta.get("tags", [])

        # Extract preview from first content entry, skip heading lines
        content_row = conn.execute(
            "SELECT content FROM thoughts WHERE source_id = ? ORDER BY turn_number LIMIT 1",
            (row["id"],),
        ).fetchone()
        preview = ""
        if content_row and content_row["content"]:
            lines = content_row["content"].split("\n")
            body_lines = [l for l in lines if not l.startswith("#") and l.strip()]
            preview = " ".join(body_lines)[:120]

        entry["title"] = title
        entry["preview"] = preview
        entry["tags"] = tags if isinstance(tags, list) else []
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        pass  # graceful fallback
    finally:
        conn.close()


def _classify_system_message(text: str) -> dict | None:
    """Detect harness-injected system messages in user entries.

    Returns a compact dict with type 'system' and a summary, or None if the
    text is a normal user message.
    """
    stripped = text.strip()

    # --- task-notification: extract status + summary -----------------------
    if "<task-notification>" in stripped:
        summary = ""
        status = ""
        m_summary = re.search(r"<summary>(.*?)</summary>", stripped, re.DOTALL)
        m_status = re.search(r"<status>(.*?)</status>", stripped, re.DOTALL)
        if m_summary:
            summary = m_summary.group(1).strip()
        if m_status:
            status = m_status.group(1).strip()
        label = summary if summary else f"Task {status}" if status else "Task notification"
        return {"summary": label, "tag": "task-notification"}

    # --- system-reminder: compact label, preserve body for expand ----------
    if "<system-reminder>" in stripped:
        m = re.search(r"<system-reminder>(.*?)</system-reminder>", stripped, re.DOTALL)
        body = m.group(1).strip() if m else stripped
        return {"summary": "System reminder", "tag": "system-reminder", "body": body}

    # --- local-command-stdout: summarise -----------------------------------
    if "<local-command-stdout>" in stripped:
        return {"summary": "Command output", "tag": "local-command-stdout"}

    # --- command-name: summarise -------------------------------------------
    if "<command-name>" in stripped:
        m = re.search(r"<command-name>(.*?)</command-name>", stripped, re.DOTALL)
        name = m.group(1).strip() if m else "command"
        return {"summary": f"Command: {name}", "tag": "command-name"}

    return None


def _parse_jsonl_entry(line: str) -> dict | None:
    """Parse a single JSONL line into a display entry."""
    return parse_claude_log_line(line)



def _enrich_entries(entries: list[dict], session_dir: Path | None = None) -> None:
    """Post-process parsed entries: enrich Agent tool_results with subagent info."""
    enrich_claude_entries(entries, session_dir=session_dir)


def _dedup_queued_entries(entries: list[dict]) -> list[dict]:
    """Remove duplicate user/crosstalk entries that follow a queued version."""
    return dedup_claude_entries(entries)


def _find_session_files(run_name: str, *, run_dir: Path | None = None) -> list[Path]:
    """Find JSONL session files for a run, checking multiple locations.

    Bead/librarian dispatches: the run directory is ``AGENT_RUNS_DIR /
    run_name``. Agentic dispatches: the dispatch_runs row carries an
    explicit ``output_dir`` because the slug + timestamp aren't tied
    to the run name. Callers pass that as ``run_dir`` when the row
    is agentic so we don't have to repeat the agentic-vs-bead split.
    """
    # 1. Run directory sessions — use rglob because Claude Code writes JSONL
    #    into a subdirectory (e.g. sessions/-workspace-repo/<hash>.jsonl)
    rd = run_dir if run_dir is not None else (AGENT_RUNS_DIR / run_name)
    sessions_dir = rd / "sessions"
    if sessions_dir.exists():
        files = sorted(sessions_dir.rglob("*.jsonl"), key=lambda f: f.stat().st_mtime)
        if files:
            return files

    return []


async def _read_session_jsonl(
    session_file: Path, *, after: int, run_name: str,
) -> JSONResponse:
    """Read a JSONL session file and return tail JSONResponse from byte ``after``.

    Shared between the bead-style ``/api/dispatch/tail/{run}`` path and the
    agentic-run path which finds the JSONL via ``dispatch_runs.output_dir``
    instead of the bead-naming glob. Both paths emit the same shape so the
    overlay client doesn't have to branch.
    """
    file_size = session_file.stat().st_size
    is_live = (import_time() - session_file.stat().st_mtime) < 120
    session_uuid = session_file.stem
    project = session_file.parent.name
    tmux_name = run_name
    session_id = tmux_name

    if after >= file_size:
        return JSONResponse({
            "entries": [], "offset": file_size, "is_live": is_live,
            "session_id": session_id, "tmux_name": tmux_name,
            "tmux_session": tmux_name, "session_uuid": session_uuid,
            "project": project,
        })

    reader = session_harness.resolve_harness_for_path(session_file)
    harness = reader.harness
    with open(session_file, "rb") as f:
        f.seek(after)
        data = f.read()
    # Never advance the cursor past a partial trailing line — a mid-write
    # read would otherwise silently lose that line on the next poll.
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        data = b""
        new_offset = after
    else:
        data = data[:last_nl + 1]
        new_offset = after + last_nl + 1
    entries = reader.parse_bytes_with_refs(data, base_offset=after)
    entries = harness.postprocess_entries(
        entries, session_dir=session_file.parent / session_file.stem,
    )
    session_harness.finalize_entry_refs(entries)
    return JSONResponse({
        "entries": entries,
        "offset": new_offset,
        "is_live": is_live,
        "session_id": session_id,
        "tmux_name": tmux_name,
        "tmux_session": tmux_name,
        "session_uuid": session_uuid,
        "project": project,
        "chain": [session_file.stem],
        "cursor": {"file": session_file.stem, "off": new_offset},
    })


async def api_dispatch_tail(request):
    """Tail JSONL session data for a dispatch run.

    Returns parsed entries after a byte offset for incremental polling.
    GET /api/dispatch/tail/{run}?after=N
    """
    run_name = request.path_params["run"]
    after = int(request.query_params.get("after", "0"))

    # Mock mode: resolve by run_dir against the fixture, mirroring
    # api_session_tail's DASHBOARD_MOCK branch.
    if os.environ.get("DASHBOARD_MOCK"):
        # First look up the run by id — a dashboard fixture may carry
        # entries under the run_id directly (this is how agentic-run
        # fixtures are seeded; they have no run_dir).
        sess = dao_sessions.get_session_by_run_dir(run_name)
        if sess is None:
            entries = dao_sessions.get_session_entries(run_name)
            if entries is not None:
                TaskStateTracker().enrich(run_name, entries)
                for i, e in enumerate(entries):
                    if isinstance(e, dict):
                        e.setdefault("entry_ref", {"file": "mock", "off": i, "sub": 0})
                return JSONResponse({
                    "entries": entries,
                    "offset": len(entries),
                    "is_live": True,
                    "session_id": run_name,
                    "tmux_session": run_name,
                    "tmux_name": run_name,
                    "project": "autonomy",
                    "type": "agentic",
                    "role": "",
                    "resolved": True,
                    "seq": len(entries),
                    "chain": ["mock"],
                })
        if sess:
            sid = sess.get("session_id") or sess.get("tmux_session") or run_name
            entries = dao_sessions.get_session_entries(sid) or []
            TaskStateTracker().enrich(sid, entries)
            for i, e in enumerate(entries):
                if isinstance(e, dict):
                    e.setdefault("entry_ref", {"file": "mock", "off": i, "sub": 0})
            return JSONResponse({
                "entries": entries,
                "offset": len(entries),
                "is_live": bool(sess.get("is_live", True)),
                "session_id": sid,
                "tmux_session": sess.get("tmux_session") or sid,
                "tmux_name": sess.get("tmux_session") or sid,
                "project": sess.get("project") or "autonomy",
                "type": sess.get("type") or "dispatch",
                "role": sess.get("role") or "",
                "resolved": True,
                "seq": len(entries),
                "chain": ["mock"],
            })
        # fall through to filesystem scan

    # Agentic-run path: dispatch_runs.output_dir is the authoritative
    # JSONL location (set at launch by api_agent_action_dispatch). When
    # the run is kind='agentic' we resolve the file via that column,
    # NOT by the bead-style "agent-{bead_id}-{pid}" globbing.
    try:
        run_row = dao_dispatch.get_run(run_name)
    except Exception:
        run_row = None
    if run_row and (run_row.get("kind") or "bead") == "agentic":
        output_dir = run_row.get("output_dir") or ""
        if not output_dir:
            logger.error(
                "api_dispatch_tail: agentic run %s has no output_dir", run_name,
            )
            return JSONResponse(
                {"error": "agentic run missing output_dir", "run_id": run_name},
                status_code=500,
            )
        sessions_dir = Path(output_dir) / "sessions"
        jsonl_files = sorted(
            sessions_dir.rglob("*.jsonl") if sessions_dir.exists() else [],
            key=lambda f: f.stat().st_mtime,
        )
        if not jsonl_files:
            return JSONResponse({
                "entries": [], "offset": 0,
                "is_live": (run_row.get("status") == "RUNNING"),
                "session_id": run_name,
                "tmux_name": run_name,
                "tmux_session": run_name,
                "project": "autonomy",
            })
        # Multi-JSONL handling deferred per Round 7h scope: claude emits
        # exactly one .jsonl per session today; take the first.
        session_file = jsonl_files[0]
        return await _read_session_jsonl(
            session_file, after=after, run_name=run_name,
        )

    session_files = _find_session_files(run_name)
    if not session_files:
        # Try docker exec fallback for running containers
        entries, new_offset, is_live = await _tail_from_container(run_name, after)
        if entries is not None:
            return JSONResponse({
                "entries": entries,
                "offset": new_offset,
                "is_live": is_live,
            })
        return JSONResponse({"entries": [], "offset": 0, "is_live": False})

    # Read from the largest/most recent session file
    session_file = session_files[-1]
    file_size = session_file.stat().st_size
    is_live = (import_time() - session_file.stat().st_mtime) < 120

    session_uuid = session_file.stem
    project = session_file.parent.name

    # Resolve tmux_name for this dispatch run. The monitor broadcasts
    # session:messages keyed on tmux_name (session_monitor.py:1075), so the
    # tail response MUST key its session_id on tmux_name too — otherwise the
    # overlay's SSE filter drops every broadcast (auto-yaw58).
    db_row = session_monitor.get_one(run_name)
    if db_row is None:
        owner = session_monitor._find_session_by_uuid(session_uuid)
        if owner:
            db_row = session_monitor.get_one(owner)
    tmux_name = db_row["tmux_name"] if db_row else run_name
    session_id = tmux_name

    if after >= file_size:
        return JSONResponse({
            "entries": [], "offset": file_size, "is_live": is_live,
            "session_id": session_id, "tmux_name": tmux_name,
            "tmux_session": tmux_name, "session_uuid": session_uuid,
            "project": project,
        })

    # B6: this bead-style branch parses the same way as _read_session_jsonl
    # — complete-line clamped, ref-stamped, finalized. Every server path
    # emits canonical identities; a refless entry would make the client
    # mint a fresh synthetic ref per delivery and duplicate tiles.
    reader = session_harness.resolve_harness_for_path(session_file)
    harness = reader.harness
    with open(session_file, "rb") as f:
        f.seek(after)
        data = f.read()
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        data = b""
        new_offset = after
    else:
        data = data[:last_nl + 1]
        new_offset = after + last_nl + 1
    entries = reader.parse_bytes_with_refs(data, base_offset=after)
    entries = harness.postprocess_entries(
        entries,
        session_dir=session_file.parent / session_file.stem,
    )
    session_harness.finalize_entry_refs(entries)
    return JSONResponse({
        "entries": entries,
        "offset": new_offset,
        "is_live": is_live,
        "session_id": session_id,
        "tmux_name": tmux_name,
        "tmux_session": tmux_name,
        "session_uuid": session_uuid,
        "project": project,
        "chain": [session_file.stem],
        "cursor": {"file": session_file.stem, "off": new_offset},
    })


def import_time():
    """Lazy import of time.time()."""
    import time
    return time.time()


async def _tail_from_container(run_name: str, after: int) -> tuple:
    """Try to tail session data from a running container via docker exec."""
    # Extract bead ID from run name
    parts = run_name.rsplit("-", 2)
    if len(parts) < 3:
        return None, 0, False

    bead_id = parts[0]

    # Find running container for this bead
    stdout, _, rc = await run_cli(
        ["docker", "ps", "--filter", f"name=agent-{bead_id}", "--format", "{{.Names}}"],
        timeout=5,
    )
    if rc != 0 or not stdout.strip():
        return None, 0, False

    container_name = stdout.strip().split("\n")[0]

    # Find session files inside container
    stdout, _, rc = await run_cli(
        ["docker", "exec", container_name, "sh", "-c",
         "ls -t /home/agent/.claude/projects/*/*.jsonl 2>/dev/null | head -1"],
        timeout=5,
    )
    if rc != 0 or not stdout.strip():
        return None, 0, False

    session_path = stdout.strip()

    # Read from offset using tail -c (O(1) seek, unlike dd bs=1 which is O(N))
    if after > 0:
        # tail -c +N starts reading at byte N (1-indexed)
        stdout, _, rc = await run_cli(
            ["docker", "exec", container_name, "sh", "-c",
             f"tail -c +{after + 1} '{session_path}'"],
            timeout=10,
        )
    else:
        stdout, _, rc = await run_cli(
            ["docker", "exec", container_name, "cat", session_path],
            timeout=10,
        )

    if rc != 0:
        return None, 0, False

    # B6: container tails carry the same canonical identity as every other
    # path — file stem from the in-container session path, byte offsets
    # from the actual read position, clamped to complete lines.
    raw = stdout.encode("utf-8")
    last_nl = raw.rfind(b"\n")
    if last_nl == -1:
        return [], after, True
    raw = raw[:last_nl + 1]
    new_offset = after + last_nl + 1
    stem = Path(session_path).stem
    entries = session_harness.parse_lines_with_refs(
        CLAUDE_HARNESS, raw, stem=stem, base_offset=after, ctx={},
    )
    entries = CLAUDE_HARNESS.postprocess_entries(entries)
    session_harness.finalize_entry_refs(entries)
    return entries, new_offset, True


async def _latest_from_container(run_name: str) -> dict | None:
    """Get latest assistant text from container using tail (not cat of entire file)."""
    parts = run_name.rsplit("-", 2)
    if len(parts) < 3:
        return None

    bead_id = parts[0]

    stdout, _, rc = await run_cli(
        ["docker", "ps", "--filter", f"name=agent-{bead_id}", "--format", "{{.Names}}"],
        timeout=5,
    )
    if rc != 0 or not stdout.strip():
        return None

    container_name = stdout.strip().split("\n")[0]

    stdout, _, rc = await run_cli(
        ["docker", "exec", container_name, "sh", "-c",
         "ls -t /home/agent/.claude/projects/*/*.jsonl 2>/dev/null | head -1"],
        timeout=5,
    )
    if rc != 0 or not stdout.strip():
        return None

    session_path = stdout.strip()

    # Get file size for token estimation
    size_stdout, _, size_rc = await run_cli(
        ["docker", "exec", container_name, "sh", "-c",
         f"stat -c %s '{session_path}' 2>/dev/null || echo 0"],
        timeout=5,
    )
    file_size_bytes = int(size_stdout.strip()) if size_rc == 0 and size_stdout.strip().isdigit() else 0

    # Only read last 4KB — enough to find the latest assistant text
    stdout, _, rc = await run_cli(
        ["docker", "exec", container_name, "sh", "-c",
         f"tail -c 4096 '{session_path}'"],
        timeout=5,
    )
    if rc != 0:
        return None

    for line in reversed(stdout.strip().split("\n")):
        line = line.strip()
        if not line:
            continue
        parsed = CLAUDE_HARNESS.parse_line(line)
        if parsed is None:
            continue
        entries = parsed if isinstance(parsed, list) else [parsed]
        for e in reversed(entries):
            if e.get("type") == "assistant_text":
                return {
                    "text": e["content"][:100],
                    "timestamp": e.get("timestamp", ""),
                    "type": "assistant_text",
                    "is_live": True,
                    "file_size_bytes": file_size_bytes,
                }

    return None


async def api_dispatch_latest(request):
    """Return just the most recent entry for snippet display.

    GET /api/dispatch/latest/{run}
    Returns {text, timestamp, type, is_live, file_size_bytes}.
    file_size_bytes enables rough token estimation on the client (÷4).
    """
    run_name = request.path_params["run"]
    session_files = _find_session_files(run_name)

    if not session_files:
        # Try container fallback — use _latest_from_container to avoid
        # catting the entire session file every poll
        result = await _latest_from_container(run_name)
        if result:
            return JSONResponse(result)
        return JSONResponse({"text": "", "timestamp": "", "type": "", "is_live": False, "file_size_bytes": 0})

    session_file = session_files[-1]
    is_live = (import_time() - session_file.stat().st_mtime) < 120
    reader = session_harness.resolve_harness_for_path(session_file)

    # Read last ~4KB to find latest assistant text
    file_size = session_file.stat().st_size
    read_from = max(0, file_size - 4096)
    with open(session_file, "rb") as f:
        f.seek(read_from)
        data = f.read().decode("utf-8", errors="replace")

    # Parse lines in reverse to find latest assistant text
    for line in reversed(data.strip().split("\n")):
        line = line.strip()
        if not line:
            continue
        parsed = reader.parse_line(line)
        if parsed is None:
            continue
        if isinstance(parsed, list):
            for entry in reversed(parsed):
                if entry.get("type") == "assistant_text":
                    return JSONResponse({
                        "text": entry["content"][:100],
                        "timestamp": entry.get("timestamp", ""),
                        "type": "assistant_text",
                        "is_live": is_live,
                        "file_size_bytes": file_size,
                    })
        elif parsed.get("type") == "assistant_text":
            return JSONResponse({
                "text": parsed["content"][:100],
                "timestamp": parsed.get("timestamp", ""),
                "type": "assistant_text",
                "is_live": is_live,
                "file_size_bytes": file_size,
            })

    return JSONResponse({"text": "", "timestamp": "", "type": "", "is_live": is_live, "file_size_bytes": file_size})


# ── Session Tail & Send API ────────────────────────────────────


async def api_voiceover_ask(request):
    """Ask the spoken-first local model about one monitored session.

    This is intentionally read-only: it resolves and normalizes the same
    transcript the viewer consumes, but has no tmux or harness execution path.
    """
    from tools.dashboard import voiceover
    from tools.dashboard import feature_flags

    if not feature_flags.is_enabled("voice.voiceover_enabled"):
        return JSONResponse(
            {"ok": False, "code": "voiceover_disabled", "error": "Voiceover is disabled."},
            status_code=404,
        )

    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(
            {"ok": False, "code": "invalid_json", "error": "Request body must be JSON."},
            status_code=400,
        )
    if not isinstance(payload, dict):
        return JSONResponse(
            {"ok": False, "code": "invalid_request", "error": "Request body must be an object."},
            status_code=400,
        )

    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return JSONResponse(
            {"ok": False, "code": "missing_session", "error": "Choose a session for Voiceover."},
            status_code=400,
        )

    row = session_monitor.get_one(session_id)
    if row is None:
        owner = session_monitor._find_session_by_uuid(session_id)
        if owner:
            row = session_monitor.get_one(owner)
    session_file = session_monitor.resolve_session_file(session_id)
    if row is None or session_file is None or not session_file.exists():
        return JSONResponse(
            {"ok": False, "code": "session_not_found", "error": "Voiceover cannot read that session yet."},
            status_code=404,
        )

    resolved_id = str(row.get("tmux_name") or session_id)
    try:
        answer = await voiceover.ask_session(
            session_id=resolved_id,
            question=str(payload.get("question") or ""),
            row=row,
            path=session_file,
            history=payload.get("history"),
        )
    except voiceover.VoiceoverError as exc:
        return JSONResponse(
            {"ok": False, "code": exc.code, "error": exc.message},
            status_code=exc.status_code,
        )
    except Exception:
        logger.exception("voiceover ask failed for session=%s", resolved_id)
        return JSONResponse(
            {"ok": False, "code": "voiceover_failed", "error": "Voiceover could not answer right now."},
            status_code=500,
        )

    return JSONResponse({
        "ok": True,
        "text": answer.text,
        "model": answer.model,
        "session_id": answer.session_id,
    })


# ── Chain-aware tail machinery (auto-16g9t) ─────────────────────────────
#
# The canonical identity: entry_ref = (filename stem, line byte offset,
# sub index), total-ordered by (chain position, off, sub). The session's
# chain is the ordered `session_uuids` list; cursors are (file, offset)
# pairs on every new-mode request, so a cursor left in a rolled-over file
# keeps meaning exactly what it says.

_FORWARD_CAP_BYTES = 4 * 1024 * 1024   # per-response bound; client loops


def _session_chain_files(
    db_row: dict | None, session_file: Path,
) -> list[tuple[str, Path]]:
    """Ordered (stem, path) rollover chain, ending at the current file.

    Degrades to ``[(current stem, current file)]`` whenever the chain is
    unknowable — legacy rows with empty/sparse ``session_uuids``, stems
    whose files are gone, or an ordering that doesn't end at the linked
    file. Old sessions must keep working exactly as single-file sessions.
    """
    cur_stem = session_file.stem
    stems: list[str] = []
    if db_row is not None:
        try:
            raw = json.loads(db_row.get("session_uuids") or "[]")
            if isinstance(raw, list):
                stems = [str(s) for s in raw if s]
        except (TypeError, ValueError, json.JSONDecodeError):
            stems = []
    if not stems or stems[-1] != cur_stem:
        return [(cur_stem, session_file)]
    search_dirs = [session_file.parent]
    res_dir = (db_row or {}).get("resolution_dir")
    if res_dir:
        rp = Path(res_dir)
        if rp not in search_dirs:
            search_dirs.append(rp)
    chain: list[tuple[str, Path]] = []
    for stem in stems:
        if stem == cur_stem:
            chain.append((stem, session_file))
            continue
        for d in search_dirs:
            cand = d / f"{stem}.jsonl"
            if cand.exists():
                chain.append((stem, cand))
                break
        # A missing predecessor shortens the reachable chain; the walk
        # simply starts at the earliest file that still exists.
    return chain or [(cur_stem, session_file)]


def _renderable_count(data: bytes, *, path: Path) -> int:
    """How many non-internal entries a raw window parses to."""
    count = 0
    reader = session_harness.resolve_harness_for_path(path)
    for line, _off in session_harness.iter_jsonl_lines_with_offsets(data):
        try:
            parsed = reader.parse_line(line)
        except Exception:
            continue
        if parsed is None:
            continue
        for e in (parsed if isinstance(parsed, list) else [parsed]):
            if not (e.get("internal") or e.get("hidden")):
                count += 1
    return count


def _read_file_window_backward(
    path: Path, harness, *, need: int, before: int | None,
) -> dict | None:
    """Grow a raw-line window backward within ONE file until ``need``
    entries render or the file is exhausted (the renderable-entries rule:
    a page of unparseable lines must never come back empty while content
    exists behind it)."""
    lines_guess = max(need * 2, 16)
    prev_start: int | None = None
    while True:
        data, start, end = _read_jsonl_tail_window(path, n=lines_guess, before=before)
        if not data:
            return None
        count = _renderable_count(data, path=path)
        if count >= need or start <= 0 or start == prev_start:
            return {"start": start, "end": end, "data": data, "count": count}
        prev_start = start
        lines_guess *= 2


def _read_chain_window_backward(
    chain: list[tuple[str, Path]], harness, *, n: int,
    before_file: str | None, before_off: int | None,
) -> tuple[list[dict], dict | None, bool]:
    """Walk the chain backward collecting ~n renderable entries.

    Returns (segments ascending, older_cursor, has_more). ``has_more`` is
    False only at the true chain start. A cursor at byte 0 of file k
    continues from the end of file k-1 on the next call.
    """
    idx = len(chain) - 1
    limit_off = None
    if before_file is not None:
        for i, (stem, _p) in enumerate(chain):
            if stem == before_file:
                idx = i
                limit_off = before_off
                break
    segments: list[dict] = []
    total = 0
    while idx >= 0 and total < n:
        stem, path = chain[idx]
        seg = _read_file_window_backward(
            path, harness, need=n - total, before=limit_off,
        )
        if seg is not None:
            segments.insert(0, {"stem": stem, "path": path, **seg})
            total += seg["count"]
            if seg["start"] > 0:
                break   # window growth stopped mid-file: need satisfied
        idx -= 1
        limit_off = None
    if not segments:
        return [], None, False
    first = segments[0]
    older = {"file": first["stem"], "off": first["start"]}
    at_chain_start = (
        first["stem"] == chain[0][0] and first["start"] <= 0
    )
    return segments, older, not at_chain_start


def _last_complete_offset_in(path: Path) -> int:
    """Largest offset ending on a complete line (0 if none).

    Scans backward CHUNKWISE to the actual preceding newline — round-2
    review RB1: the old single-64KiB probe mapped any unterminated
    trailing line larger than 64KiB to "complete at physical EOF", which
    re-opened the B1 permanent-loss timeline at production line sizes
    (large tool results are normal). An absurd line costs a longer scan,
    never a wrong answer.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            pos = size
            chunk = 65536
            while pos > 0:
                read = min(chunk, pos)
                fh.seek(pos - read)
                buf = fh.read(read)
                nl = buf.rfind(b"\n")
                if nl != -1:
                    return pos - read + nl + 1
                pos -= read
            return 0
    except OSError:
        return 0


def _read_chain_forward(
    chain: list[tuple[str, Path]], *, after_file: str, after_off: int,
) -> tuple[list[dict], dict, bool]:
    """Read from (after_file, after_off) to the chain's complete-line end,
    byte-capped. A cursor in a superseded file returns the remainder of
    that file, then the successors — never a caught-up lie.

    Returns (segments ascending, cursor, has_more_forward).
    """
    idx = None
    for i, (stem, _p) in enumerate(chain):
        if stem == after_file:
            idx = i
            break
    if idx is None:
        # Unknown cursor file (pruned mid-chain, or a chain reset) —
        # self-heal by re-serving the current file from byte 0. The
        # client's tuple merge dedupes any overlap.
        idx = len(chain) - 1
        after_file = chain[idx][0]
        after_off = 0
    segments: list[dict] = []
    budget = _FORWARD_CAP_BYTES
    cursor = {"file": after_file, "off": after_off}
    has_more = False
    for i in range(idx, len(chain)):
        stem, path = chain[i]
        start = after_off if i == idx else 0
        complete = _last_complete_offset_in(path)
        if complete <= start:
            cursor = {"file": stem, "off": max(start, complete) if i == idx else complete}
            continue
        # Drain THIS file to its complete-line end before moving on — the
        # old single-read structure could leave a newline-clipped tail
        # stranded with has_more=False (review B2: 2,234 bytes silently
        # unreachable until an unrelated future wake).
        while start < complete:
            if budget <= 0:
                has_more = True
                break
            take = min(complete - start, budget)
            try:
                with open(path, "rb") as fh:
                    fh.seek(start)
                    data = fh.read(take)
            except OSError:
                break
            last_nl = data.rfind(b"\n")
            if last_nl == -1:
                # A single complete line larger than the remaining budget.
                # Read the WHOLE line regardless of the cap — progress must
                # always be possible, or the client continuation loop has
                # nothing it can ever do (review B2: over-cap line returned
                # has_more with an unmoved cursor → infinite zero-delay
                # reschedule). The bust is bounded by one line.
                line_end = _next_line_end(path, start, complete)
                if line_end <= start:
                    break
                try:
                    with open(path, "rb") as fh:
                        fh.seek(start)
                        data = fh.read(line_end - start)
                except OSError:
                    break
                end = line_end
            else:
                data = data[:last_nl + 1]
                end = start + last_nl + 1
            segments.append({
                "stem": stem, "path": path, "start": start, "end": end,
                "data": data, "count": 0,
            })
            cursor = {"file": stem, "off": end}
            budget -= len(data)
            start = end
        if has_more:
            break
    return segments, cursor, has_more


def _next_line_end(path: Path, start: int, limit: int) -> int:
    """Offset just past the first newline at/after ``start`` (≤ limit)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            pos = start
            while pos < limit:
                chunk = fh.read(min(1 << 20, limit - pos))
                if not chunk:
                    break
                nl = chunk.find(b"\n")
                if nl != -1:
                    return pos + nl + 1
                pos += len(chunk)
    except OSError:
        pass
    return limit


def _trim_entries_to_last_n(entries: list[dict], n: int) -> list[dict]:
    """Keep the last n entries, widening left so one raw line's sub-group
    is never split across the page boundary."""
    if len(entries) <= n:
        return entries
    cut = len(entries) - n
    while cut > 0:
        prev = entries[cut - 1].get("entry_ref")
        cur = entries[cut].get("entry_ref")
        if prev and cur and (prev["file"], prev["off"]) == (cur["file"], cur["off"]):
            cut -= 1
        else:
            break
    return entries[cut:]


def _reconstruct_read_state(
    chain: list[tuple[str, Path]],
    harness,
    *,
    upto_file: str,
    upto_off: int,
) -> dict:
    """Replay the chain prefix THROUGH the cursor and return the stream
    state exactly as it stood there (review B3/B4).

    A snapshot of the live monitor's state is state at EOF — enriching a
    cursor-range replay with it drops or re-labels entries a live client
    already received (completed_tools suppressing the running-progress row;
    a wrapper output line parsed without its pending call). Content
    correctness for gap fills requires state AT the cursor, so we parse the
    prefix (discarding its entries) to rebuild: parse ctx, postprocess
    state, the Task* tracker, the queued-dedup seed, and Agent
    descriptions. Cost is one prefix parse per gap fetch — the happy path
    (caught up, no entries) never gets here. Built as a standalone helper
    so it can be reused for reverse windows if that ruling comes.
    """
    parse_ctx: dict = {}
    pp_state = harness.new_postprocess_state()
    tracker = TaskStateTracker()
    last_enqueue: str | None = None

    # Round-2 review RB2: the claim ALLOCATION is part of the stream state.
    # Collecting descriptions alone left claimed_subagents empty, so a
    # replay re-claimed the first matching subagent for a repeated
    # description and emitted the wrong tool_calls at the same canonical
    # ref. Replay the SAME enrichment function over the prefix so both the
    # descriptions and the claim set stand exactly as they did at the
    # cursor (the prefix entries themselves are discarded).
    class _ReconTs:
        agent_descriptions: dict[str, str] = {}
        claimed_subagents: set[str] = set()
    recon_ts = _ReconTs()
    recon_ts.agent_descriptions = {}
    recon_ts.claimed_subagents = set()

    for stem, path in chain:
        try:
            complete = _last_complete_offset_in(path)
        except OSError:
            continue
        limit = min(upto_off, complete) if stem == upto_file else complete
        if limit > 0:
            try:
                with open(path, "rb") as fh:
                    data = fh.read(limit)
            except OSError:
                data = b""
            reader = session_harness.resolve_harness_for_path(
                path, ctx=parse_ctx,
            )
            prefix = reader.parse_bytes_with_refs(
                data, stem=stem, base_offset=0,
            )
            prefix, last_enqueue, _ = session_monitor_mod.dedup_queued_entries(
                prefix, last_enqueue,
            )
            out = harness.postprocess_entries(
                prefix, session_dir=path.parent / path.stem, state=pp_state,
            )
            try:
                session_monitor_mod.SessionMonitor._enrich_agent_entries(
                    {"jsonl_path": str(path)}, recon_ts, out,
                )
            except Exception:
                logger.exception("tail: prefix agent-claim replay failed")
            tracker.enrich("_reconstruct", out)
        if stem == upto_file:
            break
    return {
        "parse_ctx": parse_ctx,
        "postprocess_state": pp_state,
        "tracker": tracker,
        "last_enqueue_content": last_enqueue,
        "agent_descriptions": recon_ts.agent_descriptions,
        "claimed_subagents": recon_ts.claimed_subagents,
    }


def _parse_and_enrich_segments(
    segments: list[dict],
    harness,
    db_row: dict | None,
    tmux_name: str,
    *,
    trim_to: int | None = None,
    reconstruct_from: tuple[list[tuple[str, Path]], str, int] | None = None,
) -> tuple[list[dict], list[dict], dict | None]:
    """Parse + postprocess + enrich chain segments the same way the live
    stream does.

    Two state modes (review B3/B4, coordinator-approved split):
    - ``reconstruct_from=(chain, file, off)`` — forward gap fills into the
      committed buffer: stream state is REBUILT through the cursor so the
      replay is semantically identical to what the live stream delivered.
    - otherwise (reverse/scroll-back pages) — a read-only SNAPSHOT of the
      live state; display-fidelity only, guarded client-side by the
      field-level richness preferences in mergeSessionEntries and counted
      via the merge_downgrades_blocked diag counter.

    Returns (entries, spans, older_cursor_adjustment) where the last item
    is the (file, off) of the first kept entry after trimming (None when
    nothing was trimmed).
    """
    recon = None
    snap = None
    if reconstruct_from is not None:
        chain, upto_file, upto_off = reconstruct_from
        recon = _reconstruct_read_state(
            chain, harness, upto_file=upto_file, upto_off=upto_off,
        )
    else:
        snap = session_monitor.snapshot_read_context(tmux_name) if tmux_name else None

    parse_ctx: dict = (recon or {}).get("parse_ctx") or {}
    entries: list[dict] = []
    per_file_end: dict[str, int] = {}
    for seg in segments:
        reader = session_harness.resolve_harness_for_path(
            seg["path"], ctx=parse_ctx,
        )
        entries.extend(reader.parse_bytes_with_refs(
            seg["data"], stem=seg["stem"], base_offset=seg["start"],
        ))
        per_file_end[seg["stem"]] = seg["end"]

    trimmed_from: dict | None = None
    if trim_to is not None and len(entries) > trim_to:
        entries = _trim_entries_to_last_n(entries, trim_to)
        first_ref = entries[0].get("entry_ref") if entries else None
        if first_ref:
            trimmed_from = {"file": first_ref["file"], "off": first_ref["off"]}

    # Queued-message dedup, mirroring the live tailer. Forward deltas seed
    # from the reconstructed cursor state; backward windows start
    # mid-history and seed empty.
    seed = (recon or {}).get("last_enqueue_content")
    entries, _last, _dropped = session_monitor_mod.dedup_queued_entries(entries, seed)

    # Postprocess per file (session_dir differs per file for Claude's
    # subagent enrichment) with ONE stream state ascending: the
    # cursor-reconstructed state on forward reads, a snapshot copy on
    # reverse reads.
    pp_state = (recon or {}).get("postprocess_state")
    if pp_state is None:
        pp_state = (snap or {}).get("postprocess_state")
    if pp_state is None:
        pp_state = harness.new_postprocess_state()
    out: list[dict] = []
    file_order: list[str] = []
    by_file: dict[str, list[dict]] = {}
    for e in entries:
        ref = e.get("entry_ref") or {}
        stem = ref.get("file", "")
        if stem not in by_file:
            by_file[stem] = []
            file_order.append(stem)
        by_file[stem].append(e)
    # One postprocess per FILE, in ascending order — a capped forward read
    # can produce several segments of the same file, and iterating
    # segments here postprocessed (and served) each file's entries once
    # per segment (round-1 review fix pass: duplicated every entry).
    seg_paths: dict[str, Path] = {}
    for seg in segments:
        seg_paths.setdefault(seg["stem"], seg["path"])
    for stem in file_order:
        path = seg_paths.get(stem)
        out.extend(harness.postprocess_entries(
            by_file[stem],
            session_dir=(path.parent / path.stem) if path is not None else None,
            state=pp_state,
        ))
    session_harness.finalize_entry_refs(out)

    # Task* tile annotations: the cursor-reconstructed tracker on forward
    # reads (annotations exactly as they stood at the cursor); a fork of
    # the LIVE tracker on reverse reads (fast-open windows used to enrich
    # against nothing); a fresh tracker for dead sessions.
    if recon is not None:
        recon["tracker"].enrich("_reconstruct", out)
    else:
        key = tmux_name or "_http"
        _task_state_tracker.fork_session(key).enrich(key, out)

    # Agent tool_calls enrichment against reconstructed/snapshot copies
    # (never the live descriptions/claims — reads must not consume live
    # matching state).
    agent_state = None
    if recon is not None:
        agent_state = (recon["agent_descriptions"], recon["claimed_subagents"])
    elif snap is not None:
        agent_state = (snap["agent_descriptions"], snap["claimed_subagents"])
    if agent_state is not None and db_row is not None:
        class _TsView:
            agent_descriptions = agent_state[0]
            claimed_subagents = agent_state[1]
        try:
            session_monitor_mod.SessionMonitor._enrich_agent_entries(
                db_row, _TsView, out,
            )
        except Exception:
            logger.exception("tail: agent enrichment failed for %s", tmux_name)

    spans: list[dict] = []
    seen_files = {(e.get("entry_ref") or {}).get("file") for e in out}
    for seg in segments:
        if seg["stem"] not in seen_files and trimmed_from is not None:
            continue   # fully trimmed away
        frm = seg["start"]
        if trimmed_from is not None and seg["stem"] == trimmed_from["file"]:
            frm = trimmed_from["off"]
        spans.append({"file": seg["stem"], "from": frm, "to": seg["end"]})
    return out, spans, trimmed_from


async def api_session_tail(request):
    """Tail JSONL entries for any session by project/session_id.

    GET /api/session/{project}/{session_id}/tail?after=N
    Returns {entries: [...], offset: N, is_live: bool}.
    Increment `after` with each poll to receive only new entries.
    """
    project = request.path_params["project"]
    session_id = request.path_params["session_id"]
    tail_lines_raw = request.query_params.get("tail_lines")
    tail_entries_raw = request.query_params.get("tail_entries")
    before_raw = request.query_params.get("before")
    before_file = request.query_params.get("before_file")
    after_file = request.query_params.get("after_file")
    # Four request modes. The two legacy modes keep byte-for-byte legacy
    # behavior (already-open tabs poll them until well after client
    # uptake); the two chain modes carry (file, offset) pair cursors.
    #   chain_reverse : ?tail_entries=N [&before_file=STEM&before=OFF]
    #   chain_forward : ?after_file=STEM&after=OFF
    #   legacy reverse: ?tail_lines=N [&before=OFF]
    #   legacy forward: ?after=N (default 0)
    chain_reverse = tail_entries_raw is not None
    chain_forward = after_file is not None and not chain_reverse
    reverse_window = chain_reverse or tail_lines_raw is not None
    tail_lines = 0
    before = None
    after = 0
    if chain_reverse:
        try:
            tail_lines = int(tail_entries_raw or "0")
        except ValueError:
            return JSONResponse({"error": "invalid tail_entries"}, status_code=400)
        if tail_lines <= 0:
            return JSONResponse({"error": "tail_entries must be >= 1"}, status_code=400)
        try:
            before = int(before_raw) if before_raw is not None else None
        except ValueError:
            return JSONResponse({"error": "invalid before"}, status_code=400)
    elif tail_lines_raw is not None:
        try:
            tail_lines = int(tail_lines_raw or "0")
        except ValueError:
            return JSONResponse({"error": "invalid tail_lines"}, status_code=400)
        if tail_lines <= 0:
            return JSONResponse({"error": "tail_lines must be >= 1"}, status_code=400)
        try:
            before = int(before_raw) if before_raw is not None else None
        except ValueError:
            return JSONResponse({"error": "invalid before"}, status_code=400)
    else:
        if before_raw is not None and not chain_forward:
            return JSONResponse(
                {"error": "before requires tail_lines"},
                status_code=400,
            )
        try:
            after = int(request.query_params.get("after", "0"))
        except ValueError:
            return JSONResponse({"error": "invalid after"}, status_code=400)

    # Mock mode: return fixture entries if available
    if os.environ.get("DASHBOARD_MOCK"):
        entries = dao_sessions.get_session_entries(session_id)
        if entries is not None:
            # Look up session metadata from mock DAO for is_live and type.
            # Use get_session_by_id so dispatch/librarian rows (filtered out
            # of get_active_sessions) still resolve — otherwise is_live
            # defaults to True and dead-dispatch viewers render as live.
            mock_session = dao_sessions.get_session_by_id(session_id) or {}
            TaskStateTracker().enrich(session_id, entries)
            # Synthetic identity for fixture entries: one pseudo-file
            # "mock", off = fixture index. Keeps the client's tuple merge
            # (and the behavioral sweep driving it) working against mock.
            for i, e in enumerate(entries):
                if isinstance(e, dict):
                    e.setdefault("entry_ref", {"file": "mock", "off": i, "sub": 0})
            if reverse_window:
                end_idx = len(entries) if before is None else max(0, min(before, len(entries)))
                start_idx = max(0, end_idx - tail_lines)
                chunk_entries = entries[start_idx:end_idx]
            elif chain_forward:
                start_idx = max(0, min(after, len(entries)))
                chunk_entries = entries[start_idx:]
            else:
                start_idx = 0
                chunk_entries = entries
            resp = {
                "entries": chunk_entries, "offset": len(entries),
                "is_live": bool(mock_session.get("is_live", True)),
                "type": mock_session.get("type", ""),
                "role": mock_session.get("role", ""),
                "tmux_session": mock_session.get("tmux_session", session_id),
                "seq": len(entries),
                "resolved": bool(mock_session.get("resolved", True)),
                "chain": ["mock"],
            }
            if reverse_window:
                resp["older_before"] = start_idx
                resp["has_more"] = start_idx > 0
                resp["older_cursor"] = {"file": "mock", "off": start_idx}
                end_idx_val = end_idx if before is not None else len(entries)
                resp["window_spans"] = [
                    {"file": "mock", "from": start_idx, "to": end_idx_val}]
            if chain_forward or not reverse_window:
                resp["cursor"] = {"file": "mock", "off": len(entries)}
            return JSONResponse(resp)

    # First, try resolving via DB (session_id may be a tmux_name,
    # a dispatch session_uuid, a UUID in session_uuids, or a run_dir id)
    session_file = None
    db_row = session_monitor.get_one(session_id)
    if db_row is None:
        owner = session_monitor._find_session_by_uuid(session_id)
        if owner is None:
            # Also try the dead/historical rows where session_uuid matches
            from tools.dashboard.dao.dashboard_db import get_conn as _get_conn
            try:
                conn = _get_conn()
                row = conn.execute(
                    "SELECT tmux_name FROM tmux_sessions"
                    " WHERE session_uuid=? OR session_uuids LIKE ?"
                    " ORDER BY created_at DESC LIMIT 1",
                    (session_id, f"%{session_id}%"),
                ).fetchone()
                if row:
                    owner = row[0] if not hasattr(row, "keys") else row["tmux_name"]
            except Exception:
                owner = None
        if owner:
            db_row = session_monitor.get_one(owner)

    # Cross-org guard BEFORE any jsonl path/fallback resolution, stat, or read
    # (auto-49esb). Attribute the resolved session to its org via the canonical
    # dashboard_db row and refuse a cross-org (or unresolvable) session as the
    # SAME 404 an unknown session returns, so existence never leaks. An
    # org-stamped caller therefore never triggers the fallback tree scan for a
    # session outside its org.
    org_row = dashboard_db.get_session(db_row["tmux_name"]) if db_row else None
    if _session_hidden_cross_org(request, org_row):
        return JSONResponse(
            {"error": "Session not found", "session_id": session_id},
            status_code=404,
        )

    if db_row and db_row.get("jsonl_path"):
        candidate = Path(db_row["jsonl_path"])
        if candidate.exists():
            session_file = candidate

    # Last-resort: scan data/agent-runs/ for a JSONL matching the id
    if session_file is None:
        fallback = session_monitor.resolve_session_file(session_id)
        if fallback is not None:
            session_file = fallback
            # Persist the resolved path so subsequent SSE tails don't pay the
            # full-tree scan cost and the tailer picks this session up on
            # the next inotify tick. Only makes sense when we have a DB row
            # to associate the path with — raw UUID lookups without a row
            # have nothing to update.
            if db_row is not None and not db_row.get("jsonl_path"):
                try:
                    session_monitor._handle_jsonl_appeared(
                        db_row["tmux_name"], fallback, source="tail_endpoint",
                    )
                    # Refresh db_row for the response so `resolved=True`
                    # reflects the freshly-persisted state.
                    refreshed = session_monitor.get_one(db_row["tmux_name"])
                    if refreshed is not None:
                        db_row = refreshed
                except Exception:
                    # Never let persistence failures break the tail response.
                    logger.exception(
                        "api_session_tail: failed to persist fallback jsonl_path for %s",
                        db_row.get("tmux_name"),
                    )

    if session_file is None:
        # Session file not resolved — check if it's a newly created session
        # registered in the monitor but with no JSONL yet
        if db_row and derive_lifecycle_state(db_row) not in ("ENDED", "FAILED"):
            return JSONResponse({
                "entries": [], "offset": 0, "is_live": True,
                "type": db_row.get("type", ""),
                "role": db_row.get("role", ""),
                "session_id": db_row.get("tmux_name", ""),
                "tmux_session": db_row.get("tmux_name", ""),
                "tmux_name": db_row.get("tmux_name", ""),
                "session_uuid": db_row.get("session_uuid", ""),
                "seq": 0, "resolved": False,
            })
        return JSONResponse(
            {"error": "Session not found", "session_id": session_id},
            status_code=404,
        )
    if not session_file.exists():
        if db_row and derive_lifecycle_state(db_row) not in ("ENDED", "FAILED"):
            return JSONResponse({
                "entries": [], "offset": 0, "is_live": True,
                "type": db_row.get("type", ""),
                "role": db_row.get("role", ""),
                "session_id": db_row.get("tmux_name", ""),
                "tmux_session": db_row.get("tmux_name", ""),
                "tmux_name": db_row.get("tmux_name", ""),
                "session_uuid": db_row.get("session_uuid", ""),
                "seq": 0, "resolved": False,
            })
        return JSONResponse(
            {"error": "Session not found", "session_id": session_id},
            status_code=404,
        )

    file_size = session_file.stat().st_size
    # Determine session type from resolved path
    home_projects = (
        Path(os.environ.get("AUTONOMY_HOST_HOME") or Path.home())
        / ".claude" / "projects"
    )
    session_type = "host" if session_file.is_relative_to(home_projects) else "container"

    # Liveness from DB — the one state column decides
    is_live = (
        derive_lifecycle_state(db_row) not in ("ENDED", "FAILED")
        if db_row else False
    )
    tmux_name = db_row.get("tmux_name", "") if db_row else ""
    # Resolved: session has a linked JSONL path or non-empty session_uuids
    resolved = bool(db_row.get("jsonl_path")) or (
        bool(db_row.get("session_uuids")) and db_row["session_uuids"] != "[]"
    ) if db_row else False

    role = db_row.get("role", "") if db_row else ""
    activity_state = db_row.get("activity_state", "idle") if db_row else "idle"
    session_uuid = db_row.get("session_uuid", "") if db_row else ""
    ts_obj = session_monitor._tail_states.get(tmux_name) if tmux_name else None
    pending_tool_ids = sorted(ts_obj.pending_tool_ids) if ts_obj else []
    harness = resolve_harness_for_session_row(db_row)
    chain = _session_chain_files(db_row, session_file)
    chain_stems = [stem for stem, _p in chain]
    # NOTE (auto-16g9t): the fake `seq: 0` is gone from every real-file
    # response — sequence numbers exist only on the SSE transport.
    base_resp = {"entries": [], "offset": file_size, "is_live": is_live,
                 "type": session_type, "role": role,
                 "activity_state": activity_state,
                 "pending_tool_ids": pending_tool_ids,
                 "resolved": resolved, "chain": chain_stems}
    if tmux_name:
        base_resp["session_id"] = tmux_name
        base_resp["tmux_session"] = tmux_name
        base_resp["tmux_name"] = tmux_name
    if session_uuid:
        base_resp["session_uuid"] = session_uuid

    def _finish(resp: dict) -> JSONResponse:
        # Stamp trusted session identity onto entry types whose serve URLs
        # depend on it — mirror of the live-tail loop stamping (the HTTP
        # tail bypasses the monitor entirely).
        if tmux_name:
            for entry in resp.get("entries", []):
                if entry.get("type") == "viewer_attachment":
                    entry["session"] = tmux_name
        return JSONResponse(resp)

    # ── Chain modes: (file, offset) pair cursors over the rollover chain ──
    if chain_reverse:
        segments, older_cursor, has_more = _read_chain_window_backward(
            chain, harness, n=tail_lines,
            before_file=before_file, before_off=before,
        )
        entries, spans, trimmed_from = _parse_and_enrich_segments(
            segments, harness, db_row, tmux_name, trim_to=tail_lines,
        )
        if trimmed_from is not None:
            older_cursor = trimmed_from
            has_more = not (
                trimmed_from["file"] == chain_stems[0]
                and trimmed_from["off"] <= 0
            )
        resp = dict(base_resp)
        resp.update({
            "entries": entries,
            "window_spans": spans,
            "older_cursor": older_cursor,
            "has_more": has_more,
        })
        return _finish(resp)

    if chain_forward:
        cur_stem = chain_stems[-1]
        cur_complete = _last_complete_offset_in(chain[-1][1])
        if after_file == cur_stem and after >= cur_complete:
            # Happy-path caught-up: a few hundred bytes, no entries.
            resp = dict(base_resp)
            resp.update({
                "cursor": {"file": cur_stem, "off": cur_complete},
                "has_more_forward": False,
            })
            return _finish(resp)
        segments, cursor, has_more_fwd = _read_chain_forward(
            chain, after_file=after_file, after_off=after,
        )
        # Reconstruction anchor = the position the forward read actually
        # started from (mirrors _read_chain_forward's unknown-stem
        # degrade: current file from 0 — replaying past the served window
        # would poison the state with the window's own content).
        if after_file in chain_stems:
            recon_anchor = (after_file, after)
        else:
            recon_anchor = (chain_stems[-1], 0)
        entries, spans, _trimmed = _parse_and_enrich_segments(
            segments, harness, db_row, tmux_name,
            reconstruct_from=(chain, recon_anchor[0], recon_anchor[1]),
        )
        resp = dict(base_resp)
        resp.update({
            "entries": entries,
            "window_spans": spans,
            "cursor": cursor,
            "has_more_forward": has_more_fwd,
        })
        return _finish(resp)

    # ── Legacy modes: unchanged single-file behavior for old clients ──
    if not reverse_window and after >= file_size:
        return JSONResponse(base_resp)

    if reverse_window:
        data, window_start, window_end = _read_jsonl_tail_window(
            session_file,
            n=tail_lines,
            before=before,
        )
        new_offset = file_size
        reader = session_harness.resolve_harness_for_path(
            session_file,
        )
        entries = reader.parse_bytes_with_refs(
            data, base_offset=window_start,
        )
    else:
        with open(session_file, "rb") as f:
            f.seek(after)
            data = f.read()
        # Never advance the cursor past a partial trailing line — the old
        # code did, silently losing whatever the writer was mid-writing.
        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            data = b""
            new_offset = after
        else:
            data = data[:last_nl + 1]
            new_offset = after + last_nl + 1
        reader = session_harness.resolve_harness_for_path(
            session_file,
        )
        entries = reader.parse_bytes_with_refs(data, base_offset=after)

    entries = harness.postprocess_entries(
        entries,
        session_dir=session_file.parent / session_file.stem,
    )
    session_harness.finalize_entry_refs(entries)
    # Task* tile annotations need full-history context to resolve taskId→subject.
    # Partial forward polls (after>0) miss earlier TaskCreates, so replay from
    # offset 0. Reverse-window fast-open intentionally skips that full-history
    # pass to keep the initial payload cheap; older pages fill in context as the
    # operator scrolls back.
    if not reverse_window and after > 0:
        history: list = []
        history_reader = session_harness.resolve_harness_for_path(
            session_file,
        )
        with open(session_file, "rb") as f:
            history_data = f.read(after)
        for line in history_data.decode("utf-8", errors="replace").split("\n"):
            line = line.strip()
            if not line:
                continue
            parsed = history_reader.parse_line(line)
            if parsed is None:
                continue
            if isinstance(parsed, list):
                history.extend(parsed)
            else:
                history.append(parsed)
        local_tracker = TaskStateTracker()
        local_tracker.enrich("_http", history)
        local_tracker.enrich("_http", entries)
    else:
        TaskStateTracker().enrich("_http", entries)
    resp = dict(base_resp)
    resp.update({"entries": entries, "offset": new_offset})
    if reverse_window:
        resp["older_before"] = window_start
        resp["has_more"] = window_start > 0
    return _finish(resp)


async def api_session_send(request):
    """Send a message to a tmux-managed session via paste-buffer injection.

    POST /api/session/send
    POST /api/session/{project}/{session_id}/send  (project/session_id ignored)
    Body: {"tmux_session": "auto-t2", "message": "text", "client_id": "..." (optional)}

    Only works for tmux-managed sessions (terminal, chatwith, dispatch agents).
    Host interactive sessions have no stdin injection path — returns 404.
    Returns 400 if tmux_session or message is not provided.
    Returns 404 if the tmux session does not exist.
    Returns 503 if tmux is not available in this environment.

    auto-rsvzk: when ``client_id`` is supplied, the message is stashed in
    the per-session ``pending_outbound`` ring before tmux_send fires.
    The inotify tailer matches the echo back to the client_id and
    attaches it to the broadcast ``session:messages`` payload so the
    frontend can promote its locally-rendered "sending" entry to
    "confirmed" without appending a duplicate row.

    Retries posting the same ``client_id`` short-circuit with
    ``{status: "in_flight"}`` and do NOT re-paste into tmux — that's
    the gate that makes the optimistic-outbound retry path idempotent.
    """
    body = await request.json()
    message = (body.get("message") or "")
    tmux_session = (body.get("tmux_session") or "").strip()
    client_id = (body.get("client_id") or "").strip()

    if not tmux_session:
        return JSONResponse(
            {"error": "tmux_session is required. "
                       "Host interactive sessions have no stdin injection path."},
            status_code=400,
        )
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)

    try:
        exists = _tmux_session_exists(tmux_session)
    except FileNotFoundError:
        return JSONResponse(
            {"error": "tmux is not available in this environment"},
            status_code=503,
        )

    if not exists:
        return JSONResponse(
            {"error": f"tmux session '{tmux_session}' not found"},
            status_code=404,
        )

    # auto-rsvzk: idempotent retry — if this client_id is already
    # pending an echo, do not re-paste into the harness. The original
    # send's echo will land on session:messages with this client_id
    # attached, promoting the frontend's "sending" entry to "confirmed"
    # without a duplicate row.
    if client_id:
        from tools.dashboard import pending_outbound
        if pending_outbound.is_in_flight(tmux_session, client_id):
            logger.info(
                "[session-send] dedup in-flight  tmux=%s  client_id=%s",
                tmux_session, client_id,
            )
            return JSONResponse({
                "ok": True,
                "tmux_session": tmux_session,
                "status": "in_flight",
            })
        pending_outbound.record_send(tmux_session, client_id, message)

    # Inject via unified tmux_send (per-session lock + double-Enter retry)
    logger.warning("[session-send] tmux=%r message=%r", tmux_session, message)
    try:
        await tmux_send(tmux_session, message)
    except FileNotFoundError:
        return JSONResponse(
            {"error": "tmux is not available in this environment"},
            status_code=503,
        )

    # This endpoint is the authoritative boundary for direct operator input.
    # Persist it only after tmux accepted the paste; assistant/tool/CrossTalk
    # traffic reaches sessions through other paths and therefore cannot move
    # the Recent Input ordering.
    last_input_at = time.time()
    if not os.environ.get("DASHBOARD_MOCK"):
        try:
            dashboard_db.update_last_input_at(tmux_session, last_input_at)
        except Exception:
            # tmux already accepted the paste, so returning an error here could
            # make the client retry and duplicate the operator's message.
            logger.exception(
                "[session-send] failed to persist last_input_at for %s",
                tmux_session,
            )

    resp: dict[str, Any] = {
        "ok": True,
        "tmux_session": tmux_session,
        "last_input_at": last_input_at,
    }
    if client_id:
        resp["client_id"] = client_id
    return JSONResponse(resp)


_SESSION_NOTIFICATION_IDS: dict[tuple[str, str], float] = {}
_SESSION_NOTIFICATION_MAX = 4096


async def api_session_notify(request):
    """Deliver one idempotent, harness-neutral system notification.

    The task-notification envelope is already understood by both Claude and
    Codex transcript adapters. Unlike CrossTalk or ordinary session input, it
    is normalized as ``type=system`` and therefore does not update operator
    activity. Callers provide a stable notification id so retries cannot wake
    the agent twice during one dashboard process lifetime.
    """
    body = await request.json()
    tmux_session = str(body.get("tmux_session") or "").strip()
    notification_id = str(body.get("notification_id") or "").strip()
    kind = str(body.get("kind") or "system").strip()
    status = str(body.get("status") or "complete").strip()
    summary = str(body.get("summary") or "").strip()
    detail = str(body.get("body") or "").strip()
    if not tmux_session or not notification_id or not summary:
        return JSONResponse(
            {"error": "tmux_session, notification_id, and summary are required"},
            status_code=400,
        )
    identifier = re.compile(r"^[A-Za-z0-9:._-]{1,200}$")
    if not identifier.fullmatch(notification_id) or not identifier.fullmatch(kind):
        return JSONResponse({"error": "invalid notification_id or kind"}, status_code=400)
    if len(summary) > 2000 or len(detail) > 4000:
        return JSONResponse({"error": "notification content exceeds its bounded limit"}, status_code=400)
    try:
        exists = _tmux_session_exists(tmux_session)
    except FileNotFoundError:
        return JSONResponse({"error": "tmux is not available in this environment"}, status_code=503)
    if not exists:
        return JSONResponse({"error": f"tmux session '{tmux_session}' not found"}, status_code=404)

    key = (tmux_session, notification_id)
    if key in _SESSION_NOTIFICATION_IDS:
        return JSONResponse({"ok": True, "status": "duplicate", "notification_id": notification_id})
    from tools.dashboard.session_notify import build_task_notification_envelope
    envelope = build_task_notification_envelope(
        notification_id, kind=kind, status=status, summary=summary, body=detail,
    )
    await tmux_send(tmux_session, envelope)
    _SESSION_NOTIFICATION_IDS[key] = time.time()
    if len(_SESSION_NOTIFICATION_IDS) > _SESSION_NOTIFICATION_MAX:
        oldest = sorted(_SESSION_NOTIFICATION_IDS, key=_SESSION_NOTIFICATION_IDS.get)
        for stale in oldest[: len(_SESSION_NOTIFICATION_IDS) - _SESSION_NOTIFICATION_MAX]:
            _SESSION_NOTIFICATION_IDS.pop(stale, None)
    return JSONResponse({"ok": True, "status": "accepted", "notification_id": notification_id})


async def api_agent_test_leases(request):
    """Acquire, renew, release, or inspect machine-wide Agent Test slots."""
    auth_error = api_auth.require_authenticated_api_caller(request)
    if auth_error is not None:
        return auth_error
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "JSON object required"}, status_code=400)
    body = dict(body)
    organization = api_auth.organization_scope_from_request(request)
    if organization:
        # Scope comes from the authenticated request, never from client JSON.
        body["organization"] = organization
    action = str(body.get("action") or "status").strip()
    result = await asyncio.to_thread(_agent_test_leases.transact, action, body)
    status_code = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status_code)


async def api_session_interrupt(request):
    """Send Escape key to a tmux session to interrupt a running tool.

    POST /api/session/{tmux_name}/interrupt
    Returns: {"ok": true}
    """
    tmux_name = request.path_params["tmux_name"]
    try:
        exists = _tmux_session_exists(tmux_name)
    except FileNotFoundError:
        return JSONResponse(
            {"error": "tmux is not available in this environment"},
            status_code=503,
        )
    if not exists:
        return JSONResponse(
            {"error": f"tmux session '{tmux_name}' not found"},
            status_code=404,
        )
    subprocess.run(
        ["tmux", "send-keys", "-t", tmux_name, "Escape"],
        capture_output=True,
    )
    logger.warning("[session-interrupt] tmux=%r", tmux_name)
    return JSONResponse({"ok": True})


async def api_session_background(request):
    """Send Ctrl-B to a tmux session to background a running task.

    Claude harness reads Ctrl-B as "background the running tool" rather than
    cancelling it (which is what Escape does via api_session_interrupt).

    POST /api/session/{tmux_name}/background
    Returns: {"ok": true}
    """
    tmux_name = request.path_params["tmux_name"]
    try:
        exists = _tmux_session_exists(tmux_name)
    except FileNotFoundError:
        return JSONResponse(
            {"error": "tmux is not available in this environment"},
            status_code=503,
        )
    if not exists:
        return JSONResponse(
            {"error": f"tmux session '{tmux_name}' not found"},
            status_code=404,
        )
    subprocess.run(
        ["tmux", "send-keys", "-t", tmux_name, "C-b"],
        capture_output=True,
    )
    logger.warning("[session-background] tmux=%r", tmux_name)
    return JSONResponse({"ok": True})


async def api_terminal_unclaimed(request):
    """Return unclaimed host tmux sessions — those with no jsonl_path yet.

    GET /api/terminal/unclaimed
    Returns live host sessions from dashboard.db that don't yet have a JSONL link.
    """
    sessions = dashboard_db.get_live_sessions()
    now = time.time()
    result = []
    for row in sessions:
        if row["type"] != "host":
            continue
        if row.get("jsonl_path"):
            continue  # already linked
        # Verify still alive
        alive = _tmux_session_exists(row["tmux_name"])
        if not alive:
            continue
        elapsed = int(now - row["created_at"])
        result.append({
            "tmux_session": row["tmux_name"],
            "elapsed_seconds": elapsed,
            "cmd": "",
        })
    return JSONResponse(result)


async def api_session_send_handshake(request):
    """Send a handshake string to a candidate tmux session for link confirmation.

    POST /api/session/send-handshake
    Body: {"tmux_session": "auto-t6"}
    Returns: {"ok": true, "handshake": "<the string sent>"}
    """
    body = await request.json()
    tmux_session = (body.get("tmux_session") or "").strip()
    if not tmux_session:
        return JSONResponse({"error": "tmux_session is required"}, status_code=400)

    handshake = "[dashboard] confirming terminal link \u2014 please reply with I SEE IT"

    try:
        exists = _tmux_session_exists(tmux_session)
    except FileNotFoundError:
        return JSONResponse(
            {"error": "tmux is not available in this environment"},
            status_code=503,
        )
    if not exists:
        return JSONResponse(
            {"error": f"tmux session '{tmux_session}' not found"},
            status_code=404,
        )

    # Inject via unified tmux_send (per-session lock + double-Enter retry)
    logger.warning("[send-handshake] tmux=%r", tmux_session)
    try:
        await tmux_send(tmux_session, handshake)
    except FileNotFoundError:
        return JSONResponse(
            {"error": "tmux is not available in this environment"},
            status_code=503,
        )

    return JSONResponse({"ok": True, "handshake": handshake})


async def api_session_confirm_link(request):
    """Confirm a terminal link after handshake — scans filesystem for JSONL.

    POST /api/session/confirm-link
    Body: {"tmux_session": "auto-t6", "handshake": "[dashboard] confirming..."}
    Returns: {"ok": true, "project": "...", "session_id": "..."}

    Scans ~/.claude/projects/ for the newest JSONL files containing the
    handshake text.  No SSE/store dependency — solves the chicken-and-egg
    problem where entries are empty because jsonl_path is NULL.
    """
    body = await request.json()
    tmux_session = (body.get("tmux_session") or "").strip()
    handshake_text = (body.get("handshake") or "").strip()

    if not tmux_session:
        return JSONResponse({"error": "tmux_session required"}, status_code=400)

    # Scan all project directories for newest JSONL containing handshake
    claude_projects = (
        Path(os.environ.get("AUTONOMY_HOST_HOME") or Path.home())
        / ".claude" / "projects"
    )
    if not claude_projects.exists():
        return JSONResponse({"error": "no projects directory"}, status_code=404)

    # Collect all JSONL files across all projects, sorted by mtime descending
    all_jsonls = []
    for project_dir in claude_projects.iterdir():
        if not project_dir.is_dir():
            continue
        for jf in project_dir.glob("*.jsonl"):
            all_jsonls.append(jf)

    all_jsonls.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    # Check newest files first — read last 5 entries for handshake text
    for jf in all_jsonls[:5]:  # only check 5 newest files
        try:
            lines = jf.read_text(encoding="utf-8", errors="replace").strip().split("\n")
            tail = lines[-5:] if len(lines) > 5 else lines
            for line in tail:
                if handshake_text and handshake_text in line:
                    # Found it — this is our file
                    project = jf.parent.name
                    session_id = jf.stem
                    logger.info("confirm-link: FOUND handshake in %s/%s", project, session_id[:12])
                    dashboard_db.link_and_enrich(
                        tmux_session,
                        session_uuid=session_id,
                        jsonl_path=str(jf),
                        project=project,
                    )
                    # Install inotify watches so the tailer starts broadcasting
                    # session:messages as new entries arrive. Without this, the
                    # link is persisted in the DB but no live SSE flows —
                    # the viewer only sees new content on /tail?after=0 fetches
                    # (nav or force refresh). See signpost d931649b-413 §2/§7c.
                    from tools.dashboard.session_monitor import _TailState
                    if tmux_session not in session_monitor._tail_states:
                        session_monitor._tail_states[tmux_session] = _TailState(
                            resolution_dir=jf.parent,
                        )
                    session_monitor._add_file_watch(tmux_session, str(jf))
                    session_monitor._add_dir_watch(tmux_session, str(jf.parent))
                    # auto-suvcp: persisted re-attach + catch-up drain, so the
                    # handshake transcript's existing bytes become visible
                    # without waiting for the next write. The registry
                    # publishes AFTER that drain (invariant 9) — no direct
                    # broadcast here, or the card durably shows
                    # resolved=true with zero entries (R3).
                    session_monitor.observe_rollout(
                        tmux_session, jf, source="confirm_link",
                    )
                    return JSONResponse({"ok": True, "project": project, "session_id": session_id})
        except Exception:
            continue

    return JSONResponse({"error": "handshake not found in any recent JSONL"}, status_code=404)


async def api_session_get(request):
    """GET /api/session/{tmux_name} — return session details."""
    tmux_name = request.path_params["tmux_name"]

    # Mock mode: synthesize the detail payload from the fixture. The viewer
    # fetches this endpoint before backfilling messages, so without a mock
    # branch every DASHBOARD_MOCK session viewer 404s here and renders empty.
    if os.environ.get("DASHBOARD_MOCK"):
        mock_session = dao_sessions.get_session_by_id(tmux_name)
        if not mock_session:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({
            "session_id": tmux_name,
            "pending_approval": None,
            "session_uuid": mock_session.get("session_uuid"),
            "graph_source_id": mock_session.get("graph_source_id"),
            "file_path": "",
            "resumable": False,
            "type": mock_session.get("type", ""),
            "role": mock_session.get("role", ""),
            "activity_state": mock_session.get("activity_state", "idle"),
            "project": mock_session.get("project", ""),
            "org": mock_session.get("org", ""),
            "is_live": bool(mock_session.get("is_live", True)),
            "dispatch_nag_enabled": bool(mock_session.get("dispatch_nag")),
            "nag_enabled": bool(mock_session.get("nag_enabled")),
            "nag_interval": mock_session.get("nag_interval"),
            "nag_message": mock_session.get("nag_message"),
            "nag_last_sent": mock_session.get("nag_last_sent"),
        })

    session = dashboard_db.get_session(tmux_name)
    # Cross-org guard BEFORE reconcile/approval lookup (auto-49esb): a cross-org
    # session is refused as the same 404 a nonexistent one returns, so existence
    # never leaks. A missing session and a hidden one are one branch on purpose.
    if not session or _session_hidden_cross_org(request, session):
        return JSONResponse({"error": "not found"}, status_code=404)
    from tools.dashboard.org_identity import resolve_session_org
    # Read-side reconcile so a drifted/empty stored ID does not leak out
    # the session-detail surface (auto-4nr14 §A).
    resolved_source_id = dashboard_db.reconcile_session_graph_source_id(session)
    # Is this session blocked awaiting an operator approval (e.g. a commit
    # signature)? {id, kind} or None — the viewer opens the approval overlay
    # when set. Guarded so the session surface never breaks on a
    # rendezvous-store hiccup.
    try:
        from tools.dashboard.dao import approval_requests as _approvals
        pending_approval = _approvals.pending_for_session(tmux_name)
    except Exception:
        pending_approval = None
    return JSONResponse({
        "session_id": session["tmux_name"],
        "pending_approval": pending_approval,
        "session_uuid": session.get("session_uuid"),
        "graph_source_id": resolved_source_id or None,
        # The tmux_sessions transcript column is ``jsonl_path`` (the graph
        # ``sources`` table is what carries ``file_path``). Surface it under
        # ``file_path`` because that is the key /api/session/resume expects
        # in its request body.
        "file_path": session.get("jsonl_path") or "",
        # resumable = the session's JSONL still exists on disk, so the
        # viewer can offer an in-place Resume affordance (mirrors the
        # session-list dead_resumable gate, dao/sessions.py). Computed
        # read-side so a pruned transcript flips the button off without a
        # schema change.
        "resumable": bool(
            session.get("jsonl_path") and Path(str(session.get("jsonl_path"))).exists()
        ),
        "type": session.get("type"),
        "role": session.get("role", ""),
        "activity_state": session.get("activity_state", "idle"),
        "project": session.get("project"),
        "org": resolve_session_org(session),
        "is_live": derive_lifecycle_state(session) not in ("ENDED", "FAILED"),
        "dispatch_nag_enabled": bool(session.get("dispatch_nag")),
        "nag_enabled": bool(session.get("nag_enabled")),
        "nag_interval": session.get("nag_interval"),
        "nag_message": session.get("nag_message"),
        "nag_last_sent": session.get("nag_last_sent"),
    })


_TMUX_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


async def api_session_output(request):
    """GET /api/session/{tmux_name}/output/{path:path}

    Serve a file the container CLI dropped into ``/workspace/output`` for the
    named session. Resolves to ``data/agent-runs/<tmux_name>-<ts>/<path>``,
    picking the most recent run directory when the session has been launched
    more than once. Used by the viewer's ``viewer_attachment`` tile to render
    images shared via ``graph share``.

    Security: the trusted session value lives on the parsed entry (stamped
    by SessionMonitor from the source JSONL stream); the viewer reads it
    from there, not from the agent's tool_result content. We additionally
    validate ``tmux_name`` shape so the glob below can't be coerced into
    matching unrelated run directories via metacharacters.
    """
    tmux_name = request.path_params["tmux_name"]
    rel_path = request.path_params["path"]

    if not _TMUX_NAME_RE.match(tmux_name):
        return JSONResponse({"error": "invalid session name"}, status_code=400)

    # Path-traversal guard — reject any segment that escapes upward or has
    # an absolute component. We then resolve and re-check against the run
    # directory so symlinks can't fan out either.
    if not rel_path or rel_path.startswith("/") or ".." in Path(rel_path).parts:
        return JSONResponse({"error": "invalid path"}, status_code=400)

    # Cross-org guard BEFORE the run-dir glob/stat/read (auto-49esb): a cross-org
    # session's files are refused as the same 404 a session with no run dir
    # returns, so existence never leaks.
    if _session_hidden_cross_org(request, dashboard_db.get_session(tmux_name)):
        return JSONResponse({"error": "session run dir not found"}, status_code=404)

    # Candidate base dirs, newest-first. Container sessions resolve under
    # their data/agent-runs/<name>-<ts>/ run dir(s); host (terminal) sessions
    # have no run dir, so their uploads live under data/host-uploads/<name>/.
    # Both are searched the same way (resolve + re-check containment) so the
    # viewer tile serves identically regardless of session kind.
    base_dirs = []
    if AGENT_RUNS_DIR.exists():
        base_dirs.extend(sorted(
            (p for p in AGENT_RUNS_DIR.glob(f"{tmux_name}-*") if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ))
    host_dir = HOST_UPLOADS_DIR / tmux_name
    if host_dir.is_dir():
        base_dirs.append(host_dir)

    if not base_dirs:
        return JSONResponse({"error": "session run dir not found"}, status_code=404)

    for base_dir in base_dirs:
        candidate = (base_dir / rel_path).resolve()
        try:
            candidate.relative_to(base_dir.resolve())
        except ValueError:
            continue
        if candidate.is_file():
            mime, _ = mimetypes.guess_type(candidate.name)
            return FileResponse(
                candidate,
                media_type=mime or "application/octet-stream",
                headers={"Cache-Control": "public, max-age=31536000, immutable"},
            )
    return JSONResponse({"error": "file not found"}, status_code=404)


async def api_session_label(request):
    """Set or clear the user-facing label for a session.

    PUT /api/session/{tmux_name}/label
    Body: {"label": "Dashboard auth design"}
    Returns: {"ok": true}
    """
    t0 = time.monotonic()
    tmux_name = request.path_params["tmux_name"]
    body = await request.json()
    t_json = time.monotonic()
    label = body.get("label", "").strip()
    if os.environ.get("DASHBOARD_MOCK"):
        await event_bus.broadcast("session:registry", dao_sessions.get_active_sessions())
        return JSONResponse({"ok": True})
    dashboard_db.update_label(tmux_name, label)
    t_update = time.monotonic()
    # Also update graph source title if the session has been ingested
    session_row = dashboard_db.get_session(tmux_name)
    t_get = time.monotonic()
    graph_source_id = session_row.get("graph_source_id") if session_row else None
    if graph_source_id:
        # A session's display name belongs to the SESSION, not to whichever
        # organization the caller happens to be scoped to. Writing it under the
        # caller's org raised CrossOrgWriteError whenever the two differed --
        # unhandled, so Starlette returned a plain-text 500 with no JSON error
        # for the CLI to print. An agent renaming its own session got "HTTP
        # Error 500" and nothing else, five times in one morning.
        #
        # Acting in the source's own organization is what the write always
        # meant. A label is not cross-org content being edited by a stranger;
        # it is the session naming itself.
        try:
            origin = graph_ops._resolve_source_home(graph_source_id, org=None)
        except Exception:
            origin = None
        try:
            graph_ops.update_source_title(
                graph_source_id, label, org=origin or None)
        except Exception as exc:
            # Never a bare 500: the caller can act on a sentence and cannot
            # act on an empty body.
            logger.warning(
                "session_label: could not retitle source %s: %s",
                graph_source_id, exc, exc_info=True)
            return JSONResponse(
                {"error": f"the label was saved, but the session's graph "
                          f"source could not be retitled: {exc}"},
                status_code=502,
            )
    t_graph = time.monotonic()
    # Broadcast via SSE so all clients update
    await event_bus.broadcast("session:registry", session_monitor.get_registry())
    t_broadcast = time.monotonic()
    logger.info(
        "session_label.timings tmux=%s graph_source_id=%s "
        "json=%.1fms update_label=%.1fms get_session=%.1fms "
        "update_source_title=%.1fms broadcast=%.1fms total=%.1fms",
        tmux_name,
        graph_source_id or "",
        (t_json - t0) * 1000,
        (t_update - t_json) * 1000,
        (t_get - t_update) * 1000,
        (t_graph - t_get) * 1000,
        (t_broadcast - t_graph) * 1000,
        (t_broadcast - t0) * 1000,
    )
    return JSONResponse({"ok": True})


async def api_session_topics(request):
    """Set sub-topic status lines for a session card.

    PUT /api/session/{tmux_name}/topics
    Body: {"topics": ["Researching auth flow", "Reading server.py"]}
    Returns: {"ok": true}

    Rules: 1-4 strings, max 80 chars each, plain text only.
    """
    tmux_name = request.path_params["tmux_name"]
    body = await request.json()
    topics_raw = body.get("topics", [])

    # Validate
    if not isinstance(topics_raw, list):
        return JSONResponse({"error": "topics must be an array"}, status_code=400)
    if len(topics_raw) > 4:
        return JSONResponse({"error": "max 4 topics"}, status_code=400)

    # Sanitize: plain text, strip, truncate to 80 chars
    topics = []
    for t in topics_raw:
        if not isinstance(t, str):
            continue
        clean = t.strip().replace('<', '').replace('>', '')[:80]
        if clean:
            topics.append(clean)

    if os.environ.get("DASHBOARD_MOCK"):
        await event_bus.broadcast("session:registry", dao_sessions.get_active_sessions())
        return JSONResponse({"ok": True})
    dashboard_db.update_topics(tmux_name, topics)
    # Broadcast via SSE so all clients update
    await event_bus.broadcast("session:registry", session_monitor.get_registry())
    return JSONResponse({"ok": True})


async def api_session_role(request):
    """Set or clear the explicit role for a session.

    PUT /api/session/{tmux_name}/role
    Body: {"role": "coordinator"}
    Returns: {"ok": true}

    Any string up to 32 characters accepted. Empty string clears the role.
    """
    tmux_name = request.path_params["tmux_name"]
    body = await request.json()
    role = body.get("role", "")

    if not isinstance(role, str):
        return JSONResponse({"error": "role must be a string"}, status_code=400)

    role = role.strip().lower()
    if len(role) > 32:
        return JSONResponse({"error": "role too long (max 32 chars)"}, status_code=400)

    if os.environ.get("DASHBOARD_MOCK"):
        await event_bus.broadcast("session:registry", dao_sessions.get_active_sessions())
        return JSONResponse({"ok": True})
    dashboard_db.update_role(tmux_name, role)
    await event_bus.broadcast("session:registry", session_monitor.get_registry())
    return JSONResponse({"ok": True})


async def api_session_nag(request):
    """Configure nag alerts for a session.

    PUT /api/session/{tmux_name}/nag
    Body: {"enabled": true, "interval": 15, "message": "Status update please."}
    """
    tmux_name = request.path_params["tmux_name"]
    body = await request.json()
    enabled = body.get("enabled")
    interval = body.get("interval")
    message = body.get("message")

    if interval is not None:
        if not isinstance(interval, int) or interval < 1 or interval > 120:
            return JSONResponse({"error": "interval must be 1-120 minutes"}, status_code=400)
    if message is not None:
        message = str(message).strip().replace('<', '').replace('>', '')[:200]

    if os.environ.get("DASHBOARD_MOCK"):
        await event_bus.broadcast("session:registry", dao_sessions.get_active_sessions())
        return JSONResponse({"ok": True})
    dashboard_db.update_nag_config(
        tmux_name,
        enabled=enabled,
        interval=interval,
        message=message,
    )
    await event_bus.broadcast("session:registry", session_monitor.get_registry())
    return JSONResponse({"ok": True})


async def api_session_nag_delete(request):
    """Disable nag for a session.

    DELETE /api/session/{tmux_name}/nag
    """
    tmux_name = request.path_params["tmux_name"]
    if os.environ.get("DASHBOARD_MOCK"):
        await event_bus.broadcast("session:registry", dao_sessions.get_active_sessions())
        return JSONResponse({"ok": True})
    dashboard_db.update_nag_config(tmux_name, enabled=False)
    await event_bus.broadcast("session:registry", session_monitor.get_registry())
    return JSONResponse({"ok": True})


async def api_session_dispatch_nag(request):
    """Enable or disable dispatch completion nag for a session.

    PUT /api/session/{tmux_name}/dispatch-nag
    Body: {"enabled": true}
    """
    tmux_name = request.path_params["tmux_name"]
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    if os.environ.get("DASHBOARD_MOCK"):
        await event_bus.broadcast("session:registry", dao_sessions.get_active_sessions())
        return JSONResponse({"ok": True})
    dashboard_db.update_dispatch_nag(tmux_name, enabled)
    await event_bus.broadcast("session:registry", session_monitor.get_registry())
    return JSONResponse({"ok": True})


# ── Turn Correction API (auto-edec1.2) ──────────────────────────
#
# Sparse persisted overlay rows for the session-viewer turn-correction
# feature. The original transcript JSONL stays immutable; corrections live
# in dashboard.db keyed by (session_uuid, target_message_id) and are looked
# up by either tmux_name or session_uuid in the URL path.

def _resolve_session_uuid(session_id: str) -> str | None:
    """Resolve a session_id (tmux_name or session_uuid) to its session_uuid.

    Returns None if no session matches. Used by the turn-correction endpoints
    so callers can address sessions by either identifier.
    """
    if not session_id:
        return None
    row = dashboard_db.get_session(session_id)
    if row and row.get("session_uuid"):
        return row["session_uuid"]
    # Maybe the caller passed the session_uuid directly. Match against the
    # canonical column AND session_uuids history (legacy rolled-over JSONLs
    # carry their old uuid in the array).
    from tools.dashboard.dao.dashboard_db import get_conn as _get_conn
    conn = _get_conn()
    direct = conn.execute(
        "SELECT session_uuid FROM tmux_sessions WHERE session_uuid=?"
        " ORDER BY created_at DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if direct and direct["session_uuid"]:
        return direct["session_uuid"]
    fallback = conn.execute(
        "SELECT session_uuid FROM tmux_sessions WHERE session_uuids LIKE ?"
        " ORDER BY created_at DESC LIMIT 1",
        (f"%{session_id}%",),
    ).fetchone()
    if fallback and fallback["session_uuid"]:
        return fallback["session_uuid"]
    return None


def _serialize_turn_correction(row: dict) -> dict:
    """Project a turn-correction row to its public JSON shape."""
    return {
        "session_uuid": row.get("session_uuid"),
        "target_message_id": row.get("target_message_id"),
        "status": row.get("status"),
        "original_sha256": row.get("original_sha256"),
        "corrected_text": row.get("corrected_text"),
        "mode": row.get("mode"),
        "reason": row.get("reason"),
        "confidence": row.get("confidence"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


async def api_session_turn_corrections_list(request):
    """GET /api/session/{session_id}/turn-corrections

    Returns every persisted correction for the session (pending and terminal).
    Used to rehydrate overlay state on page load and replay.
    """
    session_id = request.path_params["session_id"]
    headers = {"Cache-Control": "no-store"}
    if os.environ.get("DASHBOARD_MOCK"):
        rows = dao_sessions.get_turn_corrections(session_id)
        return JSONResponse({
            "session_id": session_id,
            "session_uuid": session_id,
            "corrections": [_serialize_turn_correction(r) for r in rows],
        }, headers=headers)
    # Cross-org guard BEFORE uuid resolution / correction read (auto-49esb): a
    # cross-org session's corrections (which carry user message text) are refused
    # as the same 404 an unknown session returns, so existence never leaks.
    if _session_hidden_cross_org(request, dashboard_db.get_session(session_id)):
        return JSONResponse(
            {"error": "session not found", "session_id": session_id},
            status_code=404,
            headers=headers,
        )
    session_uuid = _resolve_session_uuid(session_id)
    if not session_uuid:
        return JSONResponse(
            {"error": "session not found", "session_id": session_id},
            status_code=404,
            headers=headers,
        )
    rows = dashboard_db.list_turn_corrections(session_uuid)
    return JSONResponse({
        "session_id": session_id,
        "session_uuid": session_uuid,
        "corrections": [_serialize_turn_correction(r) for r in rows],
    }, headers=headers)


async def _resolve_correction_transition(request, target_status: str):
    """Shared validation + DB transition for accept/dismiss POSTs.

    Returns a Starlette JSONResponse. Both endpoints require the caller to
    submit ``original_sha256`` so we can refuse stale targets even when the
    same target_message_id is reused after a corrupted/aborted edit cycle.
    """
    session_id = request.path_params["session_id"]
    message_id = request.path_params["message_id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    expected_sha = (body.get("original_sha256") or "").strip()
    if not expected_sha:
        return JSONResponse(
            {"error": "original_sha256 is required"}, status_code=400,
        )

    if os.environ.get("DASHBOARD_MOCK"):
        outcome, row = dao_sessions.set_turn_correction_status(
            session_id, message_id, target_status, expected_sha256=expected_sha,
        )
        if outcome == "not_found":
            return JSONResponse(
                {"error": "correction not found",
                 "target_message_id": message_id},
                status_code=404,
            )
        if outcome == "sha_mismatch":
            return JSONResponse(
                {"error": "stale target — original_sha256 does not match",
                 "stored_sha256": row["original_sha256"] if row else None,
                 "submitted_sha256": expected_sha,
                 "correction": _serialize_turn_correction(row) if row else None},
                status_code=409,
            )
        return JSONResponse({
            "ok": True,
            "correction": _serialize_turn_correction(row) if row else None,
        })

    session_uuid = _resolve_session_uuid(session_id)
    if not session_uuid:
        return JSONResponse(
            {"error": "session not found", "session_id": session_id},
            status_code=404,
        )

    outcome, row = dashboard_db.set_turn_correction_status(
        session_uuid, message_id, target_status, expected_sha256=expected_sha,
    )
    if outcome == "not_found":
        return JSONResponse(
            {"error": "correction not found",
             "session_uuid": session_uuid,
             "target_message_id": message_id},
            status_code=404,
        )
    if outcome == "sha_mismatch":
        return JSONResponse(
            {"error": "stale target — original_sha256 does not match",
             "stored_sha256": row["original_sha256"] if row else None,
             "submitted_sha256": expected_sha,
             "correction": _serialize_turn_correction(row) if row else None},
            status_code=409,
        )
    if outcome == "already_terminal":
        return JSONResponse(
            {"error": "correction already terminal",
             "status": row["status"] if row else None,
             "correction": _serialize_turn_correction(row) if row else None},
            status_code=409,
        )

    if outcome == "ok" and target_status == "accepted" and row is not None:
        await asyncio.to_thread(
            _maybe_persist_accepted_correction_to_graph,
            session_id, session_uuid, row,
        )

    if row is not None:
        await event_bus.broadcast(
            "session:turn_corrections",
            {
                "session_id": session_id,
                "session_uuid": session_uuid,
                "correction": _serialize_turn_correction(row),
            },
            dedup=False,
        )

    return JSONResponse({
        "ok": True,
        "correction": _serialize_turn_correction(row) if row else None,
    })


def _resolve_session_workspace(
    session_id: str, session_uuid: str,
) -> tuple[str, str] | None:
    """Resolve ``(workspace_id, graph_project)`` for an accepted-correction session.

    Reads the ``tmux_sessions`` row to recover the launch-time ``project``,
    then maps that to a workspace via :func:`agents.workspace_settings.get_workspace`.
    For host/path-derived sessions (e.g. ``-workspace-repo`` or a slugified
    ``…-workspace-autonomy`` checkout), falls back to the org slug derived
    by :func:`tools.dashboard.org_identity.session_org_slug` and the first
    workspace whose ``graph_project`` matches that slug. Returns ``None``
    when nothing maps — the caller fails closed and skips graph persistence.
    """
    from agents.workspace_settings import get_workspace as _get_workspace, \
        load_workspaces as _load_workspaces
    from tools.dashboard.org_identity import session_org_slug

    db_row = dashboard_db.get_session(session_id)
    if db_row is None:
        return None
    project = (db_row.get("project") or "").strip()
    if project:
        try:
            ws = _get_workspace(project)
            if ws.graph_project:
                return (ws.id, ws.graph_project)
        except KeyError:
            pass

    org_slug = session_org_slug(db_row)
    if not org_slug or org_slug == "unknown":
        return None
    for ws in _load_workspaces().values():
        if ws.graph_project == org_slug:
            return (ws.id, ws.graph_project)
    return None


def _maybe_persist_accepted_correction_to_graph(
    session_id: str, session_uuid: str, row: dict,
) -> None:
    """Best-effort: mirror an accepted correction as a graph supersedes thought.

    Gated by ``autonomy.workspace.turn_correction#1.persist_accepts_to_graph``
    on the workspace's owning org. Failures are logged and swallowed — the
    dashboard accept transition has already succeeded; graph persistence is
    opportunistic and must never break the API response.

    The corrected thought is inserted into the same ingested session source
    as the original turn, with a ``supersedes`` edge pointing back at the
    original thought. Idempotency is enforced inside
    :func:`tools.graph.ops.persist_corrected_thought` via a deterministic
    derived ``message_id`` plus the ``edges.UNIQUE(source_id, target_id,
    relation)`` constraint, so retries from a flaky operator click do not
    multiply graph artifacts.
    """
    try:
        resolved = _resolve_session_workspace(session_id, session_uuid)
        if resolved is None:
            return
        workspace_id, graph_project = resolved

        from tools.graph.schemas.turn_correction import (
            SCHEMA_REVISION as _TC_REV,
            SET_ID as _TC_SET_ID,
            resolve_payload as _resolve_tc,
        )
        try:
            members = graph_ops.read_set(
                _TC_SET_ID,
                org=graph_project,
                peers=[],
                target_revision=_TC_REV,
            )
        except Exception:
            return
        raw_payload: dict | None = None
        for member in members.members:
            if member.key == workspace_id:
                if isinstance(member.payload, dict):
                    raw_payload = dict(member.payload)
                break
        resolved_setting = _resolve_tc(raw_payload)
        if not bool(resolved_setting.get("persist_accepts_to_graph")):
            return

        target_message_id = row.get("target_message_id") or ""
        original_sha256 = row.get("original_sha256") or ""
        corrected_text = row.get("corrected_text")
        if not target_message_id or not original_sha256 \
                or corrected_text is None:
            return

        graph_ops.persist_corrected_thought(
            org=graph_project,
            session_uuid=session_uuid,
            target_message_id=target_message_id,
            original_sha256=original_sha256,
            corrected_text=corrected_text,
            extra_metadata={
                "mode": row.get("mode"),
                "reason": row.get("reason"),
                "confidence": row.get("confidence"),
            },
        )
    except Exception:
        logger.exception(
            "turn_correction: graph persistence failed"
            " session_uuid=%s target=%s",
            session_uuid, row.get("target_message_id"),
        )


_TURN_CORRECTION_SUGGEST_MODES = ("off", "conservative", "balanced", "aggressive")
# Caller-controlled identity is never trusted: the session comes from the bearer
# token and the target/hash are resolved server-side. Presence of any of these
# is a hard 400, not a silently-ignored field.
_TURN_CORRECTION_FORBIDDEN_FIELDS = (
    "session_id", "session_uuid", "target_message_id", "original_sha256",
)


def _validate_turn_correction_suggestion(
    body: Any,
) -> tuple[dict | None, JSONResponse | None]:
    """Validate a suggest POST body. Returns (payload, None) or (None, error)."""
    if not isinstance(body, dict):
        return None, JSONResponse(
            {"error": "request body must be a JSON object"}, status_code=400)
    for forbidden in _TURN_CORRECTION_FORBIDDEN_FIELDS:
        if forbidden in body:
            return None, JSONResponse(
                {"error": (
                    f"caller-controlled identity field '{forbidden}' is not "
                    "accepted; the server derives session and target"
                )},
                status_code=400,
            )
    corrected = body.get("corrected_text")
    if not isinstance(corrected, str) or corrected == "":
        return None, JSONResponse(
            {"error": "corrected_text is required and must be a non-empty string"},
            status_code=400,
        )
    corrected = turn_corrections_mod.normalize_correction_text(corrected)
    mode = body.get("mode")
    if mode is not None and mode not in _TURN_CORRECTION_SUGGEST_MODES:
        return None, JSONResponse(
            {"error": "mode must be one of off|conservative|balanced|aggressive"},
            status_code=400,
        )
    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        return None, JSONResponse(
            {"error": "reason must be a string"}, status_code=400)
    confidence = body.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            return None, JSONResponse(
                {"error": "confidence must be a number in [0.0, 1.0]"},
                status_code=400,
            )
        confidence = float(confidence)
        if not (0.0 <= confidence <= 1.0):
            return None, JSONResponse(
                {"error": "confidence must be in [0.0, 1.0]"}, status_code=400)
    return {
        "corrected_text": corrected,
        "mode": mode,
        "reason": reason,
        "confidence": confidence,
    }, None


async def api_session_turn_correction_suggest(request):
    """POST /api/session/turn-corrections/suggest

    Authenticated turn-correction submission (bead auto-hmow2). This is the
    ONLY delivery path — the CLI POSTs here; the correction never becomes a
    transcript entry and SessionMonitor never participates.

    The caller's canonical tmux session is derived from the bearer
    ``SESSION_TOKEN`` (never from the URL or body). The server resolves the
    target from that session's recent canonical user turns, computes
    ``original_sha256`` from the immutable original text, persists exactly one
    sparse row, then broadcasts the exact committed row on
    ``session:turn_corrections``. Persist first, broadcast second; no failure
    path writes a row or broadcasts.
    """
    identity, err = authenticate_session_request(request)
    if err is not None:
        return err
    tmux_name, _org = identity

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    payload, verr = _validate_turn_correction_suggestion(body)
    if verr is not None:
        return verr

    session = dashboard_db.get_session(tmux_name)
    if session is None:
        return JSONResponse(
            {"error": "session not found", "session_id": tmux_name},
            status_code=404,
        )
    session_uuid = session.get("session_uuid")
    jsonl_path = session.get("jsonl_path")
    if not session_uuid or not jsonl_path:
        logger.warning(
            "turn_correction suggest 409: session not linked to JSONL/UUID session=%s",
            tmux_name,
        )
        return JSONResponse(
            {"error": "session is not linked to a JSONL/session UUID"},
            status_code=409,
        )

    try:
        users = turn_corrections_mod.read_recent_canonical_user_turns(jsonl_path)
    except session_harness.TranscriptParseContextError as exc:
        logger.warning(
            "turn_correction suggest 409: transcript parse context unavailable "
            "session=%s jsonl_path=%s error=%s",
            tmux_name, jsonl_path, exc,
        )
        return JSONResponse(
            {"error": "transcript parse context unavailable", "detail": str(exc)},
            status_code=409,
        )

    def _target_unavailable(message_id: str) -> bool:
        # A target already carrying a terminal (accepted/dismissed) correction
        # must not be re-targeted by a fresh pending suggestion. Pending rows
        # stay available so the DAO's pending-upsert refreshes them in place.
        existing = dashboard_db.get_turn_correction(session_uuid, message_id)
        return existing is not None and existing.get("status") in ("accepted", "dismissed")

    target = turn_corrections_mod.resolve_best_correction_target(
        users=users,
        corrected_text=payload["corrected_text"],
        unavailable=_target_unavailable,
    )
    if target is None:
        logger.warning(
            "turn_correction suggest 409: no acceptable target session=%s "
            "candidates=%d corrected_text=%r",
            tmux_name, len(users), payload["corrected_text"][:120],
        )
        return JSONResponse(
            {"error": "no acceptable recent user turn to correct"},
            status_code=409,
        )

    original_sha256 = hashlib.sha256(
        str(target["content"]).encode("utf-8")).hexdigest()
    try:
        row = dashboard_db.upsert_turn_correction(
            session_uuid,
            str(target["message_id"]),
            original_sha256=original_sha256,
            corrected_text=payload["corrected_text"],
            mode=payload["mode"],
            reason=payload["reason"],
            confidence=payload["confidence"],
        )
    except Exception:
        logger.exception(
            "turn_correction suggest: persistence failed session=%s target=%s",
            tmux_name, target.get("message_id"),
        )
        return JSONResponse({"error": "persistence failure"}, status_code=500)

    if not row:
        return JSONResponse({"error": "persistence failure"}, status_code=500)
    if row.get("status") != "pending":
        # A concurrent accept/dismiss reached this target between resolution and
        # upsert; the DAO preserved the terminal row. Report the conflict rather
        # than claim a pending create for a row we did not write.
        return JSONResponse(
            {"error": "target already has a terminal correction",
             "correction": _serialize_turn_correction(row)},
            status_code=409,
        )

    public_row = _serialize_turn_correction(row)
    await event_bus.broadcast(
        "session:turn_corrections",
        {
            "session_id": tmux_name,
            "session_uuid": session_uuid,
            "correction": public_row,
        },
        dedup=False,
    )
    return JSONResponse(
        {"ok": True, "session_id": tmux_name, "correction": public_row},
        status_code=201,
    )


async def api_session_turn_correction_accept(request):
    """POST /api/session/{session_id}/turn-corrections/{message_id}/accept

    Body: {"original_sha256": "..."}
    Transitions a pending correction to ``accepted``. Returns 409 on stale
    or already-terminal rows.
    """
    return await _resolve_correction_transition(request, "accepted")


async def api_session_turn_correction_dismiss(request):
    """POST /api/session/{session_id}/turn-corrections/{message_id}/dismiss

    Body: {"original_sha256": "..."}
    Transitions a pending correction to ``dismissed``. Returns 409 on stale
    or already-terminal rows.
    """
    return await _resolve_correction_transition(request, "dismissed")


async def _resolve_primer(primer: str) -> str | None:
    """Resolve a graph:// URL to its text content.  Returns None on failure."""
    graph_id = primer.removeprefix("graph://") if primer.startswith("graph://") else primer
    if not graph_id:
        return None
    try:
        # Cap protects the LLM context window — primer text is injected
        # verbatim into the next session's first message.
        payload = await asyncio.to_thread(
            graph_ops.read_source_full, graph_id, max_chars=50000,
        )
        if payload is None:
            logger.warning("_resolve_primer: source not found  id=%s", graph_id)
            return None
        # Render a text blob from source title + entries (mirrors the
        # ``graph read`` CLI shape expected by callers of this helper).
        src = payload.get("source") or {}
        parts: list[str] = []
        title = src.get("title") or ""
        if title:
            parts.append(f"# {title}")
        for e in payload.get("entries") or []:
            parts.append(e.get("content") or "")
        text = "\n\n".join(p for p in parts if p).strip()
        return text or None
    except Exception:
        logger.warning("_resolve_primer: exception resolving %s", graph_id, exc_info=True)
    return None


_HOST_MODEL_FALLBACK = "claude-opus-4-8[1m]"
# Step budgets come from the FSM module's STEP_TIMEOUTS_S — the single
# source both the worker (enforcing inside each blocking step) and the
# liveness reaper's orphan belt (budget + margin) read. Defining a number
# here that the reaper can't see is how the 300s-grace < 600s-setup
# ordering bug happened.
from tools.dashboard.session_lifecycle_worker import STEP_TIMEOUTS_S as _STEP_TIMEOUTS_S

_LIFECYCLE_PREPARING_TIMEOUT_S = _STEP_TIMEOUTS_S["preparing_workspace"]
_LIFECYCLE_LAUNCHING_TIMEOUT_S = _STEP_TIMEOUTS_S["launching_container"]
_LIFECYCLE_SETUP_TIMEOUT_S = _STEP_TIMEOUTS_S["setup_running"]
_LIFECYCLE_WAITING_READY_TIMEOUT_S = _STEP_TIMEOUTS_S["harness_starting"]
_LIFECYCLE_INJECTING_TIMEOUT_S = _STEP_TIMEOUTS_S["awaiting_first_response"]
_LIFECYCLE_REGISTER_TIMEOUT_S = 5
_LIFECYCLE_TMUX_OP_TIMEOUT_S = 5
_LIFECYCLE_STOP_TIMEOUT_S = _STEP_TIMEOUTS_S["stopping"]
_LIFECYCLE_REMOVE_WATCHERS_TIMEOUT_S = 5
_LIFECYCLE_DEREGISTER_TIMEOUT_S = 5
_LIFECYCLE_CLEANUP_WORKTREE_TIMEOUT_S = 60


def _resolve_host_session_model() -> str:
    """Model id to pass to host `claude` invocations.

    Reads ``autonomy.workspace#1[autonomy].model`` so the operator can
    change the default in one place. Falls back to a constant when the
    Setting is unset or unreadable.
    """
    try:
        ws = workspace_settings.get_workspace("autonomy")
        if ws.model:
            return ws.model
    except (KeyError, workspace_settings.WorkspaceSettingsError):
        pass
    return _HOST_MODEL_FALLBACK


def _remaining_step_timeout(deadline: float, phase: str) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"{phase} timed out")
    return max(1, int(remaining))


def _run_tmux_capture(tmux_name: str, *, timeout: float | None = None) -> str:
    result = subprocess.run(
        ["tmux", "capture-pane", "-pJ", "-S", "-200", "-t", tmux_name],
        capture_output=True,
        text=True,
        timeout=timeout or _LIFECYCLE_TMUX_OP_TIMEOUT_S,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise RuntimeError(f"tmux capture-pane failed: {stderr}")
    return result.stdout or ""


def _container_exists(tmux_name: str) -> bool | None:
    """Does a docker container named after this session exist? Tri-state:
    True/False are authoritative; None means the probe itself failed
    (same fail-safe contract as the tmux liveness probe)."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", tmux_name],
            capture_output=True,
            text=True,
            timeout=_LIFECYCLE_TMUX_OP_TIMEOUT_S,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode == 0


def _pane_tail(tmux_name: str, lines: int = 15) -> str:
    """Best-effort last pane lines — the docker error is usually here."""
    try:
        text = _run_tmux_capture(tmux_name)
    except Exception:
        return ""
    stripped = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(stripped[-lines:])


def _verify_container_started(*, tmux_name: str, deadline: float) -> None:
    """Fail fast when ``docker run`` produced no container.

    The tmux spawn succeeding only proves tmux ran the command — a docker
    invocation that aborts during container init (bad mount, OCI error)
    leaves NO container, and without this check the launch would sit in
    phantom ``setup_running`` for the full setup budget before failing
    (auto-0709-092918: three 600s cycles against a mount error that was
    printed in the pane within two seconds). Polls until the container
    object exists; probe failures (None) don't count against it.
    """
    saw_missing = False
    while time.monotonic() < deadline:
        exists = _container_exists(tmux_name)
        if exists:
            return
        if exists is False:
            saw_missing = True
        time.sleep(1.0)
    detail = "docker run produced no container"
    if not saw_missing:
        detail = "container presence could not be verified (docker probe failing)"
    tail = _pane_tail(tmux_name)
    if tail:
        detail += f"; pane tail: {tail[-800:]}"
    raise RuntimeError(detail)


def _wait_for_setup_complete(
    *,
    tmux_name: str,
    run_dir: Path,
    startup_script: Path | None,
    deadline: float,
) -> None:
    """Wait for the optional project startup script's exit marker.

    Also watches the container itself: a container that dies mid-setup can
    never write ``.setup-exit``, and waiting the full setup budget for a
    corpse is the phantom-setup failure mode. Two consecutive authoritative
    "container gone" probes fail the step immediately with the pane tail.
    """
    if startup_script is None:
        return
    setup_exit = run_dir / ".setup-exit"
    container_missing_streak = 0
    tick = 0
    while time.monotonic() < deadline:
        if setup_exit.exists():
            exit_code = setup_exit.read_text().strip()
            if exit_code == "0":
                return
            log_tail = ""
            setup_log = run_dir / ".setup.log"
            try:
                log_tail = "\n".join(setup_log.read_text(errors="replace").splitlines()[-20:])
            except FileNotFoundError:
                pass
            detail = f"setup exited {exit_code}"
            if log_tail:
                detail += f": {log_tail[-1000:]}"
            raise RuntimeError(detail)
        tick += 1
        if tick % 5 == 0:
            exists = _container_exists(tmux_name)
            if exists is False:
                container_missing_streak += 1
                if container_missing_streak >= 2:
                    detail = "container died during setup"
                    tail = _pane_tail(tmux_name)
                    if tail:
                        detail += f"; pane tail: {tail[-800:]}"
                    raise RuntimeError(detail)
            elif exists:
                container_missing_streak = 0
        time.sleep(1.0)
    raise TimeoutError(f"setup timed out after {_LIFECYCLE_SETUP_TIMEOUT_S}s")


def _read_harness_state(tmux_name: str) -> dict[str, Any]:
    row = dashboard_db.get_session(tmux_name)
    raw = (row or {}).get("harness_state") if row else None
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _wait_for_prompt(
    *,
    tmux_name: str,
    deadline: float,
    writer: SessionLifecycleStateWriter | None = None,
) -> None:
    """Wait for the pane-poller's durable composer_ready signal.

    The pane-poller (session_monitor._screen_poll_loop) is the single pane
    reader and keystroke sender during a launch: armed via
    arm_startup_state at the launch entrypoints, it runs the harness screen
    adapter (trust auto-confirm, composer detection, grace fallback for
    unreadable panes) and persists the result into harness_state. The
    worker step just waits on that signal — it never touches the pane, so
    there is exactly one detector per session. Two concurrent capture+
    keystroke loops (the pre-signal design) could double-send the trust
    confirmation.

    When a ``writer`` is supplied, the trust dialog surfaces on the chip:
    the poller only reports ``confirming_trust_prompt`` in harness_state,
    and the worker — the single state writer — mirrors it into the
    ``confirming_trust``/``waiting_ready`` states as it flips.
    """
    in_trust = False
    while time.monotonic() < deadline:
        state = _read_harness_state(tmux_name)
        if state.get("composer_ready"):
            return
        if writer is not None:
            trust_now = bool(state.get("confirming_trust_prompt"))
            if trust_now != in_trust:
                in_trust = trust_now
                writer.set_state(
                    tmux_name,
                    "confirming_trust" if trust_now else "waiting_ready",
                )
        time.sleep(0.5)
    raise TimeoutError(f"waiting_ready timed out after {_LIFECYCLE_WAITING_READY_TIMEOUT_S}s")


def _resolve_primer_sync(primer: str) -> str | None:
    graph_id = primer.removeprefix("graph://") if primer.startswith("graph://") else primer
    if not graph_id:
        return None
    payload = graph_ops.read_source_full(graph_id, max_chars=50000)
    if payload is None:
        return None
    src = payload.get("source") or {}
    parts: list[str] = []
    title = src.get("title") or ""
    if title:
        parts.append(f"# {title}")
    for entry in payload.get("entries") or []:
        parts.append(entry.get("content") or "")
    text = "\n\n".join(part for part in parts if part).strip()
    return text or None


def _append_workspace_startup_notice(message: str | None, proj) -> str | None:
    """Append one compact, actionable sentence for degraded workspace starts."""
    issues = tuple(getattr(proj, "capability_issues", ()) or ())
    if message is None or not issues:
        return message

    subjects = list(dict.fromkeys(
        issue.subject for issue in issues if getattr(issue, "subject", "")
    ))
    shown = subjects[:3]
    subject_text = ", ".join(shown)
    if len(subjects) > len(shown):
        subject_text += f", +{len(subjects) - len(shown)} more"
    scope = f" ({subject_text})" if subject_text else ""
    check_word = "check" if len(issues) == 1 else "checks"
    notice = (
        f"Startup degraded: {len(issues)} capability {check_word} failed{scope}; "
        f"diagnose via `GET /api/orgs/{proj.graph_project}/workspaces/health`."
    )
    return f"{message.rstrip()}\n\n{notice}"


def _render_worker_first_message(
    *,
    tmux_name: str,
    proj,
    primer_url: str | None,
) -> tuple[str | None, bool]:
    if primer_url:
        try:
            resolved = _resolve_primer_sync(primer_url)
            if resolved:
                return _append_workspace_startup_notice(resolved, proj), True
            logger.warning(
                "session_lifecycle: primer unresolved tmux=%s primer=%r; falling back to orientation",
                tmux_name,
                primer_url,
            )
        except Exception:
            logger.warning(
                "session_lifecycle: primer resolve failed tmux=%s primer=%r",
                tmux_name,
                primer_url,
                exc_info=True,
            )

    from tools.dashboard.session_orientation import render_orientation

    try:
        return (
            _append_workspace_startup_notice(render_orientation(
                tmux_name=tmux_name,
                workspace_id=proj.id,
                workspace_name=proj.name,
                org=proj.graph_project,
            ), proj),
            False,
        )
    except Exception:
        logger.warning(
            "session_lifecycle: orientation render failed tmux=%s; falling back to literal Hello",
            tmux_name,
            exc_info=True,
        )
        return _append_workspace_startup_notice("Hello", proj), False


def _echo_candidates(message: str) -> list[str]:
    candidates: list[str] = []
    for line in message.splitlines():
        stripped = re.sub(r"\s+", " ", line.strip())
        if len(stripped) >= 8:
            candidates.append(stripped[:120])
    if not candidates and message.strip():
        candidates.append(re.sub(r"\s+", " ", message.strip())[:80])
    # Prefer edge lines; very long primers may only leave the bottom of the
    # paste visible in the pane.
    return candidates[:3] + candidates[-3:]


def _message_echo_visible(before: str, after: str, message: str) -> bool:
    normalized_after = re.sub(r"\s+", " ", after)
    for candidate in _echo_candidates(message):
        if candidate and candidate in normalized_after:
            return True
    return bool(after.strip() and after.strip() != before.strip())


def _inject_echo_verified(
    *,
    tmux_name: str,
    message: str,
    harness_name: str | None,
    deadline: float,
) -> None:
    harness = (harness_name or "").lower()
    settle = {"codex": 1.5, "claude": 0.0}.get(harness, 0.0)
    if settle:
        time.sleep(min(settle, max(0.0, deadline - time.monotonic())))

    last_error = "paste echo was not visible"
    while time.monotonic() < deadline:
        op_timeout = min(
            _LIFECYCLE_TMUX_OP_TIMEOUT_S,
            _remaining_step_timeout(deadline, "injecting"),
        )
        before = _run_tmux_capture(tmux_name, timeout=op_timeout)
        tmux_paste_checked_sync(tmux_name, message, timeout=op_timeout)
        time.sleep(0.2)
        after = _run_tmux_capture(tmux_name, timeout=op_timeout)
        if _message_echo_visible(before, after, message):
            tmux_enter_checked_sync(tmux_name, timeout=op_timeout)
            return
        # Codex may accept a bracketed/multi-line paste into its composer
        # without rendering any of the pasted bytes in capture-pane until the
        # input is submitted.  Retrying in that state only duplicates the
        # orientation in the hidden input buffer, then tears down an otherwise
        # healthy container when the visibility deadline expires.  A
        # successful tmux paste is the strongest acknowledgement available for
        # this harness, so submit it once and let the normal transcript/first-
        # response path provide the durable confirmation.
        if harness == "codex":
            logger.warning(
                "session_lifecycle: codex paste had no visible echo; "
                "submitting once tmux=%s",
                tmux_name,
            )
            tmux_enter_checked_sync(tmux_name, timeout=op_timeout)
            return
        last_error = "paste echo was not visible"
        time.sleep(0.5)
    raise TimeoutError(f"injecting timed out: {last_error}")


def _run_cleanup_step(
    *,
    name: str,
    timeout: float,
    func,
) -> str | None:
    started = time.monotonic()
    try:
        func()
    except subprocess.TimeoutExpired:
        return f"{name} timed out after {timeout:g}s"
    except TimeoutError:
        return f"{name} timed out after {timeout:g}s"
    except Exception as exc:
        return f"{name} failed: {type(exc).__name__}: {exc}"
    elapsed = time.monotonic() - started
    if elapsed > timeout:
        return f"{name} exceeded timeout budget ({elapsed:.1f}s > {timeout:g}s)"
    return None


def _teardown_stop_container_and_tmux(tmux_name: str) -> None:
    """Idempotent process kill: docker container (if any) + tmux session."""
    subprocess.run(
        ["docker", "rm", "-f", tmux_name],
        capture_output=True,
        text=True,
        timeout=_LIFECYCLE_STOP_TIMEOUT_S,
    )
    subprocess.run(
        ["tmux", "kill-session", "-t", tmux_name],
        capture_output=True,
        text=True,
        timeout=_LIFECYCLE_STOP_TIMEOUT_S,
    )


def _teardown_remove_watches(tmux_name: str) -> None:
    session_monitor._remove_watches(tmux_name)
    session_monitor._tail_states.pop(tmux_name, None)
    session_monitor._phase_progress.pop(tmux_name, None)


def _teardown_deregister(
    tmux_name: str,
    loop: asyncio.AbstractEventLoop | None,
    *,
    record_death: bool = True,
) -> None:
    """Untail + revoke. ``record_death=False`` on the FAILED-cleanup path:
    the row is already terminal FAILED and must stay there."""
    if loop is not None and loop.is_running():
        fut = asyncio.run_coroutine_threadsafe(
            session_monitor.deregister(tmux_name, record_death=record_death),
            loop,
        )
        fut.result(timeout=_LIFECYCLE_DEREGISTER_TIMEOUT_S)
    elif record_death:
        dashboard_db.mark_dead(tmux_name)
    auth_db.revoke_token(tmux_name)


def _teardown_cleanup_worktrees(tmux_name: str) -> None:
    errors: list[BaseException] = []

    def _target() -> None:
        try:
            cleanup_session_worktrees(
                tmux_name,
                force=True,
                worktrees_dir=WORKTREES_DIR,
            )
        except BaseException as exc:  # noqa: BLE001 - propagate through parent thread
            errors.append(exc)

    thread = threading.Thread(
        target=_target,
        name=f"lifecycle-cleanup-{tmux_name}",
        daemon=True,
    )
    thread.start()
    thread.join(_LIFECYCLE_CLEANUP_WORKTREE_TIMEOUT_S)
    if thread.is_alive():
        raise TimeoutError("cleanup worktree timed out")
    if errors:
        raise errors[0]


def _cleanup_after_lifecycle_failure(
    *,
    tmux_name: str,
    loop: asyncio.AbstractEventLoop | None,
    cleanup_worktrees: bool = True,
) -> list[str]:
    """Best-effort bounded teardown after startup fails.

    The worker remains the single lifecycle owner: it marks the session failed,
    cleans up process/worktree state from the same worker thread, then restores
    the failed terminal state for operator visibility.

    ``cleanup_worktrees=False`` for RESUME failures: a resumed session's
    worktrees carry its whole uncommitted/unmerged history — a failed
    relaunch must never delete them. Fresh creates own their just-made
    worktrees, so cleaning those is safe.
    """
    errors: list[str] = []

    steps: list[tuple[str, float, Callable[[], None]]] = [
        ("stop_container_tmux", _LIFECYCLE_STOP_TIMEOUT_S,
         lambda: _teardown_stop_container_and_tmux(tmux_name)),
        ("remove_watchers", _LIFECYCLE_REMOVE_WATCHERS_TIMEOUT_S,
         lambda: _teardown_remove_watches(tmux_name)),
        ("deregister", _LIFECYCLE_DEREGISTER_TIMEOUT_S,
         lambda: _teardown_deregister(tmux_name, loop, record_death=False)),
    ]
    if cleanup_worktrees:
        steps.append(
            ("cleanup_worktree", _LIFECYCLE_CLEANUP_WORKTREE_TIMEOUT_S,
             lambda: _teardown_cleanup_worktrees(tmux_name)),
        )
    for name, timeout, func in steps:
        err = _run_cleanup_step(name=name, timeout=timeout, func=func)
        if err:
            logger.warning(
                "session_lifecycle: cleanup step issue tmux=%s error=%s",
                tmux_name,
                err,
            )
            errors.append(err)

    return errors


def _fail_lifecycle_start_with_cleanup(
    *,
    writer: SessionLifecycleStateWriter,
    tmux_name: str,
    phase: str,
    reason: str,
    attempt: int,
    loop: asyncio.AbstractEventLoop | None,
    cleanup_worktrees: bool = True,
) -> None:
    writer.fail(tmux_name, phase=phase, reason=reason, attempt=attempt)
    writer.set_state(
        tmux_name,
        "cleaning",
        phase=phase,
        reason="startup failed; cleaning partial session",
        attempt=attempt,
    )
    cleanup_errors = _cleanup_after_lifecycle_failure(
        tmux_name=tmux_name,
        loop=loop,
        cleanup_worktrees=cleanup_worktrees,
    )
    final_reason = reason
    if cleanup_errors:
        final_reason = f"{reason}; cleanup errors: {'; '.join(cleanup_errors)}"
    writer.fail(tmux_name, phase=phase, reason=final_reason, attempt=attempt)


def _register_project_session_from_worker(
    *,
    tmux_name: str,
    proj,
    run_dir: Path,
    primer_url: str | None,
    loop: asyncio.AbstractEventLoop | None,
    harness: str,
) -> None:
    """Register the launched session without doing lifecycle state writes."""
    sess_dir = run_dir / "sessions"
    if loop is not None and loop.is_running():
        fut = asyncio.run_coroutine_threadsafe(
            session_monitor.register(
                tmux_name=tmux_name,
                session_type="container",
                project=proj.id,
                jsonl_path=sess_dir,
                harness=harness,
                seed_message="Starting..." if not primer_url else "",
            ),
            loop,
        )
        fut.result(timeout=_LIFECYCLE_REGISTER_TIMEOUT_S)
        return

    # Fallback for tests and early worker wiring before the monitor loop is
    # supplied. It backfills the durable row; monitor tail-state setup remains
    # the responsibility of the startup wiring.
    conn = dashboard_db.get_conn()
    conn.execute(
        "UPDATE tmux_sessions"
        " SET type=?, project=?, harness=?, resolution_dir=?"
        " WHERE tmux_name=?",
        ("container", proj.id, harness, str(sess_dir), tmux_name),
    )
    conn.commit()
    if not primer_url:
        dashboard_db.update_tail_state(tmux_name, last_message="Starting...")


def _apply_env_from_host(names, extra_env: dict, *, context: str) -> None:
    """Copy each var in ``names`` from THIS (dashboard) process's environment
    into ``extra_env`` for the launched container — the LEGACY host-passthrough
    credential mechanism (pre-vault). A var not present in the dashboard's own
    environment used to drop SILENTLY (``if val is not None`` with no else), so
    a workspace could ship with no GitHub auth and nothing said — which is
    exactly what cost the debugging time on anchore/enterprise-ng (the dashboard
    process simply never had GH_TOKEN). Log the miss by NAME (never the value)
    so it is diagnosable. The durable fix is migrating the workspace to the
    vault ``credential:<key>`` scheme (agents/…/vault_credential.py), which is
    preflight-protected and audited; this only ends the silence for whatever
    still rides env_from_host.
    """
    for name in names or ():
        val = os.environ.get(name)
        if val is not None:
            extra_env[name] = val
        else:
            logger.warning(
                "env_from_host: %r requested by %s but not set in the dashboard "
                "process environment — launched WITHOUT it (legacy host-"
                "passthrough; migrate this workspace to the vault credential "
                "scheme).", name, context,
            )


def _run_project_session_start(job: LifecycleJob, writer: SessionLifecycleStateWriter) -> None:
    """Worker-thread implementation of workspace prepare/launch/tmux/register."""
    tmux_name = job.tmux_name
    project_id = str(job.config["project_id"])
    primer_url = job.config.get("primer_url")
    attempt = int(job.config.get("attempt", 1))
    loop = job.config.get("event_loop")
    if loop is not None and not isinstance(loop, asyncio.AbstractEventLoop):
        loop = None

    try:
        proj = workspace_settings.get_workspace(project_id)
    except Exception as exc:
        writer.fail(
            tmux_name,
            phase="requested",
            reason=f"{type(exc).__name__}: {exc}",
            attempt=attempt,
        )
        return

    # Per-launch harness override (request body → job config), falling back to
    # the workspace default. Resolve ONCE so every downstream write — session
    # row, monitor registration, first-message injection, launch, trace —
    # records the SAME harness. A mismatch makes the viewer pick the wrong
    # parser: a codex rollout read by the claude parser renders as an empty
    # session even though turns/topic still update.
    resolved_harness = job.config.get("harness") or proj.harness or "claude"

    phase = "start"
    try:
        phase = "preparing"
        writer.set_state(tmux_name, "preparing")
        prepare_deadline = time.monotonic() + _LIFECYCLE_PREPARING_TIMEOUT_S

        def _on_repo_prepared(repo_index: int, total_repos: int, repo_name: str):
            logger.info(
                "session_lifecycle: preparing tmux=%s repo_index=%d total=%d current_repo=%s",
                tmux_name, repo_index, total_repos, repo_name,
            )
            if loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    session_monitor.update_phase(
                        tmux_name,
                        progress={
                            "repo_index": repo_index,
                            "total": total_repos,
                            "current_repo": repo_name,
                        },
                    ),
                    loop,
                )

        project_mounts = prepare_session_mounts(
            proj,
            tmux_name,
            refresh_existing_worktree=True,
            progress_callback=_on_repo_prepared,
            git_timeout=_LIFECYCLE_PREPARING_TIMEOUT_S,
        )
        _remaining_step_timeout(prepare_deadline, "preparing")
        project_mounts.update(workspace_settings.artifact_mounts(proj))

        # Settings-built image freshness. Entered ONLY when this workspace
        # launches its own <org>/<workspace-id> image AND the resolved
        # dockerfile is stale or the image is absent here — the background
        # builder normally builds within seconds of a provision write, so
        # this stage covers the race window and the fresh-machine case.
        # A stale image never launches silently; a failed build fails the
        # launch with the builder's error.
        if proj.image == image_builder.derive_image_name(
                proj.graph_project, proj.id):
            staleness = image_builder.image_staleness(
                proj.graph_project, proj.id)
            if staleness:
                phase = "building_image"
                writer.set_state(tmux_name, "building_image")
                logger.info(
                    "session_lifecycle: building_image tmux=%s image=%s (%s)",
                    tmux_name, proj.image, staleness)
                build = image_builder.build_workspace(
                    proj.graph_project, proj.id,
                    repo_root=_REPO_ROOT, force=True)
                if build is None or build.action != "built":
                    raise RuntimeError(
                        f"workspace image {proj.image} could not be built: "
                        f"{build.detail if build else 'no dockerfile resolved'}"
                        f" (full build status: autonomy.workspace.image-build"
                        f" row {proj.graph_project}:{proj.id}, machine store)")

        phase = "launching"
        writer.set_state(tmux_name, "launching")
        launch_deadline = time.monotonic() + _LIFECYCLE_LAUNCHING_TIMEOUT_S
        meta: dict = {
            "tmux_session": tmux_name,
            "project": proj.id,
            # Canonical org key — the launcher stamps the session token from
            # metadata["org"] alone and refuses to mint without it.
            "org": proj.graph_project,
        }
        if proj.default_tags:
            meta["graph_tags"] = list(proj.default_tags)
        extra_env: dict[str, str] = dict(proj.env) if proj.env else {}
        _apply_env_from_host(
            proj.env_from_host, extra_env,
            context=f"workspace {getattr(proj, 'id', None) or proj.graph_project}",
        )
        extra_env = extra_env or None

        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        run_dir = DATA_ROOT / "agent-runs" / f"{tmux_name}-{ts}"
        run_dir.mkdir(parents=True, exist_ok=True)
        primer_path = run_dir / ".claude_md"
        primer_path.write_text(render_workspace_primer(proj))
        startup_script = workspace_settings.materialize_startup_script(
            proj, run_dir)
        working_dir = proj.working_dir or "/workspace/repo"

        cmd_str = launch_session(
            session_type="terminal",
            name=tmux_name,
            prompt=None,
            detach=False,
            image=proj.image,
            mounts=project_mounts or None,
            metadata=meta,
            harness=resolved_harness,
            model=job.config.get("model") or proj.model or None,
            extra_env=extra_env,
            output_dir=str(run_dir),
            global_claude_md=primer_path,
            startup_script=startup_script,
            needs_nested_docker=proj.needs_nested_docker,
            runtime=proj.session_runtime,
            working_dir=working_dir,
            network_host=proj.network_host,
            capabilities=proj.capabilities,
        )
        _remaining_step_timeout(launch_deadline, "launching")
        if not cmd_str:
            raise RuntimeError(f"launch_session failed for project '{proj.id}'")

        tmux_cmd = [
            "tmux", "new-session", "-d", "-s", tmux_name, "-x", "120",
            "-y", "40", cmd_str,
        ]
        result = subprocess.run(
            tmux_cmd,
            env={**os.environ, "TERM": "xterm-256color"},
            capture_output=True,
            timeout=_remaining_step_timeout(launch_deadline, "launching"),
        )
        if result.returncode != 0:
            stderr = result.stderr.decode().strip()
            raise RuntimeError(f"tmux creation failed: {stderr}")
        for opt, val in (
            ("set-clipboard", "on"),
            ("mouse", "on"),
            ("allow-passthrough", "on"),
        ):
            subprocess.run(
                ["tmux", "set-option", "-t", tmux_name, opt, val],
                capture_output=True,
                timeout=_remaining_step_timeout(launch_deadline, "launching"),
            )

        # Fail fast if docker produced no container (bad mount / OCI init
        # error): the tmux spawn alone proves nothing.
        _verify_container_started(
            tmux_name=tmux_name,
            deadline=time.monotonic() + 20,
        )

        phase = "setup"
        writer.set_state(tmux_name, "setup")
        _register_project_session_from_worker(
            tmux_name=tmux_name,
            proj=proj,
            run_dir=run_dir,
            primer_url=primer_url if isinstance(primer_url, str) else None,
            loop=loop,
            harness=resolved_harness,
        )
        setup_deadline = time.monotonic() + _LIFECYCLE_SETUP_TIMEOUT_S
        _wait_for_setup_complete(
            tmux_name=tmux_name,
            run_dir=run_dir,
            startup_script=startup_script,
            deadline=setup_deadline,
        )

        phase = "waiting_ready"
        writer.set_state(tmux_name, "waiting_ready")
        wait_deadline = time.monotonic() + _LIFECYCLE_WAITING_READY_TIMEOUT_S
        _wait_for_prompt(
            tmux_name=tmux_name,
            deadline=wait_deadline,
            writer=writer,
        )

        writer.set_state(tmux_name, "composer_ready")
        first_message, used_primer = _render_worker_first_message(
            tmux_name=tmux_name,
            proj=proj,
            primer_url=primer_url if isinstance(primer_url, str) else None,
        )
        if first_message:
            phase = "injecting"
            writer.set_state(tmux_name, "injecting")
            inject_deadline = time.monotonic() + _LIFECYCLE_INJECTING_TIMEOUT_S
            _inject_echo_verified(
                tmux_name=tmux_name,
                message=first_message,
                harness_name=resolved_harness,
                deadline=inject_deadline,
            )
            logger.info(
                "session_lifecycle: first message injected tmux=%s len=%d primer=%s harness=%s",
                tmux_name,
                len(first_message),
                used_primer,
                resolved_harness,
            )
        writer.set_state(tmux_name, "running")
    except TimeoutError as exc:
        failed_phase = str(exc).split()[0]
        _fail_lifecycle_start_with_cleanup(
            writer=writer,
            tmux_name=tmux_name,
            phase=failed_phase,
            reason=str(exc),
            attempt=attempt,
            loop=loop,
        )
    except (WorkspaceError, workspace_settings.WorkspaceMountError, RuntimeError, subprocess.SubprocessError) as exc:
        _fail_lifecycle_start_with_cleanup(
            writer=writer,
            tmux_name=tmux_name,
            phase=phase,
            reason=f"{type(exc).__name__}: {exc}",
            attempt=attempt,
            loop=loop,
        )
    except Exception as exc:
        _fail_lifecycle_start_with_cleanup(
            writer=writer,
            tmux_name=tmux_name,
            phase=phase,
            reason=f"{type(exc).__name__}: {exc}",
            attempt=attempt,
            loop=loop,
        )


def _register_resumed_session_from_worker(
    *,
    tmux_name: str,
    cfg: dict,
    loop: asyncio.AbstractEventLoop | None,
) -> None:
    """(Re-)register a relaunched session for tailing, without state writes."""
    jsonl_path = Path(cfg["jsonl_path"])
    if cfg.get("revived"):
        coro = session_monitor.register_revived(
            tmux_name=tmux_name,
            jsonl_path=jsonl_path,
        )
    else:
        coro = session_monitor.register(
            tmux_name=tmux_name,
            session_type=cfg.get("session_type") or "container",
            project=cfg.get("register_project") or "autonomy",
            jsonl_path=jsonl_path,
            session_uuid=cfg.get("resume_uuid"),
        )
    if loop is not None and loop.is_running():
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        fut.result(timeout=_LIFECYCLE_REGISTER_TIMEOUT_S)
    else:
        # Test / early-wiring path: the durable row already exists (revive or
        # register_pending ran in the API handler); monitor watch setup is
        # the startup wiring's responsibility.
        coro.close()


def _render_host_orientation(
    *, tmux_name: str, resumed: bool = False,
) -> str | None:
    """Render the personal Settings-backed welcome for a native host session."""
    from tools.dashboard.session_orientation import render_orientation

    return render_orientation(
        tmux_name=tmux_name,
        workspace_id="host",
        workspace_name="host",
        org="personal",
        resumed=resumed,
    )


def _render_resume_message(*, tmux_name: str, cfg: dict) -> str | None:
    """Resume-appropriate orientation (continue, don't re-orient as fresh)."""
    from tools.dashboard.session_orientation import render_orientation

    is_host = cfg.get("kind") == "host"
    try:
        if is_host:
            return _render_host_orientation(tmux_name=tmux_name, resumed=True)
        return render_orientation(
            tmux_name=tmux_name,
            workspace_id=cfg.get("project_id") or "",
            workspace_name=cfg.get("workspace_name") or "default",
            org=cfg.get("org") or "autonomy",
            resumed=True,
        )
    except Exception:
        logger.warning(
            "session_lifecycle: resume orientation render failed tmux=%s",
            tmux_name, exc_info=True,
        )
        return None


def _run_session_resume_start(job: LifecycleJob, writer: SessionLifecycleStateWriter) -> None:
    """Worker-thread relaunch of an existing session (container or host).

    The API handler has already resolved identity (tmux_name, jsonl,
    harness, model), revived/inserted the row, armed the FSM, and built the
    host command when applicable — everything blocking runs here.
    """
    tmux_name = job.tmux_name
    cfg = job.config
    kind = cfg.get("kind") or "container"
    attempt = int(cfg.get("attempt", 1))
    loop = cfg.get("event_loop")
    if loop is not None and not isinstance(loop, asyncio.AbstractEventLoop):
        loop = None

    phase = "start"
    try:
        run_dir = Path(cfg["output_dir"]) if cfg.get("output_dir") else None
        startup_script: Path | None = None

        if kind == "project":
            phase = "preparing"
            writer.set_state(tmux_name, "preparing")
            prepare_deadline = time.monotonic() + _LIFECYCLE_PREPARING_TIMEOUT_S
            proj = workspace_settings.get_workspace(cfg["project_id"])
            mounts = prepare_session_mounts(
                proj,
                tmux_name,
                refresh_existing_worktree=False,
                git_timeout=_LIFECYCLE_PREPARING_TIMEOUT_S,
            )
            _remaining_step_timeout(prepare_deadline, "preparing")
            mounts.update(workspace_settings.artifact_mounts(proj))
            # Same freshness gate as the primary start path: a resume
            # relaunches the container, so a stale Settings-built image
            # is rebuilt here too, under its own stage and budget.
            if proj.image == image_builder.derive_image_name(
                    proj.graph_project, proj.id):
                staleness = image_builder.image_staleness(
                    proj.graph_project, proj.id)
                if staleness:
                    phase = "building_image"
                    writer.set_state(tmux_name, "building_image")
                    logger.info(
                        "session_lifecycle: building_image tmux=%s image=%s (%s)",
                        tmux_name, proj.image, staleness)
                    build = image_builder.build_workspace(
                        proj.graph_project, proj.id,
                        repo_root=_REPO_ROOT, force=True)
                    if build is None or build.action != "built":
                        raise RuntimeError(
                            f"workspace image {proj.image} could not be "
                            f"built: "
                            f"{build.detail if build else 'no dockerfile resolved'}"
                            f" (full build status: autonomy.workspace."
                            f"image-build row {proj.graph_project}:{proj.id},"
                            f" machine store)")
        else:
            proj = None
            mounts = None

        phase = "launching"
        writer.set_state(tmux_name, "launching")
        launch_deadline = time.monotonic() + _LIFECYCLE_LAUNCHING_TIMEOUT_S

        if kind == "host":
            cmd_str = cfg["host_cmd"]
        elif kind == "project":
            meta: dict = {
                "tmux_session": tmux_name,
                "project": proj.id,
                # Canonical org key — the launcher stamps the session token from
                # metadata["org"] alone and refuses to mint without it.
                "org": proj.graph_project,
            }
            if proj.default_tags:
                meta["graph_tags"] = list(proj.default_tags)
            extra_env: dict[str, str] = dict(proj.env) if proj.env else {}
            _apply_env_from_host(
                proj.env_from_host, extra_env,
                context=f"workspace {getattr(proj, 'id', None) or proj.graph_project}",
            )
            run_dir.mkdir(parents=True, exist_ok=True)
            primer_path = run_dir / ".claude_md"
            primer_path.write_text(render_workspace_primer(proj))
            startup_script = workspace_settings.materialize_startup_script(
                proj, run_dir)
            cmd_str = launch_session(
                session_type="terminal",
                name=tmux_name,
                prompt=None,
                detach=False,
                image=proj.image,
                mounts=mounts or None,
                metadata=meta,
                harness=cfg.get("harness"),
                extra_env=extra_env or None,
                global_claude_md=primer_path,
                startup_script=startup_script,
                needs_nested_docker=proj.needs_nested_docker,
                runtime=proj.session_runtime,
                working_dir=proj.working_dir or "/workspace/repo",
                output_dir=str(run_dir),
                model=cfg.get("model"),
                resume_uuid=cfg["resume_uuid"],
                network_host=proj.network_host,
                capabilities=proj.capabilities,
            )
        else:
            cmd_str = launch_session(
                session_type="terminal",
                name=tmux_name,
                prompt=None,
                detach=False,
                image="autonomy-session-platform",
                metadata={"tmux_session": tmux_name, "org": "autonomy"},
                harness=cfg.get("harness"),
                output_dir=str(run_dir),
                model=cfg.get("model"),
                resume_uuid=cfg["resume_uuid"],
                global_claude_md=_REPO_ROOT / "agents/shared/terminal/CLAUDE.md",
            )
        _remaining_step_timeout(launch_deadline, "launching")
        if not cmd_str:
            raise RuntimeError(f"failed to build resume command for '{tmux_name}'")

        # Resume reuses the original run_dir: leftover markers from the
        # prior boot would read as instantly-completed setup. Clear before
        # the relaunched entrypoint rewrites them.
        if run_dir is not None:
            for stale in (".setup_phase", ".setup-exit"):
                try:
                    (run_dir / stale).unlink()
                except FileNotFoundError:
                    pass
                except Exception:
                    logger.debug(
                        "session_lifecycle: stale %s clear failed for %s",
                        stale, tmux_name, exc_info=True,
                    )

        tmux_cmd = [
            "tmux", "new-session", "-d", "-s", tmux_name, "-x", "120", "-y", "40",
        ]
        if kind == "host":
            # Host sessions are forked by the HOST tmux server, so every
            # path in the command must be a HOST path. A containerized
            # dashboard builds them from its own view (/app…) — proven
            # fatal 2026-08-31: the host has no /app, the command dies
            # instantly and the launch times out. _host_form() is the
            # identity when running natively.
            tmux_cmd += ["-c", _host_form(str(_REPO_ROOT))]
            cmd_str = _host_form(cmd_str)
            # tmux seeds a new session's environment from the CLIENT. From
            # a containerized dashboard that's the container's env, whose
            # bare PATH has no claude — proven 2026-08-31 (trial probe:
            # NO-CLAUDE, PATH=/usr/local/bin:…). A login shell rebuilds the
            # operator's real environment from the HOST's own profile, so
            # the host environment always comes from the host, never from
            # whichever client asked for the session.
            cmd_str = "bash -lc " + shlex.quote(cmd_str)
        tmux_cmd.append(cmd_str)
        result = subprocess.run(
            tmux_cmd,
            env={**os.environ, "TERM": "xterm-256color"},
            capture_output=True,
            timeout=_remaining_step_timeout(launch_deadline, "launching"),
        )
        if result.returncode != 0:
            stderr = result.stderr.decode().strip()
            raise RuntimeError(f"tmux creation failed: {stderr}")
        for opt, val in (
            ("set-clipboard", "on"),
            ("mouse", "on"),
            ("allow-passthrough", "on"),
        ):
            subprocess.run(
                ["tmux", "set-option", "-t", tmux_name, opt, val],
                capture_output=True,
                timeout=_remaining_step_timeout(launch_deadline, "launching"),
            )
        if kind != "host":
            # Fail fast if docker produced no container (bad mount / OCI init
            # error): the tmux spawn alone proves nothing.
            _verify_container_started(
                tmux_name=tmux_name,
                deadline=time.monotonic() + 20,
            )

        phase = "setup"
        writer.set_state(tmux_name, "setup")
        _register_resumed_session_from_worker(tmux_name=tmux_name, cfg=cfg, loop=loop)
        if startup_script is not None and run_dir is not None:
            _wait_for_setup_complete(
                tmux_name=tmux_name,
                run_dir=run_dir,
                startup_script=startup_script,
                deadline=time.monotonic() + _LIFECYCLE_SETUP_TIMEOUT_S,
            )

        phase = "waiting_ready"
        writer.set_state(tmux_name, "waiting_ready")
        _wait_for_prompt(
            tmux_name=tmux_name,
            deadline=time.monotonic() + _LIFECYCLE_WAITING_READY_TIMEOUT_S,
            writer=writer,
        )

        writer.set_state(tmux_name, "composer_ready")
        first_message = _render_resume_message(tmux_name=tmux_name, cfg=cfg)
        if first_message:
            phase = "injecting"
            writer.set_state(tmux_name, "injecting")
            _inject_echo_verified(
                tmux_name=tmux_name,
                message=first_message,
                harness_name=cfg.get("harness"),
                deadline=time.monotonic() + _LIFECYCLE_INJECTING_TIMEOUT_S,
            )
            logger.info(
                "session_lifecycle: resume message injected tmux=%s len=%d harness=%s",
                tmux_name, len(first_message), cfg.get("harness") or "claude",
            )
        writer.set_state(tmux_name, "running")
    except TimeoutError as exc:
        _fail_lifecycle_start_with_cleanup(
            writer=writer,
            tmux_name=tmux_name,
            phase=str(exc).split()[0],
            reason=str(exc),
            attempt=attempt,
            loop=loop,
            cleanup_worktrees=False,
        )
    except Exception as exc:
        _fail_lifecycle_start_with_cleanup(
            writer=writer,
            tmux_name=tmux_name,
            phase=phase,
            reason=f"{type(exc).__name__}: {exc}",
            attempt=attempt,
            loop=loop,
            cleanup_worktrees=False,
        )


def _run_simple_session_start(job: LifecycleJob, writer: SessionLifecycleStateWriter) -> None:
    """Worker-thread launch for the non-workspace creates: generic
    ``autonomy-session-platform`` containers and host sessions.

    No worktree prep and no startup script — the step list is launching →
    register → waiting_ready → injecting → running. The host first message
    is also the fingerprint _watch_for_host_session_jsonl (spawned by the
    API handler) matches to link the JSONL, so injection stays
    echo-verified here like every other kind.
    """
    tmux_name = job.tmux_name
    cfg = job.config
    kind = cfg.get("kind") or "container"
    attempt = int(cfg.get("attempt", 1))
    loop = cfg.get("event_loop")
    if loop is not None and not isinstance(loop, asyncio.AbstractEventLoop):
        loop = None

    phase = "start"
    try:
        phase = "launching"
        writer.set_state(tmux_name, "launching")
        launch_deadline = time.monotonic() + _LIFECYCLE_LAUNCHING_TIMEOUT_S

        if kind == "host":
            cmd_str = cfg["host_cmd"]
            sess_dir = None
        else:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            run_dir = DATA_ROOT / "agent-runs" / f"{tmux_name}-{ts}"
            run_dir.mkdir(parents=True, exist_ok=True)
            sess_dir = run_dir / "sessions"
            cmd_str = launch_session(
                session_type="terminal",
                name=tmux_name,
                prompt=None,
                detach=False,
                image="autonomy-session-platform",
                metadata={"tmux_session": tmux_name, "org": "autonomy"},
                output_dir=str(run_dir),
                global_claude_md=_REPO_ROOT / "agents/shared/terminal/CLAUDE.md",
            )
        _remaining_step_timeout(launch_deadline, "launching")
        if not cmd_str:
            raise RuntimeError(f"failed to build launch command for '{tmux_name}'")

        tmux_cmd = [
            "tmux", "new-session", "-d", "-s", tmux_name, "-x", "120", "-y", "40",
        ]
        if kind == "host":
            # Host sessions are forked by the HOST tmux server, so every
            # path in the command must be a HOST path. A containerized
            # dashboard builds them from its own view (/app…) — proven
            # fatal 2026-08-31: the host has no /app, the command dies
            # instantly and the launch times out. _host_form() is the
            # identity when running natively.
            tmux_cmd += ["-c", _host_form(str(_REPO_ROOT))]
            cmd_str = _host_form(cmd_str)
            # tmux seeds a new session's environment from the CLIENT. From
            # a containerized dashboard that's the container's env, whose
            # bare PATH has no claude — proven 2026-08-31 (trial probe:
            # NO-CLAUDE, PATH=/usr/local/bin:…). A login shell rebuilds the
            # operator's real environment from the HOST's own profile, so
            # the host environment always comes from the host, never from
            # whichever client asked for the session.
            cmd_str = "bash -lc " + shlex.quote(cmd_str)
        tmux_cmd.append(cmd_str)
        result = subprocess.run(
            tmux_cmd,
            env={**os.environ, "TERM": "xterm-256color"},
            capture_output=True,
            timeout=_remaining_step_timeout(launch_deadline, "launching"),
        )
        if result.returncode != 0:
            stderr = result.stderr.decode().strip()
            raise RuntimeError(f"tmux creation failed: {stderr}")
        for opt, val in (
            ("set-clipboard", "on"),
            ("mouse", "on"),
            ("allow-passthrough", "on"),
        ):
            subprocess.run(
                ["tmux", "set-option", "-t", tmux_name, opt, val],
                capture_output=True,
                timeout=_remaining_step_timeout(launch_deadline, "launching"),
            )
        if kind != "host":
            # Fail fast if docker produced no container (bad mount / OCI init
            # error): the tmux spawn alone proves nothing.
            _verify_container_started(
                tmux_name=tmux_name,
                deadline=time.monotonic() + 20,
            )

        if loop is not None and loop.is_running():
            if kind == "host":
                coro = session_monitor.register(
                    tmux_name=tmux_name,
                    session_type="host",
                    project=cfg.get("register_project") or "",
                )
            else:
                coro = session_monitor.register(
                    tmux_name=tmux_name,
                    session_type="container",
                    project=cfg.get("register_project") or "autonomy",
                    jsonl_path=sess_dir,
                    seed_message="Starting..." if not cfg.get("first_message_is_primer") else "",
                )
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            fut.result(timeout=_LIFECYCLE_REGISTER_TIMEOUT_S)

        phase = "waiting_ready"
        writer.set_state(tmux_name, "waiting_ready")
        _wait_for_prompt(
            tmux_name=tmux_name,
            deadline=time.monotonic() + _LIFECYCLE_WAITING_READY_TIMEOUT_S,
            writer=writer,
        )

        writer.set_state(tmux_name, "composer_ready")
        first_message = cfg.get("first_message") or ""
        if first_message:
            phase = "injecting"
            writer.set_state(tmux_name, "injecting")
            _inject_echo_verified(
                tmux_name=tmux_name,
                message=first_message,
                harness_name=cfg.get("harness"),
                deadline=time.monotonic() + _LIFECYCLE_INJECTING_TIMEOUT_S,
            )
        writer.set_state(tmux_name, "running")
    except TimeoutError as exc:
        _fail_lifecycle_start_with_cleanup(
            writer=writer,
            tmux_name=tmux_name,
            phase=str(exc).split()[0],
            reason=str(exc),
            attempt=attempt,
            loop=loop,
            cleanup_worktrees=False,
        )
    except Exception as exc:
        _fail_lifecycle_start_with_cleanup(
            writer=writer,
            tmux_name=tmux_name,
            phase=phase,
            reason=f"{type(exc).__name__}: {exc}",
            attempt=attempt,
            loop=loop,
            cleanup_worktrees=False,
        )


def _run_session_start(job: LifecycleJob, writer: SessionLifecycleStateWriter) -> None:
    """Single worker entrypoint for every session launch.

    Fresh creates (workspace, generic container, host) and resumes all
    share the same FSM and writer; the config decides which step list runs.
    """
    if job.config.get("resume"):
        _run_session_resume_start(job, writer)
    elif job.config.get("kind") in ("container", "host"):
        _run_simple_session_start(job, writer)
    else:
        _run_project_session_start(job, writer)


def _run_session_stop(job: LifecycleJob, writer: SessionLifecycleStateWriter) -> None:
    """Worker-thread teardown: running → stopping → cleaning → dead.

    Worktrees are NEVER cleaned on stop — dead sessions keep them so the
    Worktrees merge flow can land their commits. Every step is bounded and
    idempotent; step errors degrade to warnings and the session still
    reaches ``dead`` (a half-stopped session must not stay ``running``).
    """
    from tools.dashboard import vault_release_sweeper

    tmux_name = job.tmux_name
    loop = job.config.get("event_loop")
    if loop is not None and not isinstance(loop, asyncio.AbstractEventLoop):
        loop = None

    writer.set_state(tmux_name, "stopping")
    errors: list[str] = []
    err = _run_cleanup_step(
        name="stop_container_tmux",
        timeout=_LIFECYCLE_STOP_TIMEOUT_S,
        func=lambda: _teardown_stop_container_and_tmux(tmux_name),
    )
    if err:
        errors.append(err)
    writer.set_state(tmux_name, "cleaning")
    for name, timeout, func in (
        ("remove_watchers", _LIFECYCLE_REMOVE_WATCHERS_TIMEOUT_S,
         lambda: _teardown_remove_watches(tmux_name)),
        # The launcher's timely vault teardown (auto-pw9bs.5): shred this
        # session's outstanding releases and reclaim its delivery dir the
        # moment the container/tmux is gone, instead of leaving 100% of
        # reclaim to the 30s periodic sweep. Running it as a stop step also
        # sequences it strictly BEFORE a restart's re-provision (restart =
        # this stop, then start, on one worker), closing the window where a
        # sweep tick reclaimed a directory the relaunch had just
        # re-provisioned and the launch preflight then refused. The periodic
        # sweep stays as the crash backstop; both paths are idempotent and
        # first-reason-wins, so racing is inert.
        ("vault_release_teardown", _LIFECYCLE_REMOVE_WATCHERS_TIMEOUT_S,
         lambda: vault_release_sweeper.on_session_end(tmux_name)),
        ("deregister", _LIFECYCLE_DEREGISTER_TIMEOUT_S,
         lambda: _teardown_deregister(tmux_name, loop)),
    ):
        err = _run_cleanup_step(name=name, timeout=timeout, func=func)
        if err:
            errors.append(err)
    writer.set_state(tmux_name, "dead")
    if errors:
        logger.warning(
            "session_lifecycle: stop finished with step issues tmux=%s errors=%s",
            tmux_name, "; ".join(errors),
        )
    # Completed Chat With sessions get ingested into the graph once dead.
    if tmux_name.startswith("chatwith-"):
        try:
            subprocess.run(
                ["graph", "sessions", "--all"],
                capture_output=True, timeout=30,
                cwd=str(Path(__file__).parents[2]),
            )
        except Exception:
            logger.debug("session_lifecycle: chatwith ingest failed", exc_info=True)


def _run_session_retry(job: LifecycleJob, writer: SessionLifecycleStateWriter) -> None:
    """Retry a failed launch: bounded cleanup of the failed attempt's
    leftovers (process + watches only — the row and worktrees stay), then
    the normal start step list with the attempt counter bumped."""
    tmux_name = job.tmux_name
    for name, timeout, func in (
        ("stop_container_tmux", _LIFECYCLE_STOP_TIMEOUT_S,
         lambda: _teardown_stop_container_and_tmux(tmux_name)),
        ("remove_watchers", _LIFECYCLE_REMOVE_WATCHERS_TIMEOUT_S,
         lambda: _teardown_remove_watches(tmux_name)),
    ):
        err = _run_cleanup_step(name=name, timeout=timeout, func=func)
        if err:
            logger.warning(
                "session_lifecycle: retry pre-clean issue tmux=%s error=%s",
                tmux_name, err,
            )
    _run_session_start(job, writer)


def _run_session_restart(job: LifecycleJob, writer: SessionLifecycleStateWriter) -> None:
    """Stop a live session, let teardown settle, then relaunch it.

    Restart is one lifecycle job so admission is atomic: a full queue cannot
    accept the stop half while dropping the relaunch half.  ``_run_session_stop``
    does not return until teardown has reached the durable ENDED state.  The
    additional settle window gives Docker/tmux/filesystem cleanup time to
    converge before the same session identity is reused.
    """
    tmux_name = job.tmux_name
    cfg = job.config
    loop = cfg.get("event_loop")
    if loop is not None and not isinstance(loop, asyncio.AbstractEventLoop):
        loop = None

    _run_session_stop(job, writer)
    row = dashboard_db.get_session(tmux_name)
    if not row or derive_lifecycle_state(row) != "ENDED":
        writer.fail(
            tmux_name,
            phase="restart",
            reason="session teardown did not reach the ended state",
            retryable=True,
            attempt=int(cfg.get("attempt", 1)),
        )
        return

    time.sleep(max(0.0, float(cfg.get("settle_seconds", 5.0))))
    dashboard_db.revive_session(tmux_name, file_offset=0)

    pending = session_monitor.register_pending(
        tmux_name,
        session_type=cfg.get("session_type") or "container",
        project=cfg.get("register_project") or "autonomy",
        harness=cfg.get("harness") or "claude",
    )
    if loop is not None and loop.is_running():
        fut = asyncio.run_coroutine_threadsafe(pending, loop)
        fut.result(timeout=_LIFECYCLE_REGISTER_TIMEOUT_S)
    else:
        # Unit-test / early-wiring path.  The start handler's first state
        # transition can legally move ENDED -> LAUNCHING.
        pending.close()

    _run_session_start(job, writer)


_SESSION_LIFECYCLE_WORKER = SessionLifecycleWorker()
_SESSION_LIFECYCLE_WORKER.register_handler("start", _run_session_start)
_SESSION_LIFECYCLE_WORKER.register_handler("stop", _run_session_stop)
_SESSION_LIFECYCLE_WORKER.register_handler("retry", _run_session_retry)
_SESSION_LIFECYCLE_WORKER.register_handler("restart", _run_session_restart)


async def _recover_stuck_lifecycle_rows() -> None:
    """Startup recovery (FSM contract, correctness addition 3).

    The lifecycle queue is process memory: a dashboard restart mid-launch
    loses the job and leaves the row frozen in a non-terminal
    startup_state with is_live=1 — the "stuck forever" class the June-18
    review predicted (40 restarts observed in a single 2-day log window).
    Sweep at boot, before requests arrive:

    - tmux session still exists → the process outlived the restart; the
      interrupted launch work is gone, but the session itself is healthy.
      Adopt as running: clear startup_state. (v1 — bead 4 re-enters the
      worker at the interrupted phase instead, and routes the write
      through the lifecycle writer once the legacy writers are deleted.)
    - tmux session gone → the launch died with the restart. Mark it
      failed(startup_recovery) so the operator gets a retryable failed
      card instead of an eternally-launching one.

    setup_failed rows are skipped: sticky, already operator-visible.
    """
    try:
        rows = dashboard_db.get_live_sessions()
    except Exception:
        logger.exception("startup_recovery: could not list live sessions")
        return
    for row in rows:
        state = row.get("startup_state")
        if not state or state == "setup_failed":
            continue
        tmux_name = row.get("tmux_name")
        if not tmux_name:
            continue
        try:
            if _tmux_session_exists(tmux_name):
                # Adopt as running through the lifecycle writer — the single
                # state writer — clearing startup_state and re-asserting
                # is_live. The activity poller re-derives idle/running on
                # its next pass.
                _SESSION_LIFECYCLE_WORKER.state_writer.set_state(tmux_name, "running")
                logger.info(
                    "startup_recovery: adopted %s (was %s, tmux alive)",
                    tmux_name, state,
                )
            else:
                SessionLifecycleStateWriter().fail(
                    tmux_name,
                    phase="startup_recovery",
                    reason=f"dashboard restarted mid-launch (was {state}); process gone",
                    retryable=True,
                )
                logger.warning(
                    "startup_recovery: failed %s (was %s, tmux gone)",
                    tmux_name, state,
                )
        except Exception:
            logger.exception("startup_recovery: sweep failed for %s", tmux_name)


async def api_session_create(request):
    """Create a new session (container, project, or host) and return its tmux name.

    POST /api/session/create
    Body: {
        "project": "enterprise-ng",   # optional — launches workspace container
        "type": "host" | "container", # optional — defaults to container
        "primer": "graph://...",      # optional — graph URL whose content is
                                      #            injected as first message
    }
    Returns: {"tmux_name": str, "label": str, "type": str}

    Semantics:
      • `project` set → validate config, register a requested row, and enqueue
        the provisioning/container lifecycle worker job.
      • `type == "host"` → start `claude --dangerously-skip-permissions` on
        the host, then watch for its JSONL to appear.
      • neither → default `autonomy-session-platform` container session.

    Workspace project sessions return immediately after the lifecycle job is
    registered; progress is delivered via startup_state/SSE.  Non-workspace
    container sessions still wait for monitor tracking on the legacy path.
    Host sessions return immediately — their JSONL is discovered
    asynchronously by `_watch_for_host_session_jsonl`.
    """
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    # auto-bpomi: throwaway phase-trace diagnostics — measure session-boot
    # slices for Bead B. Single grep target: 'phase-trace:'.
    _phase_t0 = time.monotonic()
    logger.info("phase-trace: enter  tmux=pending  dt_from_post_ms=0")

    session_type = body.get("type", "container")
    project_name = body.get("project")
    primer_url = body.get("primer")

    if session_type not in ("container", "host"):
        return JSONResponse(
            {"error": "type must be 'container' or 'host'"}, status_code=400,
        )
    if project_name and session_type == "host":
        return JSONResponse(
            {"error": "project cannot be combined with type='host'"},
            status_code=400,
        )

    # ── Generate unique tmux name ───────────────────────────────
    prefix = "host" if session_type == "host" else "auto"
    tmux_name = f"{prefix}-{time.strftime('%m%d-%H%M%S')}"
    if dashboard_db.session_exists(tmux_name):
        import random
        tmux_name = f"{prefix}-{time.strftime('%m%d-%H%M%S')}-{random.randint(10, 99)}"

    # Persistent, per-session startup trace (data/session-traces/<tmux>.jsonl).
    # ``_trace`` mirrors the scattered ``phase-trace:`` log lines into a durable,
    # structured, never-rotated per-session file so a launch can be reconstructed
    # exactly without grepping the rotating dashboard.log. dt_ms is elapsed since
    # request entry.
    def _trace(phase: str, **detail) -> None:
        session_trace.trace(
            tmux_name, phase,
            dt_ms=int((time.monotonic() - _phase_t0) * 1000),
            **detail,
        )
    _trace("enter", project=project_name, session_type=session_type, primer=bool(primer_url))

    # ── Build the command to run inside tmux ───────────────────
    proj = None
    host_project_folder: str | None = None
    if project_name:
        try:
            proj = workspace_settings.get_workspace(project_name)
        except KeyError:
            return JSONResponse(
                {"error": f"Unknown project '{project_name}'"}, status_code=400,
            )
        except workspace_settings.WorkspaceSettingsError as e:
            return JSONResponse(
                {"error": f"Project config error: {e}"}, status_code=500,
            )
        missing_artifacts = workspace_settings.validate_artifacts(proj)
        if missing_artifacts:
            first = missing_artifacts[0]
            message = workspace_settings.format_missing_artifact_error(first, proj)
            logger.warning(
                "api_session_create: missing required artifact  project=%s  name=%s  path=%s",
                proj.id, first.artifact.name, first.path,
            )
            return JSONResponse(
                {
                    "error": message,
                    "missing_artifacts": [
                        {
                            "name": m.artifact.name,
                            "description": m.artifact.description,
                            "help": m.artifact.help,
                            "expected_path": str(m.path),
                        }
                        for m in missing_artifacts
                    ],
                },
                status_code=400,
            )
        # auto-ja51w C3: register the session row IMMEDIATELY (before the
        # ~7-9s prepare_session_mounts + launch_session block) so the dashboard
        # can broadcast per-step progress via SSE during the otherwise dead-air
        # window. The normal register() call later, at the post-tmux-spawn
        # point, is idempotent — the duplicate INSERT no-ops and the regular
        # tail-state setup proceeds with the now-known jsonl_path and
        # resolution_dir.
        await session_monitor.register_pending(
            tmux_name,
            session_type="container",
            project=proj.id,
            harness=body.get("harness") or proj.harness or "claude",
        )
        job = LifecycleJob(
            "start",
            tmux_name,
            {
                "project_id": proj.id,
                "primer_url": primer_url,
                "attempt": 1,
                "event_loop": asyncio.get_running_loop(),
                # Optional per-launch model + harness overrides; each falls
                # back to the workspace config in the worker when absent.
                "model": body.get("model"),
                "harness": body.get("harness"),
            },
        )
        if not _SESSION_LIFECYCLE_WORKER.try_enqueue(job):
            reason = "session lifecycle queue is full"
            _SESSION_LIFECYCLE_WORKER.state_writer.fail(
                tmux_name,
                phase="requested",
                reason=reason,
                retryable=True,
                attempt=1,
            )
            logger.warning(
                "api_session_create: lifecycle queue full tmux=%s project=%s",
                tmux_name,
                proj.id,
            )
            return JSONResponse(
                {"error": reason, "tmux_name": tmux_name, "retryable": True},
                status_code=503,
            )

        _trace("queued", project=proj.id, harness=body.get("harness") or proj.harness or "claude")
        logger.info("phase-trace: response-ready  tmux=%s  dt_from_post_ms=%d  queued=1",
                    tmux_name, int((time.monotonic() - _phase_t0) * 1000))
        return JSONResponse({
            "tmux_name": tmux_name,
            "label": "",
            "type": "container",
            "pending": True,
        }, status_code=202)
    elif session_type == "host":
        model = _resolve_host_session_model()

        # Pick an account the same way a container session does. Without
        # this the command inherits whatever the dashboard process happens
        # to have, which in practice is the one credential sitting in
        # ~/.claude -- so the host terminal uses a single account forever
        # and dies with it when that account reaches its weekly ceiling,
        # while other installed accounts sit unused.
        from agents.session_launcher import _resolve_credentials
        host_creds = _resolve_credentials(prefer_alias=body.get("alias"))
        if host_creds is None or host_creds.get("type") != "token":
            return JSONResponse(
                {"error": "no Claude account is installed to start a host "
                          "terminal with — run `graph claude install`"},
                status_code=503,
            )

        # HOST form, not this process's view: the session runs on the host
        # and Claude derives the transcript dir from the HOST cwd. A
        # containerized dashboard's _REPO_ROOT is /app, which slugged to
        # "-app" and made the JSONL watcher stare at a directory no host
        # session ever writes (proven 2026-08-31: transcript landed in
        # -opt-autonomy-code, auto-link never fired).
        host_project_folder = _host_form(str(_REPO_ROOT)).replace("/", "-")
        await session_monitor.register_pending(
            tmux_name,
            session_type="host",
            project=host_project_folder,
            harness="claude",
            # Which account this terminal is burning. A container session
            # records this; a host one did not, so host sessions could never
            # be attributed to an account — and the account that ran out was
            # the one nothing could account for.
            harness_token=host_creds.get("harness_token"),
        )
        host_cmd = (
            _mint_host_session_token(tmux_name)
            + f"GRAPH_API={_own_dashboard_url()} "
            + f"CLAUDE_CODE_OAUTH_TOKEN={shlex.quote(host_creds['token'])} "
            f"BD_ACTOR=terminal:{tmux_name} AUTONOMY_SESSION={tmux_name} "
            f"claude --dangerously-skip-permissions --model {model}"
        )
        # The orientation message is both the agent's first turn AND the
        # unique fingerprint _watch_for_host_session_jsonl matches to link
        # the JSONL (a host Claude writes no JSONL until it gets input).
        # render returning None means the operator disabled orientation —
        # no injection, manual "Link Terminal" fallback covers it. Only a
        # render CRASH falls back to a unique fingerprint line.
        try:
            first_message = _render_host_orientation(tmux_name=tmux_name)
        except Exception:
            logger.warning(
                "api_session_create: host orientation render failed for %s; "
                "falling back to a unique fingerprint line",
                tmux_name, exc_info=True,
            )
            first_message = f"Session {tmux_name} started."

        job = LifecycleJob(
            "start",
            tmux_name,
            {
                "kind": "host",
                "attempt": 1,
                "host_cmd": host_cmd,
                "harness": "claude",
                "register_project": host_project_folder,
                "first_message": first_message,
                "event_loop": asyncio.get_running_loop(),
            },
        )
        if not _SESSION_LIFECYCLE_WORKER.try_enqueue(job):
            reason = "session lifecycle queue is full"
            _SESSION_LIFECYCLE_WORKER.state_writer.fail(
                tmux_name, phase="requested", reason=reason, retryable=True, attempt=1,
            )
            return JSONResponse(
                {"error": reason, "tmux_name": tmux_name, "retryable": True},
                status_code=503,
            )
        # HOST home, not this process's: host transcripts live under the
        # operator's ~/.claude/projects (mounted read-only at the identical
        # path in a containerized dashboard — see docker-compose.yml).
        projects_dir = (
            Path(_host_form(str(Path.home())))
            / ".claude" / "projects" / host_project_folder
        )
        asyncio.create_task(
            _watch_for_host_session_jsonl(projects_dir, tmux_name, timeout=120.0),
        )
        _trace("queued", kind="host")
        logger.info("phase-trace: response-ready  tmux=%s  dt_from_post_ms=%d  queued=1",
                    tmux_name, int((time.monotonic() - _phase_t0) * 1000))
        return JSONResponse({
            "tmux_name": tmux_name,
            "label": "",
            "type": "host",
            "pending": True,
        }, status_code=202)
    else:
        # Generic (non-workspace) container session on the default image.
        # Everything blocking (credential resolution, docker command build,
        # tmux spawn, composer wait, injection) runs on the lifecycle
        # worker; this handler resolves the primer/orientation text (fast,
        # async-friendly) and enqueues.
        await session_monitor.register_pending(
            tmux_name,
            session_type="container",
            project="autonomy",
            harness="claude",
        )
        first_message: str | None = None
        primer_error: str | None = None
        if primer_url:
            resolved = await _resolve_primer(primer_url)
            if resolved:
                first_message = resolved
            else:
                primer_error = f"Could not resolve primer {primer_url!r}, falling back to orientation"
                logger.warning("api_session_create: %s", primer_error)
        if first_message is None:
            from tools.dashboard.session_orientation import render_orientation
            try:
                first_message = render_orientation(
                    tmux_name=tmux_name,
                    workspace_id="",
                    workspace_name="default",
                    org="autonomy",
                )
            except Exception:
                logger.warning(
                    "api_session_create: orientation render failed for %s; "
                    "falling back to literal Hello",
                    tmux_name, exc_info=True,
                )
                first_message = "Hello"

        job = LifecycleJob(
            "start",
            tmux_name,
            {
                "kind": "container",
                "attempt": 1,
                "harness": "claude",
                "register_project": "autonomy",
                "first_message": first_message,
                "first_message_is_primer": bool(primer_url and not primer_error),
                "event_loop": asyncio.get_running_loop(),
            },
        )
        if not _SESSION_LIFECYCLE_WORKER.try_enqueue(job):
            reason = "session lifecycle queue is full"
            _SESSION_LIFECYCLE_WORKER.state_writer.fail(
                tmux_name, phase="requested", reason=reason, retryable=True, attempt=1,
            )
            return JSONResponse(
                {"error": reason, "tmux_name": tmux_name, "retryable": True},
                status_code=503,
            )
        _trace("queued", kind="container")
        logger.info("phase-trace: response-ready  tmux=%s  dt_from_post_ms=%d  queued=1",
                    tmux_name, int((time.monotonic() - _phase_t0) * 1000))
        resp = {
            "tmux_name": tmux_name,
            "label": "",
            "type": "container",
            "pending": True,
        }
        if primer_error:
            resp["primer_warning"] = primer_error
        return JSONResponse(resp, status_code=202)


_OWN_DASHBOARD_URL: str | None = None


def _own_dashboard_url() -> str:
    """The URL a HOST-side process reaches THIS dashboard at.

    Derived, never configured: containerized, one self-inspect of our own
    port bindings yields the published host port — whatever the operator
    mapped, 8081 or anything else; natively the serving default holds.
    Stamped into host sessions' env (GRAPH_API) so their CLI talks to the
    dashboard that minted their token: with two dashboards up (the
    native→Compose interregnum) the CLI's bare-localhost fallback sent a
    trial-launched session to the NATIVE store and every call 401'd
    (proven 2026-08-31, session host-0831-042827).
    """
    global _OWN_DASHBOARD_URL
    if _OWN_DASHBOARD_URL is None:
        url = "https://localhost:8080"
        try:
            from agents import mount_plan
            cid = mount_plan._own_container_id()
            if cid:
                out = subprocess.run(
                    ["docker", "inspect", "--format",
                     "{{json .HostConfig.PortBindings}}", cid],
                    capture_output=True, text=True, timeout=15,
                )
                if out.returncode == 0 and out.stdout.strip():
                    bindings = json.loads(out.stdout) or {}
                    entries = bindings.get("8080/tcp") or []
                    host_port = (entries[0] or {}).get("HostPort") if entries else None
                    if host_port:
                        url = f"https://localhost:{host_port}"
        except Exception:
            logger.exception("_own_dashboard_url: self-inspect failed; using default")
        _OWN_DASHBOARD_URL = url
    return _OWN_DASHBOARD_URL


def _mint_host_session_token(tmux_name: str) -> str:
    """Mint an org-less local-operator session token for a host session and
    return the ``CROSSTALK_TOKEN=...`` shell prefix that delivers it.

    A host session IS a local operator — its session row is type ``host``, so
    :func:`_is_local_caller` classifies its token as ``LOCAL_SESSION`` with
    full authority — but it reaches the dashboard over HTTP with no bearer, so
    the authenticated-reader guards refuse it. Minting a bearer (org ``None``,
    the deliberate local value :func:`authenticate_session_request` requires
    for a host session) lets the host CLI authenticate exactly as a container
    does, granting it no org's scope in the process.
    """
    import secrets

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    auth_db.insert_token(token_hash, tmux_name, None)
    return f"CROSSTALK_TOKEN={shlex.quote(raw_token)} "


def _build_host_resume_cmd(
    *,
    tmux_name: str,
    harness: str,
    model: str | None,
    session_uuid: str,
) -> str:
    """Shell command that relaunches a host session's own harness CLI."""
    env_prefix = (
        _mint_host_session_token(tmux_name)
        + f"GRAPH_API={_own_dashboard_url()} "
        + f"BD_ACTOR=terminal:{tmux_name} AUTONOMY_SESSION={tmux_name} "
    )
    if harness == "codex":
        # session_uuid is the rollout filename stem; codex resume needs
        # the canonical UUID tail (same extraction the launcher uses).
        m = re.search(
            r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
            session_uuid,
        )
        codex_uuid = m.group(1) if m else session_uuid
        model_flag = f"--model {shlex.quote(model)} " if model else ""
        return (
            env_prefix
            + "codex --no-alt-screen --dangerously-bypass-approvals-and-sandbox "
            + model_flag
            + f"resume {shlex.quote(codex_uuid)}"
        )
    resolved_model = model or _resolve_host_session_model()
    return (
        env_prefix
        + "claude --dangerously-skip-permissions "
        + f"--model {shlex.quote(resolved_model)} --resume {shlex.quote(session_uuid)}"
    )


def _build_session_relaunch_config(
    row: dict,
    *,
    attempt: int,
    event_loop: asyncio.AbstractEventLoop,
) -> tuple[dict | None, str | None]:
    """Rebuild a safe launch config from a durable session row.

    Retry and restart deliberately share this path.  A row with a transcript
    resumes that exact harness/session; a pre-transcript workspace launch is
    recreated from its workspace config.  If a recorded transcript vanished,
    refuse to guess because a fresh launch could strand or overwrite the
    session's existing worktree history.
    """
    tmux_name = row["tmux_name"]
    harness = row.get("harness") or "claude"
    model = row.get("model") or None
    session_uuid = row.get("session_uuid") or ""
    jsonl_path = row.get("jsonl_path") or ""
    session_type = "host" if row.get("type") == "host" else "container"
    project = row.get("project") or ""

    proj = None
    if session_type == "container" and project:
        try:
            proj = workspace_settings.get_workspace(project)
        except (KeyError, workspace_settings.WorkspaceSettingsError):
            proj = None

    if session_uuid and jsonl_path and not Path(jsonl_path).exists():
        return None, (
            f"session '{tmux_name}' has a recorded transcript that no longer "
            f"exists on disk ({jsonl_path}); cannot rebuild a launch config safely"
        )

    if session_uuid and jsonl_path:
        if session_type == "host":
            kind = "host"
            output_dir = None
            host_cmd = _build_host_resume_cmd(
                tmux_name=tmux_name,
                harness=harness,
                model=model,
                session_uuid=session_uuid,
            )
        else:
            kind = "project" if proj is not None else "container"
            host_cmd = None
            output_root = Path(jsonl_path)
            while output_root.parent != output_root and output_root.name != "sessions":
                output_root = output_root.parent
            output_dir = str(output_root.parent)
        return {
            "resume": True,
            "kind": kind,
            "attempt": attempt,
            "project_id": proj.id if (kind == "project" and proj is not None) else None,
            "workspace_name": proj.name if (kind == "project" and proj is not None) else None,
            "org": proj.graph_project if (kind == "project" and proj is not None) else "autonomy",
            "resume_uuid": session_uuid,
            "output_dir": output_dir,
            "jsonl_path": jsonl_path,
            "harness": harness,
            "model": model,
            "revived": True,
            "host_cmd": host_cmd,
            "session_type": session_type,
            "register_project": project or "autonomy",
            "event_loop": event_loop,
        }, None

    if proj is not None:
        return {
            "project_id": proj.id,
            "primer_url": None,
            "attempt": attempt,
            "event_loop": event_loop,
            "session_type": session_type,
            "register_project": project,
            "harness": harness,
        }, None

    if session_type == "host":
        env_prefix = f"BD_ACTOR=terminal:{tmux_name} AUTONOMY_SESSION={tmux_name} "
        return {
            "kind": "host",
            "attempt": attempt,
            "host_cmd": (
                env_prefix
                + "claude --dangerously-skip-permissions "
                + f"--model {_resolve_host_session_model()}"
            ),
            "harness": "claude",
            "register_project": project or str(_REPO_ROOT).replace("/", "-"),
            "first_message": _render_host_orientation(tmux_name=tmux_name),
            "event_loop": event_loop,
            "session_type": session_type,
        }, None

    return {
        "kind": "container",
        "attempt": attempt,
        "harness": harness,
        "register_project": project or "autonomy",
        "first_message": None,
        "event_loop": event_loop,
        "session_type": session_type,
    }, None


async def api_session_retry(request):
    """Relaunch a failed session through the lifecycle worker.

    POST /api/session/{tmux_name}/retry
    Returns 202 {"tmux_name", "status": "retrying"} once the retry job is
    queued. Only rows in the failed terminal state are retryable. The
    launch config is rebuilt from the durable row: rows with a
    session_uuid + existing JSONL re-resume; workspace rows re-create;
    host rows rebuild the host command.
    """
    tmux_name = request.path_params["tmux_name"]
    row = dashboard_db.get_session(tmux_name)
    if not row:
        return JSONResponse({"error": f"unknown session '{tmux_name}'"}, status_code=404)
    failed = (
        row.get("activity_state") == "failed"
        or row.get("startup_state") == "setup_failed"
    )
    if not failed:
        return JSONResponse(
            {"error": f"session '{tmux_name}' is not in a failed state"},
            status_code=409,
        )

    try:
        detail = json.loads(row.get("lifecycle_detail") or "{}")
    except (TypeError, json.JSONDecodeError):
        detail = {}
    attempt = int(detail.get("attempt", 1) or 1) + 1
    config, config_error = _build_session_relaunch_config(
        row,
        attempt=attempt,
        event_loop=asyncio.get_running_loop(),
    )
    if config_error:
        return JSONResponse({"error": config_error}, status_code=409)
    assert config is not None

    # Reset the row for the fresh attempt (is_live, harness_state,
    # startup_state) and re-enter the FSM. ORDER IS LOAD-BEARING: revive
    # (is_live=1) must precede the arm — the pane-poller reconciles its
    # armed set against live rows every cycle, and arming a still-dead row
    # would let that reconcile silently drop the watch before the worker's
    # first transition re-asserts liveness.
    dashboard_db.revive_session(tmux_name, file_offset=0)
    await session_monitor.register_pending(
        tmux_name,
        session_type=config.get("session_type") or "container",
        project=config.get("register_project") or "autonomy",
        harness=config.get("harness") or "claude",
    )
    if not _SESSION_LIFECYCLE_WORKER.try_enqueue(LifecycleJob("retry", tmux_name, config)):
        reason = "session lifecycle queue is full"
        _SESSION_LIFECYCLE_WORKER.state_writer.fail(
            tmux_name, phase="requested", reason=reason, retryable=True, attempt=attempt,
        )
        return JSONResponse(
            {"error": reason, "tmux_name": tmux_name, "retryable": True},
            status_code=503,
        )
    logger.info(
        "api_session_retry: queued tmux=%s attempt=%d resume=%s",
        tmux_name, attempt, bool(config.get("resume")),
    )
    return JSONResponse(
        {"tmux_name": tmux_name, "status": "retrying", "attempt": attempt},
        status_code=202,
    )


async def api_session_restart(request):
    """Gracefully stop and resume a live session as one lifecycle job.

    POST /api/session/{tmux_name}/restart

    The worker owns the complete close -> ENDED -> five-second settle ->
    relaunch sequence.  The request returns only after that indivisible job is
    admitted, so a saturated queue cannot turn Restart into an accidental
    Close.
    """
    tmux_name = request.path_params["tmux_name"]
    row = dashboard_db.get_session(tmux_name)
    if not row:
        return JSONResponse({"error": f"unknown session '{tmux_name}'"}, status_code=404)

    state = derive_lifecycle_state(row)
    if state in ("ENDED", "FAILED"):
        return JSONResponse(
            {"error": f"session '{tmux_name}' is already closed"},
            status_code=409,
        )
    if state == "STOPPING":
        return JSONResponse(
            {"error": f"session '{tmux_name}' is already stopping"},
            status_code=409,
        )

    try:
        detail = json.loads(row.get("lifecycle_detail") or "{}")
    except (TypeError, json.JSONDecodeError):
        detail = {}
    attempt = int(detail.get("attempt", 1) or 1) + 1
    config, config_error = _build_session_relaunch_config(
        row,
        attempt=attempt,
        event_loop=asyncio.get_running_loop(),
    )
    if config_error:
        return JSONResponse({"error": config_error}, status_code=409)
    assert config is not None
    config["settle_seconds"] = 5.0

    if not _SESSION_LIFECYCLE_WORKER.try_enqueue(
        LifecycleJob("restart", tmux_name, config),
    ):
        return JSONResponse(
            {
                "error": "session lifecycle queue is full",
                "tmux_name": tmux_name,
                "retryable": True,
            },
            status_code=503,
        )

    logger.info(
        "api_session_restart: queued tmux=%s attempt=%d settle_seconds=5",
        tmux_name,
        attempt,
    )
    return JSONResponse(
        {
            "tmux_name": tmux_name,
            "status": "restarting",
            "attempt": attempt,
        },
        status_code=202,
    )


async def api_session_resume(request):
    """Resume an existing Claude session.

    POST /api/session/resume
    Body: {"source_id": "abc123"} OR {"session_uuid": "uuid", "file_path": "/path/to.jsonl"}
    Returns: {"tmux_name": str, "label": str, "type": str}
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    source_id = body.get("source_id")
    session_uuid = body.get("session_uuid")
    file_path = body.get("file_path")
    session_type = None  # "container" or "host"

    # ── Resolve from graph source metadata if source_id provided ──
    if source_id:
        try:
            located = graph_ops.locate_source_org(source_id)
        except Exception:
            return JSONResponse({"error": "Failed to query graph.db"}, status_code=500)

        if located is None:
            return JSONResponse({"error": f"Source '{source_id}' not found"}, status_code=404)

        owner_org = located["org"]
        principal = api_auth.principal_from_request(request)
        if principal.org_bound and principal.org != owner_org:
            logger.warning(
                "api_authz_refused action=session.resume caller=%s "
                "caller_org=%s owner_org=%s",
                principal.subject,
                principal.org,
                owner_org,
            )
            # Remote callers must not learn that a source exists in another
            # org.  The audit is explicit; the network response stays opaque.
            return JSONResponse({"error": f"Source '{source_id}' not found"}, status_code=404)

        if located.get("type") != "session":
            return JSONResponse(
                {"error": f"Source '{source_id}' is type '{located.get('type')}', not a session"},
                status_code=400,
            )

        try:
            # Location is a server-side fact.  Resolve content only after the
            # caller is authorized, in the located owner org, never through
            # the ambient X-Graph-Org selection.
            # ``located.id`` is already the unambiguous full ID.  Use the
            # ordinary exact source reader for the authorized content fetch;
            # repeating prefix/session-UUID resolution here adds a second
            # lookup contract after location has already been decided.
            src = graph_ops.get_source(
                located.get("id") or source_id,
                org=owner_org,
                peers=[],
            )
        except Exception:
            return JSONResponse({"error": "Failed to query graph.db"}, status_code=500)

        if src is None:
            return JSONResponse({"error": f"Source '{source_id}' not found"}, status_code=404)
        if src.get("type") != "session":
            return JSONResponse(
                {"error": f"Source '{source_id}' is type '{src.get('type')}', not a session"},
                status_code=400,
            )

        file_path = src.get("file_path", "")
        meta = {}
        if src.get("metadata"):
            try:
                meta = json.loads(src["metadata"])
            except Exception:
                pass
        session_uuid = meta.get("session_uuid", "")

    # ── Validate required fields ──
    if not session_uuid:
        return JSONResponse({"error": "session_uuid is required (provide source_id or session_uuid)"}, status_code=400)
    if not file_path:
        return JSONResponse({"error": "file_path is required (provide source_id or file_path)"}, status_code=400)

    # ── Verify JSONL file exists on disk ──
    # The stored file_path may be in any historical frame (/data, the old
    # host path, /app/data). Re-root it onto THIS process's data frame by the
    # agent-runs anchor so the real file resolves regardless of who ingested
    # it (tools.data_paths.local_session_path; identity for host .claude
    # transcripts). 2026-08-31.
    from tools.data_paths import local_session_path
    file_path = local_session_path(file_path)
    jsonl_path = Path(file_path)
    if not jsonl_path.exists():
        return JSONResponse(
            {"error": f"JSONL file not found: {file_path}"},
            status_code=404,
        )

    # ── Determine session type ──
    agent_runs_dir = str(DATA_ROOT / "agent-runs")
    if session_type is None:
        if file_path.startswith(agent_runs_dir) or "/agent-runs/" in file_path:
            session_type = "container"
        else:
            session_type = "host"

    # ── Guard: reject if session is already active ──
    live_session = dashboard_db.find_live_session(
        session_uuid=session_uuid, file_path=file_path,
    )
    if live_session:
        return JSONResponse(
            {"error": f"Session is already active as '{live_session['tmux_name']}'"},
            status_code=409,
        )

    # ── Look up existing dead session in dashboard.db ──
    dead_session = dashboard_db.find_dead_session(
        session_uuid=session_uuid, file_path=file_path,
    )

    if dead_session and derive_lifecycle_state(dead_session) in ("ENDED", "FAILED"):
        # Reuse the original session identity
        tmux_name = dead_session["tmux_name"]
        label = dead_session.get("label", "")
    else:
        # No dead session found — derive name from graph metadata or generate one
        tmux_name = None
        label = ""

        # Try to get original container_name from graph source metadata
        if source_id:
            try:
                meta_str = src.get("metadata", "")
                meta_obj = json.loads(meta_str) if isinstance(meta_str, str) and meta_str else {}
                tmux_name = meta_obj.get("container_name", "")
            except Exception:
                pass

        if not tmux_name:
            tmux_name = f"resume-{time.strftime('%m%d-%H%M%S')}"

        if dashboard_db.session_exists(tmux_name):
            import random
            tmux_name = f"resume-{time.strftime('%m%d-%H%M%S')}-{random.randint(10, 99)}"

    # Preserve the ORIGINAL session's harness + model. A Codex session's
    # JSONL is a codex rollout that can ONLY be resumed by ``codex`` with its
    # own gpt model — relaunching it as the workspace/host default Claude
    # (the old behaviour) was broken: wrong CLI, wrong model. dashboard.db
    # records both on the session row; fall back to the graph source
    # metadata when there is no live db row to read from.
    resume_harness = (dead_session or {}).get("harness") or None
    resume_model = (dead_session or {}).get("model") or None
    if (not resume_harness or not resume_model) and source_id and src:
        try:
            _src_meta = json.loads(src.get("metadata") or "{}")
        except Exception:
            _src_meta = {}
        resume_harness = resume_harness or _src_meta.get("harness")
        resume_model = resume_model or _src_meta.get("model")
    resume_harness_recorded = bool(resume_harness)
    resume_harness = resume_harness or "claude"

    # Keep the session's own model. Only a Claude session falls back to the
    # host default — never hand Codex a Claude model id (which is exactly
    # what _resolve_host_session_model() would have returned).
    #
    # Never forward a placeholder id: "<synthetic>" comes from error /
    # local-command JSONL entries and was historically persisted by both the
    # dashboard tailer and graph ingestion. Booting `--model <synthetic>`
    # fails on a nonexistent model (auto-0709-092918, 2026-07-14), so a
    # placeholder falls through to the default like no model at all.
    if resume_model and resume_model.startswith("<"):
        resume_model = None
    model = resume_model
    if not model and resume_harness == "claude":
        model = _resolve_host_session_model()

    if session_type == "container":
        # Derive output_dir (the run dir) by walking up to the "sessions" parent.
        # Claude:  <run>/sessions/<uuid>/<file>.jsonl
        # Codex:   <run>/sessions/YYYY/MM/DD/<file>.jsonl
        _od = jsonl_path
        while _od.parent != _od and _od.name != "sessions":
            _od = _od.parent
        output_dir = str(_od.parent)

        # If this was a workspace (project-scoped) session, resume with the
        # same image, mounts, and env.  dashboard.db.project stores the
        # project id (e.g. "enterprise-ng") for workspace sessions.
        proj_for_resume = None
        dead_project = (dead_session or {}).get("project") if dead_session else None
        if dead_project:
            try:
                proj_for_resume = workspace_settings.get_workspace(dead_project)
            except (KeyError, workspace_settings.WorkspaceSettingsError):
                proj_for_resume = None
            if (proj_for_resume is not None and not resume_harness_recorded
                    and proj_for_resume.harness):
                resume_harness = proj_for_resume.harness
            # Only let the workspace model override a Claude session that has
            # no recorded model of its own; legacy rows without a recorded
            # harness inherit the workspace harness/model together.
            if (proj_for_resume is not None and proj_for_resume.model
                    and not model):
                model = proj_for_resume.model

        if proj_for_resume is not None:
            missing_artifacts = workspace_settings.validate_artifacts(proj_for_resume)
            if missing_artifacts:
                first = missing_artifacts[0]
                message = workspace_settings.format_missing_artifact_error(first, proj_for_resume)
                logger.warning(
                    "api_session_resume: missing required artifact  project=%s  name=%s  path=%s",
                    proj_for_resume.id, first.artifact.name, first.path,
                )
                return JSONResponse(
                    {
                        "error": message,
                        "missing_artifacts": [
                            {
                                "name": m.artifact.name,
                                "description": m.artifact.description,
                                "help": m.artifact.help,
                                "expected_path": str(m.path),
                            }
                            for m in missing_artifacts
                        ],
                    },
                    status_code=400,
                )
        # Everything blocking (git worktree prep, credential resolution,
        # docker command build, tmux spawn) runs on the lifecycle worker.
        kind = "project" if proj_for_resume is not None else "container"
        host_cmd = None
    else:
        # Host session: relaunch the SAME harness CLI on the host. Hardcoding
        # ``claude`` here meant resuming a host Codex session ran the wrong
        # CLI against a codex rollout it can't read.
        kind = "host"
        host_cmd = _build_host_resume_cmd(
            tmux_name=tmux_name,
            harness=resume_harness,
            model=model,
            session_uuid=session_uuid,
        )
        output_dir = None

    # ── Revive/seed the row, arm the FSM, enqueue the relaunch ──
    # Everything blocking (worktree prep, credential resolution, docker
    # command build, tmux spawn, setup/composer waits, resume-message
    # injection) runs on the lifecycle worker; this handler only writes the
    # row and returns 202. Progress reaches the UI via the worker's
    # transition broadcasts.
    if dead_session:
        # Revive the existing row: is_live=1, file_offset=0 for full
        # backfill, startup_state/harness_state reset for the fresh boot.
        dashboard_db.revive_session(tmux_name, file_offset=0)
    register_project = (
        proj_for_resume.id if (session_type == "container" and proj_for_resume is not None)
        else ((dead_session or {}).get("project")
              or (str(_REPO_ROOT).replace("/", "-") if session_type == "host" else "autonomy"))
    )
    await session_monitor.register_pending(
        tmux_name,
        session_type=session_type,
        project=register_project,
        harness=resume_harness,
    )

    job = LifecycleJob(
        "start",
        tmux_name,
        {
            "resume": True,
            "kind": kind,
            "attempt": 1,
            "project_id": proj_for_resume.id if (session_type == "container" and proj_for_resume is not None) else None,
            "workspace_name": proj_for_resume.name if (session_type == "container" and proj_for_resume is not None) else None,
            "org": proj_for_resume.graph_project if (session_type == "container" and proj_for_resume is not None) else "autonomy",
            "resume_uuid": session_uuid,
            "output_dir": output_dir,
            "jsonl_path": file_path,
            "harness": resume_harness,
            "model": model,
            "revived": bool(dead_session),
            "host_cmd": host_cmd,
            "session_type": session_type,
            "register_project": register_project,
            "event_loop": asyncio.get_running_loop(),
        },
    )
    if not _SESSION_LIFECYCLE_WORKER.try_enqueue(job):
        reason = "session lifecycle queue is full"
        _SESSION_LIFECYCLE_WORKER.state_writer.fail(
            tmux_name,
            phase="requested",
            reason=reason,
            retryable=True,
            attempt=1,
        )
        logger.warning(
            "api_session_resume: lifecycle queue full tmux=%s", tmux_name,
        )
        return JSONResponse(
            {"error": reason, "tmux_name": tmux_name, "retryable": True},
            status_code=503,
        )
    logger.info(
        "phase-trace: resume queued  tmux=%s  kind=%s  revived=%s",
        tmux_name, kind, bool(dead_session),
    )

    if not label:
        label = src.get("title", "") if source_id and src else ""
    if not label:
        label = f"Resumed: {session_uuid[:12]}"

    return JSONResponse({
        "tmux_name": tmux_name,
        "label": label,
        "type": session_type,
        "pending": True,
    }, status_code=202)


def _deliver_file_to_session(tmux_session: str, host_path: str, container_dest: str | None = None) -> str:
    """Return the path *tmux_session*'s agent should read for a host file.

    Container sessions get the file docker-cp'd to ``container_dest`` (default
    ``/tmp/<name>``) and read that path; host (terminal) sessions run on the
    host filesystem and read ``host_path`` directly — ``docker inspect``
    naturally distinguishes them (no running container ⇒ host path). Single
    source of truth for the host-vs-container split so callers (upload,
    screenshot, …) don't each re-implement it and grow the same edge cases.
    """
    if not tmux_session:
        return host_path
    container_path = container_dest or f"/tmp/{Path(host_path).name}"
    try:
        inspect = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", tmux_session],
            capture_output=True, text=True,
        )
    except (FileNotFoundError, OSError):
        inspect = None
    if inspect and inspect.returncode == 0 and inspect.stdout.strip() == "true":
        try:
            cp = subprocess.run(
                ["docker", "cp", host_path, f"{tmux_session}:{container_path}"],
                capture_output=True, text=True,
            )
        except (FileNotFoundError, OSError):
            cp = None
        if cp and cp.returncode == 0:
            return container_path
        logger.warning(
            "[deliver] docker cp failed for %s: %s",
            tmux_session, (cp.stderr if cp else "docker unavailable"),
        )
    return host_path


async def api_upload(request):
    """Upload a file to the workspace.

    POST /api/upload
    Multipart form: file field required, optional path param for target directory.
    Saves to data/uploads/ by default (or the specified subdirectory of repo root).
    Returns: {"ok": true, "path": "/workspace/repo/data/uploads/filename.jpg", "filename": "filename.jpg"}
    """
    try:
        form = await _parse_form_data(request)
    except Exception:
        return JSONResponse({"error": "invalid multipart form"}, status_code=400)

    # Support multi-file POSTs: one tile per file. The dashboard SPA's
    # addFiles loop fires N separate POSTs, but mobile share-sheets and
    # raw curl can bundle N files into one multipart body — that shape
    # was previously dropping every part past the first.
    uploads = form.getlist("file") if hasattr(form, "getlist") else (
        [form.get("file")] if form.get("file") is not None else []
    )
    uploads = [u for u in uploads if u is not None]
    if not uploads:
        return JSONResponse({"error": "file field is required"}, status_code=400)

    target_dir_param = (form.get("path") or "").strip()
    tmux_session = (form.get("tmux_session") or "").strip()
    if target_dir_param:
        target_dir = (_REPO_ROOT / target_dir_param).resolve()
        if not str(target_dir).startswith(str(_REPO_ROOT)):
            return JSONResponse({"error": "invalid path"}, status_code=400)
    elif tmux_session:
        if not _TMUX_NAME_RE.match(tmux_session):
            return JSONResponse({"error": "invalid tmux_session"}, status_code=400)
        run_dirs = sorted(
            (p for p in AGENT_RUNS_DIR.glob(f"{tmux_session}-*") if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ) if AGENT_RUNS_DIR.exists() else []
        if run_dirs:
            target_dir = run_dirs[0] / ".uploads"
        else:
            # Host (terminal) sessions run on the host filesystem, not in a
            # container, so they never have a data/agent-runs/<name>-* run
            # dir — uploading to one used to 404 ("no run dir"). Save to a
            # per-session dir under data/host-uploads/ instead; the host
            # agent reads it directly via the returned host_path (no docker
            # cp needed, and the cp block below no-ops because there's no
            # container), and api_session_output serves it back to the tile.
            # A CONTAINER session with no run dir genuinely can't receive the
            # file, so it still 404s.
            _sess = dashboard_db.get_session(tmux_session)
            if _sess and _sess.get("type") == "host":
                target_dir = HOST_UPLOADS_DIR / tmux_session
            else:
                return JSONResponse(
                    {"error": f"no run dir for session {tmux_session!r}"},
                    status_code=404,
                )
    else:
        target_dir = DATA_ROOT / "uploads"

    target_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for upload in uploads:
        filename = Path(upload.filename).name if upload.filename else "upload"
        filename = re.sub(r"[^\w.\-]", "_", filename)[:200] or "upload"

        dest = target_dir / filename
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            counter = 1
            while dest.exists():
                dest = target_dir / f"{stem}_{counter}{suffix}"
                counter += 1

        contents = await upload.read()
        dest.write_bytes(contents)

        host_path = str(dest)
        agent_path = _deliver_file_to_session(tmux_session, host_path) if tmux_session else host_path

        rel_path = ""
        if tmux_session and AGENT_RUNS_DIR in dest.parents:
            rel_path_parts = dest.relative_to(_REPO_ROOT).parts
            if len(rel_path_parts) > 3:
                rel_path = "/".join(rel_path_parts[3:])
        elif tmux_session and HOST_UPLOADS_DIR in dest.parents:
            # Host session: rel_path is the file path under the session's
            # data/host-uploads/<session>/ dir. The viewer tile renders via
            # /api/session/<session>/output/<rel_path>, which api_session_output
            # resolves back to this file — the same rel_path contract the
            # container path uses, so the tile converts identically.
            rel_path = dest.relative_to(HOST_UPLOADS_DIR / tmux_session).as_posix()

        results.append({
            "path": agent_path,
            "host_path": host_path,
            "filename": dest.name,
            "rel_path": rel_path,
            "mime": (mimetypes.guess_type(dest.name)[0]
                     or "application/octet-stream"),
            "size": len(contents),
        })

    # Top-level path/host_path/filename keep the first file so existing
    # single-file callers (the SPA's per-file POST) read it unchanged.
    first = results[0]
    return JSONResponse({
        "ok": True, "files": results,
        "path": first["path"], "host_path": first["host_path"],
        "filename": first["filename"],
    })


# ── WebSocket Terminal ─────────────────────────────────────────

# Per-project locks to serialise host session JSONL watchers
async def _watch_for_host_session_jsonl(
    projects_dir: Path, tmux_name: str, timeout: float = 30.0
) -> None:
    """Watch for this host session's JSONL to appear and link it by content.

    A host Claude writes no JSONL until it receives input. ``api_session_create``
    injects the orientation message — which contains ``tmux_name`` — right after
    launch; this watcher waits for the JSONL that message creates and links it.

    Matching is by **content** (the JSONL whose text contains ``tmux_name``), not
    by mtime. The orientation line is unique per session, so concurrent host
    launches in the same project dir each link their own file with no race — which
    is why no per-dir lock is needed. This is the ONLY code that sets jsonl_path
    for host sessions. Polls every 500ms for up to ``timeout`` seconds.

    Timeout is generous to absorb a slow Claude boot: injection may wait up to
    60s for a real composer prompt, then Claude must flush its first turn to
    disk.
    """
    existing = set(projects_dir.glob("*.jsonl")) if projects_dir.exists() else set()
    logger.info("JSONL watcher started  tmux=%s  existing=%d", tmux_name, len(existing))
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.5)
        if not projects_dir.exists():
            continue
        current = set(projects_dir.glob("*.jsonl"))
        new_files = current - existing
        # Link the new file whose contents carry this session's tmux_name (the
        # orientation message injected at launch). Content match — not mtime —
        # so concurrent host launches never cross-link. Keep polling until a
        # file actually matches; a new but unrelated JSONL is left alone.
        new_jsonl = None
        for jf in new_files:
            try:
                if tmux_name in jf.read_text(encoding="utf-8", errors="replace"):
                    new_jsonl = jf
                    break
            except OSError:
                continue
        if new_jsonl is None:
            continue
        logger.info("JSONL watcher found new session  uuid=%s  tmux=%s", new_jsonl.stem, tmux_name)
        # LINK + ENRICH: set session_uuid, jsonl_path, and graph_source_id
        dashboard_db.link_and_enrich(
            tmux_name,
            session_uuid=new_jsonl.stem,
            jsonl_path=str(new_jsonl),
            project=projects_dir.name,
        )

        # Set up tail state with resolution_dir FIRST — _add_file_watch
        # constructs a default _TailState (no resolution_dir) on first
        # call, so the prior "if not in _tail_states" guard was always
        # false here and resolution_dir never got assigned.
        from tools.dashboard.session_monitor import _TailState
        ts = session_monitor._tail_states.get(tmux_name)
        if ts is None:
            session_monitor._tail_states[tmux_name] = _TailState(
                resolution_dir=projects_dir)
        else:
            ts.resolution_dir = projects_dir

        # Now add the inotify watches
        session_monitor._add_file_watch(tmux_name, str(new_jsonl))
        session_monitor._add_dir_watch(tmux_name, str(projects_dir))

        # auto-suvcp R3: activation goes through the unified machine — the
        # persisted re-attach requests a catch-up drain (the orientation
        # burst already on disk becomes visible with no further write) and
        # the registry publishes AFTER that drain (invariant 9). A direct
        # broadcast here would durably show resolved=true with zero
        # entries — the CalStartupStall broadcast leg, host edition.
        session_monitor.observe_rollout(
            tmux_name, new_jsonl, source="host_watch",
        )
        return
    logger.warning("JSONL watcher timed out after %.0fs  tmux=%s", timeout, tmux_name)


def _tmux_session_exists(name: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", name],
                          capture_output=True).returncode == 0


def _live_session_names_or_none() -> "set[str] | None":
    """Live tmux session names, or None when tmux could not ANSWER.

    Destructive consumers — the vault-release sweeper's orphan and
    directory-reclaim passes — require a definitive listing. A failed
    probe is ambiguous, not authoritative (the 2026-04-20 rule), and a
    bare per-name probe cannot express the difference: on 2026-08-30 it
    answered "dead" for every live session and the sweeper rmtree'd every
    session's /run/secrets on both machines, orphaning their binds until
    relaunch. rc != 0 — INCLUDING "no server" — returns None, because a
    machine with no reachable tmux cannot distinguish "no sessions" from
    "cannot see sessions"; the cost of skipping orphan cleanup for a tick
    is a lingering empty directory, never a destroyed live secret.
    """
    tmux = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                          capture_output=True, text=True)
    if tmux.returncode != 0:
        return None
    # Container sessions are docker containers named for their session; on a
    # node where they are not tmux-wrapped, tmux alone under-reports the
    # living. Both oracles must answer, and a session alive in EITHER is
    # alive.
    docker = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                            capture_output=True, text=True)
    if docker.returncode != 0:
        return None
    return (
        {s for s in tmux.stdout.strip().split("\n") if s}
        | {s for s in docker.stdout.strip().split("\n") if s}
    )


_DASHBOARD_PREFIXES = ("auto-", "host-", "chat-", "chatwith-")


def _list_dashboard_tmux() -> list[str]:
    """List all tmux sessions created by the dashboard.

    Returns [] on subprocess failure (tmux unreachable, socket missing, etc.)
    and logs a warning. Callers that drive mark_dead off this value (notably
    api_terminals) MUST treat [] as ambiguous, not authoritative — a silent
    tmux failure was the mass-session-deactivation root cause on 2026-04-20.
    """
    result = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        # "no server" (the socket doesn't exist) just means no sessions have been
        # created yet — the normal state of an idle node, polled every few seconds,
        # so it must not warn. A genuine tmux failure while a server IS running
        # still warns (the 2026-04-20 mass-deactivation root cause).
        no_server = ("No such file or directory" in stderr
                     or "no server running" in stderr)
        if not no_server:
            logger.warning(
                "_list_dashboard_tmux: tmux list-sessions failed  rc=%d  stderr=%r",
                result.returncode, stderr[:200],
            )
        return []
    return [s for s in result.stdout.strip().split("\n")
            if any(s.startswith(p) for p in _DASHBOARD_PREFIXES)]


def _detect_terminal_type(tmux_name: str) -> dict:
    """Detect terminal session type for untracked sessions (e.g. after server restart)."""
    detected_cmd = "/bin/bash"
    detected_env = "host"
    # Check if a docker container with this name is running -> container session
    cr = subprocess.run(
        ["docker", "inspect", "-f", "{{.Path}}", tmux_name],
        capture_output=True, text=True,
    )
    if cr.returncode == 0:
        detected_env = "container"
        entrypoint = cr.stdout.strip()
        if "bash" in entrypoint or entrypoint == "sh":
            detected_cmd = "autonomy-agent-bash"
        else:
            detected_cmd = "autonomy-agent-claude"
    else:
        # Host session -- check tmux pane for claude
        pr = subprocess.run(
            ["tmux", "display-message", "-t", tmux_name, "-p",
             "#{pane_start_command} #{pane_current_command}"],
            capture_output=True, text=True,
        )
        pane_info = pr.stdout.strip().lower() if pr.returncode == 0 else ""
        if "claude" in pane_info:
            detected_cmd = "claude --dangerously-skip-permissions"
    return {"cmd": detected_cmd, "env": detected_env}


async def ws_terminal(websocket: WebSocket):
    """WebSocket endpoint that bridges xterm.js to an existing tmux session.

    This endpoint is ATTACH-ONLY — session creation happens via
    POST /api/session/create.  Requests without `?attach=` are rejected.

    The WebSocket just bridges PTY I/O and forwards resize events; the tmux
    session persists across attaches so users can disconnect and reconnect.

    Query params:
      attach — existing tmux session name to attach to (required)
    """
    await websocket.accept()

    params = websocket.query_params
    attach = params.get("attach")
    logger.info("ws_terminal: connect  attach=%s", attach)

    if not attach:
        await websocket.send_text(
            "\r\n\x1b[31mws_terminal requires ?attach=<tmux_name>; "
            "use POST /api/session/create to start a new session\x1b[0m\r\n",
        )
        await websocket.close()
        return

    if not _tmux_session_exists(attach):
        await websocket.send_text(f"\r\n\x1b[31mSession '{attach}' not found\x1b[0m\r\n")
        await websocket.close()
        return
    tmux_name = attach

    # Ensure tmux mouse mode is on for reattached sessions so scroll
    # wheel triggers tmux copy-mode (scrollback lives in tmux, not xterm.js).
    # Users hold Shift to select text at the browser level.
    subprocess.run(["tmux", "set-option", "-t", tmux_name, "mouse", "on"],
                    capture_output=True)
    subprocess.run(["tmux", "set-option", "-t", tmux_name, "set-clipboard", "on"],
                    capture_output=True)
    subprocess.run(["tmux", "set-option", "-t", tmux_name, "allow-passthrough", "on"],
                    capture_output=True)

    # Ensure session is tracked in DB for sessions not yet registered
    # (e.g. after a server restart where tmux outlived the monitor).
    if not dashboard_db.session_exists(tmux_name):
        info = _detect_terminal_type(tmux_name)
        stype = "container" if info["env"] == "container" else "host"
        try:
            await session_monitor.register(
                tmux_name=tmux_name,
                session_type=stype,
                project="unknown",
            )
        except Exception:
            pass  # already exists

    # Now attach to the tmux session via a PTY
    master_fd, slave_fd = pty.openpty()
    winsize = struct.pack("HHHH", 40, 120, 0, 0)
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)

    proc = subprocess.Popen(
        ["tmux", "attach-session", "-t", tmux_name],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env={**os.environ, "TERM": "xterm-256color"},
        preexec_fn=os.setsid,
        close_fds=True,
    )
    os.close(slave_fd)

    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    alive = True

    async def read_pty():
        nonlocal alive
        try:
            while alive:
                await asyncio.sleep(0.02)
                try:
                    data = os.read(master_fd, 65536)
                    if data:
                        await websocket.send_text(data.decode("utf-8", errors="replace"))
                except BlockingIOError:
                    continue
                except OSError:
                    break
                if proc.poll() is not None:
                    break
        except Exception:
            pass
        alive = False

    reader_task = asyncio.create_task(read_pty())

    try:
        while alive:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if "text" in msg:
                data = msg["text"]
                if data.startswith("\x1b[8;"):
                    try:
                        parts = data[4:-1].split(";")
                        rows, cols = int(parts[0]), int(parts[1])
                        ws_bytes = struct.pack("HHHH", rows, cols, 0, 0)
                        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, ws_bytes)
                        os.kill(proc.pid, signal.SIGWINCH)
                    except (ValueError, IndexError, OSError):
                        pass
                else:
                    try:
                        os.write(master_fd, data.encode("utf-8"))
                    except OSError:
                        break
            elif "bytes" in msg:
                try:
                    os.write(master_fd, msg["bytes"])
                except OSError:
                    break
    except Exception:
        pass
    finally:
        alive = False
        reader_task.cancel()
        # DON'T kill the tmux session — it persists for reconnection
        # Just detach by killing the attach process
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass


def _voice_audio_capture_enabled() -> bool:
    """Debug audio capture — OFF by default, toggled via the ``voice.audio_capture``
    graph feature flag (set ``dashboard.feature_flags``). No file on disk. Enable:
      graph set add 'dashboard.feature_flags#1' --key voice.audio_capture \
        --inline '{"enabled":true}' --state canonical
    Captures the operator's REAL mic PCM (ambient room tone, real silence) to a WAV
    for VAD/no_speech tuning AND for diagnosing the garbage-transcription bug — is
    the audio reaching WhisperLive corrupt, or is the model hallucinating on clean
    audio? Read fresh per audio frame so the operator can toggle it live."""
    try:
        from tools.dashboard import feature_flags
        return feature_flags.is_enabled("voice.audio_capture")
    except Exception:
        return False


def _open_voice_audio_capture(bind: str):
    """Open a 16kHz mono int16 WAV writer for raw browser PCM frames, or None."""
    import wave
    try:
        d = DATA_ROOT / "voice-captures"
        d.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(bind))[:40] or "voice"
        out = d / f"{safe}-{ts}.wav"
        w = wave.open(str(out), "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        logger.info("ws_voice: AUDIO CAPTURE started -> %s", out)
        return w
    except Exception:
        logger.exception("ws_voice: failed to open audio capture")
        return None


_voice_flow_monotonic = time.monotonic
_VOICE_INCOMING_MAX_MESSAGES = 64
_VOICE_AUDIO_MAX_FRAME_BYTES = 64 * 1024


async def ws_voice(websocket: WebSocket):
    """WebSocket endpoint for the voice pipe canary (S3).

    Spec: ``graph://86fd1897-d4d``. As of S3-4b, the WhisperLive
    audio pipeline is wired: on ``start`` the route opens an
    upstream WhisperLive client (sync connect + SERVER_READY wait
    so TCP/WS buffering absorbs operator audio that arrives during
    the cold-start window), then on each binary frame in LISTENING
    state forwards bytes to upstream. Transcript callbacks route
    finals into :data:`voice_buffer.MANAGER` and surface
    partial+final ``transcript`` frames back to the operator.
    ``tmux_send`` integration on commit lands in S3-5.

    Query params:
      bind — existing tmux session name (required)

    Gate failures close the connection with WebSocket close code
    1008 (Policy Violation):
      - missing ``bind`` query param
      - bound tmux session does not exist
      - ``voice.pipe_enabled`` feature flag is disabled

    Until WhisperLive (S3-4), binary audio frames are silently
    dropped per spec. Until the buffer manager (S3-3) and
    ``tmux_send`` integration (S3-5), ``commit`` actions resolve
    with ``commit_error`` code ``no_buffer`` — operators wiring
    frontends against this stub can exercise the full state machine
    and the error path; the success path becomes reachable in S3-5.
    """
    from tools.dashboard import voice_session as voice_mod
    from tools.dashboard import voice_buffer as voice_buffer_mod
    from tools.dashboard import voice_whisperlive as voice_wl
    from tools.dashboard import feature_flags

    await websocket.accept()

    bind = websocket.query_params.get("bind")
    if not bind:
        await websocket.send_json({
            "type": "error",
            "code": "missing_bind",
            "message": "?bind=<tmux_session> query param is required",
        })
        await websocket.close(code=1008, reason="missing bind param")
        return

    # New clients advertise the ordered recovery acknowledgement. Legacy PWA
    # tabs omit this query parameter and retain the old automatic reopen path
    # until they reload the versioned static bundle.
    audio_ready_required = websocket.query_params.get("audio_ack") == "1"

    if not _tmux_session_exists(bind):
        await websocket.send_json({
            "type": "error",
            "code": "session_not_found",
            "message": f"tmux session {bind!r} does not exist",
        })
        await websocket.close(code=1008, reason="bind session not found")
        return

    if not feature_flags.is_enabled("voice.pipe_enabled"):
        await websocket.send_json({
            "type": "error",
            "code": "voice_pipe_disabled",
            "message": (
                "voice.pipe_enabled feature flag is off; enable it via the "
                "Settings UI to use /ws/voice"
            ),
        })
        await websocket.close(code=1008, reason="voice.pipe_enabled disabled")
        return

    # Cross-tab guard + buffer reattach. The evict callback closes
    # THIS WS if a later connection for the same bind arrives. The
    # superseded_event lets the finally block distinguish a normal
    # disconnect (detach buffer for TTL grace) from being kicked
    # (don't touch buffer; the new owner took it).
    superseded_event = asyncio.Event()

    async def _evict_me():
        superseded_event.set()
        try:
            await websocket.close(code=1000, reason="superseded")
        except Exception:
            pass

    acq = voice_buffer_mod.MANAGER.acquire(bind, evict_callback=_evict_me)

    if acq.prior_evict_callback is not None:
        # A prior WS owned this bind. Close it (code 1000 'superseded'
        # per spec) before treating ourselves as the active owner.
        try:
            await acq.prior_evict_callback()
        except Exception:
            logger.exception("ws_voice: prior evict failed bind=%s", bind)

    if acq.buffer_text:
        # Restore the in-flight buffer that survived the disconnect.
        # Sent BEFORE any subsequent server frame so the client's
        # buffer state is correct before audio / transcripts resume.
        await websocket.send_json(
            voice_buffer_mod.buffer_state_frame(acq.buffer_text),
        )

    session = voice_mod.VoiceSession(tmux_name=bind)
    connection_id = uuid.uuid4().hex
    end_was_explicit = False
    # WhisperLive client is created on the first 'start' frame
    # (sync connect + SERVER_READY wait), then reused for the
    # lifetime of this WS. If connect fails, whisperlive_unavailable
    # latches True and subsequent 'start' attempts re-send a typed
    # error frame rather than re-attempting connect — operators
    # disconnect+reconnect to retry.
    whisperlive_client: voice_wl.WhisperLiveClient | None = None
    whisperlive_unavailable = False
    # #43: per-connection transcript-acceptance epoch. Bumped on each explicit
    # Send/Clear reset (_handle_voice_reset); every transcript/buffer_state frame
    # carries it so the client drops the re-emit of just-sent/cleared speech still
    # draining from the pre-reset WhisperLive buffer. Stays 0 (harmless) when the
    # voice.reset_suppression flag is off.
    voice_epoch = 0
    audio_received = 0
    audio_forwarded = 0
    last_audio_flow_at: float | None = None
    # Audio intake and processing are deliberately separate. Control work such
    # as reset/reconnect or tmux commit may await for long enough that more
    # browser frames arrive. A serial receive/process loop cannot tell that
    # those frames arrived while capture was suppressed: ASGI queues them and
    # hands them to us only after the await finishes. The receiver below tags
    # each binary frame with the gate state at arrival time, preserving wire
    # order while preventing stale queued audio from becoming "healthy" later.
    audio_intake_open = False
    audio_intake_generation = 0
    pending_audio_ready_token: str | None = None
    incoming: asyncio.Queue[tuple[dict, bool, int, str | None]] = asyncio.Queue(
        maxsize=_VOICE_INCOMING_MAX_MESSAGES,
    )

    def _close_audio_intake() -> None:
        nonlocal audio_intake_open, audio_intake_generation
        audio_intake_open = False
        audio_intake_generation += 1

    def _sync_audio_intake(
        *, allow_open: bool = False, expected_generation: int | None = None,
    ) -> None:
        nonlocal audio_intake_open
        can_open = bool(
            session.state == voice_mod.LISTENING
            and whisperlive_client is not None
            and whisperlive_client.is_ready()
        )
        if not can_open:
            audio_intake_open = False
        elif (
            allow_open
            and expected_generation is not None
            and audio_intake_generation == expected_generation
        ):
            audio_intake_open = True

    async def _receive_voice_messages() -> None:
        """Continuously receive and tag frames with arrival-time eligibility."""
        nonlocal audio_intake_open, audio_received, pending_audio_ready_token
        try:
            while True:
                msg = await websocket.receive()
                msg_type = msg.get("type")
                accepted_at_intake = False
                boundary_token: str | None = None
                if "bytes" in msg and msg["bytes"] is not None:
                    audio_received += 1
                    audio_bytes = msg["bytes"]
                    accepted_at_intake = bool(
                        audio_bytes
                        and len(audio_bytes) <= _VOICE_AUDIO_MAX_FRAME_BYTES
                        and audio_intake_open
                    )
                    # Closed/empty/oversized audio is accounted for but never
                    # retained. If the bounded processor queue is saturated,
                    # drop this audio frame rather than removing backpressure
                    # for controls or growing PCM memory without limit.
                    if not accepted_at_intake or incoming.full():
                        continue
                elif "text" in msg and msg["text"] is not None:
                    try:
                        control = json.loads(msg["text"])
                    except Exception:
                        control = None
                    # Close on receipt, not after the potentially blocking
                    # control handler. Start/unmute never open here: only the
                    # canonical processor may reopen after upstream readiness.
                    if (
                        isinstance(control, dict)
                        and control.get("type")
                        in {"mute", "commit", "reset", "end"}
                    ):
                        _close_audio_intake()
                        if control.get("type") in {"commit", "reset"}:
                            boundary_token = uuid.uuid4().hex
                            pending_audio_ready_token = boundary_token
                        else:
                            pending_audio_ready_token = None
                    # Reset/commit reopen only after the browser has observed
                    # the server result. This client acknowledgement is ordered
                    # after every binary frame the browser sent during the
                    # closed interval, so ASGI backlog cannot be reclassified
                    # as post-recovery audio.
                    if isinstance(control, dict) and control.get("type") == "audio_ready":
                        epoch = control.get("epoch")
                        connection = control.get("connection_id")
                        token = control.get("token")
                        if (
                            connection == connection_id
                            and isinstance(token, str)
                            and token == pending_audio_ready_token
                            and isinstance(epoch, int)
                            and not isinstance(epoch, bool)
                            and epoch == voice_epoch
                            and session.state == voice_mod.LISTENING
                            and whisperlive_client is not None
                            and whisperlive_client.is_ready()
                        ):
                            audio_intake_open = True
                            pending_audio_ready_token = None
                        continue
                await incoming.put((
                    msg, accepted_at_intake, audio_intake_generation,
                    boundary_token,
                ))
                if msg_type == "websocket.disconnect":
                    return
        except WebSocketDisconnect:
            await incoming.put((
                {"type": "websocket.disconnect"}, False,
                audio_intake_generation, None,
            ))
        except Exception as exc:
            await incoming.put((
                {"type": "voice.receive.error", "error": exc},
                False, audio_intake_generation, None,
            ))

    def _upstream_state() -> str:
        """Project the existing WhisperLive client into the wire contract."""
        if whisperlive_unavailable:
            return "unavailable"
        if whisperlive_client is not None:
            if whisperlive_client.is_ready():
                return "ready"
            if whisperlive_client.is_unavailable():
                return "unavailable"
        return "not_ready"

    def _voice_state_frame() -> dict:
        return {
            "type": "voice_state",
            "connection_id": connection_id,
            "fsm_state": session.state,
            "upstream": _upstream_state(),
            "epoch": voice_epoch,
        }

    async def _on_partial(text: str) -> None:
        # Partials are operator-visible feedback but not persisted
        # to the buffer (spec: only finals contribute). ts_ms is
        # the wall-clock at callback time; sufficient for client
        # ordering and not pretending to be a more authoritative
        # timestamp than we actually have.
        try:
            await websocket.send_json({
                "type": "transcript",
                "kind": "partial",
                "text": text,
                "ts_ms": int(time.time() * 1000),
                "epoch": voice_epoch,
            })
        except Exception:
            logger.debug("ws_voice: on_partial send failed bind=%s", bind)

    async def _on_final(text: str) -> None:
        # Finals contribute to the buffer (dedupe happened in the
        # wrapper, so this is guaranteed-new text) AND surface to
        # the operator as a transcript:final frame.
        voice_buffer_mod.MANAGER.append_final(bind, text)
        logger.info("ws_voice DIAG: FINAL transcript bind=%s text=%r", bind, text)
        try:
            await websocket.send_json({
                "type": "transcript",
                "kind": "final",
                "text": text,
                "ts_ms": int(time.time() * 1000),
                "epoch": voice_epoch,
            })
        except Exception:
            logger.debug("ws_voice: on_final send failed bind=%s", bind)

    async def _on_whisperlive_error(message: str) -> None:
        nonlocal whisperlive_unavailable
        whisperlive_unavailable = True
        _close_audio_intake()
        try:
            await websocket.send_json({
                "type": "error",
                "code": "whisperlive_session_error",
                "message": message,
            })
        except Exception:
            logger.debug("ws_voice: on_error send failed bind=%s", bind)

    async def _ensure_whisperlive_connected() -> bool:
        """Idempotent: instantiate + connect WhisperLive on the
        first 'start'. Returns True on ready, False on failure
        (typed error frame already sent to operator).

        Subsequent calls on the same WS return whisperlive_client.
        is_ready() — no re-attempt. Operators retry by reconnecting.
        """
        nonlocal whisperlive_client, whisperlive_unavailable
        if whisperlive_client is not None:
            return whisperlive_client.is_ready()
        if whisperlive_unavailable:
            return False
        # Resolve the live-tunable transcription knobs from the
        # ``dashboard.voice.transcription`` graph setting, read fresh per mic
        # connection (falls back to the voice_whisperlive module defaults if
        # the row is absent — never raises). Lets the operator tune the
        # silence-gating thresholds / language / VAD without a code change.
        _tx_cfg = _voice_transcription_settings.resolve_transcription_config()
        whisperlive_client = voice_wl.WhisperLiveClient(
            url=voice_wl.WHISPERLIVE_URL,
            uid=f"{bind}-{uuid.uuid4().hex[:8]}",
            model=_tx_cfg.model,
            language=_tx_cfg.language,
            use_vad=_tx_cfg.use_vad,
            no_speech_thresh=_tx_cfg.no_speech_thresh,
            vad_threshold=_tx_cfg.vad_threshold,
            wire_format=voice_wl.WHISPERLIVE_WIRE_FORMAT,
            on_partial=_on_partial,
            on_final=_on_final,
            on_error=_on_whisperlive_error,
        )
        logger.info("ws_voice DIAG: 'start' received → connecting WhisperLive bind=%s url=%s", bind, voice_wl.WHISPERLIVE_URL)
        try:
            await whisperlive_client.connect_and_wait_ready()
        except voice_wl.WhisperLiveConnectError as exc:
            logger.warning("ws_voice DIAG: WhisperLive connect FAILED bind=%s err=%s", bind, exc)
            whisperlive_unavailable = True
            try:
                await websocket.send_json({
                    "type": "error",
                    "code": "whisperlive_connect_failed",
                    "message": str(exc),
                })
            except Exception:
                pass
            # Wrapper went to UNAVAILABLE inside connect_and_wait_ready;
            # we leave the reference so close() in finally is a no-op
            # rather than re-instantiating.
            return False
        logger.info("ws_voice DIAG: WhisperLive READY bind=%s", bind)
        return True

    async def _handle_voice_reset(
        arrival_generation: int, boundary_token: str | None,
    ) -> None:
        """#43: explicit Send/Clear suppression control. Flag-gated.

        ON (``voice.reset_suppression``): flush the WhisperLive session at the
        source — close + reopen (~15ms), which drops its buffered audio + clock —
        so the just-sent/cleared speech can NEVER re-emit; bump the epoch; and tell
        the client the new epoch via an empty buffer_state. Audio frames that arrive
        during the ~15ms reopen are simply not forwarded (wrapper not ready) — a
        bounded clip, never a hang (proven by voice_whisper_reset_trace.py).

        OFF: behave exactly like a legacy ``discard`` — clear the manager buffer,
        emit buffer_state(""), leave WhisperLive untouched. Reversible by flag.
        """
        nonlocal voice_epoch, whisperlive_client
        voice_buffer_mod.MANAGER.clear(bind)
        if not feature_flags.is_enabled("voice.reset_suppression"):
            frame = voice_buffer_mod.buffer_state_frame("")
            if audio_ready_required and boundary_token:
                frame["audio_ready_token"] = boundary_token
            await websocket.send_json(frame)
            _sync_audio_intake(
                allow_open=not audio_ready_required,
                expected_generation=arrival_generation,
            )
            return
        voice_epoch += 1
        old = whisperlive_client
        whisperlive_client = None
        if old is not None:
            try:
                await old.close()
            except Exception:
                logger.debug("ws_voice: reset close failed bind=%s", bind)
        # Reopen a fresh session (the idempotent helper re-instantiates since we
        # nulled the ref). Buffer + audio clock are gone at the source.
        await _ensure_whisperlive_connected()
        frame = voice_buffer_mod.buffer_state_frame("")
        frame["epoch"] = voice_epoch
        if audio_ready_required and boundary_token:
            frame["audio_ready_token"] = boundary_token
        await websocket.send_json(frame)
        _sync_audio_intake(
            allow_open=not audio_ready_required,
            expected_generation=arrival_generation,
        )
        logger.info("ws_voice DIAG: reset → epoch=%d bind=%s", voice_epoch, bind)

    logger.info(
        "ws_voice: connected bind=%s state=%s restored_buffer_chars=%d",
        bind, session.state, len(acq.buffer_text),
    )
    _audio_frames = 0
    _audio_capture_enabled = _voice_audio_capture_enabled()
    _audio_capture_wav = None   # debug WAV writer (lazily opened if capture flag set)

    receiver_task = asyncio.create_task(_receive_voice_messages())
    try:
        while True:
            (
                msg, accepted_at_intake, arrival_generation, boundary_token,
            ) = await incoming.get()
            msg_type = msg.get("type")
            if msg_type == "websocket.disconnect":
                logger.info(
                    "ws_voice: disconnected bind=%s code=%s reason=%r",
                    bind,
                    msg.get("code"),
                    msg.get("reason", ""),
                )
                break
            if msg_type == "voice.receive.error":
                raise msg["error"]
            if "text" in msg and msg["text"] is not None:
                # #43: 'reset' is the explicit Send/Clear suppression control. It is
                # NOT a state-machine control, so intercept it before
                # parse_control_frame (which would reject the unknown type). The
                # flag gate lives inside _handle_voice_reset (off => legacy discard
                # semantics, no WhisperLive reset).
                try:
                    _ctrl = json.loads(msg["text"])
                except Exception:
                    _ctrl = None
                if isinstance(_ctrl, dict) and _ctrl.get("type") == "reset":
                    await _handle_voice_reset(
                        arrival_generation, boundary_token,
                    )
                    continue
                frame_type, payload = voice_mod.parse_control_frame(msg["text"])
                if frame_type is None:
                    # parse_control_frame already built the error frame.
                    await websocket.send_json(payload)
                    continue
                # 'discard' from an active state clears the buffer.
                # Catch it BEFORE handle_control runs because the
                # state machine intentionally treats discard as a
                # no-op-from-its-perspective (state unchanged); the
                # buffer-clearing side effect lives in the transport.
                if frame_type == "discard" and session.state in voice_mod.ACTIVE_STATES:
                    voice_buffer_mod.MANAGER.clear(bind)
                    # NOTE: we deliberately do NOT seal the WhisperLive audio
                    # timeline (set_cutoff) here. A controlled repro
                    # (tools/dashboard/tests/voice_whisper_repro.py) proved the
                    # cutoff STALLS dictation ~2.6s and hangs 3/4 of the time:
                    # the in-flight segment straddling the clear has no word
                    # timestamps yet, so its partials are dropped wholesale until
                    # the segment finalizes. Clear/Send must not touch the audio
                    # stream — suppression of the just-cleared text is purely
                    # client-side (voice-capture remembers the displayed text and
                    # strips its re-emit). buffer_state("") still wipes immediate
                    # in-flight stragglers; the client handles the later re-emit.
                    await websocket.send_json(
                        voice_buffer_mod.buffer_state_frame("")
                    )
                responses = session.handle_control(frame_type)
                logger.info("ws_voice DIAG: ctrl=%s → state=%s bind=%s", frame_type, session.state, bind)
                for resp in responses:
                    await websocket.send_json(resp)
                control_accepted = not any(
                    resp.get("type") == "error" for resp in responses
                )
                # 'start' from IDLE triggered the LISTENING
                # transition — that's when we connect WhisperLive
                # (sync wait for SERVER_READY so subsequent audio
                # frames find the wrapper ready). Failure sends a
                # typed error frame from inside the helper; the WS
                # stays open so the operator can still mute / end
                # the session cleanly.
                if (
                    frame_type == "start"
                    and control_accepted
                    and session.state == voice_mod.LISTENING
                ):
                    await _ensure_whisperlive_connected()
                    await websocket.send_json(_voice_state_frame())
                    _sync_audio_intake(
                        allow_open=True,
                        expected_generation=arrival_generation,
                    )
                elif frame_type in ("mute", "unmute") and control_accepted:
                    await websocket.send_json(_voice_state_frame())
                    _sync_audio_intake(
                        allow_open=True,
                        expected_generation=arrival_generation,
                    )
                # Real commit path (S3-5): when the state machine
                # transitioned into COMMITTING, read the accumulated
                # buffer and dispatch via tmux_send.
                #
                # Empty buffer → commit_error code=no_buffer (no
                # text to send; operator commit was a no-op).
                #
                # Non-empty buffer → await tmux_send(bind, text).
                # tmux_send is fire-and-forget (schedules a worker
                # task that paste-and-double-Enters); the await
                # returns immediately. We then clear the buffer and
                # emit committed. If tmux_send itself raises (e.g.
                # subprocess error), surface as commit_error
                # code=tmux_failed; the buffer is NOT cleared so
                # the operator can retry by sending commit again.
                if session.state == voice_mod.COMMITTING:
                    pending_text = voice_buffer_mod.MANAGER.get_text(bind)
                    if not pending_text:
                        finish = session.finish_commit(
                            success=False,
                            error_code=voice_mod.COMMIT_ERR_NO_BUFFER,
                            error_message="no buffer accumulated",
                        )
                    else:
                        # Use the AWAITED tmux helper, not the
                        # fire-and-forget tmux_send. The latter only
                        # schedules a worker task — awaiting it tells
                        # us nothing about whether the paste actually
                        # landed. tmux_send_awaited runs the paste +
                        # first Enter inline and raises TmuxSendError
                        # on any non-zero tmux returncode, so the
                        # committed/commit_error frame reflects the
                        # real outcome.
                        try:
                            from tools.dashboard.tmux_send import (
                                tmux_send_awaited,
                            )
                            await tmux_send_awaited(bind, pending_text)
                        except Exception as exc:
                            logger.exception(
                                "ws_voice: tmux_send_awaited failed bind=%s",
                                bind,
                            )
                            finish = session.finish_commit(
                                success=False,
                                error_code=voice_mod.COMMIT_ERR_TMUX_FAILED,
                                error_message=f"tmux send failed: {exc}",
                            )
                        else:
                            voice_buffer_mod.MANAGER.clear(bind)
                            # As with 'discard': do NOT set_cutoff on commit — it
                            # stalls dictation after Send (proven by the repro
                            # harness). Re-emit suppression of the just-sent text
                            # is client-side; the audio stream is left untouched.
                            finish = session.finish_commit(
                                success=True,
                                committed_text=pending_text,
                            )
                    for resp in finish:
                        if audio_ready_required and boundary_token:
                            resp = dict(resp)
                            resp["audio_ready_token"] = boundary_token
                        await websocket.send_json(resp)
                    _sync_audio_intake(
                        allow_open=not audio_ready_required,
                        expected_generation=arrival_generation,
                    )
                if frame_type == "end":
                    # Explicit operator 'end' — drop the buffer
                    # immediately (no TTL grace), regardless of what
                    # state the session ended up in. The state may
                    # still be COMMITTING (deferred-end latch path
                    # from eb02f95): when finish_commit eventually
                    # transitions to ENDED, the finally block must
                    # still see end_was_explicit=True so it calls
                    # release(), not detach(). Latching here captures
                    # operator intent at the moment they sent the
                    # frame, independent of state-machine timing.
                    end_was_explicit = True
                if session.state == voice_mod.ENDED:
                    break
            elif "bytes" in msg and msg["bytes"] is not None:
                audio_bytes = msg["bytes"]
                if not audio_bytes:
                    continue
                should_forward = (
                    accepted_at_intake and session.handle_audio(audio_bytes)
                )
                _audio_frames += 1
                # Debug: capture the RAW browser PCM (real mic, ambient room tone)
                # to a WAV when enabled at WS start. Never consult Settings from
                # the per-frame path.
                if _audio_capture_wav is None and _audio_capture_enabled:
                    _audio_capture_wav = _open_voice_audio_capture(bind)
                if _audio_capture_wav is not None:
                    try:
                        _audio_capture_wav.writeframes(audio_bytes)
                    except Exception:
                        pass
                if _audio_frames % 50 == 1:
                    logger.info(
                        "ws_voice DIAG: audio frame #%d forward=%s wl_ready=%s bind=%s",
                        _audio_frames, should_forward,
                        (whisperlive_client.is_ready() if whisperlive_client else None), bind,
                    )
                if (
                    should_forward
                    and whisperlive_client is not None
                    and whisperlive_client.is_ready()
                ):
                    was_forwarded = await whisperlive_client.send_audio(audio_bytes)
                    # send_audio reports the actual upstream write. Readiness
                    # alone is insufficient: empty/invalid frames may be a
                    # deliberate no-op while the wrapper remains READY.
                    if was_forwarded:
                        audio_forwarded += 1
                        now = _voice_flow_monotonic()
                        if (
                            last_audio_flow_at is None
                            or now - last_audio_flow_at >= 1.0
                        ):
                            await websocket.send_json({
                                "type": "audio_flow",
                                "connection_id": connection_id,
                                "received": audio_received,
                                "forwarded": audio_forwarded,
                                "ts_ms": int(time.time() * 1000),
                            })
                            last_audio_flow_at = now
                    elif not whisperlive_client.is_ready():
                        _close_audio_intake()
                # else: state machine said no (muted / committing /
                # ended) or wrapper not ready / unavailable.
                # Silently drop — spec says audio outside LISTENING
                # is dropped without an error frame, and the
                # whisperlive_connect_failed / whisperlive_session_error
                # frame already informed the operator if the upstream
                # is the reason.
    except WebSocketDisconnect as exc:
        logger.info(
            "ws_voice: disconnected bind=%s code=%s reason=%r",
            bind,
            getattr(exc, "code", None),
            getattr(exc, "reason", ""),
        )
    except Exception:
        logger.exception("ws_voice: unexpected error bind=%s", bind)
    finally:
        if not receiver_task.done():
            receiver_task.cancel()
            try:
                await receiver_task
            except (asyncio.CancelledError, Exception):
                pass
        if _audio_capture_wav is not None:
            try:
                _audio_capture_wav.close()
                logger.info("ws_voice: AUDIO CAPTURE closed (%d frames) bind=%s", _audio_frames, bind)
            except Exception:
                pass
        session.force_end()
        if whisperlive_client is not None:
            # Idempotent; safe to call even if already torn down.
            # Awaited so the recv-loop task finishes before the
            # route returns and the test fixture's event loop can
            # reach quiescence.
            try:
                await whisperlive_client.close()
            except Exception:
                logger.debug("ws_voice: whisperlive close failed bind=%s", bind)
        if superseded_event.is_set():
            # We were kicked by a later connection — that connection
            # already owns the buffer. Don't detach (would clear the
            # new owner's evict callback) or release (would drop
            # their buffer). Just close our socket and return.
            pass
        elif end_was_explicit:
            voice_buffer_mod.MANAGER.release(bind)
        else:
            voice_buffer_mod.MANAGER.detach(bind)
        try:
            await websocket.close()
        except Exception:
            pass


async def page_timeline(request):
    return HTMLResponse(_load_template("base.html"))

async def page_timeline_fragment(request):
    """Return the Activity page fragment shared by /timeline and /activity."""
    return templates.TemplateResponse(request, "pages/timeline.html")

async def page_trace_fragment(request):
    """Return the Trace page as an HTML fragment for SPA injection.

    The fragment is injected into #content by the client router, then
    Alpine.initTree() initialises the x-data="tracePage()" component.
    The component reads the run name from window.location.pathname on init.
    """
    return templates.TemplateResponse(request, "pages/trace.html")

async def page_terminal(request):
    return HTMLResponse(_load_template("base.html"))

async def page_terminal_fragment(request):
    """Return the Terminal page chrome as an HTML fragment for SPA injection."""
    return templates.TemplateResponse(request, "pages/terminal.html")


async def page_session_view(request):
    return HTMLResponse(_load_template("base.html"))

async def page_session_view_fragment(request):
    """Return the Session Viewer page as an HTML fragment for SPA injection."""
    return templates.TemplateResponse(request, "pages/session-view.html")


async def page_session_view_by_name(request):
    """Resolve /session/{session_id} → /session/{project}/{session_id}.

    Three-tier lookup (auto-eik9g):
      1. dashboard_db.get_session(name) — live sessions in the
         tmux_sessions table. Existing behaviour preserved.
      2. tools.graph.ops.get_session(name) — dead sessions ingested into
         the graph DB. The session source row carries metadata.project,
         so a dead session that was once ingested can still be navigated
         to its proper viewer URL.
      3. None of the above — render a 404 with a back-link instead of
         redirecting to /sessions?session=… (which was a no-op on the
         index page; the cause of the broken search → source → viewer
         workflow).

    Journal entries (auto-fjfki) link to /session/<source_session_id>
    with no project segment; the same /session/<tmux_session> chip on
    the source viewer also hits this route.
    """
    session_id = request.path_params["session_id"]
    if os.environ.get("DASHBOARD_MOCK"):
        return HTMLResponse(_load_template("base.html"))

    # Tier 1: live session in tmux_sessions.
    session = dashboard_db.get_session(session_id)
    project = (session or {}).get("project")
    if project:
        return RedirectResponse(
            url=f"/session/{project}/{session_id}",
            status_code=302,
        )

    # Tier 2: dead session in the graph DB. The graph source's metadata
    # carries the project so the viewer can render with the right org
    # scope and pull historical entries via the existing tail endpoint.
    try:
        from tools.graph import ops as _graph_ops
        graph_session = _graph_ops.get_session(session_id, org=None)
    except Exception:
        logger.warning(
            "page_session_view_by_name: graph.get_session(%s) failed",
            session_id, exc_info=True,
        )
        graph_session = None

    if graph_session:
        metadata = graph_session.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        project = (
            metadata.get("project")
            or metadata.get("graph_project")
            or graph_session.get("project")
        )
        if project:
            return RedirectResponse(
                url=f"/session/{project}/{session_id}",
                status_code=302,
            )

    # Tier 3: genuinely not found. Render a 404 with a back-link.
    # NOT a redirect to /sessions — that was the broken behaviour this
    # bead fixes.
    #
    # SECURITY (caught during auto-0524-000705 security review):
    # session_id is the URL path parameter and reaches this branch as
    # arbitrary text (Starlette's default ``str`` converter accepts
    # any percent-encoded characters including ``<>"'``). Interpolating
    # it un-escaped into HTML is reflected XSS — an attacker can craft
    # a URL whose injected script runs on the dashboard origin with
    # full access to the cookieless same-origin POSTs that mutate
    # session state. ``html.escape()`` is the fix.
    #
    # The Referer header is NOT used as a back-link target — browsers
    # do encode quote/angle characters, but a fixed safe destination
    # (``/sessions``) sidesteps any future surprise from header sources
    # we don't yet understand. The "back" affordance still works.
    import html as _html
    safe_session_id = _html.escape(session_id)
    body = (
        '<!doctype html><html><head><title>Session not found</title>'
        '<style>body{background:#0b0d12;color:#e5e7eb;'
        'font-family:-apple-system,BlinkMacSystemFont,sans-serif;'
        'padding:64px;max-width:640px;margin:0 auto;}'
        'a{color:#818cf8;text-decoration:none;}a:hover{color:#a5b4fc;}'
        'code{background:#1f2937;padding:2px 6px;border-radius:4px;'
        'font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}'
        '</style></head><body>'
        '<h1>Session not found</h1>'
        f'<p>No session with id <code>{safe_session_id}</code> exists in the '
        'dashboard or the graph database. It may have been deleted, or '
        'the link may be stale.</p>'
        '<p><a href="/sessions">← Back to sessions</a></p>'
        '</body></html>'
    )
    return HTMLResponse(body, status_code=404)


async def page_test_input(request):
    """Serve the mobile chat input prototype as a standalone full page.

    Why: safe-area-inset-bottom and visualViewport behaviors cannot be verified
    inside the Design Studio iframe — this route delivers the raw HTML with no
    dashboard chrome so real iOS keyboard/safe-area behavior can be tested.
    """
    from pathlib import Path
    no_cache = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    # Baked reference prototype (graph attachment 976c334a-97e)
    path = Path(__file__).resolve().parent / "test_fixtures/input-prototype.html"
    try:
        return HTMLResponse(path.read_text(), headers=no_cache)
    except FileNotFoundError:
        return PlainTextResponse(
            f"Prototype not found at {path}",
            status_code=404,
            headers=no_cache,
        )


async def page_voice_smoke(request):
    """Serve the standalone voice canary page at ``/_admin/voice-smoke``.

    This is intentionally NOT part of the Alpine SPA shell. The page is a
    self-contained admin canary for exercising the raw ``/ws/voice`` protocol
    from a real browser / iOS PWA surface using ``getUserMedia`` +
    ``AudioWorklet``. Keeping it standalone makes failures local and loud:
    if the page breaks, it's the canary's own JS rather than shell/router
    interference.

    Visibility is gated two ways:
      1. the dashboard's normal request auth surface (same as every other page)
      2. ``voice.pipe_enabled`` Settings flag
    """
    from tools.dashboard import feature_flags

    no_cache = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    }
    if not feature_flags.is_enabled("voice.pipe_enabled"):
        return PlainTextResponse(
            "voice.pipe_enabled is disabled",
            status_code=403,
            headers=no_cache,
        )
    return HTMLResponse(
        _load_template("admin/voice-smoke.html"),
        headers=no_cache,
    )


_TEST_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Access-Control-Allow-Origin": "*",
}
_test_debug: dict = {}
_test_version = {"v": 1}


async def api_test_debug_post(request):
    global _test_debug
    try:
        _test_debug = await request.json()
    except Exception:
        _test_debug = {}
    return Response(status_code=204, headers=_TEST_NO_CACHE)


async def api_test_debug_get(request):
    return JSONResponse(_test_debug, headers=_TEST_NO_CACHE)


async def api_test_version_get(request):
    return JSONResponse(_test_version, headers=_TEST_NO_CACHE)


async def api_test_version_bump(request):
    _test_version["v"] += 1
    return JSONResponse(_test_version, headers=_TEST_NO_CACHE)


_test_toast = {"msg": ""}


async def api_test_toast_get(request):
    return JSONResponse(_test_toast, headers=_TEST_NO_CACHE)


async def api_test_toast_post(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    _test_toast["msg"] = body.get("msg", "") if isinstance(body, dict) else ""
    return Response(status_code=204, headers=_TEST_NO_CACHE)


# ── Design Studio API ────────────────────────────────────────────

def _resolve_design_id_or_error(raw_id: str):
    """Resolve partial design/revision UUID. Returns (full_id, None) or (None, JSONResponse)."""
    full_id, matches = resolve_design_prefix(raw_id)
    if full_id:
        return full_id, None
    if matches:
        return None, JSONResponse(
            {"error": "ambiguous prefix", "matches": matches}, status_code=400
        )
    return None, JSONResponse({"error": "not found"}, status_code=404)


async def api_design_create(request):
    """Create a new design revision. Returns {id: uuid}."""
    body = await request.json()
    title = body.get("title", "Untitled Design")
    description = body.get("description")
    fixture = body.get("fixture")  # JSON string or dict
    variants = body.get("variants", [])
    # Accept both new and legacy field names
    design_id = body.get("design_id") or body.get("series_id")
    alpine = bool(body.get("alpine"))  # inject Alpine.js runtime in iframe
    creator_session_id = body.get("creator_session_id")
    creator_session_label = body.get("creator_session_label")
    force = body.get("force") is True

    # Stamp the design with the caller's middleware-approved org (invariant 1):
    # the token's org for an agent (authoritative, un-widenable), the operator's
    # selection otherwise. The design is then only visible to that org (and the
    # operator). A body/query org is never trusted for this.
    creator_org = api_auth.organization_scope_from_request(request)

    if not variants:
        return JSONResponse({"error": "At least one variant required"}, status_code=400)

    # Ensure fixture is stored as JSON string
    if fixture and not isinstance(fixture, str):
        fixture = json.dumps(fixture)

    try:
        rev_id = await asyncio.to_thread(
            create_design,
            title=title,
            description=description,
            fixture=fixture,
            variants=variants,
            design_id=design_id,
            alpine=alpine,
            creator_session_id=creator_session_id,
            creator_session_label=creator_session_label,
            org=creator_org,
            force=force,
        )
    except DuplicateDesignTitleError as exc:
        return JSONResponse(
            {
                "error": "duplicate_design_name",
                "message": (
                    f"A design named {title!r} already exists. Append a revision "
                    "with design_id, or retry with force=true."
                ),
                "existing": exc.existing,
            },
            status_code=409,
        )

    # Broadcast to SSE so gallery pages auto-update without refresh
    design_data = await asyncio.to_thread(get_design, rev_id)
    if design_data:
        topic = f"design:{design_data['design_id']}"
        await event_bus.broadcast(topic, {
            "revision_id": rev_id,
            "design_id": design_data["design_id"],
            "revision_seq": design_data["revision_seq"],
        })
        # Design Studio already exposes the creator-session presence edge.
        # Publish its inverse too so an open full-page session viewer can
        # reveal a return control as soon as a design is linked or revised.
        linked_session = design_data.get("linked_session") or creator_session_id
        if linked_session:
            await event_bus.broadcast(f"session-design:{linked_session}", {
                "revision_id": rev_id,
                "latest_revision_id": rev_id,
                "design_id": design_data["design_id"],
                "title": design_data.get("title") or "Untitled Design",
            })
            await event_bus.broadcast(
                SESSION_CONTRIBUTIONS_TOPIC,
                {"session_id": linked_session},
                dedup=False,
            )

    return JSONResponse({"id": rev_id}, status_code=201)


async def api_design_poll(request):
    """Poll design revision status. 202 while pending, 200 with results when completed."""
    rev_id, err = _resolve_design_id_or_error(request.path_params["id"])
    if err:
        return err
    design = await asyncio.to_thread(get_design, rev_id)
    # Org-scope (invariant 1): cross-org/unattributable → the same 404 as absent.
    if not design or api_auth.caller_org_scope_hides(request, design.get("org")):
        return JSONResponse({"error": "not found"}, status_code=404)

    if design["status"] == "pending":
        return JSONResponse({"status": "pending", "id": rev_id}, status_code=202)

    # Completed — return results
    results = []
    for v in design["variants"]:
        if v["selected"]:
            results.append({"id": v["id"], "rank": v["rank"]})
    results.sort(key=lambda x: x["rank"] or 999)
    return JSONResponse({
        "status": "completed",
        "id": rev_id,
        "results": results,
    })


async def api_design_get(request):
    """Get full design revision data for gallery rendering."""
    rev_id, err = _resolve_design_id_or_error(request.path_params["id"])
    if err:
        return err
    design = await asyncio.to_thread(get_design, rev_id)
    # Org-scope (invariant 1): a cross-org — or unattributable — design is the
    # same 404 a nonexistent one returns, so existence never leaks across orgs.
    if not design or api_auth.caller_org_scope_hides(request, design.get("org")):
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(design)


async def api_design_submit(request):
    """Submit ranking results."""
    rev_id, err = _resolve_design_id_or_error(request.path_params["id"])
    if err:
        return err
    design = await asyncio.to_thread(get_design, rev_id)
    if not design or api_auth.caller_org_scope_hides(request, design.get("org")):
        return JSONResponse({"error": "design not found"}, status_code=404)
    body = await request.json()
    selections = body.get("selections", [])
    ok = await asyncio.to_thread(submit_results, rev_id, selections)
    if not ok:
        return JSONResponse({"error": "design not found"}, status_code=404)
    return JSONResponse({"ok": True})


async def api_design_pending(request):
    """List pending designs (for toast notifications)."""
    pending = await asyncio.to_thread(list_pending_designs)
    # Org-scope the list (invariant 1): an org caller sees only its own org's
    # pending designs; the operator sees all.
    pending = [
        d for d in pending
        if not api_auth.caller_org_scope_hides(request, d.get("org"))
    ]
    return JSONResponse(pending)


async def api_design_dismiss(request):
    """Dismiss a design and all pending revisions."""
    rev_id, err = _resolve_design_id_or_error(request.path_params["id"])
    if err:
        return err
    design = await asyncio.to_thread(get_design, rev_id)
    if not design or api_auth.caller_org_scope_hides(request, design.get("org")):
        return JSONResponse({"error": "design not found"}, status_code=404)
    ok = await asyncio.to_thread(dismiss_design, rev_id)
    if not ok:
        return JSONResponse({"error": "design not found"}, status_code=404)
    return JSONResponse({"ok": True})



async def api_design_screenshot(request):
    """Save a screenshot blob for a design revision and optionally inject into agent.

    POST /api/design/{id}/screenshot?tmux_session=chatwith-xxx
    Body: raw image bytes (Content-Type: image/png or image/*)
    Returns: {path: "/absolute/path/to/screenshot.png", injected: bool}

    When tmux_session is provided, performs the two-send image injection:
    1. docker cp screenshot into container (so path exists for Claude Code)
    2. Send bare image path as first message (triggers isMeta=True image injection)
    3. 200ms later, send follow-up text so agent knows to act on the image
    """
    rev_id, err = _resolve_design_id_or_error(request.path_params["id"])
    if err:
        return err
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        return JSONResponse({"error": "content-type must be image/*"}, status_code=400)

    design = await asyncio.to_thread(get_design, rev_id)
    if not design or api_auth.caller_org_scope_hides(request, design.get("org")):
        return JSONResponse({"error": "not found"}, status_code=404)

    screenshot_dir = DATA_ROOT / "experiments" / rev_id
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = screenshot_dir / "screenshot.png"

    body = await request.body()
    await asyncio.to_thread(screenshot_path.write_bytes, body)

    abs_path = str(screenshot_path.resolve())

    # Two-send image injection when tmux_session is provided
    tmux_session = request.query_params.get("tmux_session", "").strip()
    injected = False
    if tmux_session and _tmux_session_exists(tmux_session):
        # Same delivery path as a normal image upload: host sessions read the
        # host path, container sessions get it docker-cp'd in (auto-resolved).
        image_path = _deliver_file_to_session(tmux_session, abs_path, "/tmp/screenshot.png")
        # First send: bare image path (triggers isMeta=True image injection)
        await tmux_send(tmux_session, image_path)
        # Second send: follow-up text so agent sees the image and acts
        await tmux_send(
            tmux_session,
            "Screenshot captured — describe what you see and continue iterating",
        )
        injected = True
        logger.info("[screenshot] injection complete for %s", tmux_session)

    return JSONResponse({"path": abs_path, "injected": injected})


async def page_experiments_redirect(request):
    """Redirect /experiments/{id} to /design/{id} for backwards compat."""
    exp_id = request.path_params["id"]
    return RedirectResponse(url=f"/design/{exp_id}", status_code=301)


# ── HTML Pages ────────────────────────────────────────────────

def _load_template(name: str, **context) -> str:
    # Render via Jinja so {% include %} partials are expanded; the static
    # version token stays a literal marker Jinja leaves untouched.
    # ``shell_org`` flows into ``base.html`` as the deployment's
    # effective org so the SPA can stamp ``X-Graph-Org`` on shell-route
    # fetches (auto-t0auy).
    content = templates.env.get_template(name).render(
        shell_org=_dashboard_default_org(),
        **context,
    )
    return content.replace("__STATIC_VERSION__", _static_version())


async def api_version(request):
    return JSONResponse({"version": _static_version()})


async def page_web_push_proof(request):
    return HTMLResponse(_load_template("web-push-proof.html"))

def _welcome_gate_open() -> bool:
    """True when the onboarding empty-state still holds (bead auto-inpkd).

    Layer-1 gate, above the Layer-0 harness bootstrap: while the machine
    lacks a personal identity OR a collaborative organization, the Welcome
    shell renders instead of the session UI, then passes through silently
    once both hold. It never intercepts again.

    Workspace is step three's destination (the board), not a gate input:
    the committed design (graph://9a4219b3) has no "opening a workspace"
    intermediate state — it collapses that step into the exit CTA — and
    there is no reliable server-side workspace signal that would not regress
    an established install. Identity + a collaborative org are the two hard,
    verifiable signals; the third is "you're now on the board."

    Any read error fails OPEN (gate closed) so a substrate hiccup never
    traps the dashboard behind onboarding.
    """
    try:
        return not (_has_personal_identity() and _has_collaborative_org())
    except Exception:
        logger.exception("welcome gate check failed; not gating")
        return False


def _has_personal_identity() -> bool:
    """Whether this machine carries an enrolled personal identity.

    The same signal ``/api/identity/status`` reports as ``personal_identity``:
    a canonical personal row whose payload holds armored key material. Pinned
    to the personal DB (``org=None``) inside ``_personal_member`` — never
    follows caller-org context.
    """
    from tools.dashboard import identity_routes
    member = identity_routes._personal_member()
    return bool(member is not None and member.payload.get("armored_private_key"))


def _has_collaborative_org() -> bool:
    """Whether any collaborative organization exists on this machine.

    ``list_orgs`` enumerates ``data/orgs/*.db``; the operator's own
    ``personal`` store is not a collaborative org, so it is excluded. A
    genuinely fresh invite-join machine (personal store only) reads False
    here until the operator creates or joins one.
    """
    from tools.graph import org_ops
    return any(ref.slug != "personal" for ref in org_ops.list_orgs())


def _fleet_enrollment_first_render() -> dict | None:
    """Public, non-authoritative state for an install awaiting approval.

    The comparison code is designed to be shown on both machines. No invite
    bearer, resume credential, root armor, machine identity, or signing
    material enters the page.
    """
    try:
        from tools.network.fleet_enrollment_client import FleetJoinStateStore

        state = FleetJoinStateStore()
        recovery = state.latest_any()
    except Exception:
        logger.exception("could not read local fleet enrollment state")
        return None
    if recovery is None:
        return None
    return {
        # Delivery is durable in machine.db. Rendering this fact directly
        # makes a process death after root delivery recover to the approved
        # unlock screen without depending on one more anonymous relay poll.
        "status": (
            "approved"
            if state.load_delivery(recovery.request_id) is not None
            else "pending"
        ),
        "code": recovery.verification_code,
        "request_id_prefix": recovery.request_id[:12],
    }


def _welcome_page(*, fleet_sync: bool = False) -> HTMLResponse:
    return HTMLResponse(_load_template(
        "welcome.html",
        fleet_enrollment=_fleet_enrollment_first_render(),
        fleet_sync=fleet_sync,
    ))


async def page_index(request):
    # First-launch gate: with no verified harness recorded, render the
    # deterministic bootstrap walkthrough rather than the session UI. This is
    # a server-side decision (not a client redirect) so a clean machine lands
    # on setup, not on a session-create board it cannot use yet.
    # A Fleet install already made its first network request. Show the exact
    # comparison state first, even when the generic harness/identity gates
    # would otherwise own a clean machine's initial page.
    #
    # Only while the enrollment is still IN FLIGHT. `approved` means the
    # delivery landed and the machine is on the roster — the join is over, and
    # continuing to render the comparison screen strands a working fleet member
    # on a welcome page it can never leave. Nothing deleted the join-state row
    # on success, so `_fleet_enrollment_first_render()` kept returning a value
    # forever and every subsequent visit re-entered the welcome shell (observed
    # live 2026-08-30 on a node that had joined days earlier).
    _fleet_first = _fleet_enrollment_first_render()
    if _fleet_first is not None and _fleet_first.get("status") != "approved":
        return _welcome_page()
    # NO harness gate here. It used to render the Layer-0 walkthrough whenever
    # no harness CLI resolved on PATH *of the process serving this page*, which
    # asks the wrong machine: the node never runs a harness. Sessions do, and
    # the session image carries claude and codex (agents/Dockerfile). A
    # containerized node therefore always failed the probe and served first-run
    # setup forever, on a machine that was already enrolled, credentialed and
    # able to launch sessions.
    #
    # The walkthrough still exists and is still reachable at /bootstrap for an
    # operator who wants it; it just no longer intercepts the front page on a
    # detection that cannot be true here.
    #
    # A fleet member's collaborative orgs arrive as SYNCED roster entries, not
    # local creations. Materialise any org DB stub the synced org roster names
    # but this machine has not built yet, BEFORE the empty-state gate — so a
    # joined member lands on its (syncing) dashboard instead of being asked to
    # "create or join an organization" for orgs it already belongs to. Local +
    # idempotent (no network); a genuine fresh machine has an empty roster, so
    # this is a no-op and the create/join step still shows correctly.
    if _has_personal_identity():
        try:
            from tools.network.fleet_sync_scheduler import (
                materialize_org_scopes_from_roster,
            )
            materialize_org_scopes_from_roster()
        except Exception:
            logger.exception("org scope materialisation failed; continuing to gate")
    # Empty-state gate (bead auto-inpkd): once the harness is set up but the
    # machine still lacks an identity or an organization, the Welcome shell
    # renders — same server-side-decision pattern, one layer up.
    if _welcome_gate_open():
        return _welcome_page()
    return RedirectResponse(url="/beads")


async def page_welcome(request):
    """GET /welcome — the onboarding empty-state shell.

    Always serves the shell; the page reads identity + org state on load and
    renders the matching step (fresh / mid / ready). Reachable directly so an
    invited operator can land here org-attached, and so setup can be revisited
    even after the gate has closed.
    """
    return _welcome_page(
        fleet_sync=request.query_params.get("fleet_sync") == "1"
    )


async def page_bootstrap(request):
    """GET /bootstrap — the Layer-0 harness setup walkthrough.

    Always serves the walkthrough shell; the page probes on load and, once a
    harness verifies, its Start button lands on ``/`` (which then falls
    through to the session UI). Reachable directly so an operator can revisit
    setup even after the gate has closed.
    """
    return HTMLResponse(_load_template("bootstrap.html"))


async def api_bootstrap_probe(request):
    """GET /api/bootstrap/probe — discover claude/codex state on this host.

    Discovery only: resolves each CLI on PATH, parses --version, and runs one
    authenticated no-op to classify not-installed / needs-sign-in / ready.
    Writes nothing.
    """
    harnesses = await asyncio.to_thread(_harness_bootstrap.probe_all)
    return JSONResponse({"harnesses": list(harnesses.values())})


async def _bootstrap_harness_arg(request) -> str | None:
    try:
        body = await request.json()
    except Exception:
        return None
    slug = (body or {}).get("harness")
    if slug in _harness_bootstrap.HARNESS_SPECS:
        return slug
    return None


async def api_bootstrap_verify(request):
    """POST /api/bootstrap/verify {harness} — verify + record one harness.

    Runs the live probe (version + authenticated no-op) and, when the CLI is
    present, upserts its ``autonomy.harness.bootstrap#1`` discovery row. The
    row carries discovery results ONLY — never any credential material.
    Returns the probe result.
    """
    slug = await _bootstrap_harness_arg(request)
    if not slug:
        return JSONResponse(
            {"error": "harness must be 'claude' or 'codex'"}, status_code=400,
        )
    result = await asyncio.to_thread(_harness_bootstrap.verify_and_record, slug)
    return JSONResponse(result)


async def api_bootstrap_install(request):
    """POST /api/bootstrap/install {harness} — run the guided install.

    Runs the harness's own installer command on the host, then re-probes and
    records. Auth is NOT attempted here — sign-in stays inside the harness's
    own tooling. Returns ``{ok, result, error}``.
    """
    slug = await _bootstrap_harness_arg(request)
    if not slug:
        return JSONResponse(
            {"error": "harness must be 'claude' or 'codex'"}, status_code=400,
        )
    spec = _harness_bootstrap.HARNESS_SPECS[slug]
    cmd = spec["install_cmd"].split()
    code, out = await asyncio.to_thread(
        _harness_bootstrap._run, cmd, 600,
    )
    if code != 0:
        return JSONResponse(
            {"ok": False, "error": out or "install command failed",
             "install_cmd": spec["install_cmd"]},
            status_code=502,
        )
    result = await asyncio.to_thread(_harness_bootstrap.verify_and_record, slug)
    return JSONResponse({"ok": True, "result": result})

async def page_network_join(request):
    """The invite bridge's local-origin half (auto-1ihgz): display/consent
    shell for an org:join invitation. One static template, rendered
    client-side from the URL's query + fragment — the server reads neither,
    and the page contains no input fields (the passphrase must never gain
    an HTTP ingress, I1). Acceptance mechanics await auto-9rw91's ruling."""
    return HTMLResponse(
        _load_template("network-join.html"),
        headers={"Cache-Control": "no-store",
                 "Referrer-Policy": "no-referrer",
                 "X-Content-Type-Options": "nosniff"},
    )


async def page_unlock(request):
    """The Unlock screen (mockup d49be06b 'Unlock' state) — the one page
    the human gate never covers. Skips itself when there is nothing to
    unlock (gate not enforced) or the session is already valid.

    EXCEPTION — a fleet completion (``?fleet=1``) is a ROOT ceremony, not a
    login. Finishing a fleet join needs the root seed to seal/complete, which a
    dashboard session does NOT provide. Skipping the ceremony just because a
    session exists sends a signed-in operator back to ``next`` (/welcome), which
    routes right back here — the exact loop between "log in" and "do the
    ceremony". So when ``fleet=1`` asks for the ceremony, present it even with a
    valid session; unlock.js runs the root ceremony + fleet completion."""
    fleet_root = request.query_params.get("fleet") == "1"
    if not unlock_routes.human_auth_enrolled():
        return RedirectResponse(
            url=unlock_routes.sanitize_next(request.query_params.get("next")))
    if not fleet_root \
            and unlock_routes.session_from_request(request) is not None:
        return RedirectResponse(
            url=unlock_routes.sanitize_next(request.query_params.get("next")))
    return HTMLResponse(_load_template("unlock.html"))

async def page_beads(request):
    return HTMLResponse(_load_template("base.html"))

async def page_beads_fragment(request):
    """Return the Beads page as an HTML fragment for SPA injection.

    Rendered via Jinja2. The fragment is injected into #content by the client
    router, then Alpine.initTree() initialises the x-data="beadsPage()" component.
    """
    return templates.TemplateResponse(request, "pages/beads.html")

async def page_dispatch(request):
    return HTMLResponse(_load_template("base.html"))

async def page_dispatch_fragment(request):
    """Return the Dispatch page as an HTML fragment for SPA injection.

    Rendered via Jinja2 so {% include %} partials work.
    The fragment is injected into #content by the client router, then
    Alpine.initTree() initialises the x-data="dispatchPage()" component.
    """
    return templates.TemplateResponse(request, "pages/dispatch.html")

async def page_sessions(request):
    return HTMLResponse(_load_template("base.html"))

async def page_sessions_fragment(request):
    """Return the Sessions page as an HTML fragment for SPA injection."""
    return templates.TemplateResponse(request, "pages/sessions.html")

async def page_worktrees(request):
    return HTMLResponse(_load_template("base.html"))

async def page_worktrees_fragment(request):
    """Return the Worktrees page as an HTML fragment for SPA injection."""
    return templates.TemplateResponse(request, "pages/worktrees.html")

def _worktree_file_json(file: GitFileChange) -> dict:
    return {
        "status": file.status,
        "path": file.path,
        "additions": file.additions,
        "deletions": file.deletions,
        "is_dir": file.is_dir,
    }

def _worktree_commit_json(commit: WorktreeCommit, *, include_patch: bool = False) -> dict:
    additions = sum(file.additions for file in commit.files)
    deletions = sum(file.deletions for file in commit.files)
    data = {
        "sha": commit.sha,
        "short_sha": commit.short_sha,
        "subject": commit.subject,
        "author": commit.author,
        "date": commit.date,
        "body": commit.body,
        "files": [_worktree_file_json(file) for file in commit.files],
        "stats": {
            "files": len(commit.files),
            "additions": additions,
            "deletions": deletions,
        },
    }
    if include_patch:
        data["patch"] = commit.patch or ""
    return data


def _worktree_dirty_detail_json(detail: WorktreeDirtyDetail) -> dict:
    payload: dict = {
        "files": [_worktree_file_json(file) for file in detail.files],
        "patch": detail.patch or "",
    }
    if getattr(detail, "stale", False):
        payload["stale"] = True
        if getattr(detail, "reason", None):
            payload["reason"] = detail.reason
    return payload


async def _signal_session_merge_celebration(
    *,
    target_session: str,
    repo_name: str,
    commit_sha: str,
    commit_message: str,
    kind: str,
) -> None:
    """Send a "you got merged" CrossTalk into the target session.

    Reuses ``_send_dashboard_ui_crosstalk`` (the same path the
    rebase-required notification uses) so the message lands in the
    session's tmux pane with the canonical Dashboard UI envelope.
    Best-effort: tmux failures are logged but never re-raised — the
    merge already succeeded.
    """
    if not target_session:
        return
    short_sha = (commit_sha or "")[:7]
    first_line = (commit_message or "").split("\n", 1)[0].strip()
    if len(first_line) > 200:
        first_line = first_line[:200] + "…"
    suffix_by_kind = {
        "ff": " (ff merge)",
        "cherry-pick": " (cherry-pick)",
    }
    message = (
        f"You got merged! {repo_name}@{short_sha}"
        + (f" — {first_line}" if first_line else "")
        + suffix_by_kind.get(kind, "")
        + "\nReminder: the dashboard hot-reloads — merged Python + templates are"
          " LIVE immediately, no redeploy. Only a browser/PWA client restart is"
          " needed to pick up changed static JS. (Non-dashboard components —"
          " Go CLI, registry — deploy on their own paths.)"
    )
    try:
        await _send_dashboard_ui_crosstalk(
            target_session, message, await_delivery=True,
        )
    except WorkspaceError:
        # Target session's tmux is gone. Log it — a silent drop here was the
        # invisible failure mode: the operator saw no "you got merged" and had
        # no trace to diagnose from.
        logger.warning(
            "merge celebration: target session %s has no live tmux; "
            "'you got merged' notification not delivered",
            target_session,
        )
    except Exception:
        logger.exception(
            "merge celebration: dashboard-ui crosstalk failed for target=%s",
            target_session,
        )


def _find_worktree_row(rows: list[WorktreeState], session_name: str, repo_name: str) -> WorktreeState | None:
    return next(
        (
            item for item in rows
            if item.session_name == session_name and item.repo_name == repo_name
        ),
        None,
    )


async def _send_dashboard_ui_crosstalk(
    target_session: str, message: str, *, await_delivery: bool = False,
) -> None:
    """Deliver a dashboard-authored CrossTalk message to one live session.

    With ``await_delivery=True`` the tmux paste is awaited to completion
    (``tmux_send_awaited``) instead of scheduled fire-and-forget
    (``tmux_send``, which returns before its ~0.8s paste+Enter worker runs).
    The merge-celebration path needs this: a merge that lands dashboard code
    triggers a ``uvicorn --reload`` restart, and a fire-and-forget paste can
    be killed mid-flight before the keystrokes land.
    """
    if not _tmux_session_exists(target_session):
        raise WorkspaceError(f"target session not found: {target_session}")

    sender = "dashboard-ui"
    label = "Dashboard UI"
    iso_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    envelope = (
        f'<crosstalk from="{sender}"\n'
        f'           label="{label}"\n'
        f'           source="" turn="0"\n'
        f'           harness="dashboard" model=""\n'
        f'           timestamp="{iso_now}">\n'
        f'{message}\n'
        f'</crosstalk>'
    )

    if await_delivery:
        await tmux_send_awaited(target_session, envelope)
    else:
        await tmux_send(target_session, envelope)
    await asyncio.to_thread(
        auth_db.insert_message,
        sender,
        label,
        target_session,
        None,
        None,
        message,
        time.time(),
    )


async def _terminal_crosstalk_notifier(target_session: str, message: str) -> None:
    """Deliver a per-PR terminal CrossTalk for ``nag_when_terminal`` rows.

    Wraps :func:`_send_dashboard_ui_crosstalk` so a dead session or
    transient tmux failure logs but does not propagate — the cache
    write that triggered this call already succeeded, and the worktree
    monitor would otherwise drop the firing-state record on a raised
    exception (see :meth:`WorktreeMonitor._fire_terminal_transitions`).
    """
    try:
        await _send_dashboard_ui_crosstalk(target_session, message)
    except WorkspaceError:
        # Session went dead between cache write and notify — nothing
        # to do, the worktree monitor still records this fire so we
        # don't re-attempt for the same head_sha.
        pass
    except Exception:
        logger.exception(
            "terminal CrossTalk delivery failed for target=%s", target_session,
        )


def _session_meta_for_tmux(tmux_name: str) -> dict:
    """Look up a session's title + project + harness in one pass.

    ``project`` lets the worktree review screen build a deeplink back
    to the page-mode session viewer (``/session/<project>/<tmux>``)
    for any live row, so the operator can hop from a commit/dirty
    review straight to the conversation that produced it.

    ``harness`` and ``model`` are surfaced (auto-ngis4 / icon-rail
    principle 553c7437-036) so worktree rows can render a harness badge
    inline without a second lookup.
    """
    from tools.dashboard.org_identity import resolve_session_org

    row = dashboard_db.get_session(tmux_name) or {}
    return {
        "title": (row.get("label") or "").strip(),
        "project": (row.get("project") or "").strip(),
        "harness": row.get("harness") or None,
        "model": row.get("model") or None,
        # Resolved org identity {slug,name,color,initial,...} — the
        # worktrees page scopes its whole view to one org at a time.
        "org": resolve_session_org(row),
    }


def _worktree_state_json(row: WorktreeState) -> dict:
    meta = _session_meta_for_tmux(row.session_name)
    payload: dict = {
        "session_name": row.session_name,
        "session_title": meta["title"],
        "session_project": meta["project"],
        # auto-ngis4: surface the running session's harness (claude / codex)
        # so the worktree review chrome can render the icon-rail badge.
        "session_harness": meta["harness"],
        "session_model": meta["model"],
        "org": meta["org"],
        "repo_name": row.repo_name,
        "worktree_path": str(row.worktree_path),
        "managed_clone": str(row.managed_clone) if row.managed_clone else None,
        "branch": row.branch,
        "commits_ahead": row.commits_ahead,
        "is_dirty": row.is_dirty,
        "ff_eligible": row.ff_eligible,
        "clone_stale": row.clone_stale,
        "rebase_required": row.rebase_required,
        "session_live": row.session_live,
        "cherry_pick_eligible": row.cherry_pick_eligible,
        "cherry_pick_commit": row.cherry_pick_commit,
        # PURE: read the label resolved on the sweep's worker thread and
        # carried on the row (auto-yq27f). This serializer must never spawn
        # git/subprocess/filesystem work — it runs on the event loop for
        # every /api/worktrees poll, and a single blocking call here stalls
        # SSE delivery for every connected viewer. Enforced by
        # test_worktree_state_json_is_pure_no_git.
        "target_branch": row.target_branch,
        "commits": [_worktree_commit_json(commit) for commit in row.commits],
        # The list payload ships only a 3-entry preview; the true count travels
        # separately so the UI can render "+N more" without the full array, and
        # the per-worktree /changes endpoint serves the complete list on demand.
        "dirty_count": len(row.dirty_files),
        "dirty_files": [_worktree_file_json(file) for file in row.dirty_files[:3]],
        "net_empty": row.net_empty,
        "orphaned": row.orphaned,
        "duplicate_commits": [
            {
                "sha": dup.sha,
                "short_sha": dup.short_sha,
                "subject": dup.subject,
                "of_session": dup.of_session,
                "of_repo": dup.of_repo,
            }
            for dup in row.duplicate_commits
        ],
    }
    snapshot = worktree_monitor.get_source_control(row.session_name, row.repo_name)
    if snapshot is not None:
        payload["source_control"] = snapshot
    return payload

def _cleanup_result_json(result) -> dict:
    return {
        "removed": list(result.removed),
        "preserved": [
            {"path": path, "reason": reason}
            for path, reason in result.preserved
        ],
        "errors": [
            {"path": path, "error": error}
            for path, error in result.errors
        ],
    }

def _org_filter_param(request) -> str:
    return (request.query_params.get("org") or "").strip()


def _filter_worktree_payload_by_org(payload: list[dict], org: str) -> list[dict]:
    """Keep rows whose resolved org slug matches ``org`` (no-op when empty)."""
    if not org:
        return payload
    return [p for p in payload if ((p.get("org") or {}).get("slug") or "") == org]


def _worktree_org_session_filter(org: str):
    """Session-name predicate for org-scoped worktree scans.

    Resolves each session's org through the same identity cascade the
    row payloads use; memoized per call because the scan asks once per
    session directory and sessions repeat across repos.
    """
    from tools.dashboard.org_identity import session_org_slug

    memo: dict[str, bool] = {}

    def _match(session_name: str) -> bool:
        hit = memo.get(session_name)
        if hit is None:
            row = dashboard_db.get_session(session_name) or {}
            hit = session_org_slug(row) == org
            memo[session_name] = hit
        return hit

    return _match


async def api_worktrees(request):
    org = _org_filter_param(request)
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse(_filter_worktree_payload_by_org(dao_sessions.get_worktrees(), org))
    # Fast path: the sweep already rendered + encoded this payload on its
    # worker thread (auto-yq27f). Serve the pre-encoded bytes directly —
    # zero git, zero json.dumps on the event loop, so SSE frames keep
    # flowing while this returns. ``org`` selects a pre-partitioned slice.
    rendered = worktree_monitor.get_rendered_json(org)
    if rendered is not None:
        return Response(rendered, media_type="application/json")
    # Fallback (cold cache before the first sweep, or a monitor with no
    # renderer registered — e.g. some tests): render per-request. Still
    # pure git-wise, since the target-branch label lives on the row.
    payload = [
        _worktree_state_json(row)
        for row in worktree_monitor.get_all()
    ]
    return JSONResponse(_filter_worktree_payload_by_org(payload, org))


async def api_worktrees_orgs(request):
    """Org summary for the worktrees page: one entry per org with counts.

    Backs the org dropdown (the page always shows exactly one org at a
    time), so it has to be cheap: aggregates the monitor's cached rows
    through the pure serializer — no git work. The target-branch label
    the serializer used to resolve per row (and the ~0.5s git sweep that
    caused) now lives on the row itself, resolved once per background
    sweep (auto-yq27f), so this claim is now true rather than aspirational.
    """
    if os.environ.get("DASHBOARD_MOCK"):
        payload = dao_sessions.get_worktrees()
    else:
        payload = [_worktree_state_json(row) for row in worktree_monitor.get_all()]
    orgs: dict[str, dict] = {}
    for row in payload:
        identity = row.get("org") or {}
        slug = identity.get("slug") or "unknown"
        entry = orgs.setdefault(slug, {
            "slug": slug,
            "name": identity.get("name") or slug,
            "color": identity.get("color"),
            "initial": identity.get("initial"),
            "favicon": identity.get("favicon"),
            "worktrees": 0,
            "commits": 0,
            "dirty": 0,
        })
        entry["worktrees"] += 1
        entry["commits"] += len(row.get("commits") or [])
        entry["dirty"] += 1 if row.get("is_dirty") else 0
    ordered = sorted(orgs.values(), key=lambda e: (-e["worktrees"], e["slug"]))
    return JSONResponse(ordered)


async def api_worktrees_refresh(request):
    org = _org_filter_param(request)
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse(_filter_worktree_payload_by_org(dao_sessions.get_worktrees(), org))
    # Top-level Refresh = scoped git sweep + repo-LEVEL PR discovery.
    # Discovery is one ``gh pr list`` per repo (host mode), NOT the
    # per-row fan-out that caused the historical rate-limit trouble —
    # that anti-pattern stays dead. Per-row force-fetch remains on
    # ``POST /api/worktrees/{session}/{repo}/refresh`` (see below).
    #
    # With ``?org=`` the git sweep itself is scoped: only that org's
    # session worktrees are rescanned (the page shows one org at a time,
    # so a Refresh click shouldn't pay for every other org's git calls);
    # the other orgs' rows stay cached.
    if org:
        session_filter = _worktree_org_session_filter(org)
        rows = await worktree_monitor.refresh(session_filter=session_filter)
        scoped_rows = [r for r in rows if session_filter(r.session_name)]
    else:
        rows = await worktree_monitor.refresh()
        scoped_rows = rows
    # Repo-level PR discovery (auto-jwbgb): one host-mode ``gh pr list``
    # per GitHub-backed repo in scope, hydrating every row — dead
    # sessions included — before the payload is built. Operator-initiated
    # only; the background tick never discovers.
    await worktree_monitor.discover_prs(scoped_rows)
    # Then chase full check state for the (bounded) set of rows with
    # review bindings — discovery is identity-only, and a PR badge with
    # zero checks behind it reads green-by-absence.
    await worktree_monitor.refresh_bound_rows(scoped_rows)
    # discover_prs / refresh_bound_rows mutated source-control snapshots
    # after refresh() rendered — re-render so the shared pre-encoded cache
    # the next poll serves reflects the freshly discovered PR state
    # (auto-yq27f).
    await worktree_monitor.refresh_rendered_cache()
    payload = [
        _worktree_state_json(row)
        for row in rows
    ]
    return JSONResponse(_filter_worktree_payload_by_org(payload, org))


async def api_worktree_refresh(request):
    """Force-refresh a single worktree row's source_control snapshot.

    The operator-explicit force-GET path. Scoped to one
    ``(session, repo)`` row so a click on the review overlay's
    Refresh button doesn't fan out gh calls for every live row.
    Bypasses the per-row TTL + poll budget; rate-limit backoff is
    still respected.

    Behavior on edge cases:

    * **Row not found in the scan** → 404. The worktree directory
      doesn't exist on disk anymore.
    * **Row found but session is dead** → 200 with the row JSON.
      ``refresh_one`` fetches PR state in host mode when the row has
      review bindings and a host token file is configured for its git
      host (auto-rn1dp); otherwise it skips the capability fetch (no
      live container to ``docker exec`` into). The local-git rescan
      runs either way and any cached snapshot is preserved.
      Caller can detect dead via ``session_live=False`` in the
      returned row. Capability re-resolution for dead sessions is
      a separate architectural piece — see graph://d9764756-c49.
    * **Row found and live** → 200 with the row JSON. Fresh capability
      fetch happens (subject to rate-limit backoff).
    """
    session_name = request.path_params["session"]
    repo_name = request.path_params["repo"]

    if os.environ.get("DASHBOARD_MOCK"):
        rows = dao_sessions.get_worktrees()
        row = next(
            (
                r for r in rows
                if r.get("session_name") == session_name
                and r.get("repo_name") == repo_name
            ),
            None,
        )
        if row is None:
            return JSONResponse({"error": "worktree not found"}, status_code=404)
        return JSONResponse(row)

    nag_when_terminal_raw = request.query_params.get("nag_when_terminal", "")
    nag_when_terminal = (
        str(nag_when_terminal_raw).strip().lower() in ("1", "true", "yes", "on")
    )
    if nag_when_terminal:
        # Arm before refresh so the cache write that follows triggers
        # a terminal CrossTalk if checks are already green/red. The 2h
        # cap matches the smart-cadence ceiling baked into
        # ``next_poll_delay``.
        from tools.dashboard.worktree_monitor import (
            NAG_WHEN_DONE,
            NAG_DONE_TIMEOUT_SECONDS,
        )
        try:
            worktree_monitor.set_nag_mode(
                session_name, repo_name, NAG_WHEN_DONE,
                duration_seconds=NAG_DONE_TIMEOUT_SECONDS,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    rows = await worktree_monitor.refresh_one(session_name, repo_name)
    row = _find_worktree_row(rows, session_name, repo_name)
    if row is None:
        return JSONResponse({"error": "worktree not found"}, status_code=404)
    return JSONResponse(_worktree_state_json(row))

async def api_worktree_commit(request):
    session_name = request.path_params["session"]
    repo_name = request.path_params["repo"]
    sha = request.path_params["sha"]

    if os.environ.get("DASHBOARD_MOCK"):
        commit = dao_sessions.get_worktree_commit_detail(session_name, repo_name, sha)
        if not commit:
            return JSONResponse({"error": "worktree commit not found"}, status_code=404)
        return JSONResponse(commit)

    try:
        commit = await asyncio.to_thread(
            get_session_worktree_commit_detail,
            session_name,
            repo_name,
            sha,
        )
    except WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)

    return JSONResponse(_worktree_commit_json(commit, include_patch=True))


async def api_worktree_changes(request):
    session_name = request.path_params["session"]
    repo_name = request.path_params["repo"]

    if os.environ.get("DASHBOARD_MOCK"):
        detail = dao_sessions.get_worktree_changes_detail(session_name, repo_name)
        if not detail:
            return JSONResponse({"error": "worktree changes not found"}, status_code=404)
        return JSONResponse(detail)

    try:
        detail = await asyncio.to_thread(
            get_session_worktree_dirty_detail,
            session_name,
            repo_name,
        )
    except WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)

    return JSONResponse(_worktree_dirty_detail_json(detail))


async def api_worktree_integrated_diff(request):
    """Integrated PR diff for one worktree.

    Powers the auto-r098a PR-mode review overlay: when the operator
    clicks the PR row in the on-card navigator, the overlay fetches
    this endpoint instead of an individual commit. Same JSON shape as
    ``/changes`` (file list + patch) since :class:`WorktreeDirtyDetail`
    is reused.

    When operator-declared review bindings (auto-nrqbs) exist for the
    row, scopes the diff to ``binding.base_sha..cache.head_sha`` so
    stacked PRs render only their own commits. ``?review_id=X``
    disambiguates when the row carries multiple bindings; without it
    the first binding wins. Falls back to ``merge-base..HEAD`` when no
    binding exists. When the requested SHAs aren't present locally
    (force-push, orphaned commits), the response carries
    ``stale: true`` so the UI shows "refresh required" instead of
    crashing.
    """
    session_name = request.path_params["session"]
    repo_name = request.path_params["repo"]

    if os.environ.get("DASHBOARD_MOCK"):
        detail = dao_sessions.get_worktree_integrated_diff_detail(
            session_name, repo_name,
            review_id=request.query_params.get("review_id"),
        )
        if not detail:
            return JSONResponse(
                {"error": "worktree integrated diff not found"}, status_code=404,
            )
        return JSONResponse(detail)

    base_sha, head_sha = _resolve_binding_diff_shas(
        session_name, repo_name,
        review_id=request.query_params.get("review_id"),
    )

    try:
        detail = await asyncio.to_thread(
            get_session_worktree_integrated_diff,
            session_name,
            repo_name,
            base_sha=base_sha,
            head_sha=head_sha,
        )
    except WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)

    return JSONResponse(_worktree_dirty_detail_json(detail))


def _resolve_binding_diff_shas(
    session_name: str, repo_name: str, *, review_id: str | None,
) -> tuple[str | None, str | None]:
    """Pick the (base_sha, head_sha) pair to scope ``/pr-diff`` to.

    Returns ``(None, None)`` when no binding exists or the cache hasn't
    been refreshed yet — caller falls back to the worktree's
    merge-base..HEAD diff. When the row has multiple bindings (stacked
    PRs), ``review_id`` disambiguates; without it the first binding
    wins so the existing single-PR overlay click keeps working.
    """
    snapshot = worktree_monitor.get_source_control(session_name, repo_name)
    if not snapshot:
        return None, None
    reviews = snapshot.get("reviews") or []
    if not reviews:
        single = snapshot.get("review")
        reviews = [single] if single else []
    if not reviews:
        return None, None
    if review_id:
        for r in reviews:
            if r and (str(r.get("review_id") or "") == str(review_id)
                      or str(r.get("number") or "") == str(review_id)):
                return r.get("base_sha") or None, r.get("head_sha") or None
        # Operator asked for a specific review_id we don't carry —
        # fall back to whole-branch rather than diffing the wrong PR.
        return None, None
    chosen = reviews[0] or {}
    return chosen.get("base_sha") or None, chosen.get("head_sha") or None

async def _record_worktree_merge_timeline(
    *,
    session_name: str,
    branch: str | None,
    result: dict,
    reason: str,
) -> None:
    """Best-effort write of a ``kind='worktree-merge'`` row to dispatch_runs.

    The merge already succeeded by the time this is called — the row is
    observability, not correctness. Any writer failure is logged and
    swallowed so the merge response stays ``ok: True``.
    """
    commit_sha = result.get("commit", "") or ""
    try:
        await asyncio.to_thread(
            record_worktree_merge_run,
            commit_hash=commit_sha,
            commit_message=result.get("message", "") or "",
            branch=branch or f"session/{session_name}",
            branch_base=result.get("target_branch") or None,
            container_name=session_name,
            reason=reason,
            target_repo=result.get("target_repo") or None,
        )
    except Exception:  # noqa: BLE001 — observability must not break merges
        logger.exception(
            "worktree-merge timeline write failed for %s commit=%s reason=%s",
            session_name, commit_sha, reason,
        )


async def _deliver_merge_notification_and_timeline(
    *,
    session_name: str,
    repo_name: str,
    branch: str | None,
    result: dict,
    celebration_kind: str,
    timeline_reason: str,
) -> None:
    """Send the 'you got merged' CrossTalk and write the timeline row.

    Both are fast — a tmux paste and a single DB insert — and both are run
    INLINE, before the merge response returns, so they complete before a
    merge-triggered ``uvicorn --reload`` can restart this process. That is the
    fix for the f6bf4e0d race, where these ran in a post-response background
    task that the reload killed. Neither call raises (each swallows and logs
    its own failures), so the merge response stays ``ok: True`` regardless.
    """
    await _signal_session_merge_celebration(
        target_session=session_name,
        repo_name=repo_name,
        commit_sha=result.get("commit", ""),
        commit_message=result.get("message", ""),
        kind=celebration_kind,
    )
    await _record_worktree_merge_timeline(
        session_name=session_name,
        branch=branch,
        result=result,
        reason=timeline_reason,
    )


# Hold references to detached deferred-refresh tasks so the event loop does not
# garbage-collect them mid-sleep (asyncio only keeps weak refs to tasks).
_DEFERRED_MERGE_REFRESH_TASKS: set[asyncio.Task] = set()


def _schedule_deferred_worktree_refresh(delay: float = 5.0) -> asyncio.Task:
    """Queue a worktree rescan ``delay`` seconds out, detached from the request.

    The rescan only refreshes the cached worktree list the UI reads. It is
    deliberately NOT a response BackgroundTask (uvicorn's graceful shutdown
    would wait on that, delaying the reload) but a detached task, so:

    - If the merge landed dashboard code, ``uvicorn --reload`` restarts this
      process within the window and this task is cancelled before it runs —
      which is correct, because the fresh process re-scans in
      ``worktree_monitor.start()`` on boot (and the periodic poll follows).
    - If there is no reload, the rescan runs normally after ``delay`` so the
      worktree list reflects the just-merged branch.
    """
    async def _run() -> None:
        try:
            await asyncio.sleep(delay)
            await worktree_monitor.refresh()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — merge already succeeded
            logger.exception("deferred post-merge worktree refresh failed")

    task = asyncio.create_task(_run())
    _DEFERRED_MERGE_REFRESH_TASKS.add(task)
    task.add_done_callback(_DEFERRED_MERGE_REFRESH_TASKS.discard)
    return task


async def api_worktree_commit_merge(request):
    session_name = request.path_params["session"]
    repo_name = request.path_params["repo"]
    sha = request.path_params["sha"]

    try:
        result = await asyncio.to_thread(
            merge_session_worktree_commit,
            session_name,
            repo_name,
            sha,
        )
    except RebaseRequiredError as exc:
        return JSONResponse(
            {
                "error": "rebase_required",
                "message": str(exc),
                "commits_behind": exc.commits_behind,
                "session_live": exc.session_live,
                "target_branch": exc.target_branch,
                "fork_sha": exc.fork_sha,
            },
            status_code=409,
        )
    except WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    await _deliver_merge_notification_and_timeline(
        session_name=session_name,
        repo_name=repo_name,
        branch=None,
        result=result,
        celebration_kind="commit",
        timeline_reason="commit-merge",
    )
    _schedule_deferred_worktree_refresh()
    return JSONResponse({
        "ok": True,
        "commit": result.get("commit", ""),
        "message": result.get("message", ""),
    })

async def api_worktree_merge(request):
    session_name = request.path_params["session"]
    repo_name = request.path_params["repo"]
    # ONLY RE-READ WHEN ABOUT TO REFUSE. Eligibility comes from whatever the
    # background poll last cached, so a session that wrote files, committed
    # them a second later and asked to merge was refused against a snapshot
    # taken mid-edit -- modified files with zero additions and zero
    # deletions, while the worktree itself was clean. It cleared on the next
    # poll, so the only apparent fix was waiting and retrying.
    #
    # Refreshing unconditionally would fix that and make every merge in the
    # fleet pay a scoped git sweep, a rate-limited capability fetch and the
    # monitor lock. Staleness is only ever wrong in ONE direction here: a
    # cached "eligible" that has gone stale is harmless, because the merge
    # itself is the real check and fails safely. A cached "not eligible" is
    # the one that costs a caller minutes. So spend the refresh there, on
    # the path that would otherwise be a refusal, and nowhere else.
    row = _find_worktree_row(worktree_monitor.get_all(), session_name, repo_name)
    if row is not None and not row.ff_eligible:
        rows = await worktree_monitor.refresh_one(session_name, repo_name)
        row = _find_worktree_row(rows, session_name, repo_name)
    if row is None:
        return JSONResponse({"error": "worktree not found"}, status_code=404)
    if not row.ff_eligible:
        # NOTHING TO MERGE IS NOT A REFUSAL. A branch whose commits are
        # already on master is not ff-eligible for the plain reason that
        # there is nothing left to fast-forward, and reporting that as a
        # failure reads as "your merge did not happen" -- which invites
        # committing the same work again.
        if not getattr(row, "commits_ahead", None) and not getattr(row, "is_dirty", False):
            return JSONResponse({
                "ok": True,
                "merged": False,
                "reason": "nothing to merge — this branch is already on the target",
                "state": _worktree_state_json(row),
            })
        return JSONResponse(
            {
                "error": "worktree is not ff-eligible",
                "state": _worktree_state_json(row),
            },
            status_code=409,
        )

    try:
        result = await asyncio.to_thread(
            merge_session_worktree,
            session_name,
            repo_name,
        )
    except WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    await _deliver_merge_notification_and_timeline(
        session_name=session_name,
        repo_name=repo_name,
        branch=getattr(row, "branch", None),
        result=result,
        celebration_kind="ff",
        timeline_reason="ff",
    )
    _schedule_deferred_worktree_refresh()
    return JSONResponse({
        "ok": True,
        "commit": result.get("commit", ""),
        "message": result.get("message", ""),
    })


async def api_worktree_cherry_pick(request):
    session_name = request.path_params["session"]
    repo_name = request.path_params["repo"]
    # Same shape as the merge path: the cached answer is trusted when it
    # says yes, and re-read only when it is about to say no.
    row = _find_worktree_row(worktree_monitor.get_all(), session_name, repo_name)
    if row is not None and not row.cherry_pick_eligible:
        rows = await worktree_monitor.refresh_one(session_name, repo_name)
        row = _find_worktree_row(rows, session_name, repo_name)
    if row is None:
        return JSONResponse({"error": "worktree not found"}, status_code=404)
    if not row.cherry_pick_eligible:
        return JSONResponse(
            {
                "error": "worktree is not cherry-pick eligible",
                "state": _worktree_state_json(row),
            },
            status_code=409,
        )

    try:
        result = await asyncio.to_thread(
            cherry_pick_session_worktree,
            session_name,
            repo_name,
        )
    except WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    await _deliver_merge_notification_and_timeline(
        session_name=session_name,
        repo_name=repo_name,
        branch=getattr(row, "branch", None),
        result=result,
        celebration_kind="cherry-pick",
        timeline_reason="cherry-pick",
    )
    _schedule_deferred_worktree_refresh()
    return JSONResponse({
        "ok": True,
        "commit": result.get("commit", ""),
        "source_commit": result.get("source_commit", ""),
        "message": result.get("message", ""),
    })


async def api_worktree_watch_set(request):
    """Persist the source_control nag mode for a worktree row.

    Body shape::

        {"mode": "silent" | "nag_all" | "nag_done",
         "duration_seconds": <optional float, default 1h, capped at 4h>}

    Per Jeremy (2026-04-30) every nag request is time-limited; the
    duration defaults to ``NAG_DEFAULT_DURATION_SECONDS`` and is
    clamped to ``NAG_MAX_DURATION_SECONDS`` (4 h). After the duration
    elapses the row reverts to silent automatically, no operator
    action required. The live timers remain in-memory on
    :class:`WorktreeMonitor`, but the requested mode is persisted in
    Settings so restart can reconstruct it.
    """
    from tools.dashboard.worktree_monitor import NAG_MODES, NAG_DEFAULT

    session_name = request.path_params["session"]
    repo_name = request.path_params["repo"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    mode = (body or {}).get("mode") if isinstance(body, dict) else None
    if mode is None:
        mode = NAG_DEFAULT
    if mode not in NAG_MODES:
        return JSONResponse(
            {
                "error": "invalid mode",
                "valid": sorted(NAG_MODES),
            },
            status_code=400,
        )
    duration_seconds = (body or {}).get("duration_seconds") if isinstance(body, dict) else None
    try:
        worktree_monitor.set_nag_mode(
            session_name, repo_name, mode, duration_seconds=duration_seconds,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({
        "ok": True,
        "session_name": session_name,
        "repo_name": repo_name,
        "mode": mode,
        "expires_in_seconds": int(
            worktree_monitor.get_nag_expiry_remaining(session_name, repo_name)
        ),
    })


async def api_worktree_sync_base(request):
    session_name = request.path_params["session"]
    repo_name = request.path_params["repo"]

    try:
        await asyncio.to_thread(
            sync_session_worktree_base,
            session_name,
            repo_name,
        )
    except WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    rows = await worktree_monitor.refresh()
    row = _find_worktree_row(rows, session_name, repo_name)
    if row is None:
        return JSONResponse({"error": "worktree not found"}, status_code=404)
    return JSONResponse({"ok": True, "state": _worktree_state_json(row)})


async def api_session_request_identity_refresh(request):
    """POST /api/session/{tmux_name}/request-identity-refresh

    Operator nudges the session to re-set its working title, topics,
    and role via CrossTalk. The drawer surfaces this as a button when
    one of the three is empty — agents sometimes forget to update them
    after the operator briefs them on a task. The primer reminds the
    agent to do this proactively, this is the operator's escape hatch
    for when they didn't.
    """
    tmux_name = request.path_params["tmux_name"]
    message = (
        "Operator is asking you to update your session identity so the "
        "dashboard shows what you're working on:\n"
        "\n"
        "  graph set-label \"<short working title>\"\n"
        "  graph set-topics \"<status line 1>\" \"<status line 2>\"\n"
        "  graph set-role <designer|builder|researcher|reviewer|...>\n"
        "\n"
        "Set whichever are missing or stale based on the current task. "
        "Two short topic lines are usually enough — one for what you're "
        "doing right now, one for the bead/scope it ties back to."
    )
    try:
        await _send_dashboard_ui_crosstalk(tmux_name, message)
    except WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    return JSONResponse({"ok": True, "session": tmux_name})


async def api_worktree_cleanup(request):
    session_name = request.path_params["session"]
    force = False
    try:
        body = await request.json()
        if isinstance(body, dict):
            force = bool(body.get("force"))
    except Exception:
        force = False

    try:
        result = await asyncio.to_thread(
            cleanup_session_worktrees,
            session_name,
            force=force,
            worktrees_dir=WORKTREES_DIR,
        )
    except WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    await worktree_monitor.refresh()
    return JSONResponse({"ok": True, **_cleanup_result_json(result)})


async def api_worktree_discard(request):
    session_name = request.path_params["session"]
    repo_name = request.path_params["repo"]

    try:
        result = await asyncio.to_thread(
            cleanup_session_worktree,
            session_name,
            repo_name,
            force=True,
            worktrees_dir=WORKTREES_DIR,
        )
    except WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)

    await worktree_monitor.refresh()
    return JSONResponse({"ok": True, **_cleanup_result_json(result)})

async def api_dao_active_sessions(request):
    if os.environ.get("DASHBOARD_MOCK"):
        sessions = dao_sessions.get_active_sessions()
    else:
        # Read directly from session monitor — zero filesystem access
        sessions = session_monitor.get_registry()
    # Active list surfaces interactive sessions only. Dispatch + librarian
    # rows are first-class in the monitor registry for SSE and tail, but
    # filtered out here (auto-ylj6r Phase 5).
    from tools.dashboard.dao.sessions import _ACTIVE_SESSION_TYPES
    sessions = [s for s in sessions if s.get("type") in _ACTIVE_SESSION_TYPES]
    return JSONResponse(sessions)


async def api_mock_harness_nonce(request):
    """Echo the ``__harness_nonce__`` baked into the active DASHBOARD_MOCK
    fixture. Test harnesses assert this matches the nonce they wrote before
    trusting the server they reached — turning a silent port-collision (a
    readiness probe answered by another session's server) into an immediate
    error. Mock-mode only; 404 otherwise so it never exists in production."""
    if not os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"error": "not found"}, status_code=404)
    from tools.dashboard.dao import mock as mock_dao
    return JSONResponse({"nonce": mock_dao._load().get("__harness_nonce__")})

_recent_sessions_limit_deprecated_logged = False


async def api_dao_recent_sessions(request):
    global _recent_sessions_limit_deprecated_logged
    if "limit" in request.query_params and not _recent_sessions_limit_deprecated_logged:
        logger.warning(
            "/api/dao/recent_sessions: 'limit' query param is deprecated and ignored; "
            "row count is now controlled by server-side per-type quotas"
        )
        _recent_sessions_limit_deprecated_logged = True
    sort = request.query_params.get("sort", "lastActivity")
    since = request.query_params.get("since", "1d")
    type_group = request.query_params.get("type", "all")
    requested_org = request.query_params.get("org") or None
    snapshot = request.query_params.get("snapshot") == "1"
    org = None
    if snapshot:
        # The complete history projection spans every org, just like the
        # global session:registry SSE topic. An org-scoped agent must not be
        # able to turn this facet bootstrap into a cross-org history read.
        refused = api_auth.require_global_api_authority(request)
        if refused is not None:
            return refused
        if requested_org:
            return JSONResponse({"error": "history snapshot cannot be organization-scoped"}, status_code=400)
        # The Sessions page reads this bounded card projection ONCE after a
        # reload, behind the already-rendered Active list: the one-week global
        # list plus an age-independent ten-session floor for every org.
        # Lifecycle SSE supplies everything after that first read, so this is
        # computed directly per request in a worker thread — measured ~115ms
        # on the full nine-org host data set (2026-08-29), and WAL +
        # read-only pooled connections mean graph ingest cannot block it.
        # The old background cache + 202-on-cold-key apparatus is gone: the
        # 202's empty placeholder body was indistinguishable from a genuinely
        # empty list and pinned a false "No recent sessions" on the page.
        sessions = await asyncio.to_thread(
            dao_sessions.get_recent_sessions,
            None, "lastActivity", "1w", "all", None, True,
        )
        return JSONResponse(sessions)
    if requested_org:
        # A selected organization is a server-side scope, never a raw
        # client-controlled filter. The common-core resolver reconciles the
        # token org against the selection so an org-stamped session cannot read
        # another org.
        org, refused = api_auth.resolve_scoped_org(requested_org, request=request)
        if refused is not None:
            return refused
    # Computed directly per request, off the event loop. Scoped reads measure
    # ~70-180ms on the full host data set (cost scales with the selected org's
    # whole history — the scoped path deliberately has no since-window).
    sessions = await asyncio.to_thread(
        dao_sessions.get_recent_sessions,
        None, sort, since, type_group, org,
    )
    return JSONResponse(sessions)


async def api_dao_session_status(request):
    since = request.query_params.get("since")
    try:
        rows = await asyncio.to_thread(dao_sessions.get_session_status_rows, since)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(rows)

async def page_search(request):
    """Serve the search results page (full HTML shell for direct navigation)."""
    return HTMLResponse(_load_template("base.html"))

async def page_search_fragment(request):
    """Return the search results page as an HTML fragment for SPA injection."""
    return templates.TemplateResponse(request, "pages/search.html")

async def page_streams(request):
    """Serve the streams landing page (full HTML shell for direct navigation)."""
    return HTMLResponse(_load_template("base.html"))

async def page_streams_fragment(request):
    """Return the streams landing page as an HTML fragment for SPA injection."""
    return templates.TemplateResponse(request, "pages/streams.html")

async def page_collab(request):
    """Serve the collab hub page (full HTML shell for direct navigation)."""
    return HTMLResponse(_load_template("base.html"))

async def page_collab_fragment(request):
    """Return the collab hub page as an HTML fragment for SPA injection."""
    return templates.TemplateResponse(request, "pages/collab.html")

async def page_stream(request):
    """Serve the stream page (full HTML shell for direct navigation)."""
    return HTMLResponse(_load_template("base.html"))

async def page_stream_fragment(request):
    """Return the stream page as an HTML fragment for SPA injection."""
    return templates.TemplateResponse(request, "pages/stream.html")

async def page_source(request):
    return HTMLResponse(_load_template("base.html"))

async def page_source_redirect(request):
    """301 redirect /source/{id} → /graph/{id}, preserving query params."""
    id = request.path_params["id"]
    qs = str(request.query_params)
    target = f"/graph/{id}" + (f"?{qs}" if qs else "")
    return RedirectResponse(target, status_code=301)

async def page_source_fragment(request):
    """Return the Source/Context page as an HTML fragment for SPA injection.

    Handles both /source/{id} (full source) and /source/{id}?turn=N (context view).
    The Alpine sourcePage component reads URL params on init to select the right mode.
    """
    return templates.TemplateResponse(request, "pages/source.html")

async def page_bead(request):
    return HTMLResponse(_load_template("base.html"))

async def page_bead_fragment(request):
    """Return the Bead detail page as an HTML fragment for SPA injection.

    Rendered via Jinja2 so {% include %} partials work.
    The fragment is injected into #content by the client router, then
    Alpine.initTree() initialises the x-data="beadDetailPage()" component.
    The component reads the bead ID from window.location.pathname on init.
    """
    return templates.TemplateResponse(request, "pages/bead-detail.html")

async def api_dao_bead(request):
    """Return a single bead with labels, deps, and comments via DAO (not bd CLI).

    GET /api/dao/bead/{id}

    Prefers dao_beads.get_bead() (Dolt/MySQL: one round trip for labels, deps,
    comments and children). But the DAO degrades to None when the Dolt SQL
    server is unreachable, and that is INDISTINGUISHABLE from a genuinely-absent
    bead — so a bare None must NOT be reported as 404 "not found". To the
    operator that reads as "this bead does not exist" (they diagnosed it as an
    auth failure), when the truth is the beads backend is down. The bd CLI reads
    Dolt on disk and works without the SQL server — it is what the beads *list*
    is served from — so fall back to it: the detail page stays as resilient as
    the list, and a real 404 is returned only when the CLI also has no such bead.
    In mock mode, reads from the fixture file.

    Per-org tracker (per-org databases, autonomy@74585ba): bead IDs do not
    encode their org, so the caller names it with ``?org=`` (the list view
    knows which org it navigated from). An ORG-BOUND caller (org session
    bearer) is pinned by ``organization_scope_from_request`` and may read
    ONLY its own org — a ``?org=`` naming any other is a cross-org read and
    404s, matching ``caller_org_scope_hides``. A GLOBAL-authority caller
    (operator cookie / local session) is not pinned and may name any org;
    with none named it defaults to the shared autonomy tracker.
    """
    bead_id = request.path_params["id"]
    requested_org = request.query_params.get("org") or None
    pinned_org = api_auth.organization_scope_from_request(request)
    if pinned_org is not None:
        if requested_org is not None and requested_org != pinned_org:
            return JSONResponse({"error": "not found"}, status_code=404)
        org = pinned_org
    else:
        org = requested_org
    if not os.environ.get("DASHBOARD_MOCK") and org is not None:
        from tools.data_paths import org_beads_dir
        if org_beads_dir(org) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
    bead = await asyncio.to_thread(dao_beads.get_bead, bead_id, org)
    if bead is not None:
        return JSONResponse(bead)
    if os.environ.get("DASHBOARD_MOCK"):
        # The fixture DAO is the whole truth in mock mode; there is no CLI to
        # fall back to, so None here is a genuine miss.
        return JSONResponse({"error": "not found"}, status_code=404)
    # DAO returned nothing: the bead is absent OR the Dolt SQL server is down.
    # The CLI settles it against on-disk Dolt, targeting the same org's tracker.
    from tools.data_paths import org_beads_dir
    bd_dir = org_beads_dir(org)
    cli_bead = _normalize_bead_show_payload(
        await run_cli_json(["bd", "show", bead_id, "--json"], beads_dir=bd_dir)
    )
    if not cli_bead or cli_bead.get("error"):
        return JSONResponse({"error": "not found"}, status_code=404)
    # Served from the CLI because the DAO was unavailable. `bd show` alone omits
    # the relational arrays the DAO embeds, so the detail page would render an
    # epic with NO children — which reads as "the epic is empty / mis-wired"
    # when the parent-child edges are perfectly fine. Hydrate them from the CLI
    # too: `bd dep list --direction=up` gives the dependents (an epic's children
    # are the parent-child ones), and the down direction gives this bead's own
    # dependency rows, matching the DAO's `deps`. Comments are the one thing
    # `bd show --json` does not expose, so they degrade to empty on this path
    # (rare on epics; the DAO path restores them when Dolt is reachable).
    up = await run_cli_json(
        ["bd", "dep", "list", bead_id, "--direction=up", "--json"],
        empty=[], beads_dir=bd_dir,
    )
    down = await run_cli_json(
        ["bd", "dep", "list", bead_id, "--json"], empty=[], beads_dir=bd_dir,
    )
    up = up if isinstance(up, list) else []
    down = down if isinstance(down, list) else []
    cli_bead["children"] = [c for c in up if c.get("dependency_type") == "parent-child"]
    cli_bead["deps"] = down
    cli_bead.setdefault("comments", [])
    return JSONResponse(cli_bead)


# ── SSE EventBus endpoint ─────────────────────────────────────

async def api_internal_restart_notice(request):
    """Accept the reloader's authenticated warning before it stops this worker."""
    expected = os.environ.get("DASHBOARD_RESTART_TOKEN")
    presented = request.headers.get(_RESTART_TOKEN_HEADER)
    if not expected or not presented or not hmac.compare_digest(presented, expected):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    try:
        payload = await _announce_restart()
    except Exception:
        logger.exception("could not announce pending restart")
        return JSONResponse({"error": "restart announcement failed"}, status_code=500)
    return JSONResponse({
        "ok": True,
        "started_at_ms": payload["started_at_ms"],
        "countdown_seconds": _RESTART_WARNING_SECONDS,
    })

async def api_events(request):
    """Server-Sent Events endpoint — global broadcast.

    GET /api/events

    Every connected client receives ALL topics. Each event is sent as:
        event: {topic}
        data: {json}

    The browser EventSource API handles reconnection automatically.

    Optional ``?client_id=`` correlates this SSE queue to the diag tab id —
    used by /api/diag/sessions so each per-client envelope can report its
    SSE connection_id and subscription age. Falls back to None when absent.

    Global authority only. The stream is an UNFILTERED cross-org broadcast —
    every topic (the whole fleet's session roster, worktrees, approvals) to
    every subscriber — so its consumers are the naturally-cross-org ones: the
    operator's browser (cookie) and local host tooling. An org-stamped agent
    bearer has no business on the fleet stream and is refused (403); admitting
    it would hand one org every other org's session metadata. A side-band
    service that legitimately needs this stream (the Mission Control relay
    connector) carries its own credential, not an agent session token.
    """
    refused = api_auth.require_global_api_authority(request)
    if refused is not None:
        return refused
    client_id = request.query_params.get("client_id") or None
    queue = event_bus.subscribe(client_id=client_id)

    # Mock-mode fidelity: in production the SessionMonitor keeps a
    # session:registry snapshot cached on the bus, and subscribe() replays
    # it, so a cold-opened viewer learns its session is resolved before (or
    # shortly after) configure() runs. The monitor doesn't run against
    # fixtures, so synthesize the roster fresh from the mock DAO per
    # connection — reading the fixture file at connect time preserves the
    # fixture-swap isolation that keeps _on_startup from caching it.
    if os.environ.get("DASHBOARD_MOCK"):
        try:
            queue.put_nowait(
                ("session:registry", dao_sessions.get_active_sessions(), 0)
            )
        except Exception:
            logger.exception("mock session:registry seed failed; continuing")

    async def event_generator():
        # Activity-gated heartbeat: if no real event arrives within HEARTBEAT_S,
        # emit a JS-visible `heartbeat` event so the client can distinguish a
        # quiet-but-alive stream from a dead one (iOS EventSource won't fire
        # `error` on a silent half-open socket). Real events reset the wait, so a
        # busy stream sends zero heartbeats. No `id:` → it never advances the
        # client's seq / pollutes gap-replay. A heartbeat write to a dead socket
        # eventually errors → this generator unwinds → the subscriber is reaped.
        HEARTBEAT_S = 5.0
        try:
            while True:
                try:
                    topic, data, seq = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat", "data": "{}"}
                    continue
                yield {"id": f"{seq}:{current_server_epoch()}", "event": topic, "data": json.dumps(data)}
        except asyncio.CancelledError:
            pass
        finally:
            event_bus.unsubscribe(queue)

    return EventSourceResponse(event_generator())


async def api_events_replay(request):
    """Return missed events from the ring buffer for gap replay.

    GET /api/events/replay?from={seq}&to={seq}
    Returns {events: [...], complete: bool}.
    If complete=false, the buffer doesn't cover the range —
    caller should fall back to full re-fetch from disk.

    Global authority only — this replays the same unfiltered cross-org
    broadcast as /api/events, so it carries the same guard (see api_events).
    """
    refused = api_auth.require_global_api_authority(request)
    if refused is not None:
        return refused
    from_seq = int(request.query_params.get("from", "0"))
    to_seq = int(request.query_params.get("to", "0"))
    if from_seq <= 0 or to_seq <= 0 or from_seq > to_seq:
        return JSONResponse({"error": "Invalid range"}, status_code=400)
    events, complete = event_bus.replay(from_seq, to_seq)
    status_code = 200 if complete else 206
    return JSONResponse({"events": events, "complete": complete}, status_code=status_code)


# ── Diag round-trip ───────────────────────────────────────────
#
# /api/diag/sessions emits a `diag:request` SSE event, sleeps for the
# collection window, then aggregates the per-tab POSTs that arrived on
# /api/diag/client. The response also reports file-layer + server-tail
# state so it's obvious which layer first disagrees with the others.

import uuid as _uuid_mod

# req_id -> {"emit_ts": float, "request_type": str,
#            "deadline_ts": float, "params": dict,
#            "clients": {client_id: (recv_ts, payload_dict)}}
_DIAG_AGGREGATORS: dict[str, dict] = {}
_DIAG_AGGREGATOR_TTL_SECONDS = 30.0
_DIAG_COLLECTION_WINDOW_SECONDS = 3.0
_DIAG_DIR = DATA_ROOT / "diag"


def _diag_janitor_sweep(now: float | None = None) -> None:
    """Drop diag aggregators older than the TTL — bounded memory usage."""
    cutoff = (now if now is not None else time.time()) - _DIAG_AGGREGATOR_TTL_SECONDS
    for req_id in [k for k, v in _DIAG_AGGREGATORS.items() if v.get("emit_ts", 0) < cutoff]:
        _DIAG_AGGREGATORS.pop(req_id, None)


def _read_jsonl_tail(path: Path, n: int = 10) -> list[dict]:
    """Return the last n parsed JSONL lines as (type, timestamp, identity) tuples."""
    try:
        with open(path, "rb") as fh:
            try:
                fh.seek(0, 2)
                size = fh.tell()
            except OSError:
                return []
            chunk_size = 8192
            buf = b""
            offset = size
            while offset > 0 and buf.count(b"\n") <= n:
                read = min(chunk_size, offset)
                offset -= read
                fh.seek(offset)
                buf = fh.read(read) + buf
            lines = [ln for ln in buf.splitlines() if ln.strip()]
            tail_lines = lines[-n:] if len(lines) >= n else lines
    except (FileNotFoundError, OSError):
        return []

    from tools.dashboard.session_monitor import _entry_identity
    tails = []
    for raw in tail_lines:
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        # JSONL rows from the harness aren't always normalized to the
        # "type" the tail emits. Try a small set of possible shapes.
        etype = obj.get("type") or ""
        ts = obj.get("timestamp") or ""
        # Best-effort: if the row looks like a Claude-style {"type": "human"}
        # wrapper, _entry_identity will still produce a stable key.
        tails.append({
            "type": etype,
            "timestamp": ts,
            "identity": _entry_identity({
                "type": etype,
                "timestamp": ts,
                "tool_id": obj.get("tool_id"),
                "content": obj.get("content"),
            }),
        })
    return tails


def _read_jsonl_tail_window(
    path: Path,
    *,
    n: int,
    before: int | None = None,
) -> tuple[bytes, int, int]:
    """Return an approximate trailing JSONL window ending at ``before``.

    The window is selected by raw line count, not by normalized viewer-entry
    count. That keeps the read path cheap and intentionally approximate for the
    session viewer's fast-open mode: we want "a recent chunk" immediately, not
    exact tile accounting.

    Returns ``(raw_bytes, start_offset, end_offset)`` where ``raw_bytes`` is a
    CONTIGUOUS newline-aligned slice of the file (``end - start ==
    len(raw_bytes)``, blank lines included — review B1/S1: compacting blanks
    corrupted the byte offsets entry identity is built on). When ``before``
    is None the window ends at the last COMPLETE newline, never physical
    EOF — a cold-open taken mid-write must not commit a position inside the
    writer's partial line, or the wake-up fetch skips that line forever.
    """
    if n <= 0:
        return b"", 0, 0
    try:
        with open(path, "rb") as fh:
            try:
                fh.seek(0, 2)
                size = fh.tell()
            except OSError:
                return b"", 0, 0
            if before is None:
                end = _last_complete_offset_in(path)
            else:
                end = max(0, min(int(before), size))
            if end <= 0:
                return b"", 0, 0
            chunk_size = 8192
            buf = b""
            offset = end
            while offset > 0 and buf.count(b"\n") <= n:
                read = min(chunk_size, offset)
                offset -= read
                fh.seek(offset)
                buf = fh.read(read) + buf
            if offset > 0:
                nl = buf.find(b"\n")
                if nl != -1:
                    offset += nl + 1
                    buf = buf[nl + 1:]
            # Pick the start so the window covers the last n NON-BLANK
            # lines, but serve the original contiguous bytes from there.
            segments = buf.splitlines(keepends=True)
            count = 0
            i = len(segments)
            while i > 0 and count < n:
                i -= 1
                if segments[i].strip():
                    count += 1
            if count == 0:
                return b"", end, end
            raw = b"".join(segments[i:])
            start = end - len(raw)
            return raw, start, end
    except (FileNotFoundError, OSError, ValueError):
        return b"", 0, 0


def _count_lines_in_window(path: Path, *, head: bool, window: int = 1024) -> int | None:
    """Count newline-terminated lines in the first or last ``window`` bytes."""
    try:
        with open(path, "rb") as fh:
            if head:
                buf = fh.read(window)
            else:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - window))
                buf = fh.read(window)
    except (FileNotFoundError, OSError):
        return None
    return buf.count(b"\n")


def _diag_tail_3_alignment(file_tail: list[dict], server_tail: list[dict],
                           clients: list[dict]) -> str:
    """Classify how the layers compare on tail identity keys.

    Returns one of: "all_match", "file_server_match_clients_diverge",
    "file_diverges", "clients_disagree".  Compares the trailing min(len)
    rows so a shorter client tail (e.g. fresh tab) doesn't false-flag
    against a longer server-side tail. The name is preserved for
    backwards compatibility with prior callers.
    """
    def keys(rows: list[dict]) -> list[str]:
        return [r.get("identity", "") for r in (rows or [])]

    f = keys(file_tail)
    s = keys(server_tail)
    client_keys = [
        keys(
            c.get("session_markers", {}).get("tail_10")
            or c.get("session_markers", {}).get("tail_3", [])
        )
        for c in clients
    ]

    def _trim(a: list[str], b: list[str]) -> tuple[list[str], list[str]]:
        n = min(len(a), len(b)) or 0
        if n == 0:
            return a, b
        return a[-n:], b[-n:]

    if clients:
        # Compare only the trailing common-length window per pair.
        client_seen: list[tuple[str, ...]] = []
        for ck in client_keys:
            f_t, ck_t = _trim(f, ck)
            s_t, _ = _trim(s, ck)
            client_seen.append(tuple(ck_t))
            # Track per-client divergence implicitly via set below.
        unique_client_keys = set(client_seen)
        if len(unique_client_keys) > 1:
            return "clients_disagree"
        sole = next(iter(unique_client_keys))
        # Compare in the trailing-window sense.
        f_t, s_t = _trim(f, s) if f and s else (f, s)
        sole_list = list(sole)
        f_window, sole_f = _trim(f, sole_list)
        s_window, sole_s = _trim(s, sole_list)
        if f_window == s_window == sole_f:
            return "all_match"
        if f_window == s_window and sole_f != f_window:
            return "file_server_match_clients_diverge"
        return "file_diverges"
    # No clients: file vs server only — align on the shorter window.
    f_t, s_t = _trim(f, s)
    if f_t == s_t:
        return "all_match"
    return "file_diverges"


def _diag_build_bus_block() -> dict:
    """Snapshot the EventBus side of the diag round-trip."""
    first_seq, last_seq, first_ts, last_ts = event_bus.buffer_window()
    snapshot_path: str | None = None
    snapshot_mtime: float | None = None
    try:
        if EVENT_BUS_STATE_PATH.exists():
            snapshot_path = str(EVENT_BUS_STATE_PATH)
            snapshot_mtime = EVENT_BUS_STATE_PATH.stat().st_mtime
    except OSError:
        pass
    epoch = current_server_epoch()
    return {
        "global_seq": event_bus._seq,
        "epoch": epoch,
        "epoch_age_s": max(0, int(time.time()) - int(epoch)),
        "subscribers_count": event_bus.subscribers_count(),
        "buffer_entries": len(event_bus._buffer),
        "buffer_bytes": event_bus._buffer_bytes,
        "buffer_max_bytes": event_bus._BUFFER_MAX_BYTES,
        "buffer_first_seq": first_seq,
        "buffer_last_seq": last_seq,
        "buffer_first_ts": first_ts,
        "buffer_last_ts": last_ts,
        "broadcasts_last_60s": event_bus.broadcasts_last_60s(),
        "last_snapshot_path": snapshot_path,
        "last_snapshot_mtime": snapshot_mtime,
        "subscribers": event_bus.subscribers_metadata(),
        "recent_broadcasts": event_bus.recent_broadcasts(limit=10),
        "dedup_skipped_total": event_bus.dedup_skipped_total(),
        "restore_history": event_bus.restore_history(),
    }


def _diag_build_file_block(jsonl_path: str | None) -> dict:
    """File-layer snapshot for one session — best-effort, never raises."""
    block: dict = {
        "path": jsonl_path,
        "path_resolved": None,
        "inode": None,
        "device": None,
        "lines": None,
        "size_bytes": None,
        "mtime_ago_s": None,
        "permissions": None,
        "owner_uid": None,
        "link_count": None,
        "lines_in_first_kb": None,
        "lines_in_last_kb": None,
        "tail_10": [],
        # tail_3 retained for backwards compatibility with prior diag consumers.
        "tail_3": [],
    }
    if not jsonl_path:
        return block
    p = Path(jsonl_path)
    try:
        st = p.stat()
        block["path_resolved"] = str(p.resolve())
        block["inode"] = st.st_ino
        block["device"] = st.st_dev
        block["size_bytes"] = st.st_size
        block["mtime_ago_s"] = max(0, int(time.time() - st.st_mtime))
        block["permissions"] = oct(st.st_mode & 0o777)
        block["owner_uid"] = st.st_uid
        block["link_count"] = st.st_nlink
    except (FileNotFoundError, OSError):
        return block
    # Count lines via a quick read — tiny overhead, only on diag invocation.
    try:
        with open(p, "rb") as fh:
            block["lines"] = sum(1 for _ in fh)
    except OSError:
        pass
    block["lines_in_first_kb"] = _count_lines_in_window(p, head=True)
    block["lines_in_last_kb"] = _count_lines_in_window(p, head=False)
    tail_10 = _read_jsonl_tail(p, n=10)
    block["tail_10"] = tail_10
    block["tail_3"] = tail_10[-3:]
    return block


def _diag_build_server_block(session_id: str, ts) -> dict:
    """_TailState snapshot for one session."""
    file_offset = 0
    row = None
    try:
        from tools.dashboard.session_monitor import get_session as _gs
        row = _gs(session_id)
        if row:
            file_offset = int(row.get("file_offset") or 0)
    except Exception:
        pass

    # ── Harness fields + mismatch heuristic ──────────────────────────
    db_harness: str | None = None
    derived_harness: str | None = None
    harness_mismatch = False
    try:
        if row:
            db_harness = (row.get("harness") or "claude") or None
        jsonl_path = (row or {}).get("jsonl_path") if row else None
        if jsonl_path:
            # Filename-based heuristic first: it needs only the path and cannot
            # raise. A rollout-* JSONL registered as claude is the exact
            # rollout-* / harness=claude mismatch pattern from the bead
            # acceptance criteria, and must be flagged even when
            # resolve_harness_for_path() below fails to parse the transcript
            # (e.g. a rollout missing its Codex CLI version) — otherwise the
            # parse error would swallow the mismatch.
            if db_harness == "claude" and Path(jsonl_path).name.startswith("rollout-"):
                harness_mismatch = True
            try:
                from tools.dashboard.session_harness import resolve_harness_for_path
                derived_harness = resolve_harness_for_path(jsonl_path).name
                if db_harness and derived_harness and db_harness != derived_harness:
                    harness_mismatch = True
            except Exception:
                pass
    except Exception:
        pass

    last_broadcast_ago_s: float | None = None
    if ts and ts.last_broadcast_ts:
        last_broadcast_ago_s = max(0.0, time.time() - ts.last_broadcast_ts)
    if not ts:
        return {
            "harness": db_harness,
            "derived_harness": derived_harness,
            "harness_mismatch": harness_mismatch,
            "broadcast_seq": 0,
            "file_offset": file_offset,
            "last_entry_type": "",
            "last_broadcast_ago_s": None,
            "pending_tools": 0,
            "completed_tools": 0,
            "task_tracker_warmed": False,
            "watch_descriptor": None,
            "dir_watch_descriptor": None,
            "last_known_inode": 0,
            "needs_resolution": False,
            "resolution_dir": None,
            "last_enqueue_content_len": 0,
            "parse_errors_count": 0,
            "last_parse_error": None,
            "last_parse_error_ts": 0.0,
            "lines_processed_total": 0,
            "inotify_events_received": 0,
            "last_inotify_event_ts": 0.0,
            "enqueue_dedup_count": 0,
            "last_enqueue_dedup_ts": 0.0,
            "full_rescan_count": 0,
            "last_full_rescan_ts": 0.0,
            "tail_10": [],
            "tail_3": [],
        }
    tail_10 = [
        {"type": etype, "timestamp": tstamp, "identity": ident}
        for (etype, tstamp, ident) in list(ts.recent_processed)
    ]
    return {
        "harness": db_harness,
        "derived_harness": derived_harness,
        "harness_mismatch": harness_mismatch,
        "broadcast_seq": ts.broadcast_seq,
        "file_offset": file_offset,
        "last_entry_type": ts.last_entry_type,
        "last_broadcast_ago_s": last_broadcast_ago_s,
        "pending_tools": len(ts.pending_tool_ids),
        "completed_tools": len(ts.completed_tool_ids),
        "task_tracker_warmed": ts.task_tracker_warmed,
        "watch_descriptor": ts.watch_descriptor,
        "dir_watch_descriptor": ts.dir_watch_descriptor,
        "last_known_inode": ts.last_known_inode,
        "needs_resolution": ts.needs_resolution,
        "resolution_dir": str(ts.resolution_dir) if ts.resolution_dir else None,
        "last_enqueue_content_len": len(ts.last_enqueue_content or ""),
        "parse_errors_count": ts.parse_errors_count,
        "last_parse_error": ts.last_parse_error,
        "last_parse_error_ts": ts.last_parse_error_ts,
        "lines_processed_total": ts.lines_processed_total,
        "inotify_events_received": ts.inotify_events_received,
        "last_inotify_event_ts": ts.last_inotify_event_ts,
        "enqueue_dedup_count": ts.enqueue_dedup_count,
        "last_enqueue_dedup_ts": ts.last_enqueue_dedup_ts,
        "full_rescan_count": ts.full_rescan_count,
        "last_full_rescan_ts": ts.last_full_rescan_ts,
        "tail_10": tail_10,
        "tail_3": tail_10[-3:],
    }


def _diag_format_text_table(payload: dict) -> str:
    """Render the diag aggregate as a compact plain-text table.

    Width budget: ≤200 cols. New depth fields (parse_err, replay, dedup,
    inotify, subs) are folded into compact glyphs in a second per-row
    column so the operator sees the existing alignment view plus the new
    health signals in a single grep-friendly line.
    """
    bus = payload.get("bus", {})
    rows = payload.get("rows", [])
    bus_seq = bus.get("global_seq")
    buf = bus.get("buffer_entries")
    subs = bus.get("subscribers_count")
    dedup_skipped = bus.get("dedup_skipped_total", 0)
    bus_subs_meta = bus.get("subscribers", []) or []
    queue_depths = [s.get("queue_depth", 0) for s in bus_subs_meta if isinstance(s, dict)]
    max_qdepth = max(queue_depths) if queue_depths else 0

    header_main = (
        "session_id   file/srv/min_cli  mtime  bus_seq  buf  subs  ooo  lag(p50/max)  align"
    )
    header_glyph = (
        f"# bus: dedup_skip={dedup_skipped} qdepth_max={max_qdepth} "
        f"recent_brc={len(bus.get('recent_broadcasts', []) or [])} "
        f"restores={len(bus.get('restore_history', []) or [])}"
    )
    lines = [header_glyph, header_main]
    warnings = payload.get("warnings") or []
    for warning in warnings:
        lines.append(f"WARN: {warning}")
    for row in rows:
        sid = row.get("session_id", "?")
        f = row.get("file") or {}
        s = row.get("server") or {}
        file_lines = f.get("lines")
        srv_seq = s.get("broadcast_seq")
        min_cli = row.get("min_client_seq")
        mtime_ago = f.get("mtime_ago_s")
        ooo = row.get("max_out_of_order_count") or 0
        clients = row.get("clients") or []
        lags = [c.get("lag_ms") for c in clients if c.get("lag_ms") is not None]
        if lags:
            lags_sorted = sorted(lags)
            p50 = lags_sorted[len(lags_sorted) // 2]
            max_lag = lags_sorted[-1]
            lag_str = f"{p50}/{max_lag}ms"
        else:
            lag_str = "-/-"
        align = (row.get("drift") or {}).get("tail_3_alignment", "-")
        lines.append(
            f"{sid}   {file_lines}/{srv_seq}/{min_cli}       "
            f"{mtime_ago}s    {bus_seq}    {buf}   {subs}    {ooo}    {lag_str}      {align}"
        )
        replay_total = sum(
            (c.get("session_markers", {}) or {}).get("gap_replays_count", 0)
            for c in clients
        )
        dedup_collisions_total = sum(
            (c.get("session_markers", {}) or {}).get("dedup_collisions", 0)
            for c in clients
        )
        glyphs = (
            f"  └ harness={s.get('harness') or '-'}"
            f"{'!' if s.get('harness_mismatch') else ''} "
            f"parse_err={s.get('parse_errors_count', 0)} "
            f"lines_proc={s.get('lines_processed_total', 0)} "
            f"inotify={s.get('inotify_events_received', 0)} "
            f"dedup={s.get('enqueue_dedup_count', 0)} "
            f"rescan={s.get('full_rescan_count', 0)} "
            f"replay={replay_total} "
            f"collide={dedup_collisions_total}"
        )
        # Cap to 200 cols just in case; truncate with an ellipsis.
        if len(glyphs) > 200:
            glyphs = glyphs[:197] + "..."
        lines.append(glyphs)
    return "\n".join(lines) + "\n"


async def api_diag_sessions(request):
    """Live alignment round-trip for one or all sessions.

    GET /api/diag/sessions[?session=<id>][&format=text]

    Emits a one-shot ``diag:request`` SSE event, waits for connected tabs to
    POST to /api/diag/client, then aggregates per-layer snapshots so it's
    obvious which layer (file/server/bus/client) first disagrees.
    """
    _diag_janitor_sweep()

    requested = request.query_params.get("session")
    diag_warnings: list[str] = []
    # Resolve sessions the diag should cover.
    try:
        registry = session_monitor.get_registry()
    except Exception:
        registry = []
    # Some tests seed live rows directly into dashboard.db before the
    # lifespan starts, and under worker load the in-process monitor can
    # lag that ground truth briefly. Diagnostics should still cover every
    # known live session, so merge any DB-only rows by session_id.
    try:
        from tools.dashboard.dao.dashboard_db import get_live_sessions as _diag_get_live_sessions
        seen_ids = {
            s.get("session_id")
            for s in registry
            if isinstance(s, dict) and s.get("session_id")
        }
        for row in _diag_get_live_sessions():
            session_id = row.get("tmux_name")
            if session_id and session_id not in seen_ids:
                registry.append({
                    "session_id": session_id,
                    "type": row.get("type"),
                    "is_live": bool(row.get("is_live", 1)),
                })
                seen_ids.add(session_id)
    except Exception as exc:
        logger.exception("diag: dashboard_db live-session fallback failed")
        diag_warnings.append(
            "dashboard_db live-session fallback failed: "
            f"{type(exc).__name__}: {exc}"
        )
    if requested:
        sessions = [s for s in registry if s.get("session_id") == requested]
        # If not in live registry, still allow (covers dead-but-known tabs).
        if not sessions:
            sessions = [{"session_id": requested}]
    else:
        sessions = registry

    session_ids = [s["session_id"] for s in sessions if s.get("session_id")]

    req_id = str(_uuid_mod.uuid4())
    request_type = "session_markers"
    emit_ts = time.time()
    deadline_ts = emit_ts + _DIAG_COLLECTION_WINDOW_SECONDS

    _DIAG_AGGREGATORS[req_id] = {
        "emit_ts": emit_ts,
        "request_type": request_type,
        "deadline_ts": deadline_ts,
        "params": {"sessions": session_ids},
        "clients": {},
    }

    # Fire the request to all SSE subscribers. dedup=False because every
    # diag round-trip is unique even if the body is identical. ``emit_ts``
    # is included so each client can compute its clock skew vs the server.
    await event_bus.broadcast(
        "diag:request",
        {
            "req_id": req_id,
            "request_type": request_type,
            "params": {"sessions": session_ids},
            "deadline_ms": int(_DIAG_COLLECTION_WINDOW_SECONDS * 1000),
            "emit_ts": emit_ts,
            "emit_ts_ms": int(emit_ts * 1000),
        },
        dedup=False,
    )

    # Collect for the full window — clients that missed it just don't show up.
    await asyncio.sleep(_DIAG_COLLECTION_WINDOW_SECONDS)

    aggregator = _DIAG_AGGREGATORS.get(req_id, {})
    client_replies = aggregator.get("clients", {})

    bus_block = _diag_build_bus_block()
    # Build a client_id → subscriber-meta lookup so we can attach
    # connection_id + subscription_age_s to each diag client envelope.
    bus_subs_by_client: dict[str, dict] = {}
    for meta in bus_block.get("subscribers", []) or []:
        cid = meta.get("client_id")
        if cid:
            bus_subs_by_client.setdefault(cid, meta)

    # Build the diag-window-relevant recent_broadcasts for the
    # session:messages topic so per-row topic blocks can filter further.
    session_messages_recent = event_bus.recent_broadcasts(
        topic="session:messages", limit=10,
    )

    rows = []
    for sid in session_ids:
        ts_obj = session_monitor._tail_states.get(sid)
        from tools.dashboard.session_monitor import get_session as _gs
        row_db = _gs(sid)
        jsonl_path = row_db.get("jsonl_path") if row_db else None
        file_block = _diag_build_file_block(jsonl_path)
        server_block = _diag_build_server_block(sid, ts_obj)

        clients_for_session = []
        for client_id, (recv_ts, payload, request_meta) in client_replies.items():
            sessions_payload = (payload.get("payload") or {}).get("sessions", {})
            if sid not in sessions_payload:
                continue
            client_state = (payload.get("payload") or {}).get("client_state", {})
            session_markers = sessions_payload[sid]
            lag_ms = max(0, int((recv_ts - emit_ts) * 1000))
            sub_meta = bus_subs_by_client.get(client_id)
            clients_for_session.append({
                "client_id": client_id,
                "lag_ms": lag_ms,
                "remote_addr": request_meta.get("remote_addr"),
                "user_agent": request_meta.get("user_agent"),
                "accept_language": request_meta.get("accept_language"),
                "connection_id": (sub_meta or {}).get("connection_id"),
                "subscription_age_s": (sub_meta or {}).get("age_s"),
                "client_state": client_state,
                "session_markers": session_markers,
            })

        client_seqs = [
            c.get("session_markers", {}).get("store_seq")
            for c in clients_for_session
            if c.get("session_markers", {}).get("store_seq") is not None
        ]
        max_lag = max((c.get("lag_ms", 0) for c in clients_for_session), default=None)
        min_seq = min(client_seqs) if client_seqs else None
        ooo_counts = [
            c.get("session_markers", {}).get("out_of_order_count", 0)
            for c in clients_for_session
        ]
        max_ooo = max(ooo_counts) if ooo_counts else 0

        alignment = _diag_tail_3_alignment(
            file_block.get("tail_10", []),
            server_block.get("tail_10", []),
            clients_for_session,
        )
        drift = {
            "file_lines_vs_server_seq": (
                (file_block.get("lines") or 0) - (server_block.get("broadcast_seq") or 0)
            ),
            "file_size_vs_server_offset": (
                (file_block.get("size_bytes") or 0) - (server_block.get("file_offset") or 0)
            ),
            "server_seq_vs_min_client": (
                (server_block.get("broadcast_seq") or 0) - min_seq
                if min_seq is not None else None
            ),
            "tail_3_alignment": alignment,
        }

        # session:messages broadcasts addressed to this session (best effort:
        # filter by serialised "session_id" substring against the recent
        # broadcasts pulled from the EventBus deque). We only have seq + ts
        # in the deque, not the full payload, so we report all session:messages
        # broadcasts in the window — clients can correlate via seq vs server
        # tail_10. This is "last 10 broadcasts on the session:messages topic"
        # as the spec defines it.
        rows.append({
            "session_id": sid,
            "file": file_block,
            "server": server_block,
            "topic": {
                "name": "session:messages",
                "last_seq": event_bus._last_seq.get("session:messages"),
                "recent_broadcasts_for_session": session_messages_recent,
            },
            "clients": clients_for_session,
            "max_client_lag_ms": max_lag,
            "min_client_seq": min_seq,
            "max_out_of_order_count": max_ooo,
            "drift": drift,
        })

    payload = {
        "req_id": req_id,
        "request_type": request_type,
        "emit_ts": emit_ts,
        "emit_ts_ms": int(emit_ts * 1000),
        "collection_window_ms": int(_DIAG_COLLECTION_WINDOW_SECONDS * 1000),
        "clients_responded": len(client_replies),
        "bus": bus_block,
        "rows": rows,
        "warnings": diag_warnings,
    }

    # Best-effort cleanup — janitor handles the rest.
    _DIAG_AGGREGATORS.pop(req_id, None)

    fmt = request.query_params.get("format", "")
    accept = request.headers.get("accept", "")
    if fmt == "text" or "text/plain" in accept:
        return PlainTextResponse(_diag_format_text_table(payload))
    return JSONResponse(payload)


async def api_diag_store_dump(request):
    """Range-dump one session's CLIENT-SIDE entry buffer from live tabs.

    GET /api/diag/store-dump?session=<id>[&from=N][&limit=M]

    auto-64nx3: the incident instrumentation the diag chase proved we
    needed — emits a ``diag:request`` with request_type
    ``session_store_dump``; connected tabs reply with a bounded slice of
    their in-memory store buffer ({type, tool_name, timestamp,
    content_len, entry_ref} per entry), so a phone-only rendering report
    can be byte-diffed against server truth remotely.
    """
    session_id = request.query_params.get("session")
    if not session_id:
        return JSONResponse({"error": "session required"}, status_code=400)
    try:
        from_idx = max(0, int(request.query_params.get("from", "0")))
        limit = min(max(1, int(request.query_params.get("limit", "200"))), 500)
    except ValueError:
        return JSONResponse({"error": "invalid from/limit"}, status_code=400)

    import uuid as _uuid_mod
    req_id = str(_uuid_mod.uuid4())
    request_type = "session_store_dump"
    emit_ts = time.time()
    params = {"session": session_id, "from": from_idx, "limit": limit}
    _DIAG_AGGREGATORS[req_id] = {
        "emit_ts": emit_ts,
        "request_type": request_type,
        "deadline_ts": emit_ts + _DIAG_COLLECTION_WINDOW_SECONDS,
        "params": params,
        "clients": {},
    }
    await event_bus.broadcast(
        "diag:request",
        {
            "req_id": req_id,
            "request_type": request_type,
            "params": params,
            "deadline_ms": int(_DIAG_COLLECTION_WINDOW_SECONDS * 1000),
            "emit_ts": emit_ts,
            "emit_ts_ms": int(emit_ts * 1000),
        },
        dedup=False,
    )
    await asyncio.sleep(_DIAG_COLLECTION_WINDOW_SECONDS)

    aggregator = _DIAG_AGGREGATORS.pop(req_id, {})
    clients = []
    for client_id, (recv_ts, body, request_meta) in aggregator.get("clients", {}).items():
        payload = body.get("payload") or {}
        clients.append({
            "client_id": client_id,
            "user_agent": request_meta.get("user_agent"),
            "client_state": payload.get("client_state", {}),
            "dump": {k: v for k, v in payload.items() if k != "client_state"},
        })
    return JSONResponse({
        "req_id": req_id,
        "session": session_id,
        "from": from_idx,
        "limit": limit,
        "clients_responded": len(clients),
        "clients": clients,
    })


async def api_diag_client(request):
    """Receive a per-tab diag reply and stash it in the live aggregator.

    Request-time metadata (client IP, User-Agent, Accept-Language) is
    captured from the live request and stored alongside the JSON body so
    the per-row diag response can attach a remote_addr / user_agent /
    accept_language fingerprint to each client envelope without trusting
    the tab to self-report.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    req_id = body.get("req_id")
    request_type = body.get("request_type")
    client_id = body.get("client_id")
    if not req_id or not request_type or not client_id:
        return JSONResponse(
            {"error": "req_id, request_type, client_id required"},
            status_code=400,
        )
    aggregator = _DIAG_AGGREGATORS.get(req_id)
    if aggregator is None:
        return JSONResponse({"error": "unknown or expired req_id"}, status_code=404)
    if aggregator.get("request_type") != request_type:
        return JSONResponse(
            {"error": "request_type mismatch"}, status_code=400,
        )
    request_meta = {
        "remote_addr": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        "accept_language": request.headers.get("accept-language"),
    }
    aggregator["clients"][client_id] = (time.time(), body, request_meta)
    return JSONResponse({"ok": True})


async def api_diag_eventbus_snapshot(request):
    """Persist the live EventBus state to a timestamped file under data/diag/.

    No caller-supplied path — we always write to ``data/diag/eventbus-{ISO}.state``.
    The fixed prefix means dumps accumulate harmlessly and never overwrite the
    live ``data/event_bus.state`` consumed by the next reload.
    """
    diag_dir = _DIAG_DIR
    diag_dir.mkdir(parents=True, exist_ok=True)
    # Filename-safe ISO 8601 (colons → dashes) in UTC, with sub-second resolution
    # so two snapshots in the same second still produce distinct files.
    now_dt = datetime.now(timezone.utc)
    stamp = now_dt.strftime("%Y-%m-%dT%H-%M-%S") + f"-{now_dt.microsecond:06d}Z"
    target = diag_dir / f"eventbus-{stamp}.state"
    event_bus.snapshot(target)
    bytes_written = target.stat().st_size if target.exists() else 0
    first_seq, last_seq, _, _ = event_bus.buffer_window()
    summary = {
        "path": str(target),
        "bytes": bytes_written,
        "seq": event_bus._seq,
        "epoch": current_server_epoch(),
        "buffer_entries": len(event_bus._buffer),
        "buffer_bytes": event_bus._buffer_bytes,
        "buffer_first_seq": first_seq,
        "buffer_last_seq": last_seq,
    }
    return JSONResponse(summary)


async def api_diag_settings(request):
    """Process-local Settings throughput snapshot."""
    auth_error = api_auth.require_authenticated_api_caller(request)
    if auth_error is not None:
        return auth_error
    from tools.graph import settings_ops

    return JSONResponse(settings_ops.settings_api_stats_snapshot())


async def api_diag_settings_sets(request):
    """Storage + activity summary for Settings sets in the selected org."""
    auth_error = api_auth.require_authenticated_api_caller(request)
    if auth_error is not None:
        return auth_error
    org = api_auth.organization_scope_from_request(request)
    resolved_org, windows, rows = _settings_diag_rows(org=org)
    from tools.graph import settings_ops as _settings_ops
    try:
        # Rows the schema says cannot exist, which every read still merges.
        illegal = _settings_ops.illegal_amendments(org=org)
    except Exception:
        illegal = []
    return JSONResponse({
        "org": resolved_org,
        "windows": windows,
        "sets": rows,
        "illegal_amendments": illegal,
    })


async def api_diag_settings_set_detail(request):
    """Storage detail for one Settings set, including per-key footprint."""
    auth_error = api_auth.require_authenticated_api_caller(request)
    if auth_error is not None:
        return auth_error
    set_id = request.path_params["set_id"]
    org = api_auth.organization_scope_from_request(request)
    resolved_org, windows, rows = _settings_diag_rows(org=org)
    summary = next((row for row in rows if row["set_id"] == set_id), None)
    if summary is None:
        return JSONResponse(
            {"error": f"unknown set_id: {set_id!r}"},
            status_code=404,
        )
    member_count, member_keys, read_error = _settings_member_snapshot(
        set_id, org=org)
    key_rows = _settings_key_storage_rows(set_id, org=org)
    for row in key_rows:
        row["member_present"] = row["key"] in member_keys
    return JSONResponse({
        "org": resolved_org,
        "windows": windows,
        "set": {
            **summary,
            "member_count": member_count,
            "count": member_count,
            "read_error": read_error,
        },
        "keys": key_rows,
    })


async def api_diag_settings_mediator(request):
    """Heartbeat snapshot for the settings-mediator dispatch loop.

    GET /api/diag/settings_mediator → JSON dump of
    ``settings_mediator.HEALTH``. Read-only, no side effects, no
    auth gate beyond the dashboard's own.

    Useful when the loop wedges silently — `last_tick_age_s` answers
    "is the loop alive?", `events_received_count` answers "has the
    bus ever delivered an event?", and
    `last_handler_fired_at[name]` localises the gap when a specific
    action handler fails to fire.
    """
    from tools.dashboard.settings_mediator import HEALTH
    return JSONResponse(HEALTH.to_dict())


# ── Background watchers ───────────────────────────────────────

_DISPATCH_WATCHER_INTERVAL = 5   # seconds between dispatch polls
# 15 min: no point polling the provider /usage APIs faster than the cache
# lives (HARNESS_USAGE_CACHE_TTL = 15 min), and the slower cadence keeps us
# off the Claude /usage rate limit (was 300s).
_HARNESS_USAGE_POLL_INTERVAL = 900.0

_WATCHER_HELPERS = [
    "collect_dispatch_data", "get_bead_counts", "count_active_sessions",
    "count_terminals", "count_today_done", "get_dispatcher_state", "get_pinned_beads",
    "count_worktrees", "count_streams", "collect_harness_usage", "collect_plugin_badges",
]
_watcher_errors: dict[str, str] = {}  # helper_name -> last error string

# Last-broadcast signature for the ``worktrees`` SSE topic. Lets the
# watcher emit only on change (worktree_monitor refreshes every 30s,
# the watcher loops every 5s — without this we'd flood every connected
# client with identical payloads 6x more often than the data changes).
_worktrees_last_signature: str | None = None
_harness_usage_last_refresh_context: dict[str, tuple[tuple[str, ...], float]] = {}


_WAITING_LIST_LIMIT = 5


async def _collect_dispatch_data() -> dict:
    """Collect data for the 'dispatch' topic: active, waiting, blocked.

    Active dispatches come from SQLite dispatch_runs WHERE status=RUNNING,
    enriched with Dolt bead metadata (title, priority, labels).
    Waiting/blocked come from Dolt (readiness:approved beads), with
    currently-running beads excluded to avoid double-counting.
    """
    # Two waves, not one: the bead queries must EXCLUDE the running beads,
    # so which ones are running has to be known before they run. Same
    # parallel width as before — the metadata lookup was already serial
    # behind the first wave, and now shares the second with the bead read.
    from agents.dispatch_db import get_active_agentic_runs
    running_runs, active_agentic = await asyncio.gather(
        asyncio.to_thread(dao_dispatch.get_running_with_stats),
        asyncio.to_thread(get_active_agentic_runs),
    )
    running_bead_ids = [r["bead_id"] for r in running_runs if r.get("bead_id")]
    bead_data, bead_meta = await asyncio.gather(
        asyncio.to_thread(
            dao_beads.get_dispatch_beads, _WAITING_LIST_LIMIT, running_bead_ids,
        ),
        asyncio.to_thread(dao_beads.get_bead_title_priority, running_bead_ids),
    )

    # CPU and resident memory are sampled by ResourceMonitor for the Sessions
    # cards and keyed by the dispatch run/container name.  The dispatch DB's
    # stats columns can remain NULL for agentic runs, so use the existing live
    # sample as the current value instead of making each frontend rediscover it.
    # This single dispatch payload feeds both /dispatch and Activity.
    resource_rows = resource_monitor.snapshot().get("sessions", {})

    # Build active list from SQLite RUNNING runs + Dolt metadata
    active = []
    for run in running_runs:
        bead_id = run.get("bead_id", "")
        librarian_type = run.get("librarian_type") or None
        kind = run.get("kind") or "bead"
        agentic_source_id = run.get("agentic_source_id") or None
        meta = bead_meta.get(bead_id, {})
        resource = (
            resource_rows.get(run.get("id", ""))
            or resource_rows.get(run.get("container_name", ""))
            or {}
        )
        container = None
        if run.get("container_name"):
            container = {
                "name": run["container_name"],
                "image": run.get("image"),
                "status": None,
            }
        # Agentic runs: id is the run's container_name (== run.id); title,
        # action_label, target_*, and sender resolve via the shared agentic
        # identity helper so live and historical views match.
        # Librarian runs: use dir name as id, synthetic title, no priority.
        agentic_ident: dict | None = None
        if kind == "agentic":
            effective_id = run.get("id", "")
            agentic_ident = _resolve_agentic_identity(agentic_source_id)
            effective_title = (
                run.get("title")
                or agentic_ident["title"]
                or run.get("id", "")
            )
        elif librarian_type:
            effective_id = bead_id or run.get("id", "")
            effective_title = f"Librarian: {librarian_type}"
        else:
            effective_id = bead_id or run.get("id", "")
            effective_title = meta.get("title") or bead_id
        active_row = {
            "id": effective_id,
            "title": effective_title,
            "priority": meta.get("priority") if not (librarian_type or kind == "agentic") else None,
            "labels": meta.get("labels", []),
            "librarian_type": librarian_type,
            "kind": kind,
            "agentic_source_id": agentic_source_id,
            "container": container,
            "run_dir": run.get("id"),
            "last_snippet": run.get("last_snippet"),
            "token_count": run.get("token_count"),
            "tool_count": run.get("tool_count"),
            "turn_count": run.get("turn_count"),
            "cpu_pct": (
                resource.get("cpu_pct")
                if resource.get("cpu_pct") is not None
                else run.get("cpu_pct")
            ),
            "cpu_usec": run.get("cpu_usec"),
            "mem_mb": (
                resource["mem_bytes"] / 1_000_000
                if resource.get("mem_bytes") is not None
                else run.get("mem_mb")
            ),
            "duration_secs": run.get("duration_secs") or (
                int(time.time() - datetime.fromisoformat(run["started_at"]).replace(tzinfo=timezone.utc).timestamp())
                if run.get("started_at") else None
            ),
            "last_activity": (
                datetime.fromisoformat(run["last_activity"]).replace(tzinfo=timezone.utc).timestamp()
                if run.get("last_activity") else None
            ),
        }
        if agentic_ident is not None:
            monitored = dashboard_db.get_session(run.get("id", "")) or {}
            monitored_last_activity = monitored.get("last_activity")
            tool_count = run.get("tool_count")
            if tool_count is None:
                # SessionMonitor intentionally does not maintain a separate
                # tool-use counter. Reuse the dispatcher's established JSONL
                # parser for that one field rather than creating another
                # monitor metric.
                from agents.dispatcher import _agentic_jsonl_metrics, _find_jsonl_file
                jsonl_file = _find_jsonl_file(str(run.get("output_dir") or ""))
                _snippet, _turns, tool_count, _activity = _agentic_jsonl_metrics(jsonl_file)
            if monitored_last_activity is not None:
                monitored_last_activity = float(monitored_last_activity)
            active_row["action_label"] = agentic_ident["action_label"]
            active_row["member_key"] = agentic_ident["member_key"]
            active_row["target_kind"] = agentic_ident["target_kind"]
            active_row["target_source_id"] = agentic_ident["target_source_id"]
            active_row["target_org"] = agentic_ident["target_org"]
            active_row["dispatched_by_session"] = agentic_ident["dispatched_by_session"]
            active_row["last_snippet"] = (
                active_row["last_snippet"] or monitored.get("last_message") or None
            )
            active_row["token_count"] = (
                active_row["token_count"]
                if active_row["token_count"] is not None
                else monitored.get("context_tokens")
            )
            active_row["turn_count"] = (
                active_row["turn_count"]
                if active_row["turn_count"] is not None
                else monitored.get("entry_count")
            )
            active_row["tool_count"] = tool_count
            active_row["last_activity"] = (
                active_row["last_activity"]
                if active_row["last_activity"] is not None
                else monitored_last_activity
            )
            # Provider identity belongs to the monitored session.  The eager
            # agentic source metadata is the launch-time fallback for the
            # short interval before the JSONL registration arrives.
            active_row["harness"] = (
                monitored.get("harness") or agentic_ident["harness"]
                or run.get("harness") or None
            )
            active_row["model"] = (
                monitored.get("model") or agentic_ident["model"]
                or run.get("model") or None
            )
        active.append(active_row)

    # Running beads are excluded in SQL (get_dispatch_beads(exclude_ids=...)),
    # which is what keeps the list and the total agreeing. Filtering here
    # instead is the bug this replaced: the count came from SQL and the list
    # was trimmed in Python, so a running bead was counted and not shown —
    # "showing top 0 of 1 waiting" over an empty section.
    waiting = [
        {
            "id": b["id"], "title": b["title"],
            "priority": b["priority"], "labels": b.get("labels", []),
            "status": b.get("status"),
        }
        for b in bead_data["approved_waiting"]
    ]

    # Accepted-but-not-launched agentic dispatches belong in the same
    # section: QUEUED rows wait on the launch queue, PREPARING rows are
    # in workspace prep behind the semaphore. This is where the 202
    # restructure's backpressure becomes VISIBLE — before it, a dispatch
    # in prep existed only as an open HTTP request.
    pending_agentic = [
        run for run in active_agentic
        if run.get("status") in ("QUEUED", "PREPARING")
    ]
    pending_agentic.sort(key=lambda r: str(r.get("started_at") or ""))
    waiting_total = bead_data.get(
        "approved_waiting_total", len(waiting)) + len(pending_agentic)
    for run in pending_agentic:
        run_status = run.get("status")
        ident = _resolve_agentic_identity(run.get("agentic_source_id"))
        waiting.append({
            "id": run.get("id", ""),
            "title": run.get("title") or ident.get("title")
                     or ident.get("action_label") or run.get("id", ""),
            "priority": None,
            "labels": [],
            "status": run_status.lower(),
            "kind": "agentic",
            "action_label": ident.get("action_label"),
            # routeForRun() resolves the card link from these, exactly as
            # the active agentic cards do.
            "agentic_source_id": run.get("agentic_source_id"),
            "target_kind": ident.get("target_kind"),
            "target_source_id": ident.get("target_source_id"),
        })
    # Top-N display list; the totals badge carries the real count. A
    # thousand-row waiting list in every 5s SSE frame helped no one
    # (operator directive): beads arrive SQL-LIMITed, and the combined
    # list re-caps after the queued agentic rows join.
    waiting = waiting[:_WAITING_LIST_LIMIT]

    # Blocked: rename open_blockers → blockers, exclude currently-running beads
    blocked = [
        {
            "id": b["id"], "title": b["title"],
            "priority": b["priority"], "labels": b.get("labels", []),
            "status": b.get("status"),
            "blockers": b.get("open_blockers", []),
        }
        for b in bead_data["approved_blocked"]
    ]

    return {
        "active": active,
        "waiting": waiting,
        "waiting_total": waiting_total,
        "blocked": blocked,
        "paused": _get_pause_state(),
        "pause_reasons": _get_pause_reasons(),
    }


def _count_active_sessions() -> int:
    """Count active Claude Code sessions from the session monitor."""
    return session_monitor.count()


def _count_terminals() -> int:
    """Count active dashboard tmux sessions."""
    return len(_list_dashboard_tmux())


def _count_today_done() -> int:
    """Count dispatch runs completed successfully today."""
    conn = _timeline_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM dispatch_runs WHERE status='DONE' AND completed_at >= date('now')"
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _count_streams() -> int:
    """Count distinct tags across all notes (active stream count)."""
    return graph_ops.count_active_streams()


def _collect_plugin_badges() -> dict[str, dict]:
    """Walk the plugin registry, call each declared `badge_counter`, and
    namespace the result under ``plugins.<id>.badge``. Failures are
    logged but never propagate — a broken plugin badge does not take
    the dispatch watcher down.
    """
    out: dict[str, dict] = {}
    for p in PLUGIN_REGISTRY:
        if p.badge_counter is None:
            continue
        try:
            out[p.id] = {"badge": p.badge_counter()}
        except Exception:
            logger.exception("[plugin %s] badge_counter raised", p.id)
    return out


def _count_worktrees() -> dict[str, int]:
    """Count pending worktree stacks and dirty worktrees from the cached monitor."""
    rows = worktree_monitor.get_all()
    return {
        "with_commits": sum(1 for row in rows if row.commits),
        "with_changes": sum(1 for row in rows if row.is_dirty),
    }


def _parse_harness_state(raw_state: Any) -> dict[str, Any]:
    if isinstance(raw_state, dict):
        return raw_state
    if not isinstance(raw_state, str) or not raw_state.strip():
        return {}
    try:
        parsed = json.loads(raw_state)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _state_timestamp(state: dict[str, Any], key: str) -> float:
    value = state.get(key)
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _has_rate_limit_state(state: dict[str, Any]) -> bool:
    windows = state.get("windows")
    return (
        state.get("kind") == "rate_limits"
        and isinstance(windows, dict)
        and bool(windows)
    )


def _collect_harness_usage() -> dict[str, list[dict[str, Any]]]:
    """Summarize rate-limit telemetry for each live harness."""

    if os.environ.get("DASHBOARD_MOCK"):
        rows = dao_sessions.get_session_status_rows(None)
    else:
        rows = dashboard_db.get_live_sessions()

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not row.get("is_live", 1):
            continue
        harness = str(row.get("harness") or "claude").strip().lower() or "claude"
        bucket = grouped.setdefault(
            harness,
            {
                "harness": harness,
                "session_count": 0,
                "available": False,
                "state": None,
                "_updated_at": 0.0,
            },
        )
        bucket["session_count"] += 1
        state = _parse_harness_state(row.get("harness_state"))
        if not _has_rate_limit_state(state):
            continue
        updated_at = _state_timestamp(state, "updated_at")
        if (not bucket["available"]) or updated_at >= bucket["_updated_at"]:
            bucket["available"] = True
            bucket["state"] = state
            bucket["_updated_at"] = updated_at

    harnesses: list[dict[str, Any]] = []
    order = {"claude": 0, "codex": 1}
    for bucket in grouped.values():
        item = {
            "harness": bucket["harness"],
            "session_count": bucket["session_count"],
            "available": bucket["available"],
        }
        if bucket["available"]:
            item["state"] = bucket["state"]
        else:
            item["reason"] = "No rate-limit telemetry captured yet"
        harnesses.append(item)
    harnesses.sort(key=lambda item: (order.get(item["harness"], 99), item["harness"]))
    return {"harnesses": harnesses}


def _dashboard_default_org() -> str:
    """Deployment's effective org — the shell's default for any
    non-plugin page render.

    Bead auto-t0auy: ``base.html`` injects this value into
    ``<meta name="autonomy-shell-org">`` so the SPA can stamp it as
    ``X-Graph-Org`` on every shell-route fetch. Without it, calls like
    ``Schema.of('dashboard.harness.usage').all()`` fall through to the
    server's scopeless default and silently return ``[]``.

    # org-scope: machine — dashboard.shell.default-org declares this
    # node's shell rendering default (seeded by first-run).
    """
    from tools.graph.schemas.dashboard_shell import shell_default_org
    return shell_default_org()


def _harness_usage_org() -> str:
    # Claude credential/usage rows are host-local, per-instance secrets —
    # pinned to personal.db (read with peers=[]), independent of the dashboard
    # shell's default org, so the poller write, launcher, refresh, and CLI all
    # converge on the same rows. (Do NOT route these through the shell org.)
    return _harness_usage_settings.HARNESS_USAGE_ORG


def _should_run_harness_usage_poller() -> bool:
    if os.environ.get("DASHBOARD_MOCK"):
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return True


def operator_is_idle(*, threshold_minutes: int = 15) -> bool:
    from tools.graph.surface import OperatorActivity

    return OperatorActivity.is_idle(threshold=timedelta(minutes=threshold_minutes))


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z",
    )


# Debounce UI-driven operator-activity writes. The idle gate
# (OperatorActivity) historically only advanced when a session parsed a
# user/crosstalk turn, so merely viewing/navigating the dashboard read as
# idle (and idle-gated UI like the harness-usage strip vanished). The client
# pings api_operator_active on genuine interaction (already client-throttled);
# we debounce again here so a chatty/misbehaving tab can't hammer the singleton
# graph write. One write per interval is plenty against a 15-30 min idle gate.
_OPERATOR_ACTIVE_MIN_INTERVAL_S = 45.0
_operator_active_last_write_mono = 0.0


async def api_operator_active(request):
    """Record operator UI interaction (nav / click / scroll) as input.

    POST-only, no body. Returns ``{"ok": true}`` (``debounced: true`` when the
    write was coalesced). Never raises into the request — the underlying write
    is itself fire-and-forget.
    """
    global _operator_active_last_write_mono
    now_mono = time.monotonic()
    if now_mono - _operator_active_last_write_mono < _OPERATOR_ACTIVE_MIN_INTERVAL_S:
        return JSONResponse({"ok": True, "debounced": True})
    _operator_active_last_write_mono = now_mono
    try:
        from tools.dashboard.session_monitor import _record_operator_input
        _record_operator_input(_now_iso())
    except Exception:
        logger.exception("api_operator_active: record failed")
    return JSONResponse({"ok": True})


async def api_voice_diag(request):
    """Receive a client-side voice-capture trace line and log it so the operator
    can reproduce a bug on-device while we tail the dashboard log. Temporary
    debugging aid (removed once the re-emit suppression bug is found)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    logger.info("VOICE-DIAG %s", str(body.get("msg", ""))[:500])
    return JSONResponse({"ok": True})


VOICE_TRACE_MAX_BODY_BYTES = 2 * 1024 * 1024
VOICE_TRACE_MAX_FRAMES = 1200
VOICE_TRACE_MAX_FILES = 100
VOICE_TRACE_MAX_TOTAL_BYTES = 50 * 1024 * 1024
VOICE_TRACE_MAX_AGE_S = 7 * 24 * 60 * 60


def _prune_voice_traces(traces_dir: Path, *, now: float) -> int:
    """Bound disposable voice diagnostics by age, count, and total bytes."""
    kept: list[tuple[Path, os.stat_result]] = []
    removed = 0
    cutoff = now - VOICE_TRACE_MAX_AGE_S
    for path in traces_dir.glob("voice-trace-*.json"):
        try:
            stat = path.stat()
            if stat.st_mtime < cutoff:
                path.unlink()
                removed += 1
            else:
                kept.append((path, stat))
        except OSError:
            logger.warning("VOICE-TRACE could not inspect/prune %s", path,
                           exc_info=True)

    kept.sort(key=lambda item: (item[1].st_mtime, item[0].name))
    total_bytes = sum(stat.st_size for _path, stat in kept)
    while kept and (len(kept) > VOICE_TRACE_MAX_FILES
                    or total_bytes > VOICE_TRACE_MAX_TOTAL_BYTES):
        path, stat = kept.pop(0)
        try:
            path.unlink()
            removed += 1
            total_bytes -= stat.st_size
        except OSError:
            logger.warning("VOICE-TRACE could not prune %s", path,
                           exc_info=True)
    return removed


async def api_voice_trace(request):
    """Persist a client-side voice failure-trace (real inbound frames + render +
    clear events) so a flaky clear can be replayed deterministically in the mock
    harness instead of guessed. Debug aid — the client only POSTs when enabled
    via ?vtrace=1, once per clear (never per audio frame)."""
    chunks: list[bytes] = []
    nbytes = 0
    async for chunk in request.stream():
        nbytes += len(chunk)
        if nbytes > VOICE_TRACE_MAX_BODY_BYTES:
            return JSONResponse(
                {"ok": False, "error": "voice trace exceeds the 2 MiB limit"},
                status_code=413,
            )
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"ok": False, "error": "voice trace must be JSON"},
                            status_code=400)
    if not isinstance(body, dict) or not isinstance(body.get("frames"), list):
        return JSONResponse(
            {"ok": False, "error": "voice trace must carry a frames list"},
            status_code=400,
        )
    if len(body["frames"]) > VOICE_TRACE_MAX_FRAMES:
        return JSONResponse(
            {"ok": False, "error": "voice trace exceeds the 1200-frame limit"},
            status_code=413,
        )
    try:
        rendered = json.dumps(body, indent=2).encode("utf-8")
        if len(rendered) > VOICE_TRACE_MAX_BODY_BYTES:
            return JSONResponse(
                {"ok": False, "error": "rendered voice trace exceeds the 2 MiB limit"},
                status_code=413,
            )
        traces_dir = DATA_ROOT / "voice-traces"
        traces_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        reason = re.sub(r"[^a-z0-9]+", "-", str(body.get("reason", "trace")).lower())[:24] or "trace"
        out = traces_dir / f"voice-trace-{ts}-{reason}.json"
        out.write_bytes(rendered)
        nframes = len(body.get("frames", []) or [])
        pruned = _prune_voice_traces(traces_dir, now=time.time())
        logger.info("VOICE-TRACE saved %s (%d frames, %d pruned)",
                    out, nframes, pruned)
        return JSONResponse({"ok": True, "path": str(out), "frames": nframes,
                             "pruned": pruned})
    except Exception:
        logger.exception("api_voice_trace: save failed")
        return JSONResponse({"ok": False}, status_code=500)


def _publish_harness_usage_snapshot() -> None:
    if operator_is_idle(threshold_minutes=15):
        return

    rows = dashboard_db.get_live_sessions()
    updated_at = _now_iso()

    codex_rows = [
        row for row in rows
        if str(row.get("harness") or "claude").strip().lower() == "codex"
    ]
    claude_rows = [
        row for row in rows
        if str(row.get("harness") or "claude").strip().lower() == "claude"
    ]

    _maybe_publish_harness_usage(
        harness="codex",
        rows=codex_rows,
        updated_at=updated_at,
        collector=_collect_codex_usage_payloads,
    )
    # auto-08n3f: Claude usage is enumerated from substrate-stored
    # credentials, not from live sessions, so the publisher runs every
    # tick regardless of how many Claude sessions are alive. Codex still
    # gates on live sessions because its telemetry is harvested from
    # session transcripts.
    _publish_claude_harness_usage_unconditional(
        updated_at=updated_at,
    )


def _publish_claude_harness_usage_unconditional(
    *, updated_at: str,
) -> None:
    """Run the Claude collector every tick and persist changed payloads.

    Bypasses the `rows`-based dedup in :func:`_maybe_publish_harness_usage`
    because Claude usage no longer depends on live sessions — every
    installed credentials row gets a tick whether or not anyone is
    burning it. The collector decides whether each row is `ok` or
    `unavailable`; the shared publisher suppresses unchanged telemetry.
    """
    payloads = _collect_claude_usage_payloads([], updated_at)
    for key, payload in payloads:
        _harness_usage_settings.publish_if_changed(
            key,
            payload,
            upsert_by_key=graph_ops.upsert_by_key,
        )


def _maybe_publish_harness_usage(
    *,
    harness: str,
    rows: list[dict[str, Any]],
    updated_at: str,
    collector,
) -> None:
    if not rows:
        _harness_usage_last_refresh_context.pop(harness, None)
        return

    session_signature = tuple(sorted(
        str(row.get("tmux_name") or "").strip()
        for row in rows
        if str(row.get("tmux_name") or "").strip()
    ))
    latest_user_message_at = _latest_harness_user_message_at(rows)
    previous_context = _harness_usage_last_refresh_context.get(harness)
    if previous_context is not None:
        previous_signature, previous_user_message_at = previous_context
        if (
            previous_signature == session_signature
            and latest_user_message_at <= previous_user_message_at
        ):
            return

    payloads = collector(rows, updated_at)
    for key, payload in payloads:
        _harness_usage_settings.publish_if_changed(
            key,
            payload,
            upsert_by_key=graph_ops.upsert_by_key,
        )
    _harness_usage_last_refresh_context[harness] = (
        session_signature,
        latest_user_message_at,
    )


def _latest_harness_user_message_at(rows: list[dict[str, Any]]) -> float:
    latest = 0.0
    for row in rows:
        state = _parse_harness_state(row.get("harness_state"))
        latest = max(latest, _state_timestamp(state, "last_user_message_at"))
    return latest


def _collect_codex_usage_payloads(
    rows: list[dict[str, Any]],
    updated_at: str,
) -> list[tuple[str, dict[str, Any]]]:
    freshest_state: dict[str, Any] | None = None
    freshest_ts = 0.0
    for row in rows:
        state = _parse_harness_state(row.get("harness_state"))
        if not _has_rate_limit_state(state):
            continue
        state_ts = _state_timestamp(state, "updated_at")
        if freshest_state is None or state_ts >= freshest_ts:
            freshest_state = state
            freshest_ts = state_ts

    key = _harness_usage_settings.make_harness_usage_key("codex", "default")
    if freshest_state is None:
        return [(
            key,
            _harness_usage_settings.make_unavailable_usage_payload(
                harness="codex",
                identity_id="default",
                identity_label="default",
                source="transcript",
                note="No transcript rate-limit telemetry captured yet",
                updated_at=updated_at,
            ),
        )]

    return [(
        key,
        _harness_usage_settings.normalize_codex_usage_payload(
            freshest_state,
            updated_at=updated_at,
        ),
    )]


def _collect_claude_usage_payloads(
    rows: list[dict[str, Any]],
    updated_at: str,
) -> list[tuple[str, dict[str, Any]]]:
    """Build harness-usage payloads for every installed Claude credential.

    auto-08n3f: enumerates ``dashboard.claude.credentials`` rows instead
    of walking host ``~/.claude/.setup-token*`` files. Each row carries
    a fresh ``access_token`` (the OAuth refresh poller — bead 3 — keeps
    it ahead of the 8h Anthropic expiry); we use it as the Bearer for
    GET ``/api/oauth/usage``. The harness-usage row is keyed by the
    bare ``claude:org:<uuid>`` (no alias suffix), since the credentials
    set is keyed-per-entity by org UUID and one row per org is the
    natural shape. Alias is kept on the harness-usage payload as
    informational so existing UI rendering paths still see it.

    The ``rows`` parameter is retained for symmetry with
    ``_collect_codex_usage_payloads`` (the publisher dispatches on
    harness) but is intentionally unused here; sessions no longer
    contribute Claude usage telemetry.

    On failure (HTTP 4xx / 5xx / network) we still write a row for the
    org with ``status='unavailable'`` so the dashboard footer can show
    "telemetry unavailable for <alias>" rather than silently dropping
    the account — matches the codex-side pattern.
    """
    del rows  # Claude usage is enumerated from substrate credentials now.

    from tools.graph import ops as graph_ops_local
    from tools.graph.schemas.claude_credentials import (
        CLAUDE_CREDENTIALS_SET_ID,
    )

    payloads: dict[str, dict[str, Any]] = {}
    org = _harness_usage_org()
    try:
        members = graph_ops_local.read_set(CLAUDE_CREDENTIALS_SET_ID, org=org, peers=[])
    except Exception:
        logger.exception(
            "claude harness usage: read_set(%s) failed; tick aborted",
            CLAUDE_CREDENTIALS_SET_ID,
        )
        return []
    rows_iter = list(getattr(members, "members", []) or [])
    if not rows_iter:
        return []

    for member in rows_iter:
        payload = member.payload if isinstance(member.payload, dict) else {}
        org_uuid = member.key
        alias = payload.get("alias") if isinstance(payload.get("alias"), str) else None
        access_token = payload.get("access_token")
        identity_id = f"org:{org_uuid}"
        row_key = _harness_usage_settings.make_harness_usage_key(
            "claude", identity_id,
        )
        if not isinstance(access_token, str) or not access_token:
            payloads[row_key] = _harness_usage_settings.make_unavailable_usage_payload(
                harness="claude",
                identity_id=identity_id,
                identity_label=_harness_usage_settings.short_identity_label(
                    "org", org_uuid,
                ),
                source="oauth_usage",
                note="credentials row missing access_token",
                updated_at=updated_at,
                account_id=org_uuid,
                alias=alias,
            )
            continue
        logger.info(
            "claude harness usage: fetching /usage for org=%s alias=%r",
            org_uuid, alias,
        )
        try:
            usage_body, _headers = _fetch_claude_oauth_usage(access_token)
        except Exception as exc:
            logger.exception(
                "claude harness usage: /usage call failed for org=%s alias=%r",
                org_uuid, alias,
            )
            # A failed poll must not erase a reading that is still true. Usage
            # is monotonic within a window, so a stored reading holds as a
            # lower bound until its own reset time -- and a maxed account
            # answers /usage with 429, which means the reading this would
            # overwrite is the one proving the account is exhausted.
            existing = _existing_usage_payload(row_key)
            if _harness_usage_settings.reading_still_valid(existing):
                logger.info(
                    "claude harness usage: keeping the live reading for "
                    "org=%s alias=%r; its window has not reset", org_uuid, alias,
                )
                continue
            payloads[row_key] = _harness_usage_settings.make_unavailable_usage_payload(
                harness="claude",
                identity_id=identity_id,
                identity_label=_harness_usage_settings.short_identity_label(
                    "org", org_uuid,
                ),
                source="oauth_usage",
                note=f"/usage call failed: {type(exc).__name__}: {exc}"[:200],
                updated_at=updated_at,
                account_id=org_uuid,
                alias=alias,
            )
            continue
        # The credentials row is the source of truth for org identity now;
        # we don't need the response header. Build the payload with the row's
        # own org_uuid so failure rows and ok rows key consistently.
        logger.info(
            "claude harness usage: /usage OK for org=%s alias=%r",
            org_uuid, alias,
        )
        payloads[row_key] = _harness_usage_settings.normalize_claude_usage_payload(
            bundle={
                "subscription_type": None,
                "rate_limit_tier": None,
            },
            usage_body=usage_body,
            org_id=org_uuid,
            updated_at=updated_at,
            alias=alias,
        )

    return sorted(payloads.items(), key=lambda item: item[0])


def _fetch_claude_oauth_usage(
    access_token: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    """GET /api/oauth/usage. Logs intent / success / failure with HTTP code +
    duration so production traces attribute every call to its outcome.
    Bearer token is never logged."""
    url = "https://api.anthropic.com/api/oauth/usage"
    from tools.graph.claude_oauth import CLAUDE_USER_AGENT
    req = urllib_request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
            "User-Agent": CLAUDE_USER_AGENT,
        },
        method="GET",
    )
    logger.info("claude /usage: GET %s (Bearer auth)", url)
    started = time.monotonic()
    try:
        with urllib_request.urlopen(req, timeout=10) as resp:
            body_bytes = resp.read()
            headers = {k.lower(): v for k, v in resp.headers.items()}
            status_code = resp.status
    except urllib_error.HTTPError as exc:
        elapsed_ms = (time.monotonic() - started) * 1000
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            detail = ""
        logger.error(
            "claude /usage: GET FAILED HTTP %d in %.1fms: %s",
            exc.code, elapsed_ms, detail[:160] or "<empty body>",
        )
        suffix = f": {detail[:160]}" if detail else ""
        raise RuntimeError(f"Claude usage API returned HTTP {exc.code}{suffix}") from exc
    except Exception as exc:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.error(
            "claude /usage: GET ERROR in %.1fms: %s",
            elapsed_ms, type(exc).__name__,
        )
        raise RuntimeError(f"Claude usage API failed: {type(exc).__name__}") from exc

    elapsed_ms = (time.monotonic() - started) * 1000
    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.error(
            "claude /usage: GET HTTP %d in %.1fms returned invalid JSON",
            status_code, elapsed_ms,
        )
        raise RuntimeError("Claude usage API returned invalid JSON") from exc
    if not isinstance(body, dict):
        logger.error(
            "claude /usage: GET HTTP %d in %.1fms returned non-object payload",
            status_code, elapsed_ms,
        )
        raise RuntimeError("Claude usage API returned a non-object payload")
    org_id = headers.get("anthropic-organization-id", "<unset>")
    logger.info(
        "claude /usage: GET OK HTTP %d in %.1fms org=%s",
        status_code, elapsed_ms, org_id,
    )
    return body, headers


async def _harness_usage_poller() -> None:
    while True:
        try:
            await asyncio.to_thread(_publish_harness_usage_snapshot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("harness usage poller tick failed")
        await asyncio.sleep(_HARNESS_USAGE_POLL_INTERVAL)


async def _vault_release_sweeper() -> None:
    """Destroy delivered secrets at their deadline or session end (auto-pw9bs.5).

    The durable record store survives a dashboard restart; this loop is the
    live half that acts on it. Pure bookkeeping since the private-ramfs
    redesign (2026-08-30): it closes leases for gone sessions; delivered
    files die with their container's own mount namespace, so a wrong
    liveness answer can no longer destroy anything. One failing tick never
    kills the loop — the record stays outstanding and the next tick
    retries."""
    from tools.dashboard import vault_release_sweeper as _vault_sweeper

    while True:
        try:
            live = await asyncio.to_thread(_live_session_names_or_none)
            await asyncio.to_thread(
                _vault_sweeper.sweep,
                session_exists=(
                    (lambda s, _live=live: s in _live)
                    if live is not None else None
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("vault release sweeper tick failed")
        await asyncio.sleep(_vault_sweeper.SWEEP_INTERVAL_S)


# Run-ids already reconciled this process — repeat work is harmless but
# noisy, so both sets are per-process memos, rebuilt from the run table
# after a restart (which is exactly the catch-up sweep that demotes any
# card stranded while the dashboard was down).
_dispatch_sessions_registered: set = set()
_dispatch_sessions_demoted: set = set()
_last_orphan_sweep: float = 0.0


def _dispatch_run_session_name(row: dict) -> str | None:
    """The monitor tmux_name a dispatch run's session was registered under."""
    kind = (row.get("kind") or "bead")
    if kind == "agentic":
        return row.get("id") or None
    output_dir = row.get("output_dir")
    if output_dir:
        return Path(output_dir).name
    return row.get("bead_id") or None


async def _reconcile_dispatch_sessions() -> None:
    """Make dispatch-born session cards follow their run rows, in-process.

    The dispatcher's HTTP /api/monitor/register + /deregister calls send
    no bearer and 401 against the authenticated API (2026-08-28 handoff
    148ead24 item 4: 158 finished agentic sessions stranded ACTIVE, every
    dispatch affected). Rather than teaching a host daemon to hold a
    credential, the dashboard — which OWNS the monitor and already polls
    dispatch.db here — registers sessions when their run row appears and
    demotes them when it turns terminal. One missed HTTP call can no
    longer strand a card, and a dashboard restart replays the recent run
    table, sweeping up anything stranded while it was down.
    """
    from agents.dispatch_db import get_currently_running, list_runs
    running, recent = await asyncio.gather(
        asyncio.to_thread(get_currently_running),
        asyncio.to_thread(list_runs, 100),
    )
    for row in running:
        run_id = row.get("id")
        name = _dispatch_run_session_name(row)
        if not run_id or not name or run_id in _dispatch_sessions_registered:
            continue
        _dispatch_sessions_registered.add(run_id)
        if session_monitor.get_one(name) is not None:
            continue
        kind = (row.get("kind") or "bead")
        try:
            await session_monitor.register_session(
                tmux_name=name,
                type="agentic" if kind == "agentic" else "dispatch",
                run_dir=row.get("output_dir") or None,
                bead_id=row.get("bead_id") or None,
            )
        except Exception:
            _dispatch_sessions_registered.discard(run_id)
            logger.exception(
                "dispatch-session reconcile: register failed for %s", name)
    for row in recent:
        if (row.get("status") or "RUNNING") in ("RUNNING", "QUEUED", "PREPARING"):
            continue
        run_id = row.get("id")
        name = _dispatch_run_session_name(row)
        if not run_id or not name or run_id in _dispatch_sessions_demoted:
            continue
        _dispatch_sessions_demoted.add(run_id)
        existing = session_monitor.get_one(name)
        if existing is None or existing.get("state") in ("ENDED", "FAILED"):
            continue
        try:
            await session_monitor.deregister_session(name)
            logger.info(
                "dispatch-session reconcile: demoted %s (run %s %s)",
                name, run_id, row.get("status"))
        except Exception:
            _dispatch_sessions_demoted.discard(run_id)
            logger.exception(
                "dispatch-session reconcile: demote failed for %s", name)


async def _dispatch_watcher():
    """Background task: poll dispatch state and broadcast to SSE topics.

    Uses return_exceptions=True so one failing helper doesn't kill the rest.
    Logs errors on state change only (first failure / recovery).
    """
    while True:
        try:
            results = await asyncio.gather(
                _collect_dispatch_data(),
                asyncio.to_thread(dao_beads.get_bead_counts),
                asyncio.to_thread(_count_active_sessions),
                asyncio.to_thread(_count_terminals),
                asyncio.to_thread(_count_today_done),
                asyncio.to_thread(_get_dispatcher_state),
                asyncio.to_thread(dao_beads.get_beads_by_label, "pinned"),
                asyncio.to_thread(_count_worktrees),
                asyncio.to_thread(_count_streams),
                asyncio.to_thread(_collect_harness_usage),
                asyncio.to_thread(_collect_plugin_badges),
                return_exceptions=True,
            )

            # Log per-helper errors on state change (avoid spam)
            for name, result in zip(_WATCHER_HELPERS, results):
                if isinstance(result, BaseException):
                    err_str = f"{type(result).__name__}: {result}"
                    if _watcher_errors.get(name) != err_str:
                        logger.error("[dispatch_watcher] %s failed: %s", name, err_str)
                        _watcher_errors[name] = err_str
                else:
                    if name in _watcher_errors:
                        logger.info("[dispatch_watcher] %s recovered", name)
                        del _watcher_errors[name]

            # Unpack with safe defaults for failed helpers
            dispatch_data = results[0] if not isinstance(results[0], BaseException) else {"active": [], "waiting": [], "blocked": [], "paused": {}}
            counts = results[1] if not isinstance(results[1], BaseException) else {}
            active_sessions = results[2] if not isinstance(results[2], BaseException) else 0
            terminal_count = results[3] if not isinstance(results[3], BaseException) else 0
            today_done = results[4] if not isinstance(results[4], BaseException) else 0
            dispatcher_state = results[5] if not isinstance(results[5], BaseException) else {"paused": False, "reason": None}
            pinned_beads = results[6] if not isinstance(results[6], BaseException) else []
            worktree_counts = results[7] if not isinstance(results[7], BaseException) else {"with_commits": 0, "with_changes": 0}
            stream_count = results[8] if not isinstance(results[8], BaseException) else 0
            harness_usage = results[9] if not isinstance(results[9], BaseException) else {"harnesses": []}
            plugin_badges = results[10] if not isinstance(results[10], BaseException) else {}

            nav_data = {
                "open_beads": counts.get("open_count", 0),
                "running_agents": len(dispatch_data["active"]),
                "approved_waiting": dispatch_data.get(
                    "waiting_total", len(dispatch_data["waiting"])),
                "approved_blocked": len(dispatch_data["blocked"]),
                "active_sessions": active_sessions,
                "terminal_count": terminal_count,
                "today_done": today_done,
                "pinned": pinned_beads,
                "worktrees_with_commits": worktree_counts.get("with_commits", 0),
                "worktrees_with_changes": worktree_counts.get("with_changes", 0),
                "stream_count": stream_count,
                "plugins": plugin_badges,
                "harness_usage": harness_usage,
            }
            await event_bus.broadcast("dispatch", dispatch_data)
            await event_bus.broadcast("nav", nav_data)
            await event_bus.broadcast("dispatcher_state", dispatcher_state)

            try:
                await _reconcile_dispatch_sessions()
            except Exception:
                logger.exception("dispatch-session reconcile pass failed")

            # Orphan sweep on a ~5-minute cadence (operator directive):
            # RUNNING agentic rows whose container is gone finalize as
            # orphaned-no-exit instead of wedging forever. The watcher
            # ticks every few seconds; this gates itself by wall clock.
            global _last_orphan_sweep
            if time.monotonic() - _last_orphan_sweep >= 300.0:
                _last_orphan_sweep = time.monotonic()
                try:
                    from agents.dispatch_db import fail_orphaned_running_agentic
                    orphaned = await asyncio.to_thread(
                        fail_orphaned_running_agentic)
                    if orphaned:
                        logger.warning(
                            "orphan sweep finalized %d agentic run(s): %s",
                            len(orphaned), ", ".join(orphaned))
                except Exception:
                    logger.exception("agentic orphan sweep failed")

            # Per-row worktree state — drives the ⌥ workspace-changes
            # indicator on session cards / page-mode header. Emit only
            # on signature change so connected clients aren't flooded
            # with identical payloads (worktree_monitor caches refresh
            # every 30s, this watcher loops every 5s).
            try:
                wt_rows = [
                    _worktree_state_json(row)
                    for row in worktree_monitor.get_all()
                ]
            except Exception:
                logger.exception("[dispatch_watcher] worktree state serialization failed")
                wt_rows = []
            global _worktrees_last_signature
            wt_signature = json.dumps(
                [
                    [
                        r.get("session_name"), r.get("repo_name"),
                        r.get("commits_ahead"), r.get("is_dirty"),
                        len(r.get("dirty_files") or []),
                    ]
                    for r in wt_rows
                ],
                sort_keys=True,
            )
            if wt_signature != _worktrees_last_signature:
                _worktrees_last_signature = wt_signature
                await event_bus.broadcast("worktrees", wt_rows)
        except Exception:
            logger.exception("[dispatch_watcher] unexpected top-level error")
        await asyncio.sleep(_DISPATCH_WATCHER_INTERVAL)


# ── Graph Write API (single-writer proxy) ─────────────────────
# Containers mount graph.db read-only and POST writes here.
# The server shells out to the graph CLI on the host, serialising all writes.

_GRAPH_SOURCE_ID_RE = re.compile(r'^[0-9a-f-]+$', re.IGNORECASE)
_GRAPH_TAGS_RE = re.compile(r'^[a-zA-Z0-9_,:-]+$')
_GRAPH_MAX_CONTENT = 100_000  # 100KB


def _safe_unlink(path: str) -> None:
    """Remove a temp file, ignoring errors."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _graph_validate_content(body: dict, field: str = "content") -> str | None:
    """Validate and return content field, or return error string."""
    content = body.get(field, "")
    if not content:
        return f"{field} required"
    if len(content) > _GRAPH_MAX_CONTENT:
        return f"{field} exceeds 100KB limit ({len(content)} bytes)"
    return None


def _graph_validate_source_id(value: str) -> str | None:
    """Return error string if source_id is malformed."""
    if not value or not _GRAPH_SOURCE_ID_RE.match(value):
        return f"malformed source_id: {value!r}"
    return None


_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024


def _token_org_or_none(request) -> str | None:
    """The org from a valid bearer session token, or ``None``.

    Invariant 1, derive-when-present (auto-h4kzx): a remote caller that
    presents a valid session-token bearer carrying an org is scoped to THAT
    org — the ``X-Graph-Org`` header and ``?org=`` query can no longer
    override it, closing the caller-picks-its-own-org spoofing surface. A
    caller with no bearer, an invalid/revoked token, or a genuine local
    (org-less) token returns ``None``, so the existing header/env cascade
    still applies unchanged.

    This is the additive half of the flip: it never refuses. Every container
    caller sends the bearer as of auto-w1ktf; the no-bearer REFUSE is a later
    hardening, safe only once all live sessions run that client.
    """
    identity, err = authenticate_session_request(request)
    if err is not None or identity is None:
        return None
    _session, org = identity
    return org  # slug for a container, None for a genuine local caller


def _graph_write_identity(request) -> tuple[str | None, str | None]:
    """Trusted `(persona_id, session_id)` for graph content writes.

    ``session_id`` is the session's **UUID** (the JSONL transcript stem), the
    canonical session identity used across the graph and matching what ingest
    stamps — resolved from the authenticated tmux session name via the session
    registry, never a client-supplied value. Falls back to None when the
    session can't be resolved (or the write is a browser-cookie write with no
    session). Persona resolution reads the local org-persona record at
    authentication time, so no operator identity is embedded in source or schema.
    """
    principal = api_auth.principal_from_request(request)
    tmux_name = principal.subject if principal.kind in (
        api_auth.ApiPrincipalKind.LOCAL_SESSION,
        api_auth.ApiPrincipalKind.ORG_SESSION,
    ) else None
    session_id = _resolve_session_uuid(tmux_name) if tmux_name else None
    return principal.persona_id, session_id


class _FallbackUpload:
    """Minimal async upload shim for stdlib multipart parsing."""

    def __init__(self, filename: str | None, content_type: str | None, content: bytes):
        self.filename = filename
        self.content_type = content_type
        self._content = content

    async def read(self) -> bytes:
        return self._content


class _FallbackFormData:
    """Tiny subset of Starlette's ``FormData`` used by our handlers."""

    def __init__(self, items: list[tuple[str, object]]):
        self._items = items

    def get(self, key: str, default=None):
        for name, value in self._items:
            if name == key:
                return value
        return default

    def __getitem__(self, key: str):
        sentinel = object()
        value = self.get(key, sentinel)
        if value is sentinel:
            raise KeyError(key)
        return value

    def multi_items(self):
        return list(self._items)

    def getlist(self, key: str):
        return [value for name, value in self._items if name == key]


async def _parse_form_data(request):
    """Return form data, falling back to stdlib multipart parsing.

    ``request.form()`` requires the optional ``python-multipart`` package.
    Some host/test environments omit it, so parse the small subset we need
    ourselves when Starlette raises that specific assertion.
    """
    try:
        return await request.form()
    except AssertionError as exc:
        if "python-multipart" not in str(exc):
            raise

    from email.parser import BytesParser
    from email.policy import default as email_policy

    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        raise ValueError("invalid multipart form")

    body = await request.body()
    envelope = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
        + body
    )
    message = BytesParser(policy=email_policy).parsebytes(envelope)
    if not message.is_multipart():
        raise ValueError("invalid multipart form")

    items: list[tuple[str, object]] = []
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            items.append((
                str(name),
                _FallbackUpload(filename, part.get_content_type(), payload),
            ))
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            value = payload.decode(charset)
        except UnicodeDecodeError:
            value = payload.decode("utf-8", errors="replace")
        items.append((str(name), value))

    return _FallbackFormData(items)


def _cross_org_error_response(err):
    """Build the 409 JSON shape emitted for CrossOrgWriteError."""
    return JSONResponse(
        {
            "error": str(err),
            "ok": False,
            "target_id": getattr(err, "target_id", None),
            "origin_org": getattr(err, "origin_org", None),
        },
        status_code=409,
    )


async def _materialize_uploads(form, key: str = "attachments"):
    """Stream multipart uploads to tempfiles; return list of paths.

    Caller must pass each path to ``_safe_unlink`` after use. Returns
    ``(paths, error_response)`` — ``error_response`` is set and paths are
    cleaned up if validation fails (e.g. size limit).
    """
    import tempfile
    tmp_paths: list[str] = []
    for k, upload in form.multi_items():
        if k != key:
            continue
        contents = await upload.read()
        if not contents:
            continue
        if len(contents) > _MAX_ATTACHMENT_BYTES:
            for p in tmp_paths:
                _safe_unlink(p)
            return None, JSONResponse(
                {"error": "attachment too large (max 50MB)"}, status_code=400,
            )
        suffix = ""
        if upload.filename and "." in upload.filename:
            suffix = "." + upload.filename.rsplit(".", 1)[1]
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(contents)
            tmp_paths.append(tmp.name)
    return tmp_paths, None


async def api_graph_note(request):
    """Create a note via direct ops call. JSON or multipart (attachments).

    ``session_hint`` (a tmux session name) lets ``graph_ops.create_note``
    resolve the ``conceived_at`` provenance edge itself — the server always
    has a live DB, unlike the container CLI, which can't do this locally
    over the HttpClient path (see ``ops._resolve_note_provenance``).
    """
    content_type = request.headers.get("content-type", "")
    org = api_auth.organization_scope_from_request(request)
    persona_id, session_id = _graph_write_identity(request)

    if "multipart/form-data" in content_type:
        try:
            form = await _parse_form_data(request)
        except Exception:
            return JSONResponse({"error": "invalid multipart form"}, status_code=400)
        content = str(form.get("content", ""))
        if not content:
            return JSONResponse({"error": "content required"}, status_code=400)
        if len(content) > _GRAPH_MAX_CONTENT:
            return JSONResponse({"error": "content exceeds 100KB limit"}, status_code=400)
        tags_raw = form.get("tags")
        if tags_raw and not _GRAPH_TAGS_RE.match(str(tags_raw)):
            return JSONResponse({"error": f"invalid tags: {tags_raw!r}"}, status_code=400)
        author = None
        session_hint = session_id
        auto_provenance_source_id = str(form["auto_provenance_source_id"]) if form.get("auto_provenance_source_id") else None
        auto_provenance_turn = int(form["auto_provenance_turn"]) if form.get("auto_provenance_turn") else None
        short_description = str(form["short_description"]) if form.get("short_description") else None
        keywords = str(form["keywords"]) if form.get("keywords") else None
        tmp_paths, err = await _materialize_uploads(form)
        if err is not None:
            return err
        html_paths, err = await _materialize_uploads(form, key="html")
        if err is not None:
            for p in tmp_paths:
                _safe_unlink(p)
            return err
        html_path = html_paths[0] if html_paths else None
        for extra in html_paths[1:]:
            _safe_unlink(extra)
        force = str(form.get("force") or "").lower() in ("1", "true")
    else:
        body = await request.json()
        e = _graph_validate_content(body)
        if e:
            return JSONResponse({"error": e}, status_code=400)
        content = body["content"]
        tags_raw = body.get("tags")
        if tags_raw and not _GRAPH_TAGS_RE.match(tags_raw):
            return JSONResponse({"error": f"invalid tags: {tags_raw!r}"}, status_code=400)
        author = None
        session_hint = session_id
        auto_provenance_source_id = body.get("auto_provenance_source_id")
        auto_provenance_turn = body.get("auto_provenance_turn")
        short_description = body.get("short_description")
        keywords = body.get("keywords")
        tmp_paths = []
        html_path = None
        force = bool(body.get("force"))

    tags = str(tags_raw).split(",") if tags_raw else []

    try:
        result = await asyncio.to_thread(
            graph_ops.create_note,
            content,
            tags=tags,
            author=author,
            session_hint=session_hint,
            auto_provenance_source_id=auto_provenance_source_id,
            auto_provenance_turn=auto_provenance_turn,
            attachments=tmp_paths or None,
            html_path=html_path,
            short_description=short_description,
            keywords=keywords,
            persona_id=persona_id,
            session_id=session_id,
            org=org,
            force=force,
        )
    except graph_ops.DuplicateNoteError as e:
        # 400 (not 409) deliberately: the client's _translate_http_error
        # turns a 400 into ValueError(message), which the CLI already
        # renders — the guidance text reaches the caller verbatim.
        return JSONResponse({
            "error": str(e),
            "duplicate_of": e.similar_id,
            "similarity": round(e.similarity, 3),
        }, status_code=400)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except graph_ops.CrossOrgWriteError as e:
        return _cross_org_error_response(e)
    finally:
        for p in tmp_paths:
            _safe_unlink(p)
        if html_path:
            _safe_unlink(html_path)

    _checkpoint_graph()
    return JSONResponse({
        "ok": True,
        "source_id": result["source_id"],
        "org": result["org"],
        "lines": result["lines"],
        "chars": result["chars"],
        "attachments": result["attachments"],
        "rich_content": result.get("rich_content", False),
        "short_description": result.get("short_description"),
        "keywords": result.get("keywords"),
    })


async def api_graph_note_versions_list(request):
    """GET /api/graph/note/<id>/versions — list every saved version of a note.

    Returns ``{"source_id": str, "versions": [{version, content, created_at}, ...]}``
    ordered ascending. Cross-org aware via the same resolution path as
    ``/api/source/<id>``. 404 if the source doesn't exist or isn't a note.
    """
    source_id = request.path_params["id"]
    e = _graph_validate_source_id(source_id)
    if e:
        return JSONResponse({"error": e}, status_code=400)
    org = api_auth.organization_scope_from_request(request)

    src = await asyncio.to_thread(graph_ops.get_source, source_id, org=org)
    if src is None:
        return JSONResponse({"error": "source not found"}, status_code=404)
    if src.get("type") != "note":
        return JSONResponse(
            {"error": f"not a note (type={src.get('type')!r})"},
            status_code=400,
        )

    home = src.get("org") or org
    versions = await asyncio.to_thread(
        _list_note_versions_in_org, src["id"], home,
    )
    return JSONResponse({
        "source_id": src["id"],
        "org": home or "",
        "versions": versions,
    })


async def api_graph_note_version_read(request):
    """GET /api/graph/note/<id>/version/<n> — read a specific version's body.

    Returns ``{"source_id", "org", "version", "content", "created_at"}``.
    404 if the source or version does not exist.
    """
    source_id = request.path_params["id"]
    e = _graph_validate_source_id(source_id)
    if e:
        return JSONResponse({"error": e}, status_code=400)
    try:
        version = int(request.path_params["n"])
    except (TypeError, ValueError):
        return JSONResponse({"error": "version must be a positive integer"}, status_code=400)
    if version < 1:
        return JSONResponse({"error": "version must be a positive integer"}, status_code=400)
    org = api_auth.organization_scope_from_request(request)

    src = await asyncio.to_thread(graph_ops.get_source, source_id, org=org)
    if src is None:
        return JSONResponse({"error": "source not found"}, status_code=404)
    if src.get("type") != "note":
        return JSONResponse(
            {"error": f"not a note (type={src.get('type')!r})"},
            status_code=400,
        )

    home = src.get("org") or org
    row = await asyncio.to_thread(
        _read_note_version_in_org, src["id"], version, home,
    )
    if row is None:
        return JSONResponse({"error": f"version {version} not found"}, status_code=404)
    return JSONResponse({
        "source_id": src["id"],
        "org": home or "",
        "version": row["version"],
        "content": row["content"],
        "created_at": row.get("created_at"),
    })


def _list_note_versions_in_org(source_id: str, org: str | None) -> list[dict]:
    """Helper for the version-list endpoint: open the source's home-org DB
    and return ``list_note_versions`` rows."""
    from tools.graph.db import GraphDB
    from tools.graph.cross_org import open_peer_db

    if org:
        db = open_peer_db(org)
        if db is None:
            return []
    else:
        db = GraphDB(graph_ops._db_path(None))
    return db.list_note_versions(source_id)


def _read_note_version_in_org(source_id: str, version: int, org: str | None) -> dict | None:
    """Helper for the version-read endpoint: open the source's home-org DB
    and return the matching version row (or None)."""
    from tools.graph.db import GraphDB
    from tools.graph.cross_org import open_peer_db

    if org:
        db = open_peer_db(org)
        if db is None:
            return None
    else:
        db = GraphDB(graph_ops._db_path(None))
    return db.get_note_version(source_id, version)


async def api_graph_note_update(request):
    """Update a note via direct ops call. JSON or multipart (attachments).

    Body changes (``content``) are optional. When omitted, the call is a
    metadata-only update — touches only ``title`` / ``short_description``
    / ``keywords`` columns, no body write, no version bump. At least one
    of ``content`` / ``title`` / ``short_description`` / ``keywords``
    must be provided; otherwise 400.
    """
    content_type = request.headers.get("content-type", "")
    org = api_auth.organization_scope_from_request(request)
    persona_id, session_id = _graph_write_identity(request)

    if "multipart/form-data" in content_type:
        try:
            form = await _parse_form_data(request)
        except Exception:
            return JSONResponse({"error": "invalid multipart form"}, status_code=400)
        source_id = str(form.get("source_id", ""))
        e = _graph_validate_source_id(source_id)
        if e:
            return JSONResponse({"error": e}, status_code=400)
        content_raw = form.get("content")
        if content_raw is None or content_raw == "":
            content = None
        else:
            content = str(content_raw)
            if len(content) > _GRAPH_MAX_CONTENT:
                return JSONResponse({"error": "content exceeds 100KB limit"}, status_code=400)
        title = str(form["title"]) if form.get("title") is not None else None
        integrate_raw = form.get("integrate_ids")
        integrate_ids: list[str] = []
        if integrate_raw:
            try:
                integrate_ids = [str(x) for x in json.loads(str(integrate_raw))]
            except (json.JSONDecodeError, TypeError):
                integrate_ids = []
        short_description = str(form["short_description"]) if form.get("short_description") is not None else None
        keywords = str(form["keywords"]) if form.get("keywords") is not None else None
        tmp_paths, err = await _materialize_uploads(form)
        if err is not None:
            return err
        html_paths, err = await _materialize_uploads(form, key="html")
        if err is not None:
            for p in tmp_paths:
                _safe_unlink(p)
            return err
        html_path = html_paths[0] if html_paths else None
        for extra in html_paths[1:]:
            _safe_unlink(extra)
    else:
        body = await request.json()
        source_id = body.get("source_id", "")
        e = _graph_validate_source_id(source_id)
        if e:
            return JSONResponse({"error": e}, status_code=400)
        # Content is optional — when present, validate length. When absent,
        # the request must carry at least one metadata field (the early
        # gate in graph_ops.update_note enforces the must-have-something
        # rule and surfaces a 400).
        if "content" in body and body["content"] is not None and body["content"] != "":
            content = body["content"]
            if len(content) > _GRAPH_MAX_CONTENT:
                return JSONResponse(
                    {"error": f"content exceeds 100KB limit ({len(content)} bytes)"},
                    status_code=400,
                )
        else:
            content = None
        title = body.get("title")
        integrate_ids = [str(x) for x in body.get("integrate_ids") or []]
        short_description = body.get("short_description")
        keywords = body.get("keywords")
        tmp_paths = []
        html_path = None

    try:
        result = await asyncio.to_thread(
            graph_ops.update_note,
            source_id,
            content,
            title=title,
            integrate_comments=integrate_ids,
            attachments=tmp_paths or None,
            html_path=html_path,
            short_description=short_description,
            keywords=keywords,
            persona_id=persona_id,
            session_id=session_id,
            org=org,
        )
    except graph_ops.CrossOrgWriteError as e:
        return _cross_org_error_response(e)
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    finally:
        for p in tmp_paths:
            _safe_unlink(p)
        if html_path:
            _safe_unlink(html_path)

    _checkpoint_graph()
    return JSONResponse({
        "ok": True,
        "source_id": result["source_id"],
        "new_version": result["new_version"],
        "org": result["org"],
        "lines": result["lines"],
        "chars": result["chars"],
        "integrated": result["integrated"],
        "not_found_comments": result["not_found_comments"],
        "attachments": result["attachments"],
        "rich_content": result.get("rich_content", False),
        "title": result.get("title"),
        "short_description": result.get("short_description"),
        "keywords": result.get("keywords"),
    })


async def api_graph_note_withdraw(request):
    """POST /api/graph/note/withdraw — hide a source from search/listings.

    Reversible ``deprecated`` flag flip (see ``graph note withdraw``); the
    record and its links survive and still resolve via ``graph read``.
    """
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"ok": True, "output": "  ✓ Mock: withdraw operation skipped"})
    org = api_auth.organization_scope_from_request(request)
    body = await request.json()
    source_id = body.get("source_id", "")
    e = _graph_validate_source_id(source_id)
    if e:
        return JSONResponse({"error": e}, status_code=400)

    try:
        result = await asyncio.to_thread(graph_ops.withdraw_note, source_id, org=org)
    except graph_ops.CrossOrgWriteError as e:
        return _cross_org_error_response(e)
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)

    _checkpoint_graph()
    return JSONResponse({
        "ok": True,
        "source_id": result["source_id"],
        "title": result["title"],
        "already_withdrawn": result["already_withdrawn"],
    })


async def api_graph_comment_get(request):
    """GET /api/graph/comment/<id> — fetch a single comment (cross-org)."""
    comment_id = request.path_params["id"]
    if not _GRAPH_SOURCE_ID_RE.match(comment_id):
        return JSONResponse(
            {"error": f"malformed comment_id: {comment_id!r}"}, status_code=400,
        )
    org = api_auth.organization_scope_from_request(request)
    comment = graph_ops.get_comment(comment_id, org=org)
    if comment is None:
        return JSONResponse(
            {"error": f"comment not found: {comment_id}"}, status_code=404,
        )
    return JSONResponse(comment)


async def api_graph_turn_content(request):
    """GET /api/graph/turn/<source_id>?turn=N — single-turn content.

    Used by ``graph link`` to echo the linked turn's snippet (via
    HttpClient.get_turn_content). Keeps the CLI off the local DB.
    """
    source_id = request.path_params["source_id"]
    if not _GRAPH_SOURCE_ID_RE.match(source_id):
        return JSONResponse(
            {"error": f"malformed source_id: {source_id!r}"}, status_code=400,
        )
    turn_raw = request.query_params.get("turn")
    try:
        turn = int(turn_raw)
    except (TypeError, ValueError):
        return JSONResponse(
            {"error": f"invalid turn: {turn_raw!r}"}, status_code=400,
        )
    org = api_auth.organization_scope_from_request(request)
    content = graph_ops.get_turn_content(source_id, turn, org=org)
    if content is None:
        return JSONResponse({"error": "turn not found"}, status_code=404)
    return JSONResponse({"source_id": source_id, "turn": turn, "content": content})


async def api_graph_comment(request):
    """Add a comment to a note via direct ops call."""
    body = await request.json()

    source_id = body.get("source_id", "")
    e = _graph_validate_source_id(source_id)
    if e:
        return JSONResponse({"error": e}, status_code=400)
    e = _graph_validate_content(body)
    if e:
        return JSONResponse({"error": e}, status_code=400)

    org = api_auth.organization_scope_from_request(request)
    persona_id, session_id = _graph_write_identity(request)
    anchor = body.get("anchor")

    # Source lookup goes through the full-surface cross-org resolver so
    # peer-raw notes still produce the correct CrossOrgWriteError
    # response instead of a bare 404 (otherwise a caller with an explicit
    # X-Graph-Org header would get the wrong signal).
    resolved = graph_ops.get_source(source_id, org=org)
    if resolved is None:
        # Fall back to caller-org only — handles the case where the full
        # id was provided but the caller has explicit org + peer-raw
        # source isn't visible. Try ops.add_comment anyway; it will
        # raise CrossOrgWriteError or LookupError as appropriate.
        try:
            comment = await asyncio.to_thread(
                graph_ops.add_comment,
                source_id, body["content"],
                actor="user",
                persona_id=persona_id,
                session_id=session_id,
                org=org,
                anchor=anchor,
            )
        except graph_ops.CrossOrgWriteError as ex:
            return _cross_org_error_response(ex)
        except ValueError as ex:
            return JSONResponse({"error": str(ex)}, status_code=400)
        _checkpoint_graph()
        return JSONResponse({
            "ok": True,
            "comment_id": comment["id"],
            "source_id": source_id,
            "comment": comment,
        })

    if resolved.get("type") != "note":
        return JSONResponse(
            {"error": f"comments only supported on notes (got {resolved.get('type')!r})"},
            status_code=400,
        )

    try:
        comment = await asyncio.to_thread(
            graph_ops.add_comment,
            resolved["id"], body["content"],
            actor="user",
            persona_id=persona_id,
            session_id=session_id,
            org=org,
            anchor=anchor,
        )
    except graph_ops.CrossOrgWriteError as ex:
        return _cross_org_error_response(ex)
    except ValueError as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)

    _checkpoint_graph()
    return JSONResponse({
        "ok": True,
        "comment_id": comment["id"],
        "source_id": resolved["id"],
        "comment": comment,
    })


async def api_graph_comment_integrate(request):
    """Mark a comment as integrated via direct ops call."""
    body = await request.json()

    comment_id = body.get("comment_id", "")
    e = _graph_validate_source_id(comment_id)
    if e:
        return JSONResponse({"error": f"malformed comment_id: {comment_id!r}"}, status_code=400)

    org = api_auth.organization_scope_from_request(request)
    comment = graph_ops.get_comment(comment_id, org=org)
    if comment is None:
        return JSONResponse(
            {"error": f"comment not found: {comment_id}"}, status_code=404,
        )

    try:
        changed = await asyncio.to_thread(
            graph_ops.integrate_comment, comment["id"], org=org,
        )
    except graph_ops.CrossOrgWriteError as ex:
        return _cross_org_error_response(ex)

    return JSONResponse({
        "ok": True,
        "comment_id": comment["id"],
        "integrated": True,
        "changed": changed,
    })


async def api_graph_bead(request):
    """Create a bead (via ``bd`` subprocess for the tracker side) + link
    provenance edge via direct ops call.

    ``bd`` is intentionally retained — the bead tracker is a separate
    system (Dolt-backed) and owns bead lifecycle; only the graph edge
    goes through ops.
    """
    body = await request.json()

    title = body.get("title", "")
    if not title:
        return JSONResponse({"error": "title required"}, status_code=400)
    if len(title) > 200:
        return JSONResponse({"error": "title too long (max 200 chars)"}, status_code=400)

    priority = body.get("priority", 1)
    if not isinstance(priority, int) or priority < 0 or priority > 3:
        return JSONResponse({"error": "priority must be integer 0-3"}, status_code=400)

    desc = body.get("description", "") or ""
    if desc and len(desc) > _GRAPH_MAX_CONTENT:
        return JSONResponse({"error": "description exceeds 100KB"}, status_code=400)

    source_id = body.get("source")
    if source_id:
        e = _graph_validate_source_id(source_id)
        if e:
            return JSONResponse({"error": e}, status_code=400)

    # Create bead via bd (tracker system), in the CALLER ORG's tracker
    # (per-org databases, autonomy@74585ba) with the org label applied
    # by convention (graph note 74e2b864).
    caller_org = api_auth.organization_scope_from_request(request)
    labels = "readiness:idea" + (f",org:{caller_org}" if caller_org else "")
    bd_cmd = ["bd", "create", title, "-p", str(priority), "-l", labels]
    # Attribute to the authenticated caller from the request's bearer/crosstalk
    # token, never to this server process's own ambient BD_ACTOR env var — the
    # dashboard is a single long-lived process shared by every session, so its
    # environment reflects whichever shell last started it, not who's asking.
    principal = api_auth.principal_from_request(request)
    if principal.subject:
        bd_cmd += ["--actor", f"session:{principal.subject}"]
    if desc:
        bd_cmd += ["-d", desc]
    if body.get("type"):
        bd_cmd += ["-t", body["type"]]

    # A WRITE resolves the org's own tracker, provisioning it on first sight.
    # Filing into the shared tracker because this org has never been seen is
    # the silent mis-attribution the per-org split exists to prevent, so a
    # node that cannot provision returns 503 rather than writing somewhere else.
    from tools.beads_provision import BeadsProvisionError, beads_dir_for_write
    try:
        bd_dir = await asyncio.to_thread(beads_dir_for_write, caller_org)
    except BeadsProvisionError as exc:
        return JSONResponse(
            {"error": f"no bead tracker for org {caller_org!r}: {exc}"},
            status_code=503,
        )
    stdout, stderr, rc = await run_cli(bd_cmd, timeout=60, beads_dir=bd_dir)
    if rc != 0:
        return JSONResponse({"error": stderr, "rc": rc}, status_code=500)

    import re as _re
    match = _re.search(r"Created issue: (\S+)", stdout)
    if not match:
        return JSONResponse({"ok": True, "output": stdout})
    bead_id = match.group(1)

    # Create the provenance edge via ops.
    edge = None
    if source_id and body.get("turns"):
        org = api_auth.organization_scope_from_request(request)
        turns_arg = str(body["turns"])
        parts = turns_arg.split("-")
        try:
            if len(parts) == 2:
                turns = (int(parts[0]), int(parts[1]))
            elif len(parts) == 1:
                turns = (int(parts[0]), int(parts[0]))
            else:
                turns = None
        except ValueError:
            turns = None

        if turns is not None:
            edge = await asyncio.to_thread(
                graph_ops.create_edge,
                bead_id, source_id,
                from_type="bead",
                to_type="source",
                relation="conceived_at",
                turns=turns,
                note=body.get("note"),
                org=org,
            )
            _checkpoint_graph()

    return JSONResponse({
        "ok": True,
        "output": stdout,
        "bead_id": bead_id,
        "edge_id": (edge or {}).get("id"),
    })


async def api_graph_link(request):
    """Create a provenance edge via direct ops call."""
    body = await request.json()

    bead_id = body.get("bead_id", "")
    if not bead_id:
        return JSONResponse({"error": "bead_id required"}, status_code=400)

    source_id = body.get("source_id", "")
    e = _graph_validate_source_id(source_id)
    if e:
        return JSONResponse({"error": e}, status_code=400)

    relationship = body.get("relationship", "informed_by")
    org = api_auth.organization_scope_from_request(request)

    turns: tuple[int, int] | None = None
    turns_arg = body.get("turn") or body.get("turns")
    if turns_arg:
        parts = str(turns_arg).split("-")
        try:
            if len(parts) == 2:
                turns = (int(parts[0]), int(parts[1]))
            elif len(parts) == 1:
                turns = (int(parts[0]), int(parts[0]))
        except ValueError:
            return JSONResponse(
                {"error": f"invalid turns: {turns_arg!r}"}, status_code=400,
            )

    # Bead IDs stay as-is. For source-to-source, resolve the from side too.
    from_type = "bead" if bead_id.startswith("auto-") else "source"

    edge = await asyncio.to_thread(
        graph_ops.create_edge,
        bead_id, source_id,
        from_type=from_type,
        to_type="source",
        relation=relationship,
        turns=turns,
        note=body.get("note"),
        org=org,
    )
    _checkpoint_graph()
    return JSONResponse({
        "ok": True,
        "edge_id": edge["id"],
        "source_id": edge["target_id"],
        "bead_id": edge["source_id"],
        "relation": edge["relation"],
    })


_ingest_lock = asyncio.Lock()
_GRAPH_SESSIONS_INGEST_TIMEOUT = int(
    os.environ.get("DASHBOARD_GRAPH_SESSIONS_INGEST_TIMEOUT", "600")
)
_GRAPH_SESSIONS_TOTAL_RE = re.compile(
    r"Total:\s+(\d+)\s+new,\s+(\d+)\s+updated,\s+(\d+)\s+refreshed,\s+(\d+)\s+skipped"
)


def _parse_graph_sessions_counts(output: str) -> dict[str, int]:
    match = _GRAPH_SESSIONS_TOTAL_RE.search(output)
    if not match:
        return {"ingested": 0, "updated": 0, "refreshed": 0, "skipped": 0}
    ingested, updated, refreshed, skipped = match.groups()
    return {
        "ingested": int(ingested),
        "updated": int(updated),
        "refreshed": int(refreshed),
        "skipped": int(skipped),
    }


async def _run_graph_sessions_ingest_cli(
    *,
    all_projects: bool,
    project: str | None,
    force: bool,
) -> tuple[str, str, int]:
    """Run CPU-heavy session ingest out-of-process.

    ``ingest_all_claude_code`` parses JSONL and updates FTS indexes. Running
    that in a dashboard worker thread can still hold the GIL and stall the
    event loop, so this endpoint intentionally uses the graph CLI in host mode.
    """
    cmd = ["graph", "--force-host", "sessions"]
    if all_projects:
        cmd.append("--all")
    elif project:
        cmd.extend(["--project", project])
    if force:
        cmd.append("--force")

    env = os.environ.copy()
    # Defense in depth: --force-host bypasses HttpClient, and removing
    # GRAPH_API prevents future CLI path changes from recursing into this API.
    env.pop("GRAPH_API", None)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=_GRAPH_SESSIONS_INGEST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return "", "graph sessions ingest timed out", -1
    return stdout.decode(), stderr.decode(), proc.returncode


async def _refresh_graph_session_source(source: dict) -> dict:
    """Best-effort refresh for one already-known session source."""
    if source.get("type") != "session":
        return source

    async with _ingest_lock:
        def _run_refresh() -> dict:
            from tools.graph.ingest import refresh_session_source
            return refresh_session_source(source)

        try:
            return await asyncio.to_thread(_run_refresh)
        except Exception:
            logger.exception(
                "[graph] session refresh failed for %s",
                source.get("id", "?")[:12],
            )
            return source


async def api_graph_sessions(request):
    """Ingest sessions. Two modes:

    ``{"session": <tmux_name>}`` — single-session mode (W4). Resolves
    jsonl_path via dashboard.db and ingests just that one file in-process
    (asyncio.to_thread) — a single file is cheap enough not to need the
    subprocess isolation the full sweep requires.

    Anything else (``--all``/``--project``/bare) — unchanged: runs the
    graph CLI in a subprocess so CPU-bound parsing/FTS work cannot stall
    the event loop.
    """
    if _ingest_lock.locked():
        return JSONResponse({"ok": True, "output": "ingest already in progress", "skipped": True})

    async with _ingest_lock:
        body = await request.json()
        session = str(body["session"]) if body.get("session") else None

        if session:
            def _run_single() -> dict:
                from tools.graph.ingest import _open_db_for_session, ingest_session_file
                row = dashboard_db.get_session(session)
                jsonl_path = row.get("jsonl_path") if row else None
                if not jsonl_path:
                    return {"error": f"no jsonl_path for session {session!r}"}
                path = Path(jsonl_path)
                db = _open_db_for_session(path)
                if db is None:
                    return {"error": "no resolvable graph_org for this session"}
                try:
                    return ingest_session_file(db, path, force=bool(body.get("force")))
                finally:
                    db.close()

            result = await asyncio.to_thread(_run_single)
            if result.get("error"):
                return JSONResponse(result, status_code=404)
            return JSONResponse({"ok": True, "session": session, "result": result})

        force = bool(body.get("force"))
        project = str(body["project"]) if body.get("project") else None
        all_flag = bool(body.get("all"))

        stdout, stderr, rc = await _run_graph_sessions_ingest_cli(
            all_projects=all_flag,
            project=project,
            force=force,
        )
        if rc != 0:
            return JSONResponse(
                {"error": stderr.strip() or stdout.strip() or "graph sessions failed", "rc": rc},
                status_code=500,
            )

        counts = _parse_graph_sessions_counts(stdout)
        summary = (
            f"Total: {counts['ingested']} new, {counts['updated']} updated, "
            f"{counts['refreshed']} refreshed, {counts['skipped']} skipped"
        )
        return JSONResponse({"ok": True, "output": stdout or summary, "counts": counts})


async def _run_graph_docs_ingest_cli(
    *,
    path: str,
    org: str | None,
    force: bool,
) -> tuple[str, str, int]:
    """Run ``graph docs-ingest`` out-of-process, host-side.

    Containers mount the per-org graph DBs read-only, so ``docs-ingest``
    cannot write directly. This runs the CLI in ``--force-host`` mode against
    the writable host DB, pinning the target org via ``GRAPH_ORG``. Parsing
    markdown and rebuilding FTS indexes is CPU-bound, so — like the session
    sweep — it runs in a subprocess rather than a worker thread to avoid
    stalling the event loop under the GIL.
    """
    cmd = ["graph", "--force-host", "docs-ingest", path]
    if force:
        cmd.append("--force")

    env = os.environ.copy()
    # Defense in depth: --force-host bypasses HttpClient, and removing
    # GRAPH_API prevents future CLI path changes from recursing into this API.
    env.pop("GRAPH_API", None)
    if org:
        env["GRAPH_ORG"] = org
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=_GRAPH_SESSIONS_INGEST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return "", "graph docs-ingest timed out", -1
    return stdout.decode(), stderr.decode(), proc.returncode


async def api_graph_docs(request):
    """Ingest documentation files host-side (container-safe).

    Containers mount the per-org graph DBs read-only, so ``graph docs-ingest``
    cannot write directly and crashes with ``attempt to write a readonly
    database``. The container CLI POSTs here instead; we run the ingest in a
    host subprocess (``--force-host``) against the writable DB. Mirrors
    :func:`api_graph_sessions`.

    Body: ``{"path": <file-or-dir>, "org": <slug?>, "force": <bool?>}``.
    The path is resolved on the host filesystem — it must be visible to the
    dashboard process (shared bind mount / worktree), not container-only.
    """
    body = await request.json()
    path = body.get("path")
    if not path:
        return JSONResponse({"error": "path required"}, status_code=400)
    org = body.get("org") or api_auth.organization_scope_from_request(request)
    force = bool(body.get("force"))

    async with _ingest_lock:
        stdout, stderr, rc = await _run_graph_docs_ingest_cli(
            path=str(path),
            org=org,
            force=force,
        )
    if rc != 0:
        return JSONResponse(
            {"error": stderr.strip() or stdout.strip() or "graph docs-ingest failed", "rc": rc},
            status_code=500,
        )
    return JSONResponse({"ok": True, "output": stdout})


async def api_graph_attach(request):
    """Attach a file to the graph via multipart form upload."""
    import tempfile
    try:
        form = await _parse_form_data(request)
    except Exception:
        return JSONResponse({"error": "invalid multipart form"}, status_code=400)
    upload = form.get("file")
    if not upload:
        return JSONResponse({"error": "file field required"}, status_code=400)

    contents = await upload.read()
    if not contents:
        return JSONResponse({"error": "empty file"}, status_code=400)
    if len(contents) > _MAX_ATTACHMENT_BYTES:
        return JSONResponse({"error": "file too large (max 50MB)"}, status_code=400)

    source_id = form.get("source_id")
    if source_id and not _GRAPH_SOURCE_ID_RE.match(str(source_id)):
        return JSONResponse(
            {"error": f"malformed source_id: {source_id!r}"}, status_code=400,
        )
    turn = form.get("turn")
    try:
        turn_int = int(turn) if turn is not None and str(turn).strip() else None
    except ValueError:
        return JSONResponse({"error": f"invalid turn: {turn!r}"}, status_code=400)

    suffix = ""
    if upload.filename and "." in upload.filename:
        suffix = "." + upload.filename.rsplit(".", 1)[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(contents)
        tmp_path = tmp.name

    org = api_auth.organization_scope_from_request(request)
    try:
        att = await asyncio.to_thread(
            graph_ops.attach_file,
            tmp_path,
            source_id=str(source_id) if source_id else None,
            turn_number=turn_int,
            original_filename=upload.filename or None,
            org=org,
        )
    except graph_ops.CrossOrgWriteError as ex:
        return _cross_org_error_response(ex)
    except ValueError as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)
    except FileNotFoundError as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)
    finally:
        _safe_unlink(tmp_path)

    _checkpoint_graph()
    return JSONResponse({
        "ok": True,
        "attachment_id": att["id"],
        "filename": att["filename"],
        "size_bytes": att["size_bytes"],
        "source_id": att["source_id"],
    })


# ── Attachment serving ────────────────────────────────────────

async def api_source_attachments(request):
    """List attachments linked to a source."""
    source_id = request.path_params["id"]
    if not _GRAPH_SOURCE_ID_RE.match(source_id):
        return JSONResponse({"error": f"malformed source_id: {source_id!r}"}, status_code=400)
    atts = graph_ops.list_attachments(source_id=source_id)
    return JSONResponse({"attachments": atts})


async def api_attachment_serve(request):
    """Serve an attachment file by ID with correct Content-Type."""
    attachment_id = request.path_params["attachment_id"]

    if os.environ.get("DASHBOARD_MOCK"):
        from tools.dashboard.dao import mock as mock_dao
        att = mock_dao.get_attachment(attachment_id)
        if not att:
            return JSONResponse({"error": "attachment not found"}, status_code=404)
        content = att.get("content")
        if content:
            if isinstance(content, str):
                content = content.encode()
            return Response(content, media_type=att.get("mime_type", "application/octet-stream"))
        mime = att.get("mime_type", "")
        if mime.startswith("image/"):
            import base64
            png = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
            )
            return Response(png, media_type="image/png")
        if mime == "text/html":
            return Response(b"<html><body>Mock HTML attachment</body></html>", media_type="text/html")
        return Response(b"mock content", media_type=mime or "application/octet-stream")

    att = graph_ops.get_attachment(attachment_id)
    if not att:
        return JSONResponse({"error": "attachment not found"}, status_code=404)
    file_path = Path(att["file_path"])
    if not file_path.is_absolute():
        file_path = _REPO_ROOT / file_path
    if not file_path.exists():
        return JSONResponse({"error": "file missing"}, status_code=404)
    return FileResponse(
        file_path,
        media_type=att.get("mime_type") or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


async def api_graph_search(request):
    """Container-side CLI calls this to get WAL-fresh, cross-org-ready results.

    Mirrors what ``graph search`` would return on the host: a JSON list of
    result rows from ``ops.search``. The dashboard's own search UI uses the
    older ``/api/search`` endpoint (which shells out to ``graph search``);
    this endpoint is the structured companion meant for ``HttpClient.search``.
    """
    q = request.query_params.get("q", "")
    if not q:
        return JSONResponse({"error": "missing q parameter"}, status_code=400)
    limit = int(request.query_params.get("limit", "25"))
    or_mode = bool(request.query_params.get("or"))
    tag = request.query_params.get("tag")
    states_param = request.query_params.get("states")
    states = [s for s in states_param.split(",") if s] if states_param else None
    include_raw = bool(request.query_params.get("include_raw"))
    only_org = request.query_params.get("only_org")
    peers_param = request.query_params.get("peers")
    peers = [p for p in peers_param.split(",") if p] if peers_param is not None else None
    org = api_auth.organization_scope_from_request(request)
    ssi_param = request.query_params.get("session_source_ids")
    session_source_ids = [s for s in ssi_param.split(",") if s] if ssi_param else None
    session_author_pattern = request.query_params.get("session_author_pattern")
    type_param = request.query_params.get("source_type")
    source_type = [t for t in type_param.split(",") if t] if type_param else None
    ranker = request.query_params.get("ranker", "legacy")
    if ranker not in ("legacy", "smart"):
        return JSONResponse({"error": "invalid ranker"}, status_code=400)
    results = graph_ops.search(
        q, org=org, peers=peers, only_org=only_org,
        limit=limit, or_mode=or_mode, tag=tag,
        states=states, include_raw=include_raw,
        session_source_ids=session_source_ids,
        session_author_pattern=session_author_pattern,
        source_type=source_type,
        ranker=ranker,
    )
    return JSONResponse(results)


def _graph_not_found_body(source_id: str, org: str | None) -> dict:
    """404 body for a graph-source miss, enriched with a cross-org hint.

    When the ID exists in some org DB the caller's scope can't see,
    ``exists_in_org`` names it (plus the resolved full ``source_id`` and
    ``source_type``) so the CLI can say "this session exists, but in org
    X" instead of a bare not-found. Existence-only — no content leaks.
    """
    body: dict = {"error": "not found"}
    try:
        hit = graph_ops.locate_source_org(source_id)
    except Exception:
        hit = None
    if hit and hit.get("org") and hit["org"] != (org or ""):
        body["error"] = (
            f"not found in org '{org}'" if org else "not found in caller scope"
        )
        body["exists_in_org"] = hit["org"]
        body["source_id"] = hit["id"]
        body["source_type"] = hit["type"]
    return body


async def api_graph_source_get(request):
    """Resolve a source by id across own + peer DBs (cross-org read).

    ``X-Graph-Org`` request header pins ``org``; ``?only_org=<slug>``
    restricts to a single DB; ``?peers=a,b`` overrides the default peer
    set. Returns 404 when no org can see the ID.
    """
    source_id = request.path_params["id"]
    if not _GRAPH_SOURCE_ID_RE.match(source_id):
        return JSONResponse({"error": f"malformed source_id: {source_id!r}"}, status_code=400)
    org = api_auth.organization_scope_from_request(request)
    peers_param = request.query_params.get("peers")
    peers = [p for p in peers_param.split(",") if p] if peers_param is not None else None
    src = await asyncio.to_thread(
        graph_ops.get_source, source_id, org=org, peers=peers,
    )
    if not src:
        body = await asyncio.to_thread(_graph_not_found_body, source_id, org)
        return JSONResponse(body, status_code=404)
    return JSONResponse(src)


async def api_graph_sources_list(request):
    """List sources across own + peer DBs (chronological merge).

    Per ``graph://bcce359d-a1d`` § Merge algorithms. Peer rows are
    clamped to ``published``/``canonical``. ``?only_org=<slug>`` pins to
    a single DB; ``X-Graph-Org`` header supplies ``org``.

    ``since``/``until``/``author``/``states``/``include_raw``/
    ``session_source_ids``/``session_author_pattern`` mirror what
    ``HttpClient.list_sources`` sends — previously dropped here, which
    silently made ``graph notes --since`` (and every other filter besides
    type/tags) a no-op over the container API path.
    """
    limit = int(request.query_params.get("limit", "50"))
    source_type = request.query_params.get("type")
    tags_param = request.query_params.get("tags")
    tags = [t for t in tags_param.split(",") if t] if tags_param else None
    only_org = request.query_params.get("only_org")
    peers_param = request.query_params.get("peers")
    peers = [p for p in peers_param.split(",") if p] if peers_param is not None else None
    org = api_auth.organization_scope_from_request(request)
    since = request.query_params.get("since")
    until = request.query_params.get("until")
    author = request.query_params.get("author")
    states_param = request.query_params.get("states")
    states = [s for s in states_param.split(",") if s] if states_param else None
    include_raw = bool(request.query_params.get("include_raw"))
    ssi_param = request.query_params.get("session_source_ids")
    session_source_ids = [s for s in ssi_param.split(",") if s] if ssi_param else None
    session_author_pattern = request.query_params.get("session_author_pattern")
    sources = graph_ops.list_sources(
        org=org, peers=peers, only_org=only_org,
        limit=limit, source_type=source_type, tags=tags,
        since=since, until=until, author=author,
        states=states, include_raw=include_raw,
        session_source_ids=session_source_ids,
        session_author_pattern=session_author_pattern,
    )
    return JSONResponse({"sources": sources})


async def api_graph_attachment_get(request):
    """Get attachment metadata by id. Companion to HttpClient.get_attachment."""
    attachment_id = request.path_params["attachment_id"]
    if not _GRAPH_SOURCE_ID_RE.match(attachment_id):
        return JSONResponse({"error": f"malformed attachment_id: {attachment_id!r}"}, status_code=400)
    if request.query_params.get("strict"):
        resolved = graph_ops.resolve_attachment_strict(
            attachment_id, org=api_auth.organization_scope_from_request(request),
        )
        if isinstance(resolved, list):
            return JSONResponse({"matches": resolved})
        return JSONResponse({"attachment": resolved})
    att = graph_ops.get_attachment(attachment_id)
    if not att:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(att)


async def api_graph_collab_topics(request):
    """List tag taxonomy entries. Companion to HttpClient.list_collab_topics."""
    org = api_auth.organization_scope_from_request(request)
    return JSONResponse({"topics": graph_ops.list_collab_topics(org=org)})


async def api_graph_attention(request):
    """GET /api/graph/attention — human input across sessions, chronologically.

    Companion to ``HttpClient.list_attention`` (container ``graph attention``).
    """
    params = request.query_params
    org = api_auth.organization_scope_from_request(request)
    since = params.get("since") or None
    search = params.get("search") or None
    last_raw = params.get("last")
    last = int(last_raw) if last_raw else None
    session = params.get("session") or None
    ctx_raw = params.get("context")
    context = int(ctx_raw) if ctx_raw else 0
    rows = graph_ops.list_attention(
        org=org, since=since, search=search, last=last,
        session=session, context=context,
    )
    return JSONResponse({"rows": rows})


async def api_graph_stats(request):
    """GET /api/graph/stats — DB table counts."""
    org = api_auth.organization_scope_from_request(request)
    return JSONResponse(graph_ops.stats(org=org))


async def api_graph_tree(request):
    """GET /api/graph/tree — knowledge hierarchy tree."""
    org = api_auth.organization_scope_from_request(request)
    params = request.query_params
    root = params.get("root") or None
    depth = int(params.get("depth", "3"))
    return JSONResponse({"nodes": graph_ops.get_tree(root, depth=depth, org=org)})


async def api_graph_entities(request):
    """GET /api/graph/entities — list or search entities."""
    org = api_auth.organization_scope_from_request(request)
    params = request.query_params
    query = params.get("query") or None
    etype = params.get("type") or None
    limit = int(params.get("limit", "20"))
    if query:
        entities = graph_ops.search_entities(query, limit=limit, org=org)
    else:
        entities = graph_ops.list_entities(entity_type=etype, limit=limit, org=org)
    # Annotate with mention counts so the CLI doesn't have to do N+1 round-trips.
    for e in entities:
        e["mentions"] = graph_ops.entity_mention_count(e["id"], org=org)
    return JSONResponse({"entities": entities})


async def api_graph_entity_thoughts(request):
    """GET /api/graph/entity/{id}/thoughts — thoughts mentioning an entity."""
    org = api_auth.organization_scope_from_request(request)
    entity_id = request.path_params["id"]
    limit = int(request.query_params.get("limit", "20"))
    return JSONResponse(
        {"thoughts": graph_ops.entity_thoughts(entity_id, limit=limit, org=org)},
    )


# ── Settings primitive (graph://0d3f750f-f9c) ──────────────


def _invalidate_setting_caches(
    set_id: str, *, key: str, org: str | None,
) -> None:
    """Apply targeted in-process invalidation for a committed Setting."""
    workspace_settings.invalidate_for_setting(set_id)
    from tools.dashboard import feature_flags
    if set_id == feature_flags.FEATURE_FLAGS_SET_ID:
        feature_flags.invalidate_cache(org=org)
    if set_id == _harness_usage_settings.HARNESS_USAGE_SET_ID:
        _harness_usage_settings.clear_published_payload_cache(key=key)


def _parse_settings_read_params(query_params) -> tuple[int | None, int | None, int | None, str | None]:
    """Pull target/min/stored revision + error str from query params."""
    def _to_int(name):
        v = query_params.get(name)
        if v is None:
            return None
        try:
            return int(v)
        except ValueError:
            return f"invalid {name}: {v!r}"
    for name in ("target_revision", "min_revision", "stored_revision"):
        v = _to_int(name)
        if isinstance(v, str):
            return None, None, None, v
    return (
        int(query_params["target_revision"]) if "target_revision" in query_params else None,
        int(query_params["min_revision"]) if "min_revision" in query_params else None,
        int(query_params["stored_revision"]) if "stored_revision" in query_params else None,
        None,
    )


def _settings_emit_hook(
    *,
    operation: str,
    snapshot: dict,
    org: str | None,
) -> None:
    """Hook registered with ``settings_ops.set_emit_hook`` at lifespan startup.

    Every ``settings_ops`` mutation fires this hook AFTER its transaction
    commits. The hook publishes a ``setting.changed`` event on the
    process-local EventBus. Subscribers re-resolve via ``read_set`` if
    they need payload data — a fast subscriber that resolves on receipt
    sees the just-committed row by construction (commit-then-emit
    invariant; see bead auto-5mz65 acceptance #2).

    Sync by design: ``broadcast_sync`` mirrors ``broadcast`` but uses
    ``put_nowait`` against the unbounded subscriber queues, so settings
    mutators never block on the event loop and CLI / test contexts that
    happen to register a hook against this dashboard process still get
    deterministic delivery.
    """
    set_id = snapshot.get("set_id") if isinstance(snapshot, dict) else None
    if attention_routes.is_private_central_set_id(set_id):
        key = snapshot.get("key") if isinstance(snapshot, dict) else None
        try:
            _invalidate_setting_caches(set_id, key=key, org=org)
        except Exception:
            logger.warning("private setting cache invalidation failed", exc_info=True)
        try:
            attention_routes.emit_setting_change(
                operation=operation,
                snapshot=snapshot,
                org=org,
            )
        except Exception:
            logger.warning("private attention diversion failed", exc_info=True)
        return

    payload = {
        "set_id": snapshot["set_id"],
        "schema_revision": snapshot["schema_revision"],
        "key": snapshot["key"],
        "org": org,
        "publication_state": snapshot["publication_state"],
        "deprecated": snapshot["deprecated"],
        "operation": operation,
    }
    _invalidate_setting_caches(
        payload["set_id"], key=payload["key"], org=org,
    )
    try:
        event_bus.broadcast_sync("setting.changed", payload, dedup=False)
    except Exception:
        logger.warning(
            "setting.changed broadcast failed", exc_info=True,
        )


# Register at module import. ASGITransport-based tests that bypass
# lifespan still get function-level emits this way; ``_on_startup``
# re-arms on every lifespan cycle.
graph_ops.set_emit_hook(_settings_emit_hook)


async def _emit_setting_changed(
    *,
    operation: str,
    setting_id: str | None = None,
    org: str | None = None,
    snapshot: dict | None = None,
) -> None:
    """Mock-mode emit shim.

    Real (non-mock) Settings writes go through ``settings_ops`` and emit
    via :func:`_settings_emit_hook` at the function level, so HTTP
    routes never need to call this. The mock-mode write path uses
    ``dao_mock`` (fixture state, not ``settings_ops``) and is the only
    remaining caller — kept so the dashboard's mock harness still
    surfaces ``setting.changed`` for browser-driven design work.
    """
    if snapshot is None:
        if setting_id is None:
            return
        try:
            got = graph_ops.get_setting(setting_id, org=org or graph_ops.CALLER_ORG)
        except Exception:
            logger.debug(
                "setting.changed: get_setting(%s) failed", setting_id,
                exc_info=True,
            )
            return
        if got is None:
            return
        snapshot = {
            "set_id": got.set_id,
            "schema_revision": got.stored_revision,
            "key": got.key,
            "publication_state": got.state,
            "deprecated": bool(got.deprecated),
        }
    payload = {
        "set_id": snapshot["set_id"],
        "schema_revision": snapshot["schema_revision"],
        "key": snapshot["key"],
        "org": org,
        "publication_state": snapshot["publication_state"],
        "deprecated": snapshot["deprecated"],
        "operation": operation,
    }
    _invalidate_setting_caches(
        payload["set_id"], key=payload["key"], org=org,
    )
    await event_bus.broadcast("setting.changed", payload, dedup=False)


def _client_peers_if_global(request):
    """Honour a caller-supplied ``?peers=`` ONLY for a global-authority caller.

    An org-bound caller may not name its own peer set (invariant 1): it gets its
    org's resolved peers, whose cross-org contribution is already clamped to the
    published/canonical surface. Only the operator (global authority) may select
    peers explicitly. Returns the parsed list, or ``None`` to use the resolved
    default.
    """
    principal = api_auth.principal_from_request(request)
    if not principal.global_authority:
        return None
    raw = request.query_params.get("peers")
    if raw is None:
        return None
    return [p for p in raw.split(",") if p]


async def api_graph_settings_list(request):
    """GET /api/graph/settings/<set_id> — resolved members of a SET."""
    auth_error = api_auth.require_authenticated_api_caller(request)
    if auth_error is not None:
        return auth_error
    set_id = request.path_params["set_id"]
    target, minrev, stored, err = _parse_settings_read_params(request.query_params)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    org = api_auth.organization_scope_from_request(request)
    # A caller-supplied ``peers`` lets the caller choose which orgs compose into
    # the read. Invariant 1: an org-bound caller does not select its own scope —
    # it gets its org's resolved peers (their published/canonical surface only).
    # Honour an explicit peers= only for a global-authority caller (the operator).
    peers = _client_peers_if_global(request)
    if os.environ.get("DASHBOARD_MOCK"):
        from tools.dashboard.dao import mock as dao_mock
        return JSONResponse({
            "members": dao_mock.get_settings_members(set_id, org=org),
            "dropped": {},
        })
    members = graph_ops.read_set(
        set_id, target_revision=target, min_revision=minrev,
        org=org or graph_ops.CALLER_ORG, peers=peers,
    )
    out = members.as_payload()
    if stored is not None:
        out["members"] = [m for m in out["members"] if m["stored_revision"] == stored]
    return JSONResponse(out)


async def api_graph_settings_get_by_key(request):
    """GET /api/graph/settings/<set_id>/<key> — single resolved member by key."""
    auth_error = api_auth.require_authenticated_api_caller(request)
    if auth_error is not None:
        return auth_error
    set_id = request.path_params["set_id"]
    key = request.path_params["key"]
    target, minrev, stored, err = _parse_settings_read_params(request.query_params)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    org = api_auth.organization_scope_from_request(request)
    members = graph_ops.read_set(
        set_id, target_revision=target, min_revision=minrev,
        org=org or graph_ops.CALLER_ORG,
    )
    for m in members.members:
        if m.key == key:
            return JSONResponse(m.to_dict())
    return JSONResponse({"error": "not found"}, status_code=404)


async def api_graph_setting_get(request):
    """GET /api/graph/setting/<id> — resolve a single Setting by id."""
    sid = request.path_params["id"]
    target, _, _, err = _parse_settings_read_params(request.query_params)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    org = api_auth.organization_scope_from_request(request)
    got = graph_ops.get_setting(
        sid, target_revision=target, org=org or graph_ops.CALLER_ORG,
    )
    if got is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(got.to_dict())


async def api_graph_setting_create(request):
    """POST /api/graph/setting — body: {set_id, schema_revision, key, payload, state}."""
    from tools.graph.schemas.registry import SchemaValidationError
    body = await request.json()
    required = ("set_id", "schema_revision", "key", "payload")
    missing = [k for k in required if k not in body]
    if missing:
        return JSONResponse(
            {"error": f"missing fields: {missing}"}, status_code=400,
        )
    org = api_auth.organization_scope_from_request(request)
    if os.environ.get("DASHBOARD_MOCK"):
        from tools.dashboard.dao import mock as dao_mock
        sid = dao_mock.add_setting_member(
            body["set_id"], body["key"], body["payload"], org=org,
        )
        await _emit_setting_changed(
            operation="write",
            org=org,
            snapshot={
                "set_id": body["set_id"],
                "schema_revision": int(body["schema_revision"]),
                "key": body["key"],
                "publication_state": body.get("state", "raw"),
                "deprecated": False,
            },
        )
        return JSONResponse({"id": sid}, status_code=201)
    try:
        # Upsert where upsert is legal, append where it is not. ``add_setting``
        # creates a NEW base row on every call, so an agent revising a setting
        # through `graph set add` — the only CLI verb that takes a full payload
        # — silently produced a duplicate base each time. One workspace primer
        # accumulated seven live bases that way on 2026-08-03. Upserting
        # updates the existing base in place (same id, same created_at) and
        # inserts only when the key is genuinely new.
        #
        # Two access patterns cannot be upserted at all, and refused writes
        # here rather than choosing correctly: append-only logs, and vault
        # sets, whose rows are encrypted object revisions addressed by row id.
        # That left the vault with NO write path over HTTP — sealing was
        # reachable only from inside the dashboard process — which is not a
        # decision anybody made, just the blast radius of the upsert fix.
        # ``write_by_key`` dispatches on the set: append for a log, seal-then-
        # override for a vault set, upsert for everything else. Library callers
        # that legitimately append (surface pings, keyed by uuid4) call
        # settings_ops directly and are unaffected.
        sid = graph_ops.write_by_key(
            body["set_id"],
            int(body["schema_revision"]),
            body["key"],
            body["payload"],
            state=body.get("state", "raw"),
            org=org or graph_ops.CALLER_ORG,
            vault_policy_class_id=body.get("vault_policy_class_id"),
        )
    except SchemaValidationError as e:
        return JSONResponse(
            {"error": "schema validation failed", "detail": str(e)},
            status_code=400,
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except (VaultSealerMissing, VaultSealerNotReady) as e:
        # These failures occur before the Setting row is written. Return only
        # the readiness reason; the submitted plaintext must never be echoed
        # into an HTTP error body or log-friendly exception envelope.
        return JSONResponse({"error": str(e)}, status_code=423)
    # setting.changed fires from settings_ops.add_setting via the
    # function-level emit hook — see _settings_emit_hook.
    #
    # A stored row that resolution will never return is reported here, not
    # left for the caller to discover by reading the value back and finding
    # it unchanged. A write API that says "created" about a row nothing can
    # read has told the caller the opposite of what happened.
    out: dict = {"id": sid}
    shadow = graph_ops.take_shadowed_write(body["set_id"], body["key"])
    if shadow is not None:
        out["shadowed_by"] = {
            "winner_id": shadow.winner_id,
            "winner_org": shadow.winner_org,
            "winner_state": shadow.winner_state,
            "written_state": shadow.written_state,
            "written_org": shadow.written_org,
            "message": str(shadow),
        }
    return JSONResponse(out, status_code=201)


async def api_graph_setting_override(request):
    """POST /api/graph/setting/<id>/override — body: {payload, state}."""
    from tools.graph.schemas.registry import SchemaValidationError
    target_id = request.path_params["id"]
    body = await request.json()
    if "payload" not in body:
        return JSONResponse({"error": "payload required"}, status_code=400)
    org = api_auth.organization_scope_from_request(request)
    try:
        sid = graph_ops.override_setting(
            target_id, body["payload"], state=body.get("state", "raw"),
            org=org or graph_ops.CALLER_ORG,
            vault_policy_class_id=body.get("vault_policy_class_id"),
        )
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except SchemaValidationError as e:
        return JSONResponse(
            {"error": "schema validation failed", "detail": str(e)},
            status_code=400,
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except (VaultSealerMissing, VaultSealerNotReady) as e:
        return JSONResponse({"error": str(e)}, status_code=423)
    return JSONResponse({"id": sid}, status_code=201)


async def api_graph_setting_exclude(request):
    """POST /api/graph/setting/<id>/exclude — body: {state}."""
    target_id = request.path_params["id"]
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    org = api_auth.organization_scope_from_request(request)
    try:
        sid = graph_ops.exclude_setting(
            target_id, state=body.get("state", "raw"),
            org=org or graph_ops.CALLER_ORG,
        )
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"id": sid}, status_code=201)


async def api_graph_setting_promote(request):
    """POST /api/graph/setting/<id>/promote — body: {to_state}."""
    sid = request.path_params["id"]
    body = await request.json()
    to_state = body.get("to_state") or body.get("to")
    if not to_state:
        return JSONResponse({"error": "to_state required"}, status_code=400)
    org = api_auth.organization_scope_from_request(request)
    try:
        graph_ops.promote_setting(sid, to_state, org=org or graph_ops.CALLER_ORG)
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"ok": True})


async def api_graph_source_move(request):
    """POST /api/graph/source/<id>/move — body: {from_org, to_org, reason?}."""
    source_id = request.path_params["id"]
    if not _GRAPH_SOURCE_ID_RE.match(source_id):
        return JSONResponse({"error": f"malformed source_id: {source_id!r}"}, status_code=400)
    body = await request.json()
    from_org = body.get("from_org") or body.get("from")
    if not from_org:
        return JSONResponse({"error": "from_org required"}, status_code=400)
    to_org = body.get("to_org") or body.get("to")
    if not to_org:
        return JSONResponse({"error": "to_org required"}, status_code=400)
    try:
        moved = await asyncio.to_thread(
            graph_ops.move_source,
            source_id,
            str(from_org),
            str(to_org),
            reason=body.get("reason"),
        )
    except graph_ops.CrossOrgWriteError as e:
        return _cross_org_error_response(e)
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    _checkpoint_graph()
    return JSONResponse({"ok": True, **moved})


async def api_graph_source_promote(request):
    """POST /api/graph/source/<id>/promote — body: {to_state}.

    Transition a source's ``publication_state`` (raw|curated|published|canonical).
    ``published``/``canonical`` make the source visible to cross-org readers.
    Mirrors the setting-promote route; the source-level op existed in
    ``graph_ops.promote_source`` but had no CLI/HTTP surface until now."""
    source_id = request.path_params["id"]
    if not _GRAPH_SOURCE_ID_RE.match(source_id):
        return JSONResponse({"error": f"malformed source_id: {source_id!r}"}, status_code=400)
    body = await request.json()
    to_state = body.get("to_state") or body.get("to")
    if not to_state:
        return JSONResponse({"error": "to_state required"}, status_code=400)
    org = api_auth.organization_scope_from_request(request)
    try:
        result = await asyncio.to_thread(
            graph_ops.promote_source,
            source_id,
            str(to_state),
            org=org or graph_ops.CALLER_ORG,
        )
    except graph_ops.CrossOrgWriteError as e:
        return _cross_org_error_response(e)
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    _checkpoint_graph()
    return JSONResponse({"ok": True, **result})


async def api_graph_setting_deprecate(request):
    """POST /api/graph/setting/<id>/deprecate — body: {successor_id?}."""
    sid = request.path_params["id"]
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    org = api_auth.organization_scope_from_request(request)
    try:
        graph_ops.deprecate_setting(
            sid, successor_id=body.get("successor_id"),
            org=org or graph_ops.CALLER_ORG,
        )
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return JSONResponse({"ok": True})


async def api_graph_setting_undeprecate(request):
    """POST /api/graph/setting/<id>/undeprecate — reverse a deprecation."""
    sid = request.path_params["id"]
    org = api_auth.organization_scope_from_request(request)
    try:
        graph_ops.undeprecate_setting(sid, org=org or graph_ops.CALLER_ORG)
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return JSONResponse({"ok": True})


async def api_graph_setting_delete(request):
    """DELETE /api/graph/setting/<id> — hard-delete (raw only)."""
    sid = request.path_params["id"]
    org = api_auth.organization_scope_from_request(request)
    try:
        graph_ops.remove_setting(sid, org=org or graph_ops.CALLER_ORG)
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except graph_ops.CrossOrgWriteError as e:
        return _cross_org_error_response(e)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    # setting.changed (operation=delete) fires from settings_ops via the
    # emit hook with the pre-delete snapshot.
    return JSONResponse({"ok": True})


def _settings_zero_activity_metrics() -> dict[str, int]:
    return {
        "calls": 0,
        "reads": 0,
        "writes": 0,
        "upserts": 0,
    }


def _settings_activity_windows_for_set(
    activity_snapshot: dict[str, Any],
    set_id: str,
) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for window_name, window_metrics in activity_snapshot.items():
        row = (
            window_metrics.get(set_id)
            if isinstance(window_metrics, dict) else None
        )
        merged = _settings_zero_activity_metrics()
        if isinstance(row, dict):
            merged.update({
                "calls": int(row.get("calls") or 0),
                "reads": int(row.get("reads") or 0),
                "writes": int(row.get("writes") or 0),
                "upserts": int(row.get("upserts") or 0),
            })
        out[window_name] = merged
    return out


def _settings_payload_size_bytes(payload: Any) -> int:
    if payload is None:
        return 0
    if isinstance(payload, bytes):
        return len(payload)
    if isinstance(payload, str):
        return len(payload.encode("utf-8"))
    return len(json.dumps(payload, sort_keys=True).encode("utf-8"))


def _settings_storage_summary_rows(
    *,
    org: str | None,
) -> dict[str, dict[str, Any]]:
    from tools.graph import settings_ops
    from tools.graph.db import GraphDB, resolve_caller_db_path

    resolved_org = settings_ops._resolve_settings_caller(org)
    try:
        db = GraphDB(resolve_caller_db_path(resolved_org), mode="ro")
    except Exception:
        return {}
    try:
        rows = db.conn.execute(
            """
            SELECT
              set_id,
              COUNT(*) AS stored_row_count,
              COUNT(DISTINCT key) AS stored_key_count,
              COALESCE(SUM(LENGTH(CAST(payload AS BLOB))), 0) AS payload_bytes,
              COALESCE(SUM(CASE WHEN deprecated = 1 THEN 1 ELSE 0 END), 0)
                AS deprecated_row_count,
              MAX(updated_at) AS latest_updated_at
            FROM settings
            GROUP BY set_id
            ORDER BY set_id
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        db.close()
    return {
        row["set_id"]: {
            "stored_row_count": int(row["stored_row_count"] or 0),
            "stored_key_count": int(row["stored_key_count"] or 0),
            "payload_bytes": int(row["payload_bytes"] or 0),
            "deprecated_row_count": int(row["deprecated_row_count"] or 0),
            "latest_updated_at": row["latest_updated_at"],
        }
        for row in rows
        if row["set_id"]
    }


def _settings_key_storage_rows(
    set_id: str,
    *,
    org: str | None,
) -> list[dict[str, Any]]:
    from tools.graph import settings_ops
    from tools.graph.db import GraphDB, resolve_caller_db_path

    resolved_org = settings_ops._resolve_settings_caller(org)
    try:
        db = GraphDB(resolve_caller_db_path(resolved_org), mode="ro")
    except Exception:
        return []
    try:
        rows = db.conn.execute(
            """
            SELECT key, payload, publication_state, deprecated,
                   created_at, updated_at, id
            FROM settings
            WHERE set_id = ?
            ORDER BY key ASC, updated_at DESC, created_at DESC, id DESC
            """,
            (set_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        db.close()

    summaries: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["key"]
        summary = summaries.get(key)
        if summary is None:
            summary = {
                "key": key,
                "member_present": False,
                "stored_row_count": 0,
                "payload_bytes": 0,
                "deprecated_row_count": 0,
                "latest_updated_at": row["updated_at"],
                "latest_state": row["publication_state"],
            }
            summaries[key] = summary
        summary["stored_row_count"] += 1
        summary["payload_bytes"] += _settings_payload_size_bytes(row["payload"])
        if row["deprecated"]:
            summary["deprecated_row_count"] += 1
    return [summaries[key] for key in sorted(summaries)]


def _settings_visible_set_ids(
    *,
    org: str | None,
    tracked: bool,
) -> list[str]:
    from tools.graph import settings_ops

    list_fn = graph_ops.list_set_ids if tracked else settings_ops.list_set_ids.__wrapped__
    return list_fn(org=org or graph_ops.CALLER_ORG)


def _settings_member_snapshot(
    set_id: str,
    *,
    org: str | None,
) -> tuple[int | None, set[str], str | None]:
    """Resolved members of one set, read at the set's OWN home.

    Returns ``(count, keys, error)``. ``count`` is None when the set could
    not be read, which is not the same as a count of zero.

    A diagnostic walks every visible set_id, and a set that declares it lives
    in the operator's own database is refused when read against an
    organization -- correctly, since that is the whole point of declaring a
    home. Reading each set where it actually lives is the fix; asking for all
    of them at the caller's organization means one personal-homed set takes
    the entire summary down with it.
    """
    from tools.graph import settings_ops
    from tools.graph import schemas as _schemas

    try:
        home = _schemas.declared_home(set_id)
    except Exception:
        home = None
    read_org = "personal" if home == "personal" else (org or graph_ops.CALLER_ORG)

    try:
        members = settings_ops.read_set.__wrapped__(set_id, org=read_org)
    except Exception as exc:
        # One unreadable set must not fail the whole inventory -- a tool you
        # reach for when things are broken is the worst place for all-or-
        # nothing. But it must not read as EMPTY either: a set with no rows
        # and a set that could not be read are different facts, and
        # collapsing them hides the second behind the first.
        logger.warning(
            "settings diagnostic: could not read %s at org=%r",
            set_id, read_org, exc_info=True,
        )
        return None, set(), f"{type(exc).__name__}: {exc}"[:200]
    keys = {member.key for member in members.members}
    return len(members.members), keys, None


def _settings_set_summary_row(
    set_id: str,
    *,
    org: str | None,
    storage_by_set: dict[str, dict[str, Any]],
    activity_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    member_count, _member_keys, read_error = _settings_member_snapshot(
        set_id, org=org)
    storage = storage_by_set.get(set_id, {})
    row = {
        "set_id": set_id,
        "count": member_count,
        "member_count": member_count,
        "read_error": read_error,
        "stored_row_count": int(storage.get("stored_row_count") or 0),
        "stored_key_count": int(storage.get("stored_key_count") or 0),
        "payload_bytes": int(storage.get("payload_bytes") or 0),
        "deprecated_row_count": int(storage.get("deprecated_row_count") or 0),
        "latest_updated_at": storage.get("latest_updated_at"),
    }
    if activity_snapshot is not None:
        row["activity"] = _settings_activity_windows_for_set(
            activity_snapshot, set_id,
        )
    return row


def _settings_summary_rows_for_visible_sets(
    *,
    org: str | None,
    set_ids: list[str],
) -> list[dict[str, Any]]:
    storage_by_set = _settings_storage_summary_rows(org=org)
    return [
        _settings_set_summary_row(
            set_id,
            org=org,
            storage_by_set=storage_by_set,
        )
        for set_id in set_ids
    ]


def _settings_diag_rows(
    *,
    org: str | None,
) -> tuple[str | None, list[str], list[dict[str, Any]]]:
    from tools.graph import settings_ops

    resolved_org = settings_ops._resolve_settings_caller(org)
    activity_snapshot = settings_ops.settings_api_set_metrics_snapshot(
        org=resolved_org,
    )
    visible_set_ids = _settings_visible_set_ids(org=org, tracked=False)
    storage_by_set = _settings_storage_summary_rows(org=org)
    set_ids = set(visible_set_ids)
    set_ids.update(storage_by_set)
    for window_metrics in activity_snapshot.values():
        if isinstance(window_metrics, dict):
            set_ids.update(window_metrics)
    rows = [
        _settings_set_summary_row(
            set_id,
            org=org,
            storage_by_set=storage_by_set,
            activity_snapshot=activity_snapshot,
        )
        for set_id in sorted(set_ids)
    ]
    return resolved_org, list(activity_snapshot.keys()), rows


async def api_graph_set_ids(request):
    """GET /api/graph/sets — list known set_ids."""
    org = api_auth.organization_scope_from_request(request)
    set_ids = graph_ops.list_set_ids(org=org or graph_ops.CALLER_ORG)
    summary = request.query_params.get("summary", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if not summary:
        return JSONResponse({"set_ids": set_ids})
    return JSONResponse({
        "set_ids": set_ids,
        "sets": _settings_summary_rows_for_visible_sets(
            org=org,
            set_ids=set_ids,
        ),
    })


async def api_graph_settings_migrate(request):
    """POST /api/graph/settings/<set_id>/migrate — body: {to_rev, dry_run?}.

    Rewrite stored rows up to a target schema revision. Mirrors the
    ``graph set migrate`` CLI subcommand.
    """
    set_id = request.path_params["set_id"]
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    try:
        to_rev = int(body.get("to_rev"))
    except (TypeError, ValueError):
        return JSONResponse(
            {"error": "to_rev (int) required"}, status_code=400,
        )
    dry_run = bool(body.get("dry_run", False))
    org = api_auth.organization_scope_from_request(request)
    try:
        report = graph_ops.migrate_setting_revisions(
            set_id, to_rev, dry_run=dry_run,
            org=org or graph_ops.CALLER_ORG,
        )
    except Exception as e:  # noqa: BLE001 — mirror CLI surface
        return JSONResponse({"error": str(e)}, status_code=500)
    # Per-affected setting.changed events fire from settings_ops via the
    # emit hook (only when ``dry_run=False``).
    return JSONResponse(report.to_dict())


async def api_graph_setting_resolve(request):
    """GET /api/graph/setting-resolve/<value>?org=<slug>.

    Resolve a Setting by full id or id-prefix using the same own-first /
    peer-public-surface rules as :func:`graph_ops.resolve_setting_strict`.

    * 200 ``{"id": ..., "set_id": ..., "key": ..., ...}`` — unique match.
    * 409 ``{"candidates": [{"id": ..., "set_id": ..., "key": ...}, ...]}`` —
      ambiguous prefix; CLI surfaces full UUIDs so the operator can pick.
    * 404 ``{"error": "no setting matches '<value>'"}`` — no match.
    """
    value = request.path_params["value"]
    org = api_auth.organization_scope_from_request(request)
    hit = graph_ops.resolve_setting_strict(value, org=org or graph_ops.CALLER_ORG)
    if hit is None:
        return JSONResponse(
            {"error": f"no setting matches {value!r}"}, status_code=404,
        )
    if isinstance(hit, list):
        return JSONResponse(
            {"candidates": [
                {"id": r["id"], "set_id": r["set_id"], "key": r["key"]}
                for r in hit
            ]},
            status_code=409,
        )
    return JSONResponse(hit)


async def api_graph_settings_chain(request):
    """GET /api/graph/settings/<set_id>/<key>/chain?org=<slug>.

    Return the supersedes chain for ``(set_id, key)`` as ordered layer
    contributions (base → override-1 → override-2 → ...). 404 when no
    base resolves under the caller's scope.
    """
    auth_error = api_auth.require_authenticated_api_caller(request)
    if auth_error is not None:
        return auth_error
    set_id = request.path_params["set_id"]
    key = request.path_params["key"]
    org = api_auth.organization_scope_from_request(request)
    chain = graph_ops.chain_setting(set_id, key, org=org or graph_ops.CALLER_ORG)
    if chain is None:
        return JSONResponse(
            {"error": f"no member matches ({set_id!r}, {key!r})"},
            status_code=404,
        )
    return JSONResponse(chain)


async def api_graph_settings_check(request):
    """GET one row's schema-driven readiness findings in the server frame."""
    from dataclasses import asdict
    from tools.graph import settings_ops as _settings_ops

    set_id = request.path_params["set_id"]
    key = request.path_params["key"]
    org = api_auth.settings_scope_from_request(request)
    try:
        findings, satisfied = _settings_ops.inspect_setting(
            set_id, key, org=org,
        )
    except Exception as exc:
        return JSONResponse(
            {"error": f"{type(exc).__name__}: {exc}"}, status_code=502,
        )
    return JSONResponse({
        "findings": [asdict(finding) for finding in findings],
        "satisfied": [asdict(item) for item in satisfied],
    })


async def api_graph_settings_contested(request):
    """GET /api/graph/settings/<set_id>/contested — contended slot report.

    Keys of the set holding more than one eligible signed slot at the
    winning rung and store (graph://21a0da9e-1c2 "Resolution"): resolution
    answers exactly one value, and this is the query that makes a live
    disagreement visible. Metadata only — personas, timestamps, states —
    never payloads.
    """
    from tools.graph import settings_ops as _settings_ops

    set_id = request.path_params["set_id"]
    org = api_auth.organization_scope_from_request(request)
    contested = _settings_ops.contested_keys(
        set_id, org=org or graph_ops.CALLER_ORG,
    )
    return JSONResponse({"set_id": set_id, "contested": contested})


# ── Agentic actions dispatch (auto-pqgrl) ────────────────────


# In-process idempotency cache for ``/api/agent-actions/dispatch``. Keyed
# by ``(asset_id, member_key, dispatched_by_session)`` → ``(epoch, response)``.
# UI debounce isn't enough — duplicate clicks across tabs / network retries
# can fire twice. A duplicate inside the window returns the previous
# response so the front-end routes to the in-flight trace rather than
# spawning a second agent.
_AGENT_ACTION_IDEMPOTENCY_WINDOW_SEC = 5.0
_recent_agent_action_dispatches: dict[str, tuple[float, dict]] = {}


def _agent_action_idempotency_key(
    asset_id: str, member_key: str, dispatched_by_session: str,
) -> str:
    return f"{asset_id}|{member_key}|{dispatched_by_session}"


def _agent_action_idempotency_lookup(
    key: str, *, now: float | None = None,
) -> dict | None:
    """Return a cached response if one is still inside the window."""
    epoch = now if now is not None else time.time()
    entry = _recent_agent_action_dispatches.get(key)
    if entry is None:
        return None
    ts, response = entry
    if epoch - ts > _AGENT_ACTION_IDEMPOTENCY_WINDOW_SEC:
        _recent_agent_action_dispatches.pop(key, None)
        return None
    return response


def _agent_action_idempotency_remember(
    key: str, response: dict, *, now: float | None = None,
) -> None:
    epoch = now if now is not None else time.time()
    _recent_agent_action_dispatches[key] = (epoch, response)
    # Opportunistic GC of stale entries so the dict never grows unbounded.
    cutoff = epoch - _AGENT_ACTION_IDEMPOTENCY_WINDOW_SEC
    for k, (ts, _) in list(_recent_agent_action_dispatches.items()):
        if ts < cutoff:
            _recent_agent_action_dispatches.pop(k, None)


# ── Tag taxonomy cache for prompt-template injection ──────────────────
# Populated on demand and refreshed every _TAG_TAXONOMY_TTL seconds (per
# org). The taxonomy doesn't change per dispatch, so a 1-second in-process
# cache amortizes the DB read across burst dispatches without holding stale
# data when the operator adds a tag.
_TAG_TAXONOMY_CACHE: dict[str, tuple[float, list[str]]] = {}
_TAG_TAXONOMY_TTL = 1.0  # seconds


def _get_tag_taxonomy(org: str) -> list[str]:
    """Return the tag-name list for *org*, cached for ``_TAG_TAXONOMY_TTL``s."""
    now = time.time()
    cached = _TAG_TAXONOMY_CACHE.get(org)
    if cached and now - cached[0] < _TAG_TAXONOMY_TTL:
        return cached[1]
    try:
        rows = graph_ops.list_collab_topics(org=org)
    except Exception:
        logger.exception("agent-actions: tag taxonomy lookup failed org=%s", org)
        rows = []
    names = [r.get("name", "") for r in rows if r.get("name")]
    _TAG_TAXONOMY_CACHE[org] = (now, names)
    return names


def _build_send_to_primer(
    *,
    asset: dict,
    custom_message: str = "",
) -> str:
    """Compose the markdown primer that Send-To delivers to the chosen
    session. The receiver gets the canonical asset id, type, owning org,
    title, and the action key — enough to resolve the asset against the
    correct org's graph DB without consulting the dashboard.
    """
    asset_id = str(asset.get("id") or "")
    asset_type = str(asset.get("type") or "")
    asset_org = str(asset.get("org") or "")
    asset_title = str(asset.get("title") or "")[:80]
    if not asset_org:
        logger.warning(
            "Send-To primer: asset_org unresolvable for asset_id=%s — "
            "emitting empty value to preserve shape",
            asset_id,
        )
    lines = [
        f"asset_id: {asset_id}",
        f"asset_type: {asset_type}",
        f"asset_org: {asset_org}",
        f"asset_title: {asset_title}",
        "action: session.send-to",
    ]
    if custom_message:
        lines.append(f"custom_message: {custom_message}")
    return "\n".join(lines)


async def _send_to_via_crosstalk(
    *,
    target_session: str,
    sender_session: str,
    primer: str,
    label: str | None = None,
) -> tuple[bool, str | None]:
    """Deliver a Send-To primer to *target_session* by injecting a
    crosstalk envelope into its tmux pane. Returns ``(ok, error)``.

    ``label`` overrides the envelope's ``label="..."`` attribute. When
    omitted, falls back to the sender session's stored label, then the
    sender session name itself. Dashboard-originated Send-To passes
    ``"Dashboard Send-To"`` so the receiver can recognise the subsystem
    at a glance instead of seeing the bare ``dashboard`` sentinel.
    """
    if not _tmux_session_exists(target_session):
        return False, "session not live"

    sender_row = dashboard_db.get_session(sender_session)
    sender_label = label or (sender_row or {}).get("label", "") or sender_session
    # Reconcile-on-read + graph MAX(turn_number) (auto-4nr14 §A/§B).
    sender_source_id = dashboard_db.reconcile_session_graph_source_id(sender_row)
    sender_turn = dashboard_db.get_source_max_turn_number(sender_source_id)
    sender_harness = (sender_row or {}).get("harness") or "claude"
    sender_model = (sender_row or {}).get("model") or ""
    iso_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    turn_str = str(sender_turn) if sender_turn is not None else ""
    envelope = (
        f'<crosstalk from="{sender_session}"\n'
        f'           label="{sender_label}"\n'
        f'           source="{sender_source_id}" turn="{turn_str}"\n'
        f'           harness="{sender_harness}" model="{sender_model}"\n'
        f'           timestamp="{iso_now}">\n'
        f'{primer}\n'
        f'</crosstalk>'
    )
    try:
        await tmux_send(target_session, envelope)
    except Exception as exc:
        logger.exception(
            "agent-actions Send-To: tmux_send failed target=%s", target_session,
        )
        return False, f"tmux_send failed: {exc}"
    try:
        await asyncio.to_thread(
            auth_db.insert_message,
            sender_session, sender_label, target_session,
            sender_source_id or None, sender_turn,
            primer, time.time(),
        )
    except Exception:
        logger.exception("agent-actions Send-To: crosstalk log insert failed")
    return True, None


def _resolve_agent_action_member(
    *, set_id: str, member_key: str, target_org: str,
) -> dict | None:
    """Look up the member's payload in *target_org*'s DB.

    Strictly own-org-of-asset: the dropdown for an anchore note shows
    only members defined in anchore.db; cross-org adoption is via
    Setting promotion. ``peers=[]`` enforces that here.
    """
    try:
        members = graph_ops.read_set(
            set_id, org=target_org, peers=[],
        ).members
    except Exception:
        logger.exception(
            "agent-actions: read_set failed set_id=%s target_org=%s",
            set_id, target_org,
        )
        return None
    for m in members:
        if m.key == member_key:
            return dict(m.payload) if not isinstance(m.payload, dict) else m.payload
    return None


def _resolve_workspace_for_org(target_org: str):
    """Return the first workspace whose graph_project == target_org, or None."""
    from agents.workspace_settings import load_workspaces
    for _wid, ws in load_workspaces().items():
        if ws.graph_project == target_org:
            return ws
    return None


def _source_metadata_dict(source: dict) -> dict:
    """Return ``source.metadata`` as a dict."""
    src_meta_raw = source.get("metadata") or {}
    if isinstance(src_meta_raw, str):
        try:
            return json.loads(src_meta_raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    if isinstance(src_meta_raw, dict):
        return src_meta_raw
    return {}


def _normalize_action_object(value):
    """Recursively coerce action-context objects into format-friendly data."""
    if value is None:
        return ""
    if isinstance(value, dict):
        return {
            str(k): _normalize_action_object(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_action_object(v) for v in value]
    return value


def _build_agent_action_template_context(
    *,
    asset: dict,
    source: dict | None = None,
    bead: dict | None = None,
    design: dict | None = None,
    tags: list[str],
) -> dict:
    """Expose generic nested objects to prompt templates."""
    return {
        "asset": _normalize_action_object(asset),
        "source": _normalize_action_object(source or {}),
        "bead": _normalize_action_object(bead or {}),
        "design": _normalize_action_object(design or {}),
        "tags": {
            "values": list(tags),
            "list": ", ".join(tags),
        },
    }


def _agent_action_asset_url(*, asset_kind: str, asset_id: str, request) -> str:
    """Return the canonical dashboard URL for the dispatched asset."""
    base = f"{request.url.scheme}://{request.url.netloc}"
    if asset_kind == "bead":
        return f"{base}/bead/{asset_id}"
    if asset_kind == "design":
        return f"{base}/design/{asset_id}"
    return f"{base}/graph/{asset_id}"


def _adapt_source_action_asset(
    *,
    source: dict,
) -> tuple[dict, dict, dict]:
    """Return normalized prompt objects for a source-backed action."""
    asset_id = str(source.get("id") or "")
    src_meta = _source_metadata_dict(source)
    source_obj = dict(source)
    source_obj["metadata"] = src_meta
    asset = {
        "id": asset_id,
        "title": source.get("title") or "",
        "short_description": (
            source.get("short_description")
            or src_meta.get("short_description")
            or ""
        ),
        "type": source.get("type") or "",
        "primer": "",
        "metadata": src_meta,
    }
    return asset, source_obj, {}


def _adapt_bead_action_asset(
    *,
    bead_id: str,
    bead: dict,
) -> tuple[dict, dict, dict]:
    """Return normalized prompt objects for a bead-backed action."""
    from tools.graph.primer import collect_primer_data, format_for_agent

    primer_data = collect_primer_data(bead_id)
    bead_obj = dict(bead)
    bead_obj.setdefault("id", bead_id)
    asset = {
        "id": bead_id,
        "title": bead.get("title") or bead_id,
        "short_description": bead.get("description") or "",
        "type": "bead",
        "primer": format_for_agent(primer_data),
        "metadata": {},
    }
    return asset, {}, bead_obj


def _resolve_design_action_asset(asset_id: str) -> dict | None:
    """Resolve a Design Studio revision or series id for agent actions."""
    try:
        from agents.design_db import _get_conn
    except Exception:
        logger.exception("agent-actions: design_db import failed")
        return None
    conn = _get_conn()
    try:
        rows = conn.execute("""\
            SELECT
              id,
              COALESCE(design_id, id) AS design_id,
              title,
              description,
              status,
              COALESCE(revision_seq, 1) AS revision_seq,
              created_at,
              creator_session_id,
              creator_session_label,
              CASE WHEN fixture IS NOT NULL AND fixture != '' THEN 1 ELSE 0 END AS has_fixture
            FROM designs
            WHERE id = ? OR COALESCE(design_id, id) = ?
            ORDER BY COALESCE(revision_seq, 1) ASC, created_at ASC, id ASC
        """, (asset_id, asset_id)).fetchall()
        if not rows:
            return None
        latest = rows[-1]
        variant_count = conn.execute(
            "SELECT COUNT(*) FROM revision_variants WHERE revision_id IN ("
            + ",".join("?" for _ in rows) + ")",
            tuple(row["id"] for row in rows),
        ).fetchone()[0]
    finally:
        conn.close()
    created_values = [str(row["created_at"] or "") for row in rows if row["created_at"]]
    design = {
        "id": latest["id"],
        "latest_revision_id": latest["id"],
        "design_id": latest["design_id"] or latest["id"],
        "title": latest["title"] or "Untitled Design",
        "description": latest["description"] or "",
        "status": latest["status"] or "pending",
        "revision_count": len(rows),
        "variant_count": int(variant_count or 0),
        "has_fixture": bool(latest["has_fixture"]),
        "first_created_at": min(created_values) if created_values else "",
        "latest_created_at": max(created_values) if created_values else "",
        "creator_session_id": latest["creator_session_id"] or "",
        "creator_session_label": latest["creator_session_label"] or "",
    }
    return design


def _adapt_design_action_asset(
    *,
    design: dict,
) -> tuple[dict, dict, dict]:
    """Return normalized prompt objects for a Design Studio action."""
    asset_id = str(design.get("latest_revision_id") or design.get("id") or "")
    design_obj = dict(design)
    design_obj["id"] = asset_id
    asset = {
        "id": asset_id,
        "title": design.get("title") or "Untitled Design",
        "short_description": design.get("description") or "",
        "type": "design",
        "primer": "",
        "metadata": {
            "design_id": design.get("design_id") or asset_id,
            "latest_revision_id": asset_id,
            "status": design.get("status") or "pending",
            "revision_count": design.get("revision_count") or 0,
            "variant_count": design.get("variant_count") or 0,
            "has_fixture": bool(design.get("has_fixture")),
            "first_created_at": design.get("first_created_at") or "",
            "latest_created_at": design.get("latest_created_at") or "",
            "creator_session_id": design.get("creator_session_id") or "",
            "creator_session_label": design.get("creator_session_label") or "",
        },
    }
    return asset, {}, design_obj


def _build_agent_action_context(
    *,
    asset_kind: str,
    asset_id: str,
    request,
    target_org: str,
    source: dict | None = None,
    bead: dict | None = None,
    design: dict | None = None,
) -> dict:
    """Build the generic prompt object bag for any supported asset kind."""
    tags = _get_tag_taxonomy(target_org)
    asset_url = _agent_action_asset_url(
        asset_kind=asset_kind,
        asset_id=asset_id,
        request=request,
    )
    if asset_kind == "bead":
        asset, source_obj, bead_obj = _adapt_bead_action_asset(
            bead_id=asset_id,
            bead=bead or {},
        )
        design_obj = {}
    elif asset_kind == "design":
        asset, source_obj, design_obj = _adapt_design_action_asset(
            design=design or {},
        )
        bead_obj = {}
    else:
        asset, source_obj, bead_obj = _adapt_source_action_asset(
            source=source or {},
        )
        design_obj = {}
    asset["url"] = asset_url
    asset["org"] = target_org
    return _build_agent_action_template_context(
        asset=asset,
        source=source_obj,
        bead=bead_obj,
        design=design_obj,
        tags=tags,
    )


def _template_field_root(field_name: str) -> str:
    """Return the root symbol for a ``str.format`` placeholder."""
    root = field_name.split("[", 1)[0]
    return root.split(".", 1)[0]


def _render_agent_action_prompt(
    template: str, *, page_context: dict,
    dispatched_by_session: str, member_key: str,
    custom_input: str = "",
    run_id: str | None = None,
) -> str:
    """Render ``template`` against ``page_context`` via ``str.format``.

    Strict mode (auto-gh2iv): the template is statically inspected
    BEFORE rendering. Any placeholder whose root symbol is not in
    :data:`_AGENT_ACTION_PLACEHOLDER_ROOTS` raises ``ValueError`` so the
    dispatch endpoint can surface a 500 with a clear message instead of
    silently shipping ``{undefined_field}`` literal text to the agent.

    Pre-Round 7h, missing keys returned the literal ``{key}`` to the
    agent — useful for dev iteration, terrible for production safety.
    Now we fail loudly: the agent should never see an unsubstituted
    brace, and a typo in a template ``.md`` is a launch-time error.

    If ``run_id`` is provided, a static-check failure ALSO writes a
    failure row to ``dispatch_runs`` so post-mortem can find it without
    re-reading logs.
    """
    import string as _string

    referenced = {
        f for _, f, _, _ in _string.Formatter().parse(template)
        if f and not f[0].isdigit()
    }
    unknown = {
        field for field in referenced
        if _template_field_root(field) not in AGENT_ACTION_TEMPLATE_ROOTS
    }
    if unknown:
        msg = (
            f"prompt template references undefined placeholder(s) "
            f"{sorted(unknown)}; add the root placeholder to "
            f"_AGENT_ACTION_PLACEHOLDER_ROOTS or fix the template"
        )
        logger.error("agent-actions render failure: %s", msg)
        if run_id:
            try:
                from agents.dispatch_db import record_dispatch_failure
                record_dispatch_failure(
                    run_id,
                    failure_class="prompt_render_error",
                    reason=msg,
                )
            except Exception:
                logger.exception(
                    "agent-actions: failed to record render failure for run_id=%s",
                    run_id,
                )
        raise ValueError(msg)

    fmt_ctx = dict(page_context)
    fmt_ctx["dispatched_by_session"] = dispatched_by_session
    fmt_ctx["member_key"] = member_key
    fmt_ctx["custom_input"] = custom_input
    try:
        return template.format(**fmt_ctx)
    except KeyError as e:  # defense-in-depth — static check above should have caught this
        raise ValueError(
            f"prompt template references undefined placeholder {e!s}; "
            f"this is a bug — static check should have caught it"
        ) from e


# ── Agent-action spawn throttles + off-loop workspace prep ──────────
# Operator directives from the 2026-08-28 incident (host handoff 148ead24):
# item 1 (the event-loop freeze) and item 2 (nothing caps agentic spawns).
# Limits are operator-tunable on the dispatch page, backed by the
# machine-homed autonomy.dispatch.limits#1 singleton; schema defaults
# apply when no row has been written.
_AGENTIC_CAP_WINDOW_S = 3600.0


def _resolved_dispatch_limits() -> dict:
    """Effective dispatch limits: the Settings row, else schema defaults."""
    from tools.graph.schemas import dispatch_limits as _dl
    limits = {
        "bead_max_concurrent": _dl.DEFAULT_BEAD_MAX_CONCURRENT,
        "agentic_max_concurrent": _dl.DEFAULT_AGENTIC_MAX_CONCURRENT,
    }
    try:
        row = graph_ops.read_set_key(
            _dl.SET_ID, "default", org="machine", peers=[],
        )
    except Exception:
        return limits
    payload = (row or {}).get("payload") or {}
    for name in limits:
        value = payload.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            limits[name] = value
    return limits
# Bounds how many workspace preps (git clone-sync + worktree checkout, ~20s
# each) may occupy executor threads at once — a burst of dispatches must not
# consume the default thread pool that every other to_thread caller shares.
_agentic_prep_semaphore = asyncio.Semaphore(3)


# Launch context for QUEUED rows, held in-process (run_id -> kwargs for
# _agentic_launch_task). Deliberately NOT persisted: a dashboard restart
# loses it, and the startup sweep fails the matching QUEUED/PREPARING
# rows as orphaned-prelaunch — consistent by construction.
_pending_agentic_launches: dict = {}
_agentic_queue_event: asyncio.Event = asyncio.Event()
_agentic_queue_task: asyncio.Task | None = None
_AGENTIC_QUEUE_CEILING = 200
_AGENT_ACTION_INPUT_MAX_BYTES = 512 * 1024


def _agentic_queue_depth() -> int:
    """QUEUED agentic rows (safety ceiling only, never a launch gate)."""
    from agents.dispatch_db import get_active_agentic_runs
    return sum(1 for r in get_active_agentic_runs()
               if r.get("status") == "QUEUED")


async def _drain_agentic_queue_once() -> int:
    """Launch queued dispatches into free slots; returns launches started.

    Cap semantics (operator ruling, 2026-08-29): the agentic limit
    governs CONCURRENT runs — occupancy is RUNNING plus PREPARING (a
    claimed launch about to become RUNNING; counting it prevents
    overshoot) — and excess dispatches WAIT as QUEUED rows, visible in
    the dispatch page's approved-waiting section, draining oldest-first
    as slots free. Nothing is ever rejected for being over the cap.
    """
    from agents.dispatch_db import claim_queued_run, get_active_agentic_runs
    limits = await asyncio.to_thread(_resolved_dispatch_limits)
    cap = limits["agentic_max_concurrent"]
    rows = await asyncio.to_thread(get_active_agentic_runs)
    occupied = sum(1 for r in rows
                   if r.get("status") in ("PREPARING", "RUNNING"))
    queued = sorted(
        (r for r in rows if r.get("status") == "QUEUED"),
        key=lambda r: str(r.get("started_at") or ""),
    )
    started = 0
    for row in queued:
        if occupied >= cap:
            break
        run_id = row.get("id") or ""
        ctx = _pending_agentic_launches.get(run_id)
        if ctx is None:
            # Context lost (restart) — the startup sweep owns these rows.
            continue
        if not await asyncio.to_thread(claim_queued_run, run_id):
            continue
        occupied += 1
        started += 1
        asyncio.create_task(
            _agentic_launch_task(run_id=run_id, **ctx),
            name=f"agentic-launch-{run_id}",
        )
    return started


async def _agentic_queue_drainer() -> None:
    """Background drainer: wakes on enqueue/slot events and every 5s.

    The 5s fallback is what observes slots freed by the DISPATCHER
    process finalizing completed runs (a cross-process event nothing
    in-process signals)."""
    while True:
        try:
            await asyncio.wait_for(_agentic_queue_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        _agentic_queue_event.clear()
        try:
            await _drain_agentic_queue_once()
        except Exception:
            logger.exception("agentic queue drain failed")


def _prepare_agent_action_workspace(
    workspace,
    container_name: str,
    output_dir_path: Path,
    base_metadata: dict,
):
    """Blocking half of an explicit-workspace agentic dispatch.

    Runs on a worker thread (never the event loop): artifact existence
    checks, git clone-sync + worktree creation, env resolution, primer
    render, and startup-script materialization. Returns
    ``(missing_artifacts, error, launch_kwargs_update)`` — exactly one of
    the three carries the outcome, so the async handler keeps its exact
    response shapes without doing any blocking work itself.
    """
    missing_artifacts = workspace_settings.validate_artifacts(workspace)
    if missing_artifacts:
        return missing_artifacts, None, {}
    try:
        project_mounts = prepare_session_mounts(
            workspace,
            container_name,
            refresh_existing_worktree=True,
        )
    except WorkspaceError as exc:
        return [], exc, {}
    project_mounts.update(workspace_settings.artifact_mounts(workspace))
    extra_env: dict[str, str] = dict(workspace.env) if workspace.env else {}
    _apply_env_from_host(
        workspace.env_from_host, extra_env,
        context=f"workspace {getattr(workspace, 'id', None) or workspace.graph_project}",
    )
    output_dir_path.mkdir(parents=True, exist_ok=True)
    primer_path = output_dir_path / ".claude_md"
    primer_path.write_text(render_workspace_primer(workspace))
    launch_metadata = dict(base_metadata)
    # Canonical org key only — see the launch_kwargs metadata at the call.
    launch_metadata["org"] = workspace.graph_project
    if workspace.default_tags:
        launch_metadata["graph_tags"] = list(workspace.default_tags)
    return [], None, {
        "mounts": project_mounts or None,
        "metadata": launch_metadata,
        "working_dir": workspace.working_dir or "/workspace/repo",
        "extra_env": extra_env or None,
        "global_claude_md": primer_path,
        "startup_script": workspace_settings.materialize_startup_script(
            workspace, output_dir_path),
        "needs_nested_docker": workspace.needs_nested_docker,
        "runtime": workspace.session_runtime,
        "network_host": workspace.network_host,
        "capabilities": workspace.capabilities,
    }


async def _agentic_launch_task(
    *,
    run_id: str,
    workspace,
    container_name: str,
    output_dir_path: Path,
    output_dir: str,
    launch_kwargs: dict,
    explicit_workspace: bool,
    model: str | None,
) -> None:
    """Background half of an agentic dispatch: prep, launch, register.

    Entered already claimed at PREPARING by the queue drainer (the cap's
    slot accounting counts this task from claim to completion).
    Independent of the HTTP request that accepted the dispatch — a
    client disconnect can no longer orphan a half-created run. A
    dashboard restart mid-flight is swept by fail_stale_prelaunch_runs()
    at the next startup; the drainer nudge on every exit path frees the
    slot for the next queued dispatch without waiting for the 5s poll.
    """
    from agents.dispatch_db import record_dispatch_failure, update_run_status
    try:
        if explicit_workspace:
            async with _agentic_prep_semaphore:
                # DO NOT move this back onto the event loop. Beyond git
                # cost, _resolve_org_mount's path probes inside
                # prepare_session_mounts will sit on a hard-mounted NFS
                # pool (timeo=600) once org-mounts move to the NAS — a
                # network hiccup there must block a worker thread, never
                # the loop (2026-08-28 incident: this work ran bare on the
                # loop, freezing the dashboard ~20s per dispatch, 131x).
                missing_artifacts, prep_error, prep_update = await asyncio.to_thread(
                    _prepare_agent_action_workspace,
                    workspace, container_name, output_dir_path,
                    dict(launch_kwargs["metadata"]),
                )
            if missing_artifacts:
                first = missing_artifacts[0]
                await asyncio.to_thread(
                    record_dispatch_failure, run_id,
                    failure_class="missing-artifacts",
                    reason=workspace_settings.format_missing_artifact_error(
                        first, workspace),
                )
                return
            if prep_error is not None:
                logger.error(
                    "agent-actions: workspace prep failed workspace=%s err=%s",
                    workspace.id, prep_error,
                )
                await asyncio.to_thread(
                    record_dispatch_failure, run_id,
                    failure_class="workspace-prep",
                    reason=str(prep_error),
                )
                return
            launch_kwargs.update(prep_update)

        try:
            from agents.session_launcher import launch_session
        except Exception:
            launch_session = None  # type: ignore[assignment]
        container_id: str | None = None
        if launch_session is not None and not os.environ.get("AGENT_ACTIONS_NO_LAUNCH"):
            try:
                container_id = await asyncio.to_thread(launch_session, **launch_kwargs)
            except Exception as exc:
                logger.exception("agent-actions: launch_session crashed")
                await asyncio.to_thread(
                    record_dispatch_failure, run_id,
                    failure_class="launch-crashed", reason=str(exc)[:400],
                )
                return
        await asyncio.to_thread(update_run_status, run_id, "RUNNING")
        if container_id:
            await session_monitor.register_session(
                tmux_name=run_id,
                type="agentic",
                run_dir=output_dir,
                project=workspace.id,
                harness=workspace.harness,
                model=model,
            )
    except Exception as exc:
        logger.exception("agent-actions: launch task failed run_id=%s", run_id)
        try:
            await asyncio.to_thread(
                record_dispatch_failure, run_id,
                failure_class="launch-task", reason=str(exc)[:400],
            )
        except Exception:
            pass
    finally:
        _pending_agentic_launches.pop(run_id, None)
        # Any exit — RUNNING, failed prep, crashed launch — changes slot
        # occupancy or queue state; wake the drainer immediately.
        _agentic_queue_event.set()


def _inline_dispatch_target_org(body: dict, principal) -> str:
    """The org an asset-less dispatch targets.

    An inline dispatch's natural home is the CALLER'S org — an org-bound
    session dispatching content targets its own database (member lookup,
    source row, workspace routing). Defaulting to "autonomy" here sent
    org-bound callers into target_org_auth_error, whose deliberate
    cross-org masking rendered the refusal as 'asset not found: ' with
    an empty id — the exact ghost the first inline tester chased. An
    explicit body target_org still wins and is still authz-checked.
    """
    explicit = str(body.get("target_org") or "")
    if explicit:
        return explicit
    if getattr(principal, "org_bound", False) and getattr(principal, "org", ""):
        return principal.org
    return "autonomy"


async def api_agent_action_dispatch(request):
    """POST /api/agent-actions/dispatch — spawn an agentic action.

    Body schema (minimal — anything else is derived server-side from
    the resolved source row)::

        {
          "asset_id":          "abc12345-...",       // required
          "member_key":        "note.update-summary",// required
          "target_session_name": "auto-..."          // optional, Send-To only
        }

    The set id is fixed to ``dashboard.agent-actions``; the asset's
    title / short_description / type / org / url come from the
    resolved source row in its home-org DB; ``dispatched_by_session``
    defaults to a ``"dashboard"`` sentinel since browser-initiated
    dispatches don't have a specific operator session id. None of
    those should travel on the wire — the database is the source of
    truth and round-tripping DOM-scraped values invites placeholder
    leakage and stale-state bugs (see source.js page-title fallback).

    The endpoint enforces own-org-of-asset routing — the action's
    Setting member is resolved from the *target asset's* org, not the
    operator's. The agent (when one is spawned) runs in the workspace
    registered to that same org.
    """
    principal = api_auth.principal_from_request(request)
    if not principal.authenticated:
        return JSONResponse({"error": "authentication required"}, status_code=401)

    def target_org_auth_error(target_org):
        if not principal.org_bound or principal.org == target_org:
            return None
        logger.warning(
            "api_authz_refused action=agent.dispatch caller=%s "
            "caller_org=%s owner_org=%s",
            principal.subject,
            principal.org,
            target_org,
        )
        # A remote caller must not learn that an asset exists in another org.
        return JSONResponse({"error": f"asset not found: {asset_id}"}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    set_id = "dashboard.agent-actions"
    member_key = body.get("member_key") or ""
    asset_id = str(body.get("asset_id") or "").strip()
    requested_asset_kind = str(body.get("asset_kind") or "").strip()
    target_session_name = body.get("target_session_name") or ""
    # Optional operator-typed note for Send-To primers. This is genuine
    # user input (not data the server already has), so it travels on
    # the wire. Empty string ⇒ no custom message in the primer body.
    custom_message = str(body.get("custom_message") or "")
    # Optional operator-typed input for ``input_prompt``-declaring actions
    # (schema #2). When the action's payload sets ``input_prompt``, the
    # dashboard pops a modal and forwards the operator's text here; the
    # prompt template then interpolates it via ``{custom_input}``.
    custom_input = str(body.get("custom_input") or "")
    # Browser-initiated dispatches have no specific operator session id.
    # Use a stable sentinel so the agentic source row's
    # ``dispatched_by_session`` is never NULL and the agent's prompt has
    # something to render. A caller holding a session bearer token IS a
    # specific session, so record it -- that is who gets told when the run
    # finishes (see _notify_agentic_dispatch_nag in agents/dispatcher.py).
    dispatched_by_session = "dashboard"
    if principal.subject and principal.kind in (
        api_auth.ApiPrincipalKind.LOCAL_SESSION,
        api_auth.ApiPrincipalKind.ORG_SESSION,
    ):
        dispatched_by_session = principal.subject

    if not member_key or not isinstance(member_key, str):
        return JSONResponse(
            {"error": "member_key required"}, status_code=400,
        )
    if not isinstance(asset_id, str):
        return JSONResponse({"error": "asset_id must be a string"}, status_code=400)
    # Content-carrying dispatch (operator ruling, 2026-08-29): the asset
    # is OPTIONAL — sessions were minting a graph note per dispatch just
    # to satisfy this guard, thousands of them. With no asset, the
    # dispatch carries its content in custom_input and the template
    # renders from {custom_input}; a template that references
    # {asset...}/{source...} fields fails loudly at render time, which IS
    # the target contract — no extra opt-in field needed.
    inline_dispatch = not asset_id
    if inline_dispatch and not custom_input.strip():
        return JSONResponse(
            {"error": "asset_id or custom_input required: an asset-less "
                      "dispatch must carry its content"},
            status_code=400,
        )
    if len(custom_input.encode("utf-8")) > _AGENT_ACTION_INPUT_MAX_BYTES:
        return JSONResponse(
            {"error": f"custom_input exceeds "
                      f"{_AGENT_ACTION_INPUT_MAX_BYTES} bytes"},
            status_code=400,
        )

    if os.environ.get("DASHBOARD_MOCK"):
        from tools.dashboard.dao import mock as dao_mock
        design = None
        if requested_asset_kind == "design":
            for row in dao_mock._designs():
                did = str(row.get("design_id") or row.get("id") or "")
                rid = str(row.get("id") or "")
                if asset_id in {did, rid}:
                    design = row
                    break
            src = None
            bead = None
        else:
            src = dao_mock.get_source(asset_id)
            bead = None if src is not None else dao_mock.get_bead(asset_id)
        if src is None and bead is None and design is None:
            return JSONResponse(
                {"error": f"asset not found: {asset_id}"}, status_code=404,
            )
        if src is not None:
            from tools.dashboard.org_identity import session_org_slug
            target_org = session_org_slug(src)
            asset_id = str(src.get("id") or asset_id)
            asset_type = str(src.get("type") or "")
            asset_title = str(src.get("title") or "")
        else:
            target_org = "autonomy"
            if design is not None:
                asset_id = str((design or {}).get("id") or asset_id)
                asset_type = "design"
                asset_title = str((design or {}).get("title") or asset_id)
            else:
                asset_id = str((bead or {}).get("id") or asset_id)
                asset_type = "bead"
                asset_title = str((bead or {}).get("title") or asset_id)
        auth_error = target_org_auth_error(target_org)
        if auth_error is not None:
            return auth_error
        members = dao_mock.get_settings_members(set_id, org=target_org)
        payload = next(
            (m.get("payload") or {} for m in members if m.get("key") == member_key),
            None,
        )
        if payload is None:
            return JSONResponse(
                {
                    "error": "agent-action member not found",
                    "set_id": set_id,
                    "member_key": member_key,
                    "target_org": target_org,
                },
                status_code=404,
            )
        # Schema #2 ``input_prompt`` contract — same shape as the live
        # branch so the L2.B sweep can assert the modal-required path.
        if str(payload.get("input_prompt") or "").strip():
            if not custom_input.strip():
                return JSONResponse(
                    {"error": "this action requires input but none was provided"},
                    status_code=400,
                )
        if bool(payload.get("universal")) and member_key == "session.send-to":
            if not target_session_name:
                return JSONResponse(
                    {"error": "target_session_name required for Send To"},
                    status_code=400,
                )
            # Surface the primer body in the mock-mode response so the
            # behavioural-sweep fetch spy can assert primer shape without
            # having to wire a real CrossTalk delivery in the browser harness.
            primer_body = _build_send_to_primer(
                asset={
                    "id": asset_id,
                    "type": asset_type,
                    "org": target_org,
                    "title": asset_title,
                },
                custom_message=custom_message,
            )
            return JSONResponse({
                "ok": True,
                "sent_to": target_session_name,
                "primer_body": primer_body,
            })
        return JSONResponse({
            "ok": True,
            "agentic_source_id": f"mock-{member_key}-{asset_id[:8]}",
            "custom_input": custom_input,
        })

    # ── Step 1: resolve the target asset and its owning org ──────
    # to_thread: a captured 23.7s event-loop stall bottomed out in this
    # exact SELECT under lock contention (host handoff 148ead24 item 1) —
    # the bead/design lookups beside it were already wrapped.
    source = (
        None if (inline_dispatch or requested_asset_kind == "design")
        else await asyncio.to_thread(graph_ops.get_source, asset_id)
    )
    bead = None
    design = None
    target_kind = "source"
    target_source_id = ""
    if inline_dispatch:
        target_kind = "inline"
        target_org = _inline_dispatch_target_org(body, principal)
    elif source is None:
        if requested_asset_kind == "design":
            design = await asyncio.to_thread(_resolve_design_action_asset, asset_id)
        else:
            bead = await asyncio.to_thread(dao_beads.get_bead, asset_id)
    if not inline_dispatch and source is None and bead is None and design is None:
        return JSONResponse(
            {"error": f"asset not found: {asset_id}"}, status_code=404,
        )
    if inline_dispatch:
        pass    # target_org set above; nothing to canonicalise
    elif source is not None:
        target_org = source.get("org") or ""
        if not target_org:
            return JSONResponse(
                {"error": "asset has no owning org"}, status_code=409,
            )
        # Canonicalise the asset id from the resolved source. The browser
        # may send a 12-char prefix derived from the URL; templates and the
        # idempotency key downstream want the full UUID so the agent can
        # ``graph read`` cleanly and a re-dispatch within the window is
        # correctly de-duped regardless of how each call addressed the
        # source. The ``asset_id`` variable shadows the body input from
        # here on out.
        asset_id = str(source["id"])
        target_source_id = asset_id
    else:
        target_org = "autonomy"
        if design is not None:
            target_kind = "design"
            asset_id = str(design.get("latest_revision_id") or design.get("id") or asset_id)
        else:
            target_kind = "bead"
        target_source_id = asset_id

    auth_error = target_org_auth_error(target_org)
    if auth_error is not None:
        return auth_error

    # ── Step 2: look up the action member in target_org's DB ─────
    payload = await asyncio.to_thread(
        _resolve_agent_action_member,
        set_id=set_id, member_key=member_key, target_org=target_org,
    )
    if payload is None:
        return JSONResponse(
            {
                "error": "agent-action member not found",
                "set_id": set_id,
                "member_key": member_key,
                "target_org": target_org,
            },
            status_code=404,
        )

    # ── Step 2b: enforce ``input_prompt`` contract ───────────────
    # Schema #2 actions can declare ``input_prompt``; when present, the
    # dashboard must pop a modal and forward ``custom_input``. A request
    # that omits or empties it is malformed — fail fast so the agent
    # never sees an unsubstituted ``{custom_input}`` in its prompt.
    if str(payload.get("input_prompt") or "").strip():
        if not custom_input.strip():
            return JSONResponse(
                {"error": "this action requires input but none was provided"},
                status_code=400,
            )

    # ── Step 3: idempotency window check ─────────────────────────
    # The content hash is ALWAYS part of the key, not just for asset-less
    # dispatches: two rapid dispatches against the same asset with
    # different custom_input are different requests, and the replay cache
    # must not hand the second caller the first one's response.
    _content_hash = hashlib.sha256(custom_input.encode()).hexdigest()
    idem_key = _agent_action_idempotency_key(
        f"{asset_id}:{_content_hash}" if asset_id else _content_hash,
        member_key, dispatched_by_session,
    )
    cached = _agent_action_idempotency_lookup(idem_key)
    if cached is not None:
        logger.warning(
            "agent-actions: duplicate dispatch within %.0fs window key=%s",
            _AGENT_ACTION_IDEMPOTENCY_WINDOW_SEC, idem_key,
        )
        return JSONResponse(cached)

    # ── Step 4a: universal Send-To short-circuit ─────────────────
    if bool(payload.get("universal")) and member_key == "session.send-to":
        if not target_session_name:
            return JSONResponse(
                {"error": "target_session_name required for Send To"},
                status_code=400,
            )
        primer = _build_send_to_primer(
            asset={
                "id": asset_id,
                "type": (
                    str(source.get("type") or "")
                    if source is not None else
                    target_kind
                ),
                "org": target_org,
                "title": (
                    str(source.get("title") or "")
                    if source is not None else
                    str(((design or bead) or {}).get("title") or asset_id)
                ),
            },
            custom_message=custom_message,
        )
        ok, err = await _send_to_via_crosstalk(
            target_session=target_session_name,
            sender_session=dispatched_by_session or "dashboard",
            primer=primer,
            label="Dashboard Send-To",
        )
        if not ok:
            return JSONResponse(
                {"error": err, "target_session": target_session_name},
                status_code=404,
            )
        response = {
            "ok": True,
            "sent_to": target_session_name,
            "primer_body": primer,
        }
        _agent_action_idempotency_remember(idem_key, response)
        return JSONResponse(response)

    # ── Step 4b: non-universal action — workspace lookup ─────────
    explicit_workspace = str(payload.get("workspace") or "").strip()
    if explicit_workspace:
        # Same-org enforcement, independent of the schema-level write guard
        # (auto-2izkp): get_workspace() reads a machine-global cache merged
        # across every org's DB, so an action stored in one org could name
        # another org's workspace and run its prompt against that org's
        # mounted source tree. Checked here, at the org (target_org) that
        # owns THIS action row, with peers=[] so a published/canonical
        # workspace in a different org can't be found by read-through — the
        # same check the write guard performs, kept independent so a row
        # that reaches this table by any path other than the guarded write
        # is still refused at dispatch.
        if settings_ops.read_set_key(
            workspace_settings.WORKSPACE_SET_ID, explicit_workspace,
            org=target_org, peers=[],
        ) is None:
            return JSONResponse(
                {"error": "unknown workspace", "workspace": explicit_workspace},
                status_code=409,
            )
        try:
            workspace = workspace_settings.get_workspace(explicit_workspace)
        except KeyError:
            return JSONResponse(
                {"error": "unknown workspace", "workspace": explicit_workspace},
                status_code=409,
            )
        except workspace_settings.WorkspaceSettingsError as exc:
            return JSONResponse(
                {
                    "error": "workspace config error",
                    "workspace": explicit_workspace,
                    "detail": str(exc),
                },
                status_code=500,
            )
    else:
        workspace = _resolve_workspace_for_org(target_org)
    if workspace is None:
        return JSONResponse(
            {"error": "no workspace for org", "org": target_org},
            status_code=409,
        )

    template = payload.get("prompt_template")
    if not isinstance(template, str) or not template:
        return JSONResponse(
            {
                "error": "agent-action member has no prompt_template",
                "member_key": member_key,
            },
            status_code=409,
        )
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        return JSONResponse(
            {"error": "agent-action member has no model", "member_key": member_key},
            status_code=409,
        )

    # Build the prompt-render context entirely from the resolved asset.
    # The browser sends only ``asset_id`` + ``member_key``; all asset
    # fields come from server-owned data.
    rendered_context = _build_agent_action_context(
        asset_kind=target_kind,
        asset_id=asset_id,
        request=request,
        target_org=target_org,
        source=source,
        bead=bead,
        design=design,
    )

    try:
        rendered_prompt = _render_agent_action_prompt(
            template,
            page_context=rendered_context,
            dispatched_by_session=dispatched_by_session or "",
            member_key=member_key,
            custom_input=custom_input,
        )
    except ValueError as render_err:
        return JSONResponse(
            {
                "error": "prompt template render failed",
                "detail": str(render_err),
                "member_key": member_key,
            },
            status_code=500,
        )

    # ── Queue ceiling only (operator ruling, 2026-08-29): the agentic
    # cap NEVER rejects a dispatch — excess dispatches enqueue as QUEUED
    # rows (visible in the approved-waiting section) and the drainer
    # launches them oldest-first as RUNNING slots free. The one refusal
    # left is a generous absolute queue depth, purely as a runaway
    # backstop.
    queue_depth = await asyncio.to_thread(_agentic_queue_depth)
    if queue_depth >= _AGENTIC_QUEUE_CEILING:
        return JSONResponse(
            {
                "error": (
                    f"agentic dispatch queue is full ({queue_depth} "
                    f"queued; ceiling {_AGENTIC_QUEUE_CEILING}) — this is "
                    f"a runaway backstop, not the concurrency cap; "
                    f"investigate before retrying"
                ),
                "queued": queue_depth,
                "ceiling": _AGENTIC_QUEUE_CEILING,
            },
            status_code=429,
        )

    # ── Step 5: eager-create the agentic source row in target_org ─
    title = str(payload.get("label") or member_key)
    try:
        src = await asyncio.to_thread(
            graph_ops.insert_agentic_session,
            org=target_org,
            set_id=set_id,
            set_revision=int(payload.get("set_revision") or 1),
            member_key=member_key,
            model=model,
            target_source_id=target_source_id,
            target_kind=target_kind,
            target_org=target_org,
            dispatched_by_session=dispatched_by_session or "",
            title=title,
            harness=workspace.harness,
        )
    except Exception as exc:
        logger.exception("agent-actions: insert_agentic_session failed")
        return JSONResponse(
            {"error": "failed to create agentic source", "detail": str(exc)},
            status_code=500,
        )

    # ── Step 6: spawn the agent in target_org's workspace ────────
    started_at = time.time()
    container_name = src["slug"]
    run_id = container_name
    # Pre-compute the run output_dir so we can persist it on the
    # dispatch_runs row at launch — the live-trace endpoint and the
    # completion watcher both read it from there. Mirrors the layout
    # session_launcher would otherwise pick (data/agent-runs/{name}-{ts}).
    _run_ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_dir_path = (
        DATA_ROOT / "agent-runs" / f"{container_name}-{_run_ts}"
    )
    output_dir = str(output_dir_path)

    try:
        from agents.session_launcher import launch_session
    except Exception:
        launch_session = None  # type: ignore[assignment]

    launch_kwargs: dict = {
        "session_type": "agentic",
        "name": container_name,
        "prompt": rendered_prompt,
        "mounts": None,
        "metadata": {
            # Canonical org key: the launcher stamps the session token from
            # metadata["org"] ONLY and refuses to mint without it. The legacy
            # graph_project/graph_org keys it used to read are gone — they fed
            # a fallback chain that let this call site look correct while
            # setting nothing the launcher would honour (auto-23d9m).
            "org": target_org,
            "agentic_source_id": src["id"],
            "set_id": set_id,
            "member_key": member_key,
            "dispatched_by_session": dispatched_by_session or "",
        },
        "detach": True,
        "image": workspace.image,
        "working_dir": "/workspace/repo",
        "harness": workspace.harness,
        "extra_env": None,
        "output_dir": output_dir,
        "model": model,
    }
    if explicit_workspace:
        # The artifact-contract check stays synchronous (cheap stat calls,
        # still off-loop) so a missing artifact remains a crisp 400 at
        # dispatch time. Everything heavier happens in the background task.
        missing_artifacts = await asyncio.to_thread(
            workspace_settings.validate_artifacts, workspace,
        )
        if missing_artifacts:
            first = missing_artifacts[0]
            message = workspace_settings.format_missing_artifact_error(first, workspace)
            return JSONResponse(
                {
                    "error": message,
                    "workspace": workspace.id,
                    "missing_artifacts": [
                        {
                            "name": m.artifact.name,
                            "description": m.artifact.description,
                            "help": m.artifact.help,
                            "expected_path": str(m.path),
                        }
                        for m in missing_artifacts
                    ],
                },
                status_code=400,
            )

    # ── Accept: the run row is born QUEUED and IS the status surface ──
    # (operator directive, 2026-08-28: never hold the POST open across
    # prep + launch — the event loop got frozen behind exactly that, the
    # semaphore wait was invisible, and a client disconnect orphaned the
    # half-created dispatch. Active Dispatches + /api/dispatch/runs show
    # QUEUED -> PREPARING -> RUNNING/FAILED as the background task moves.)
    try:
        from agents.dispatch_db import init_db, insert_launch_run
        await asyncio.to_thread(init_db)
        await asyncio.to_thread(
            functools.partial(
                insert_launch_run,
                run_id=run_id,
                bead_id="",
                started_at=started_at,
                branch="",
                branch_base="",
                image=workspace.image,
                container_name=container_name,
                output_dir=output_dir,
                kind="agentic",
                agentic_source_id=src["id"],
                status="QUEUED",
            )
        )
    except Exception:
        logger.exception(
            "agent-actions: dispatch_runs insert failed run_id=%s", run_id,
        )

    _pending_agentic_launches[run_id] = {
        "workspace": workspace,
        "container_name": container_name,
        "output_dir_path": output_dir_path,
        "output_dir": output_dir,
        "launch_kwargs": launch_kwargs,
        "explicit_workspace": explicit_workspace,
        "model": model,
    }
    _agentic_queue_event.set()
    if os.environ.get("AGENT_ACTIONS_SYNC_LAUNCH"):
        # Deterministic mode for tests: drain inline until this row has
        # left the queue (launched or failed); the accepted contract is
        # identical, the work just completes before the response.
        for _ in range(50):
            started = await _drain_agentic_queue_once()
            pending = [
                t for t in asyncio.all_tasks()
                if t.get_name() == f"agentic-launch-{run_id}"
            ]
            for t in pending:
                await t
            if run_id not in _pending_agentic_launches:
                break
            if started == 0 and not pending:
                # No free slot for this row (cap saturated): it stays
                # QUEUED, exactly as async mode would leave it.
                break

    response = {
        "queued": True,
        "run_id": run_id,
        "agentic_source_id": src["id"],
        "dispatched_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "target_workspace": workspace.id,
        "target_org": target_org,
        "slug": src["slug"],
    }
    _agent_action_idempotency_remember(idem_key, response)
    return JSONResponse(response, status_code=202)


# ── Org registry (graph://d970d946-f95) ──────────────────────


def _enrich_org_identity(detail: dict) -> dict:
    """Resolve identity through the override→canonical→generated cascade.

    Mutates ``detail`` in place to add ``identity_resolved`` carrying the
    final display values; preserves the raw bootstrap row + the raw
    canonical Setting under the original keys.
    """
    from tools.dashboard.org_identity import resolve_org_identity
    org = detail.get("org") or {}
    slug = org.get("slug")
    detail["identity_resolved"] = resolve_org_identity(slug)
    return detail


async def api_orgs_list(request):
    """GET /api/orgs — enumerate orgs with bootstrap row + cascade identity."""
    from tools.graph import org_ops
    orgs = org_ops.list_orgs()
    entries = []
    for ref in orgs:
        detail = org_ops.show_org(ref.slug)
        if detail is None:
            continue
        entries.append(_enrich_org_identity(detail))
    return JSONResponse({"orgs": entries})


def _finding_json(finding) -> dict:
    """One finding, as data rather than as a printed line.

    ``looked_in`` travels with every one because the same question has
    different true answers in different places, and a reader who cannot see
    where it was asked cannot tell a clean result from an unasked one.
    """
    from tools.graph import schemas as _schemas

    remediation_id = getattr(finding, "remediation_id", "")
    remediation_params = getattr(finding, "remediation_params", {}) or {}
    if remediation_id:
        try:
            remediation = _schemas.normalize_remediation_ref({
                "id": remediation_id,
                "params": remediation_params,
            })
        except _schemas.SchemaValidationError:
            # A malformed trusted hook is a code defect, not permission to
            # reflect its unvalidated parameters across the API boundary.
            remediation_id, remediation_params = "", {}
        else:
            remediation_id = remediation["id"]
            remediation_params = remediation["params"]
    return {
        "kind": finding.kind,
        "at": finding.address,
        "what": finding.detail,
        "looked_in": finding.looked_in,
        "severity": getattr(finding, "severity", "blocking"),
        # The same finding as fields rather than as a sentence. ``what`` and
        # ``looked_in`` above are rendered English and stay for the callers
        # that print them; everything below is what a UI should read, so it
        # does not have to parse a quoted path back out of a sentence to put
        # it in a heading.
        "set_id": getattr(finding, "set_id", ""),
        "key": getattr(finding, "key", ""),
        "org": getattr(finding, "org", ""),
        "field": getattr(finding, "field", ""),
        "subject": getattr(finding, "subject", ""),
        "frame": getattr(finding, "frame", ""),
        "name": getattr(finding, "name", ""),
        "description": getattr(finding, "description", ""),
        "help": getattr(finding, "help", ""),
        "expects": getattr(finding, "expects", ""),
        "field_description": getattr(finding, "field_description", ""),
        "remediation_id": remediation_id,
        "remediation_params": dict(remediation_params),
    }


def _things_missing(rows) -> list[dict]:
    """The distinct things missing, each with the workspaces that need it.

    The per-workspace view repeats a shared fact once per workspace: one
    unprovisioned credential rendered as seven separate problems, two host
    variables as six each -- twenty-three rows for six facts, on real data.
    A reader cannot see that setting one variable clears six workspaces,
    which is the only thing they actually wanted to know.

    Identity is (kind, subject): the same missing thing, however many
    declarations point at it. Severity is the WORST any declaration gave it
    -- a thing one workspace treats as optional and another requires is
    required, and reporting the softer answer says a launch will work when
    it will not.
    """
    things: dict[tuple, dict] = {}
    for w in rows:
        for finding in (*w.blocking, *w.unanswerable, *w.advisory):
            data = _finding_json(finding)
            sig = (data["kind"], data["subject"] or data["at"])
            thing = things.get(sig)
            if thing is None:
                thing = things[sig] = {**data, "needed_by": []}
            label = w.name or w.workspace_id
            if label not in thing["needed_by"]:
                thing["needed_by"].append(label)
            if data["severity"] == "blocking":
                thing["severity"] = "blocking"
            # Prefer a declaration that actually carries display metadata:
            # two rows can name the same file and only one describe it.
            for richer in ("name", "description", "help", "field_description"):
                if not thing.get(richer) and data.get(richer):
                    thing[richer] = data[richer]
            if not thing.get("remediation_id") and data.get("remediation_id"):
                thing["remediation_id"] = data["remediation_id"]
                thing["remediation_params"] = data["remediation_params"]
    return list(things.values())


def _things_satisfied(rows) -> list[dict]:
    """Distinct positive checks, without exposing any checked value."""
    from dataclasses import asdict

    things: dict[tuple, dict] = {}
    for workspace in rows:
        for item in workspace.satisfied:
            data = asdict(item)
            sig = (
                data["kind"], data["subject"], data["frame"], data["detail"],
            )
            thing = things.get(sig)
            if thing is None:
                thing = things[sig] = {**data, "used_by": []}
            label = workspace.name or workspace.workspace_id
            if label not in thing["used_by"]:
                thing["used_by"].append(label)
    return list(things.values())


async def api_org_workspace_health(request):
    """GET /api/orgs/<slug>/workspaces/health — what this machine still owes.

    An organization's workspaces arrive with it and say nothing about the
    machine that just joined: the directories, credentials and host variables
    they name are answered locally or not at all. This reports which are
    still unanswered, per workspace.

    Read-only. It provisions nothing and launches nothing.
    """
    from agents import workspace_readiness as readiness

    slug = request.path_params["slug"]
    try:
        rows = await asyncio.to_thread(readiness.org_readiness, slug)
    except Exception as exc:
        logger.exception("workspace health failed for org %s", slug)
        return JSONResponse(
            {"error": f"{type(exc).__name__}: {exc}"}, status_code=502,
        )
    return JSONResponse({
        "org": slug,
        # The frame is a property of the answer, not of any one finding, so
        # it is stated once for the whole report as well: a caller rendering
        # a clean result needs to know whether the question was asked
        # somewhere that could answer it.
        "asked_in": ("a container, which cannot see the platform host's "
                     "filesystem" if settings_ops._running_in_a_container()
                     else "the platform host"),
        # What is actually missing, once. The per-workspace lists below stay
        # for callers that read them, but they are the same facts multiplied
        # by the workspaces that happen to want them.
        "things": _things_missing(rows),
        # Positive evidence travels beside failures so callers can show what
        # was genuinely checked. Values never travel: environment evidence
        # contains a name and source only.
        "satisfied": _things_satisfied(rows),
        "workspaces": [
            {
                "id": w.workspace_id,
                "name": w.name,
                "ready": w.ready,
                "blocking": [_finding_json(f) for f in w.blocking],
                "advisory": [_finding_json(f) for f in w.advisory],
                "unanswerable": [_finding_json(f) for f in w.unanswerable],
                "satisfied": [
                    {
                        "kind": item.kind,
                        "detail": item.detail,
                        "set_id": item.set_id,
                        "key": item.key,
                        "org": item.org,
                        "subject": item.subject,
                        "field": item.field,
                        "frame": item.frame,
                        "name": item.name,
                        "description": item.description,
                        "field_description": item.field_description,
                    }
                    for item in w.satisfied
                ],
            }
            for w in rows
        ],
    })


async def api_orgs_show(request):
    """GET /api/orgs/<slug> — bootstrap row + autonomy.org#1 Setting."""
    from tools.graph import org_ops
    slug = request.path_params["slug"]
    detail = org_ops.show_org(slug)
    if detail is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(_enrich_org_identity(detail))


async def api_orgs_create(request):
    """POST /api/orgs — body: {slug, type?, identity?}.

    Creates the organization SHELL only, and never takes a passphrase
    (I1, auto-jdba4): the org root is generated and the four founding
    events are signed in the operator's browser, then folded via
    ``POST /api/network/ledger/found`` with the sealed org key submitted
    separately. The server therefore never receives the personal password
    and never decrypts the personal root.

    Founding is two calls by construction, not by preference: genesis binds
    the stable ``orgs.id`` (D21), which is minted here, so the browser
    cannot sign the batch until this call returns. ``create_org_shell``
    closes the window between them by being idempotent on an UN-FOUNDED
    shell, so a failed ceremony can be retried against the same org rather
    than stranding a half-created one.
    """
    from tools.graph import org_ops
    body = await request.json()
    slug = body.get("slug")
    if not slug:
        return JSONResponse({"error": "slug required"}, status_code=400)
    type_ = body.get("type", "shared")
    identity_payload = body.get("identity")
    try:
        ref = org_ops.create_org_shell(
            slug, type_=type_, identity_payload=identity_payload,
        )
    except org_ops.OrgExistsError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except org_ops.OrgError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"org": ref.to_dict(), "founded": False}, status_code=201)


async def api_orgs_delete(request):
    """DELETE /api/orgs/<slug>?force=1 — refuses on cross-DB references."""
    auth_error = api_auth.require_global_api_authority(request)
    if auth_error is not None:
        return auth_error

    from tools.graph import org_ops
    slug = request.path_params["slug"]
    force = request.query_params.get("force", "").lower() in ("1", "true", "yes")
    try:
        report = org_ops.remove_org(slug, force=force)
    except org_ops.OrgNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except org_ops.OrgReferencedError as e:
        return JSONResponse(
            {
                "error": str(e),
                "references": [r.to_dict() for r in e.references],
            },
            status_code=409,
        )
    except org_ops.OrgError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(report.to_dict())


async def api_graph_resolve(request):
    """Universal graph entity resolver — sources, attachments, partial ID prefix."""
    id = request.path_params["id"]
    if not _GRAPH_SOURCE_ID_RE.match(id):
        return JSONResponse({"error": f"malformed id: {id!r}"}, status_code=400)

    if os.environ.get("DASHBOARD_MOCK"):
        from tools.dashboard.dao import mock as mock_dao
        result = mock_dao.resolve_source_for_api(id)
        if result:
            _attach_source_org(result)
            return JSONResponse(result)
        att = mock_dao.get_attachment(id)
        if att:
            return JSONResponse({
                "type": "attachment",
                "id": att["id"],
                "filename": att.get("filename", ""),
                "mime_type": att.get("mime_type", ""),
                "size_bytes": att.get("size_bytes", 0),
                "source_id": att.get("source_id", ""),
                "turn": att.get("turn"),
                "created_at": att.get("created_at", ""),
                "url": f"/api/attachment/{att['id'][:12]}",
            })
        return JSONResponse({"error": "not found"}, status_code=404)

    turn_raw = request.query_params.get("turn")
    around_turn: int | None = None
    if turn_raw is not None:
        try:
            around_turn = int(turn_raw)
        except ValueError:
            return JSONResponse({"error": "invalid turn"}, status_code=400)
    try:
        window = int(request.query_params.get("window", "5"))
    except ValueError:
        return JSONResponse({"error": "invalid window"}, status_code=400)

    # ?from=-N → tail-read the last N turns. Positive ``from`` is reserved
    # for a future forward-range mode and rejected here so callers don't
    # silently get the front-of-source slice instead.
    from_raw = request.query_params.get("from")
    tail_n: int | None = None
    if from_raw is not None:
        try:
            from_val = int(from_raw)
        except ValueError:
            return JSONResponse({"error": "invalid from"}, status_code=400)
        if from_val >= 0:
            return JSONResponse(
                {"error": "from must be negative (e.g. from=-7)"},
                status_code=400,
            )
        tail_n = -from_val

    org = api_auth.organization_scope_from_request(request)
    source = await asyncio.to_thread(graph_ops.get_source, id, org=org)
    if source:
        source = await _refresh_graph_session_source(source)
        # Page-load is unbounded by design — full source for the browser.
        # The LLM-context cap belongs to _resolve_primer, which calls
        # ops.read_source_full directly with an explicit max_chars. The
        # ``?max_chars=`` query param is intentionally not parsed here;
        # the route does not accept it.
        result = await asyncio.to_thread(
            graph_ops.read_source_full, source["id"], org=org, max_chars=0,
            around_turn=around_turn, window=window, tail_n=tail_n,
        )
        if result is None:
            result = {"source": source, "entries": [], "truncated": False,
                      "total_chars": 0}
        _attach_source_org(result)
        if source.get("type") == "note":
            # The header renders an ``@vN`` chip from this; without it the
            # revision a reader is looking at is invisible.
            result["version_count"] = await asyncio.to_thread(
                _note_version_count, source["id"], org,
            )
        return JSONResponse(result)
    att = await asyncio.to_thread(graph_ops.get_attachment, id, org=org)
    if att:
        return JSONResponse({
            "type": "attachment",
            "id": att["id"],
            "filename": att["filename"],
            "mime_type": att["mime_type"],
            "size_bytes": att["size_bytes"],
            "source_id": att["source_id"],
            "turn": att.get("turn"),
            "created_at": att["created_at"],
            "url": f"/api/attachment/{att['id'][:12]}",
        })
    comment = graph_ops.get_comment(id)
    if comment:
        return JSONResponse({
            "type": "comment",
            "id": comment["id"],
            "source_id": comment["source_id"],
            "content": comment["content"],
            "actor": comment.get("actor", "user"),
            "created_at": comment.get("created_at", ""),
            "integrated": bool(comment.get("integrated", 0)),
            "anchor": comment.get("anchor"),
            "redirect": f"/graph/{comment['source_id'][:12]}?highlight={comment['id'][:12]}",
        })
    body = await asyncio.to_thread(_graph_not_found_body, id, org)
    return JSONResponse(body, status_code=404)


async def api_resolve_embed(request):
    """Resolve a ![[id]] embed reference for the dashboard renderer.

    Returns type, attachment URL, alt-text, and mime type for rendering iframes/images/toggles.
    """
    embed_id = request.path_params["id"]
    if not _GRAPH_SOURCE_ID_RE.match(embed_id):
        return JSONResponse({"error": f"malformed id: {embed_id!r}"}, status_code=400)

    version = request.query_params.get("version")

    if os.environ.get("DASHBOARD_MOCK"):
        from tools.dashboard.dao import mock as mock_dao
        result = mock_dao.resolve_embed(embed_id, version)
        if result:
            return JSONResponse(result)
        return JSONResponse({"error": "not found"}, status_code=404)

    embed = graph_ops.resolve_embed(embed_id, version=version)
    if embed:
        return JSONResponse(embed)
    return JSONResponse({"error": "not found"}, status_code=404)


def _graph_db_path() -> str | None:
    """Resolve graph DB path: GRAPH_DB env var, then default."""
    import os
    return os.environ.get("GRAPH_DB") or None


def _checkpoint_graph():
    """Flush WAL to main DB so immutable=1 readers see current data."""
    graph_ops.checkpoint()


async def api_graph_stream(request):
    """List notes matching a tag as a chronological feed."""
    if os.environ.get("DASHBOARD_MOCK"):
        tag = request.path_params["tag"]
        limit = int(request.query_params.get("limit", "50"))
        items = dao_beads.get_stream_items(tag, limit)
        return JSONResponse({"tag": tag, "count": len(items), "items": items})

    tag = request.path_params["tag"]
    limit = int(request.query_params.get("limit", "50"))
    offset = int(request.query_params.get("offset", "0"))
    items = graph_ops.stream_get(tag, limit=limit, offset=offset)
    return JSONResponse({"tag": tag, "count": len(items), "items": items})


async def api_graph_streams(request):
    """List active tag streams with note counts, descriptions, and last_active."""
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"streams": dao_beads.get_streams()})
    return JSONResponse({"streams": graph_ops.streams_summary()})


async def api_graph_notes(request):
    """List recent notes as structured JSON (cross-org chronological merge).

    Backs the /collab "Recent" tab. Unlike ``/api/graph/collab`` (which is
    pinned to ``collab``-tagged sources), this returns notes regardless of
    tag, ordered by ``created_at`` DESC. Optional filters: ``?since=24h``,
    ``?tags=a,b``, ``?only_org=<slug>``, ``?limit=N``. Caller org is read
    from the ``X-Graph-Org`` header.
    """
    if os.environ.get("DASHBOARD_MOCK"):
        limit = int(request.query_params.get("limit", "50"))
        return JSONResponse({"notes": dao_beads.get_recent_notes(limit)})

    limit = int(request.query_params.get("limit", "50"))
    since_param = request.query_params.get("since")
    since_iso = _parse_range(since_param) if since_param else None
    tags_param = request.query_params.get("tags")
    tags = [t for t in tags_param.split(",") if t] if tags_param else None
    only_org = request.query_params.get("only_org")
    org = api_auth.organization_scope_from_request(request)
    notes = graph_ops.list_notes(
        org=org, only_org=only_org, since=since_iso, tags=tags, limit=limit,
    )
    items = []
    for s in notes:
        meta_raw = s.get("metadata")
        meta = json.loads(meta_raw) if isinstance(meta_raw, str) else (meta_raw or {})
        items.append({
            "id": s["id"],
            "title": s.get("title", ""),
            "short_description": s.get("short_description"),
            "created_at": s.get("created_at", ""),
            "author": meta.get("author", ""),
            "project": s.get("project", ""),
            "org": s.get("org", ""),
            "tags": meta.get("tags", []),
            "source_type": s.get("type", "note"),
            "preview": (s.get("title", "") or "")[:140],
        })
    return JSONResponse({"notes": items})


async def api_graph_collab_list(request):
    """List collab-tagged notes as structured JSON."""
    if os.environ.get("DASHBOARD_MOCK"):
        limit = int(request.query_params.get("limit", "20"))
        return JSONResponse({"notes": dao_beads.get_collab_notes(limit)})

    limit = int(request.query_params.get("limit", "20"))
    sources = graph_ops.list_collab_sources(limit=limit)
    items = []
    for s in sources:
        meta = json.loads(s["metadata"]) if isinstance(s.get("metadata"), str) else (s.get("metadata") or {})
        items.append({
            "id": s["id"],
            "title": s.get("title", ""),
            "short_description": s.get("short_description"),
            "created_at": meta.get("created_at", s.get("created_at", "")),
            "author": meta.get("author", ""),
            "project": s.get("project", ""),
            "tags": meta.get("tags", []),
            "comment_count": s.get("comment_count", 0),
            "version": meta.get("version", 1),
        })
    return JSONResponse({"notes": items})


async def api_graph_collab_tag(request):
    """Add the 'collab' tag to an existing source."""
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"ok": True, "output": "  \u2713 Mock: tag operation skipped"})
    source_id = request.path_params["source_id"]
    if not _GRAPH_SOURCE_ID_RE.match(source_id):
        return JSONResponse({"error": f"malformed source_id: {source_id!r}"}, status_code=400)
    source = graph_ops.get_source(source_id)
    if not source:
        return JSONResponse({"error": f"no source found matching '{source_id}'"}, status_code=404)
    if isinstance(source, list):
        return JSONResponse({"error": f"multiple sources match '{source_id}' — use a longer prefix"}, status_code=400)
    added = graph_ops.add_tag(source["id"], "collab")
    title = (source.get("title") or "?")[:60]
    if added:
        msg = f"  \u2713 Tagged {source['id'][:12]} \"{title}\" as collab"
    else:
        msg = f"  Already tagged: {source['id'][:12]} \"{title}\""
    _checkpoint_graph()
    return JSONResponse({"ok": True, "output": msg})


async def api_graph_tag_add(request):
    """Add a tag to a source."""
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"ok": True, "output": "  \u2713 Mock: tag add operation skipped"})
    source_id = request.path_params["source_id"]
    tag_name = request.path_params["tag_name"]
    if not _GRAPH_SOURCE_ID_RE.match(source_id):
        return JSONResponse({"error": f"malformed source_id: {source_id!r}"}, status_code=400)
    if not _GRAPH_TAGS_RE.match(tag_name):
        return JSONResponse({"error": f"malformed tag name: {tag_name!r}"}, status_code=400)
    source = graph_ops.get_source(source_id)
    if not source:
        return JSONResponse({"error": f"no source found matching '{source_id}'"}, status_code=404)
    if isinstance(source, list):
        return JSONResponse({"error": f"multiple sources match '{source_id}' — use a longer prefix"}, status_code=400)
    added = graph_ops.add_tag(source["id"], tag_name)
    title = (source.get("title") or "?")[:60]
    if added:
        msg = f"  ✓ Tagged {source['id'][:12]} \"{title}\" ← {tag_name}"
    else:
        msg = f"  Already tagged: {source['id'][:12]} \"{title}\" ← {tag_name}"
    _checkpoint_graph()
    return JSONResponse({"ok": True, "added": bool(added), "output": msg})


async def api_graph_tag_remove(request):
    """Remove a tag from a source."""
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"ok": True, "output": "  \u2713 Mock: tag remove operation skipped"})
    source_id = request.path_params["source_id"]
    tag_name = request.path_params["tag_name"]
    if not _GRAPH_SOURCE_ID_RE.match(source_id):
        return JSONResponse({"error": f"malformed source_id: {source_id!r}"}, status_code=400)
    if not _GRAPH_TAGS_RE.match(tag_name):
        return JSONResponse({"error": f"malformed tag name: {tag_name!r}"}, status_code=400)
    source = graph_ops.get_source(source_id)
    if not source:
        return JSONResponse({"error": f"no source found matching '{source_id}'"}, status_code=404)
    if isinstance(source, list):
        return JSONResponse({"error": f"multiple sources match '{source_id}' — use a longer prefix"}, status_code=400)
    removed = graph_ops.remove_tag(source["id"], tag_name)
    title = (source.get("title") or "?")[:60]
    if removed:
        msg = f"  ✓ Untagged {source['id'][:12]} \"{title}\" ✗ {tag_name}"
    else:
        msg = f"  Not tagged: {source['id'][:12]} \"{title}\" ✗ {tag_name}"
    _checkpoint_graph()
    return JSONResponse({"ok": True, "removed": bool(removed), "output": msg})


async def api_graph_tag_merge(request):
    """Merge one tag into another."""
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"ok": True, "output": "  \u2713 Mock: tag merge operation skipped"})
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    from_tag = (body.get("from") or "").strip()
    to_tag = (body.get("to") or "").strip()
    if not from_tag or not to_tag:
        return JSONResponse({"error": "'from' and 'to' are required"}, status_code=400)
    reason = (body.get("reason") or "").strip()
    force = body.get("force", False)
    result = graph_ops.tag_merge(from_tag, to_tag, reason=reason, force=force)
    if "error" in result:
        return JSONResponse({"error": result["error"]}, status_code=result.get("status", 400))
    retagged = result["count"]
    note_id = result["note_id"]
    msg = (f"  ✓ Merged '{from_tag}' → '{to_tag}' ({retagged} sources retagged)\n"
           f"  Provenance: graph://{note_id[:12]}")
    _checkpoint_graph()
    return JSONResponse({
        "ok": True,
        "output": msg,
        "count": retagged,
        "note_id": note_id,
    })


async def api_graph_thought(request):
    """Create a thought capture via API proxy."""
    from tools.graph.models import new_id
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    content = (body.get("content") or "").strip()
    if not content:
        return JSONResponse({"error": "content is required"}, status_code=400)
    if len(content) > _GRAPH_MAX_CONTENT:
        return JSONResponse({"error": f"content exceeds {_GRAPH_MAX_CONTENT} bytes"}, status_code=400)
    thread_id = body.get("thread_id")
    source_id = body.get("source_id")
    turn_number = body.get("turn_number")
    actor = body.get("actor", "user")
    if source_id and not _GRAPH_SOURCE_ID_RE.match(source_id):
        return JSONResponse({"error": f"malformed source_id: {source_id!r}"}, status_code=400)
    if thread_id and not _GRAPH_SOURCE_ID_RE.match(thread_id):
        return JSONResponse({"error": f"malformed thread_id: {thread_id!r}"}, status_code=400)
    if thread_id:
        thread = graph_ops.get_thread(thread_id)
        if not thread:
            return JSONResponse({"error": f"thread not found: {thread_id}"}, status_code=404)
        thread_id = thread["id"]
    # Honour client-supplied capture_id so the CLI's printed id matches
    # what thoughts-list returns. Generate only if not supplied.
    capture_id = (body.get("capture_id") or "").strip() or new_id()
    graph_ops.insert_capture(
        capture_id, content,
        source_id=source_id,
        turn_number=int(turn_number) if turn_number else None,
        thread_id=thread_id,
        actor=actor,
    )
    msg = f"  \u2713 Captured: {capture_id[:11]}"
    _checkpoint_graph()
    return JSONResponse({"ok": True, "output": msg, "id": capture_id})


async def api_graph_thread(request):
    """Create a thread via API proxy."""
    from tools.graph.models import new_id
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    title = (body.get("title") or "").strip()
    if not title:
        return JSONResponse({"error": "title is required"}, status_code=400)
    if len(title) > 500:
        return JSONResponse({"error": "title too long (max 500)"}, status_code=400)
    priority = int(body.get("priority", 1))
    actor = body.get("created_by") or body.get("actor", "user")
    # Honour client-supplied thread_id so the CLI's printed id matches
    # what lists return. Generate only if the client didn't send one.
    thread_id = (body.get("thread_id") or "").strip() or new_id()
    graph_ops.insert_thread(thread_id, title, priority=priority, created_by=actor)
    msg = f"  \u2713 Thread: {thread_id[:11]} \"{title}\" [active, P{priority}]"
    _checkpoint_graph()
    return JSONResponse({"ok": True, "output": msg, "id": thread_id})


async def api_graph_thread_action(request):
    """Thread actions (park/done/active/assign/attach) via API proxy."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    action = body.get("action", "")
    thread_id = body.get("thread_id", "")
    target = body.get("target")

    if action not in ("park", "done", "active", "assign", "attach"):
        return JSONResponse({"error": f"unknown action: {action}"}, status_code=400)

    if action in ("park", "done", "active"):
        thread = graph_ops.update_thread_status(
            thread_id, "parked" if action == "park" else action,
        )
        title = thread["title"] if thread else thread_id
        return JSONResponse({"ok": True, "output": f"  \u2713 {action.capitalize()}: {thread_id} \"{title}\"\n"})
    if not target:
        return JSONResponse({"error": "assign requires target thread_id"}, status_code=400)
    graph_ops.assign_capture_to_thread(thread_id, target)
    return JSONResponse({"ok": True, "output": f"  \u2713 Assigned {thread_id} \u2192 thread {target}\n"})


async def api_graph_thoughts(request):
    """List thought captures as structured JSON."""
    if os.environ.get("DASHBOARD_MOCK"):
        limit = int(request.query_params.get("limit", "50"))
        thread_id = request.query_params.get("thread")
        since_param = request.query_params.get("since")
        return JSONResponse({"thoughts": dao_beads.get_thoughts(limit, thread_id, since_param)})

    limit = int(request.query_params.get("limit", "50"))
    thread_id = request.query_params.get("thread")
    since_param = request.query_params.get("since")
    since_iso = _parse_range(since_param) if since_param else None
    all_mode = not thread_id and not request.query_params.get("inbox")
    captures = graph_ops.list_captures(
        thread_id=thread_id,
        status="*" if all_mode else None,
        since=since_iso,
        limit=limit,
    )
    items = [
        {
            "id": c["id"],
            "content": c.get("content", ""),
            "status": c.get("status", "captured"),
            "thread_id": c.get("thread_id"),
            "source_id": c.get("source_id"),
            "turn_number": c.get("turn_number"),
            "created_at": c.get("created_at", ""),
        }
        for c in captures
    ]
    return JSONResponse({"thoughts": items})


async def api_graph_threads(request):
    """List threads as structured JSON."""
    if os.environ.get("DASHBOARD_MOCK"):
        limit = int(request.query_params.get("limit", "20"))
        status = request.query_params.get("status", "active")
        show_all = request.query_params.get("all")
        return JSONResponse({"threads": dao_beads.get_threads(limit, status=None if show_all else status)})

    limit = int(request.query_params.get("limit", "20"))
    status = request.query_params.get("status", "active")
    show_all = request.query_params.get("all")
    threads = graph_ops.list_threads(status=None if show_all else status, limit=limit)
    items = [
        {
            "id": t["id"],
            "title": t.get("title", ""),
            "status": t.get("status", "active"),
            "priority": t.get("priority", 1),
            "capture_count": t.get("capture_count", 0),
            "created_at": t.get("created_at", ""),
            "updated_at": t.get("updated_at", ""),
        }
        for t in threads
    ]
    return JSONResponse({"threads": items})


async def api_journal(request):
    """List journal entries with three zoom levels."""
    limit = int(request.query_params.get("limit", "50"))
    since_param = request.query_params.get("since")
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"entries": dao_beads.get_journal_entries(since=since_param, limit=limit)})
    since_iso = _parse_range(since_param) if since_param else None
    return JSONResponse({"entries": graph_ops.list_journal_entries(since=since_iso, limit=limit)})


async def api_graph_journal_write(request):
    """Write a journal entry via graph CLI proxy."""
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"ok": True, "output": "  \u2713 Mock: journal write skipped"})

    body = await request.json()

    # Validate required fields
    for field in ("compact", "normal", "timestamp_start", "timestamp_end"):
        if field not in body:
            return JSONResponse({"error": f"missing required field: {field}"}, status_code=400)

    org = api_auth.organization_scope_from_request(request)
    try:
        result = await asyncio.to_thread(
            graph_ops.write_journal_entry, body, org=org,
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    _checkpoint_graph()
    return JSONResponse({
        "ok": True,
        "source_id": result["source_id"],
        "edge_count": result["edge_count"],
        "org": result["org"],
    })


async def api_graph_collab_tag_describe(request):
    """Set or update a tag description via API proxy."""
    if os.environ.get("DASHBOARD_MOCK"):
        return JSONResponse({"ok": True, "output": "  \u2713 Mock: tag describe operation skipped"})
    tag_name = request.path_params["name"]
    if not _GRAPH_TAGS_RE.match(tag_name):
        return JSONResponse({"error": f"malformed tag name: {tag_name!r}"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    description = (body.get("description") or "").strip()
    if not description:
        return JSONResponse({"error": "description is required"}, status_code=400)
    if len(description) > _GRAPH_MAX_CONTENT:
        return JSONResponse({"error": f"description exceeds {_GRAPH_MAX_CONTENT} bytes"}, status_code=400)
    actor = body.get("actor", "user")
    graph_ops.update_tag_description(tag_name, description, actor=actor)
    msg = f"  \u2713 Tag '{tag_name}': {description[:60]}"
    _checkpoint_graph()
    return JSONResponse({"ok": True, "output": msg})


# ── Plugin route handlers ─────────────────────────────────────


def _make_plugin_page_handler(plugin_id: str):
    async def handler(request):
        if not _plugin_enabled_map().get(plugin_id):
            return PlainTextResponse("Not Found", status_code=404)
        return HTMLResponse(_load_template("base.html"))

    handler.__name__ = f"page_plugin_{plugin_id}"
    return handler


def _make_plugin_fragment_handler(plugin_id: str, template_name: str):
    async def handler(request):
        if not _plugin_enabled_map().get(plugin_id):
            return PlainTextResponse("Not Found", status_code=404)
        return templates.TemplateResponse(request, template_name)

    handler.__name__ = f"page_plugin_{plugin_id}_fragment"
    return handler


async def api_plugins(request):
    """Return enabled plugins with sidebar metadata + their effective org.

    Shape: ``{plugins: [{id, label, path, paths, badge_color, alpine_root, org,
    asset_rev, has_style, sidebar, identity_menu, identity_detail, voice}]}``
    — one entry per currently-enabled plugin. Each plugin's toggle row
    is read from *its own* ``manifest.org``'s DB, so unscoped browser
    requests still see the canonical state (substrate v1.1 fix). The
    ``org`` field is the runtime install scope: operator override
    (``payload.org``) when set, else ``manifest.org``. The browser
    stamps it as ``X-Graph-Org`` on plugin-originated fetches.
    """
    try:
        force_settings = (
            request.query_params.get("force_settings", "").lower()
            in {"1", "true", "yes"}
        )
        await asyncio.to_thread(
            plugin_loader.reconcile_declared_settings,
            PLUGIN_REGISTRY,
            force=force_settings,
        )
    except Exception:
        logger.exception("plugin declared-settings reconcile failed")
    cache: dict[str, dict[str, dict]] = {}
    out = []
    for idx, p in enumerate(PLUGIN_REGISTRY):
        manifest_org = p.manifest.org
        if manifest_org not in cache:
            cache[manifest_org] = plugin_loader._read_plugin_settings(
                org=manifest_org,
            )
        settings = cache[manifest_org]
        if not plugin_loader.is_enabled(
            p.id, p.plugin_dir, settings, manifest=p.manifest,
        ):
            continue
        payload = settings.get(p.id) or {}
        override = payload.get("org")
        effective_org = (
            override if isinstance(override, str) and override else manifest_org
        )
        out.append({
            "id": p.id,
            "label": p.nav_label,
            "path": p.paths[0],
            "paths": p.paths,
            "badge_color": _plugin_badge_color(idx),
            "alpine_root": p.alpine_root,
            "voice": p.manifest.frontend.voice.model_dump(),
            "org": effective_org,
            "asset_rev": _plugin_asset_rev(p),
            "has_style": bool(p.style),
            "sidebar": p.manifest.nav.sidebar,
            "identity_menu": p.manifest.nav.identity_menu,
            "identity_detail": p.manifest.nav.identity_detail,
            "capability": (
                p.manifest.capability.model_dump()
                if p.manifest.capability is not None else None
            ),
        })
    return JSONResponse({"plugins": out})


async def api_session_contributions(request):
    """Aggregate enabled plugin chrome for a batch of session ids.

    Core never asks how a session relates to a design, mission, or future
    plugin. Each declared callback receives the full batch plus the already
    authenticated request, and returns only the descriptors it owns.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_ids = body.get("session_ids")
    if not isinstance(raw_ids, list):
        return JSONResponse({"error": "session_ids must be a list"}, status_code=400)
    session_ids: list[str] = []
    seen: set[str] = set()
    for raw in raw_ids:
        session_id = str(raw or "").strip()
        if not session_id or len(session_id) > 256 or session_id in seen:
            continue
        seen.add(session_id)
        session_ids.append(session_id)
        if len(session_ids) == 100:
            break

    contributions: dict[str, list[dict[str, str]]] = {
        session_id: [] for session_id in session_ids
    }
    enabled = _plugin_enabled_map()
    for plugin in PLUGIN_REGISTRY:
        callback = plugin.session_contributions
        if callback is None or not enabled.get(plugin.id):
            continue
        try:
            plugin_result = await asyncio.to_thread(callback, session_ids, request)
        except Exception:
            logger.exception("[plugin %s] session_contributions raised", plugin.id)
            continue
        if not isinstance(plugin_result, dict):
            logger.warning(
                "[plugin %s] session_contributions returned %s, expected dict",
                plugin.id,
                type(plugin_result).__name__,
            )
            continue
        for session_id in session_ids:
            rows = plugin_result.get(session_id) or []
            if not isinstance(rows, list):
                continue
            for row in rows:
                normalized = normalize_session_contribution(
                    plugin.id,
                    session_id,
                    row,
                )
                if normalized is not None:
                    contributions[session_id].append(normalized)
    return JSONResponse({"sessions": contributions})


async def api_plugin_skill(request):
    """GET /api/plugins/{plugin_id}/skill — a plugin's agent-facing doc, if any.

    Serves the raw contents of the file ``manifest.skill`` points at
    (convention: ``SKILL.md``), the same text embedded into every session's
    workspace primer under "Dashboard Apps" while the plugin is enabled.
    Lets a session already mid-conversation re-read it on demand rather
    than needing a fresh primer. 404 when the plugin doesn't declare a
    skill doc, isn't enabled, or the file is missing.
    """
    plugin_id = request.path_params["plugin_id"]
    if not _plugin_enabled_map().get(plugin_id):
        return PlainTextResponse("Not Found", status_code=404)
    plugin = next((p for p in PLUGIN_REGISTRY if p.id == plugin_id), None)
    if plugin is None or not plugin.manifest.skill:
        return PlainTextResponse("Not Found", status_code=404)
    skill_path = plugin.plugin_dir / plugin.manifest.skill
    if not skill_path.is_file():
        return PlainTextResponse("Not Found", status_code=404)
    return PlainTextResponse(skill_path.read_text())


def _build_plugin_routes() -> list:
    """Generate page shell, fragment, api, and static routes per plugin."""
    out: list = []
    for p in PLUGIN_REGISTRY:
        for path in p.paths:
            page_handler = _make_plugin_page_handler(p.id)
            out.append(Route(path, page_handler))
            out.append(Route(f"{path}/{{path:path}}", page_handler))
        out.append(Route(
            f"/pages/{p.id}",
            _make_plugin_fragment_handler(p.id, f"plugins/{p.id}/{p.template}"),
        ))
        if p.routes:
            # Plugin routes are authenticated by construction: the plugin
            # infrastructure wraps them, plugins never add auth themselves.
            # plugin=True refuses an unauthenticated caller unconditionally
            # (an unenrolled dashboard exposes no plugin routes). The
            # enablement gate sits inside the auth wrapper, so dormant
            # means dormant for the API surface too — same live-Setting
            # semantics as the page and fragment handlers.
            out.extend(route_policy.apply_default_deny(
                route_policy.gate_plugin_enabled(
                    p.id, p.routes, _plugin_enabled_map),
                plugin=True))
        out.append(Mount(
            f"/static/plugins/{p.id}",
            app=StaticFiles(directory=str(p.plugin_dir)),
            name=f"plugin-static-{p.id}",
        ))
    return out


def _plugin_asset_rev(plugin) -> str:
    """Cheap revision token for plugin page assets.

    Used by the SPA shell to invalidate cached fragments and reload a
    plugin's page.js after live deploys. The token changes whenever the
    plugin manifest or any declared page asset changes on disk.
    """
    parts: list[str] = []
    candidates = [
        plugin.plugin_dir / "plugin.yaml",
        plugin.plugin_dir / plugin.template,
        plugin.plugin_dir / plugin.script,
    ]
    if plugin.style:
        candidates.append(plugin.plugin_dir / plugin.style)
    for path in candidates:
        try:
            st = path.stat()
            parts.append(
                f"{path.name}:{st.st_size}:{st.st_mtime_ns}"
            )
        except OSError:
            parts.append(f"{path.name}:missing")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


# ── App ───────────────────────────────────────────────────────

routes = [
    Route("/api/ping", api_ping),
    Route("/api/health", api_health),
    Route("/api/operator/active", api_operator_active, methods=["POST"]),
    Route("/api/voice/diag", api_voice_diag, methods=["POST"]),
    Route("/api/voice/trace", api_voice_trace, methods=["POST"]),
    # Pages
    Route("/", page_index),
    # Layer-0 harness bootstrap (bead auto-n130b) — pre-agent first-launch gate.
    Route("/bootstrap", page_bootstrap),
    Route("/api/bootstrap/probe", api_bootstrap_probe),
    Route("/api/bootstrap/verify", api_bootstrap_verify, methods=["POST"]),
    Route("/api/bootstrap/install", api_bootstrap_install, methods=["POST"]),
    # Layer-1 onboarding empty-state (bead auto-inpkd) — identity/org/workspace.
    Route("/welcome", page_welcome),
    Route("/beads", page_beads),
    Route("/pages/beads", page_beads_fragment),
    Route("/dispatch", page_dispatch),
    Route("/dispatch/alpine", page_dispatch),
    Route("/dispatch/lit", page_dispatch),
    Route("/pages/dispatch", page_dispatch_fragment),
    Route("/sessions", page_sessions),
    Route("/pages/sessions", page_sessions_fragment),
    Route("/worktrees", page_worktrees),
    Route("/pages/worktrees", page_worktrees_fragment),
    Route("/pages/bead", page_bead_fragment),
    Route("/pages/timeline", page_timeline_fragment),
    Route("/pages/trace", page_trace_fragment),
    Route("/search", page_search),
    Route("/pages/search", page_search_fragment),
    Route("/streams", page_streams),
    Route("/pages/streams", page_streams_fragment),
    Route("/collab", page_collab),
    Route("/pages/collab", page_collab_fragment),
    Route("/stream/{tag}", page_stream),
    Route("/pages/stream", page_stream_fragment),
    Route("/graph/{id}", page_source),
    Route("/source/{id}", page_source_redirect),
    Route("/pages/source", page_source_fragment),
    Route("/bead/{id}", page_bead),
    Route("/timeline", page_timeline),
    Route("/activity", page_timeline),
    Route("/terminal", page_terminal),
    Route("/terminal/{session_id}", page_terminal),
    Route("/pages/terminal", page_terminal_fragment),
    Route("/session/{session_id}", page_session_view_by_name),
    Route("/session/{project}/{session_id}", page_session_view),
    Route("/pages/session-view", page_session_view_fragment),
    Route("/test/input", page_test_input),
    Route("/_admin/voice-smoke", page_voice_smoke),
    Route("/web-push-proof", page_web_push_proof),
    Route("/service-worker.js", web_push_proof.service_worker),
    # /api/test/* dev-prototype routes are registered below, ONLY under
    # DASHBOARD_MOCK — suppressed in production (auto-1wwpf.6).

    # WebSocket
    WebSocketRoute("/ws/terminal", ws_terminal),
    WebSocketRoute("/ws/voice", ws_voice),

    # Events (SSE)
    Route("/api/internal/restart-notice", api_internal_restart_notice, methods=["POST"]),
    Route("/api/events", api_events),
    Route("/api/events/replay", api_events_replay),
    Route("/api/web-push/proof/config", web_push_proof.api_config),
    Route("/api/web-push/proof/send", web_push_proof.api_send, methods=["POST"]),
    *web_push_routes.ROUTES,
    *web_push.ROUTES,

    # Diag round-trip — file/server/bus/client alignment
    Route("/api/diag/sessions", api_diag_sessions),
    Route("/api/diag/client", api_diag_client, methods=["POST"]),
    Route("/api/diag/store-dump", api_diag_store_dump),
    Route("/api/diag/eventbus/snapshot", api_diag_eventbus_snapshot, methods=["POST"]),
    Route("/api/diag/settings", api_diag_settings),
    Route("/api/diag/settings/sets", api_diag_settings_sets),
    Route("/api/diag/settings/sets/{set_id}", api_diag_settings_set_detail),
    Route("/api/diag/settings_mediator", api_diag_settings_mediator),

    # API
    Route("/api/beads/ready", api_beads_ready),
    Route("/api/beads/list", api_beads_list),
    Route("/api/beads/search", api_beads_search),
    Route("/api/bead/{id}", api_bead_show),
    Route("/api/bead/{id}/tree", api_bead_tree),
    Route("/api/bead/{id}/deps", api_bead_deps),
    Route("/api/bead/{id}/approve", api_bead_approve, methods=["POST"]),
    Route("/api/pinned", api_pinned_beads),
    Route("/api/librarians/jobs", api_librarian_enqueue, methods=["POST"]),
    Route("/api/dispatch/pause", api_dispatch_pause_get),
    Route("/api/dispatch/pause", api_dispatch_pause_post, methods=["POST"]),
    Route("/api/dispatch/limits", api_dispatch_limits_get),
    Route("/api/dispatch/limits", api_dispatch_limits_post, methods=["POST"]),
    Route("/api/dispatch/resume", api_dispatch_resume, methods=["POST"]),
    Route("/api/dispatch/resume/{bead_id}", api_dispatch_resume_bead, methods=["POST"]),
    Route("/api/dispatch/pause-state", api_dispatch_pause_state),
    Route("/api/dispatch/status", api_dispatch_status),
    Route("/api/dispatch/approved", api_dispatch_approved),
    Route("/api/dispatch/runs", api_dispatch_runs),
    Route("/api/dispatch/runs/{run_id}/commit-detail", api_dispatch_run_commit_detail),
    Route("/api/dispatch/reset/{bead_id}", api_dispatch_reset, methods=["POST"]),
    Route("/api/dispatch/wait/{bead_id}", api_dispatch_wait),
    Route("/api/dispatch/trace/{run}", api_dispatch_trace),
    Route("/dispatch/trace/{run}", page_dispatch),
    Route("/api/search", api_search),
    Route("/api/sources", api_sources),
    Route("/api/graph/streams", api_graph_streams, methods=["GET"]),
    Route("/api/graph/stream/{tag}", api_graph_stream, methods=["GET"]),
    Route("/api/graph/notes", api_graph_notes, methods=["GET"]),
    Route("/api/graph/collab", api_graph_collab_list, methods=["GET"]),
    Route("/api/graph/collab/tag/{source_id}", api_graph_collab_tag, methods=["PUT"]),
    Route("/api/graph/collab/tag-describe/{name}", api_graph_collab_tag_describe, methods=["PUT"]),
    Route("/api/graph/tag/merge", api_graph_tag_merge, methods=["POST"]),
    Route("/api/graph/tag/{source_id}/{tag_name}", api_graph_tag_add, methods=["PUT"]),
    Route("/api/graph/tag/{source_id}/{tag_name}", api_graph_tag_remove, methods=["DELETE"]),
    Route("/api/journal", api_journal, methods=["GET"]),
    Route("/api/graph/thoughts", api_graph_thoughts, methods=["GET"]),
    Route("/api/graph/threads", api_graph_threads, methods=["GET"]),
    Route("/api/graph/thought", api_graph_thought, methods=["POST"]),
    Route("/api/graph/thread", api_graph_thread, methods=["POST"]),
    Route("/api/graph/thread/action", api_graph_thread_action, methods=["POST"]),
    # Service-layer GET endpoints — used by HttpClient (container CLI).
    Route("/api/graph/search", api_graph_search, methods=["GET"]),
    Route("/api/graph/sources", api_graph_sources_list, methods=["GET"]),
    Route("/api/graph/source/{id}", api_graph_source_get, methods=["GET"]),
    Route("/api/graph/attachment/{attachment_id}", api_graph_attachment_get, methods=["GET"]),
    Route("/api/graph/collab-topics", api_graph_collab_topics, methods=["GET"]),
    Route("/api/graph/attention", api_graph_attention, methods=["GET"]),
    Route("/api/graph/stats", api_graph_stats, methods=["GET"]),
    Route("/api/graph/tree", api_graph_tree, methods=["GET"]),
    Route("/api/graph/entities", api_graph_entities, methods=["GET"]),
    Route("/api/graph/entity/{id}/thoughts", api_graph_entity_thoughts, methods=["GET"]),
    # Settings primitive (graph://0d3f750f-f9c). Routes ordered specific → generic.
    Route("/api/graph/sets", api_graph_set_ids, methods=["GET"]),
    Route("/api/graph/setting-resolve/{value}", api_graph_setting_resolve, methods=["GET"]),
    Route("/api/graph/settings/{set_id}/{key}/chain", api_graph_settings_chain, methods=["GET"]),
    Route("/api/graph/settings/{set_id}/{key}/check", api_graph_settings_check, methods=["GET"]),
    # Before {set_id}/{key}, or "contested" would be captured as a key.
    Route("/api/graph/settings/{set_id}/contested", api_graph_settings_contested, methods=["GET"]),
    Route("/api/graph/settings/{set_id}/{key}", api_graph_settings_get_by_key, methods=["GET"]),
    Route("/api/graph/settings/{set_id}/migrate", api_graph_settings_migrate, methods=["POST"]),
    Route("/api/graph/settings/{set_id}", api_graph_settings_list, methods=["GET"]),
    Route("/api/graph/setting", api_graph_setting_create, methods=["POST"]),
    Route("/api/graph/setting/{id}/override", api_graph_setting_override, methods=["POST"]),
    Route("/api/graph/setting/{id}/exclude", api_graph_setting_exclude, methods=["POST"]),
    Route("/api/graph/setting/{id}/promote", api_graph_setting_promote, methods=["POST"]),
    Route("/api/graph/source/{id}/move", api_graph_source_move, methods=["POST"]),
    Route("/api/graph/source/{id}/promote", api_graph_source_promote, methods=["POST"]),
    Route("/api/graph/setting/{id}/deprecate", api_graph_setting_deprecate, methods=["POST"]),
    Route("/api/graph/setting/{id}/undeprecate", api_graph_setting_undeprecate, methods=["POST"]),
    Route("/api/graph/setting/{id}", api_graph_setting_get, methods=["GET"]),
    Route("/api/graph/setting/{id}", api_graph_setting_delete, methods=["DELETE"]),
    Route("/api/agent-actions/dispatch", api_agent_action_dispatch, methods=["POST"]),
    Route("/api/graph/{id}", api_graph_resolve),
    Route("/api/source/{id}", api_source_read),
    Route("/api/source/{id}/attachments", api_source_attachments),
    Route("/api/context/{id}/{turn}", api_context),
    Route("/api/workspaces/local", api_workspace_local_create, methods=["POST"]),
    Route("/api/projects", api_projects),
    Route("/api/orgs", api_orgs_list, methods=["GET"]),
    Route("/api/orgs", api_orgs_create, methods=["POST"]),
    Route("/api/orgs/{slug}", api_orgs_show, methods=["GET"]),
    Route("/api/orgs/{slug}/workspaces/health", api_org_workspace_health,
          methods=["GET"]),
    Route("/api/orgs/{slug}", api_orgs_delete, methods=["DELETE"]),
    Route("/api/stats", api_stats),
    Route("/api/harness_usage", api_harness_usage),
    Route("/api/attention", api_attention),
    Route("/api/active", api_active_sessions),
    Route("/api/dao/active_sessions", api_dao_active_sessions),
    Route("/api/_mock/harness-nonce", api_mock_harness_nonce),
    Route("/api/dao/recent_sessions", api_dao_recent_sessions),
    Route("/api/dao/session_status", api_dao_session_status),
    Route("/api/worktrees", api_worktrees),
    Route("/api/worktrees/orgs", api_worktrees_orgs),
    Route("/api/worktrees/refresh", api_worktrees_refresh, methods=["POST"]),
    Route("/api/worktrees/{session}/{repo}/commits/{sha}", api_worktree_commit, methods=["GET"]),
    Route("/api/worktrees/{session}/{repo}/changes", api_worktree_changes, methods=["GET"]),
    Route("/api/worktrees/{session}/{repo}/pr-diff", api_worktree_integrated_diff, methods=["GET"]),
    Route("/api/worktrees/{session}/{repo}/commits/{sha}/merge", api_worktree_commit_merge, methods=["POST"]),
    Route("/api/worktrees/{session}/{repo}/refresh", api_worktree_refresh, methods=["POST"]),
    Route("/api/worktrees/{session}/{repo}/sync-base", api_worktree_sync_base, methods=["POST"]),
    Route("/api/worktrees/{session}/{repo}/watch", api_worktree_watch_set, methods=["PUT"]),
    Route("/api/worktrees/{session}/{repo}/merge", api_worktree_merge, methods=["POST"]),
    Route("/api/worktrees/{session}/{repo}/cherry-pick", api_worktree_cherry_pick, methods=["POST"]),
    Route("/api/worktrees/{session}/{repo}/discard", api_worktree_discard, methods=["POST"]),
    Route("/api/worktrees/{session}/cleanup", api_worktree_cleanup, methods=["POST"]),
    Route("/api/dao/bead/{id}", api_dao_bead),
    Route("/api/terminals", api_terminals),
    Route("/api/terminal/{id}/kill", api_terminal_kill, methods=["POST"]),
    Route("/api/terminal/{id}/rename", api_terminal_rename, methods=["POST"]),
    Route("/api/primer/{id}", api_primer),
    Route("/api/chatwith/primer/{page_type}", api_chatwith_primer),
    Route("/api/chatwith/check", api_chatwith_check),
    Route("/api/chatwith/sessions", api_chatwith_sessions),
    Route("/api/dispatch/tail/{run}", api_dispatch_tail),
    Route("/api/dispatch/latest/{run}", api_dispatch_latest),
    Route("/api/terminal/unclaimed", api_terminal_unclaimed),
    Route("/api/session/create", api_session_create, methods=["POST"]),
    Route("/api/session/resume", api_session_resume, methods=["POST"]),
    Route("/api/session/{tmux_name}/retry", api_session_retry, methods=["POST"]),
    Route("/api/session/{tmux_name}/restart", api_session_restart, methods=["POST"]),
    Route("/api/session/send-handshake", api_session_send_handshake, methods=["POST"]),
    Route("/api/session/confirm-link", api_session_confirm_link, methods=["POST"]),
    Route("/api/session/notify", api_session_notify, methods=["POST"]),
    Route("/api/agent-test/leases", api_agent_test_leases, methods=["POST"]),
    Route("/api/session/{tmux_name}", api_session_get, methods=["GET"]),
    Route("/api/session/{tmux_name}/output/{path:path}", api_session_output, methods=["GET"]),
    Route("/api/session/{tmux_name}/request-identity-refresh", api_session_request_identity_refresh, methods=["POST"]),
    Route("/api/session/{tmux_name}/interrupt", api_session_interrupt, methods=["POST"]),
    Route("/api/session/{tmux_name}/background", api_session_background, methods=["POST"]),
    Route("/api/session/{tmux_name}/label", api_session_label, methods=["PUT"]),
    Route("/api/session/{tmux_name}/topics", api_session_topics, methods=["PUT"]),
    Route("/api/session/{tmux_name}/role", api_session_role, methods=["PUT"]),
    Route("/api/session/{tmux_name}/startup-trace", api_session_startup_trace, methods=["GET"]),
    Route("/api/session/{tmux_name}/nag", api_session_nag, methods=["PUT"]),
    Route("/api/session/{tmux_name}/nag", api_session_nag_delete, methods=["DELETE"]),
    Route("/api/session/{tmux_name}/dispatch-nag", api_session_dispatch_nag, methods=["PUT"]),
    Route(
        "/api/session/turn-corrections/suggest",
        api_session_turn_correction_suggest,
        methods=["POST"],
    ),
    Route(
        "/api/session/{session_id}/turn-corrections",
        api_session_turn_corrections_list,
        methods=["GET"],
    ),
    Route(
        "/api/session/{session_id}/turn-corrections/{message_id}/accept",
        api_session_turn_correction_accept,
        methods=["POST"],
    ),
    Route(
        "/api/session/{session_id}/turn-corrections/{message_id}/dismiss",
        api_session_turn_correction_dismiss,
        methods=["POST"],
    ),
    Route("/api/session/send", api_session_send, methods=["POST"]),
    Route("/api/session/{project}/{session_id}/tail", api_session_tail),
    Route("/api/session/{project}/{session_id}/send", api_session_send, methods=["POST"]),
    Route("/api/voiceover/ask", api_voiceover_ask, methods=["POST"]),
    Route("/api/upload", api_upload, methods=["POST"]),
    Route("/api/timeline", api_timeline),
    Route("/api/timeline/stats", api_timeline_stats),
    Route("/api/version", api_version),

    # Graph write API (single-writer proxy for containers)
    Route("/api/graph/note", api_graph_note, methods=["POST"]),
    Route("/api/graph/note/update", api_graph_note_update, methods=["POST"]),
    Route("/api/graph/note/withdraw", api_graph_note_withdraw, methods=["POST"]),
    Route("/api/graph/note/{id}/versions", api_graph_note_versions_list, methods=["GET"]),
    Route("/api/graph/note/{id}/version/{n}", api_graph_note_version_read, methods=["GET"]),
    Route("/api/graph/comment", api_graph_comment, methods=["POST"]),
    Route("/api/graph/comment/integrate", api_graph_comment_integrate, methods=["POST"]),
    Route("/api/graph/comment/{id}", api_graph_comment_get, methods=["GET"]),
    Route("/api/graph/turn/{source_id}", api_graph_turn_content, methods=["GET"]),
    Route("/api/graph/bead", api_graph_bead, methods=["POST"]),
    Route("/api/graph/link", api_graph_link, methods=["POST"]),
    Route("/api/graph/journal", api_graph_journal_write, methods=["POST"]),
    Route("/api/graph/sessions", api_graph_sessions, methods=["POST"]),
    Route("/api/graph/docs", api_graph_docs, methods=["POST"]),
    Route("/api/graph/attach", api_graph_attach, methods=["POST"]),

    # CrossTalk
    Route("/api/crosstalk/send", api_crosstalk_send, methods=["POST"]),
    Route("/api/crosstalk/broadcast", api_crosstalk_broadcast, methods=["POST"]),
    Route("/api/crosstalk/peers", api_crosstalk_peers, methods=["GET"]),
    Route("/api/crosstalk/log", api_crosstalk_log, methods=["GET"]),

    # Monitor IPC — dispatcher registers dispatch/librarian sessions here so
    # inotify watches + SSE broadcasts are wired up in-process.
    Route("/api/resources", api_resources),
    Route("/api/resources/{tmux_name}/refresh", api_resources_refresh, methods=["POST"]),
    Route("/api/monitor/register", api_monitor_register, methods=["POST"]),
    Route("/api/monitor/deregister", api_monitor_deregister, methods=["POST"]),

    # Embed resolution + attachment serving
    Route("/api/resolve/{id}", api_resolve_embed),
    Route("/api/attachment/{attachment_id}", api_attachment_serve),

    # Design Studio (formerly Experiments)
    Route("/api/design", api_design_create, methods=["POST"]),
    Route("/api/design/pending", api_design_pending),
    Route("/api/design/{id}", api_design_poll),
    Route("/api/design/{id}/full", api_design_get),
    Route("/api/design/{id}/dismiss", api_design_dismiss, methods=["POST"]),
    Route("/api/design/{id}/submit", api_design_submit, methods=["POST"]),
    Route("/api/design/{id}/screenshot", api_design_screenshot, methods=["POST"]),
    # Backwards compat redirects
    Route("/experiments/{id}", page_experiments_redirect),

    # Plugin substrate
    Route("/api/plugins", api_plugins),
    Route("/api/session-contributions", api_session_contributions, methods=["POST"]),
    Route("/api/plugins/{plugin_id}/skill", api_plugin_skill),
    *_build_plugin_routes(),


    # On-demand approval rendezvous (requester <-> operator browser),
    # e.g. commit signing
    *approvals_routes.ROUTES,

    # Settings-native Central Attention operator projection and its private
    # wake/refetch stream.  The legacy Knowledge Graph GET /api/attention
    # route above remains a separate exact path.
    *attention_routes.routes,

    # ChatGPT MCP relay: session/crosstalk resolve + approval (service-token auth)
    *mcp_relay_routes.ROUTES,

    # Machine-global operator dropbox: public approval bootstrap, upload-only
    # ingress credential, and session-authenticated global reads.
    *dropbox_routes.ROUTES,

    # auto.network identity (C2 sign-on ceremony): encrypted org key,
    # binding record, revocation forwarding
    *network_routes.ROUTES,

    # Personal identity + passkey enrollment (Get started onboarding)
    *identity_routes.ROUTES,
    *org_membership_routes.ROUTES,
    *fleet_enrollment_routes.ROUTES,
    *vault_routes.ROUTES,

    # Invite bridge, local half (auto-1ihgz): display/consent shell only.
    # Acceptance mechanics are held for the ceremony-workflow ruling
    # (auto-9rw91); this page has no inputs and no API calls by design.
    Route("/network/join", page_network_join),

    # Human unlock gate: passkey assert + password fallback + session
    Route("/unlock", page_unlock),
    *unlock_routes.ROUTES,

    # Jira broker (issue_tracker capability): host-side reads; writes ride the
    # approval rendezvous as kind=jira_write
    *jira_routes.ROUTES,

    # Static (catch-all — plugin static mounts above take precedence)
    Mount("/static", app=_VersionedStatic(directory=str(STATIC_DIR)), name="static"),
]

# Dev-prototype routes (test_fixtures/input-prototype.html): in-memory debug/
# toast/version scratch surfaces with no database and no production consumer.
# Registered ONLY on an internal/mock dashboard (DASHBOARD_MOCK, which the mock
# server sets for its temporary-fixture instances); absent in production, where
# the paths simply do not exist. Suppressing them in prod, per operator
# decision 2026-08-21 (auto-1wwpf.6).
if os.environ.get("DASHBOARD_MOCK"):
    routes += [
        Route("/api/test/debug", api_test_debug_get),
        Route("/api/test/debug", api_test_debug_post, methods=["POST"]),
        Route("/api/test/version", api_test_version_get),
        Route("/api/test/version", api_test_version_bump, methods=["POST"]),
        Route("/api/test/toast", api_test_toast_get),
        Route("/api/test/toast", api_test_toast_post, methods=["POST"]),
    ]

# Default-deny (auto-1wwpf.6): wrap every app /api route so a caller the
# ApiIdentityMiddleware did not authenticate is refused. Plugin routes are
# already wrapped unconditionally at the plugin mount (plugin=True) and carry
# the idempotence marker, so this app-strength pass skips them. App routes use
# the fail-open-then-enforce guard, which stands down while the human gate is
# not enforced so a fresh install can bootstrap; the PUBLIC_EXCEPTIONS in
# route_policy are the pre-enrolment routes served without a credential.
routes = route_policy.apply_default_deny(routes)

# Background task handles — captured during startup, cancelled during shutdown
_dispatch_watcher_task: asyncio.Task | None = None
_mock_event_watcher_task: asyncio.Task | None = None
_harness_usage_poller_task: asyncio.Task | None = None
_serving_bootstrap_task: asyncio.Task | None = None
_event_proxy_task: asyncio.Task | None = None
_claude_credentials_refresh_task: asyncio.Task | None = None
_codex_credentials_refresh_task: asyncio.Task | None = None
_event_loop_watchdog_task: asyncio.Task | None = None
_vault_release_sweeper_task: asyncio.Task | None = None
_settings_mediator_started: bool = False



# ── Event-loop stall SAMPLER (companion to the watchdog coroutine below) ──
# The watchdog coroutine runs ON the loop, so it can only MEASURE a stall after
# the fact — it can't capture WHAT blocked the loop, because it isn't running
# while the loop is blocked. This sampler runs on a separate OS thread: the
# coroutine bumps _loop_heartbeat every tick; the sampler watches that heartbeat
# and, the instant it goes stale (loop not ticking = blocked right now), reads
# the loop thread's live stack via sys._current_frames() and logs it. That's a
# READ of existing frame objects, not a suspend — the blocked thread is never
# signalled or paused. Turns each stall into a named blocking stack.
_loop_heartbeat = time.monotonic()
_loop_thread_id: int | None = None
_stall_sampler_started = False
_STALL_SAMPLE_S = 0.5   # start sampling once the loop has been stalled this long
_STALL_RESAMPLE_S = 0.4  # re-sample the stack this often WHILE a stall persists
_STALL_MAX_DUMPS = 60   # cap samples per episode (60 * 0.4s = ~24s of coverage)


def _loop_stall_sampler():
    """Sample the loop thread's stack REPEATEDLY through a stall.

    A single once-per-episode dump can't distinguish "one call stuck for 20s"
    from "a rapid burst of short blocks" — so we re-sample every
    ``_STALL_RESAMPLE_S`` for as long as the loop stays stalled, giving a stack
    timeline across the whole episode. ``#N`` in the log line is the sample
    index within one episode; a run of identical stacks = one long blocking
    call, varied stacks = accumulation.
    """
    import traceback
    episode_hb = None
    dumps = 0
    last_dump_t = 0.0
    while True:
        time.sleep(0.1)
        tid = _loop_thread_id
        if tid is None:
            continue
        hb = _loop_heartbeat
        now = time.monotonic()
        stalled = now - hb
        if stalled < _STALL_SAMPLE_S:
            continue
        if hb != episode_hb:          # new stall episode (loop ticked since last)
            episode_hb = hb
            dumps = 0
            last_dump_t = 0.0
        if dumps >= _STALL_MAX_DUMPS or (now - last_dump_t) < _STALL_RESAMPLE_S:
            continue
        frame = sys._current_frames().get(tid)
        if frame is not None:
            stack = "".join(traceback.format_stack(frame))
            logger.error(
                "EVENT-LOOP STALL STACK #%d (stalled %.2fs, loop thread mid-call):\n%s",
                dumps + 1, stalled, stack,
            )
        dumps += 1
        last_dump_t = now


async def _event_loop_watchdog():
    """Detect event-loop stalls and log them LOUD.

    The loop should wake this coroutine every ``_TICK`` seconds. If the
    wake-up is late, the loop was unable to run for that delta — i.e. a
    synchronous call blocked it, OR a CPU-bound ``to_thread`` held the GIL so
    the loop thread couldn't make progress. Either way every in-flight request
    stalled for ``lag`` seconds. This is the single detector for the recurring
    "blocking work on the event loop" class of bug: it can't be designed
    around, only observed, so we observe it continuously and name the stall.

    Cheap: one 100ms timer tick; the measurement is two ``monotonic()`` reads.
    """
    global _loop_heartbeat, _loop_thread_id
    _loop_thread_id = threading.get_ident()
    _TICK = 0.1
    _LAG_WARN_S = 0.5
    _LAG_HANG_S = 2.0
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(_TICK)
        now = time.monotonic()
        _loop_heartbeat = now
        lag = now - t0 - _TICK
        if lag >= _LAG_HANG_S:
            logger.error(
                "EVENT-LOOP STALL: loop blocked %.2fs — a sync or CPU-bound "
                "(GIL-holding) call is not yielding; all requests hung this long",
                lag,
            )
        elif lag >= _LAG_WARN_S:
            logger.warning("EVENT-LOOP LAG: loop blocked %.2fs", lag)

# Task* tile enricher — per-session taskId → subject/status map. Populated by
# the session monitor tailer as it walks JSONL entries; also used by the HTTP
# history endpoint to produce identical annotations on page load.
_task_state_tracker = TaskStateTracker()


async def _settings_mediator_session_send(session: str, text: str) -> None:
    """``Services.session_send`` — paste text into a tmux session."""
    from tools.dashboard.tmux_send import tmux_send
    await tmux_send(session, text)


def _build_settings_mediator_services():
    """Construct the substrate's :class:`Services` for the running process."""
    from tools.dashboard.settings_mediator import Services
    from tools.dashboard.surface_actions import CrosstalkService
    from tools.dashboard.tmux_send import tmux_send
    return Services(
        session_send=_settings_mediator_session_send,
        log=logging.getLogger("settings_mediator"),
        crosstalk=CrosstalkService(send_fn=tmux_send),
    )


def _warm_personal_settings_store() -> None:
    """Finish personal-store schema/WAL setup before the gate can read it."""
    from tools.graph.db import GraphDB, resolve_caller_db_path

    GraphDB(resolve_caller_db_path(None)).close()


async def _on_startup():
    global _dispatch_watcher_task, _mock_event_watcher_task, _harness_usage_poller_task
    global _claude_credentials_refresh_task, _codex_credentials_refresh_task
    global _event_loop_watchdog_task
    global _serving_bootstrap_task
    global _event_proxy_task
    # Startup phase timing (auto-network perf investigation, 2026-08-24):
    # boot has gotten intermittently slow (12-66s stalls observed in prod
    # logs) and the previous debugging pass could only narrow it to "some
    # synchronous startup step" from indirect stall-watchdog snapshots.
    # These marks give a real per-step breakdown on every restart instead
    # of guessing from where the sampler happened to land.
    _startup_t0 = time.monotonic()
    _startup_last = [_startup_t0]

    def _mark(label: str) -> None:
        now = time.monotonic()
        logger.info(
            "startup phase: %-42s %8.1fms", label, (now - _startup_last[0]) * 1000,
        )
        _startup_last[0] = now

    # Re-arm the emit hook on every lifespan startup. Module import
    # already wires it (so ASGITransport-based tests that skip lifespan
    # still get function-level emits), but we re-arm here so that
    # uvicorn --reload cycles or in-process module reloads always end
    # up with a hook bound to *this* module's ``event_bus`` name.
    from tools.graph import settings_ops as _settings_ops
    _settings_ops.set_emit_hook(_settings_emit_hook)
    # THE UNREADABLE-STORE GATE RUNS FIRST — before the event-bus restore,
    # before any monitor wiring, and in BOTH mock and real mode. The
    # monitors resolve workspaces → orgs → the operator's personal store on
    # their very first sweep, so any store-touching step ahead of this gate
    # makes it unreachable for the exact failure it was written for: a
    # corrupt store then kills startup from inside a monitor broadcast with
    # a raw traceback instead of the one message that tells the operator
    # what to do. (The mock branch previously returned before the gate
    # entirely, which is where review caught it dead.)
    from tools.data_paths import LocalStoreUnreadableError

    def _refuse_to_start_on_unreadable_store():
        # The operator's local store exists and CANNOT BE READ. This is not
        # about migration — it is refusing to run on a store we cannot
        # read: continuing would leave every later resolution of the
        # personal store raising for the life of the process while the
        # dashboard pretends to be up. Damage is not absence; absence is
        # handled (the resolver serves the real home), damage stops us.
        logger.critical(
            "the operator's local store cannot be read; refusing to start",
            exc_info=True,
        )

    if os.environ.get("DASHBOARD_MOCK"):
        # Mock mode gets the gate WITHOUT the provisioning. A mock server
        # serves fixtures and must not materialize real stores (every mock
        # TestClient boot creating SQLite files is measurable I/O across a
        # parallel test run), but its worktree monitor still resolves
        # stores on the first sweep, so the damage check must still stop
        # startup here. Resolution is path routing plus a read-only
        # classification probe — it creates nothing.
        try:
            from tools.graph.db import LOCAL_STORE_SLUGS, _local_store_db_path
            for _local_name in LOCAL_STORE_SLUGS:
                _local_store_db_path(_local_name)
        except LocalStoreUnreadableError:
            _refuse_to_start_on_unreadable_store()
            raise
    else:
        from agents.dispatch_db import init_db
        init_db()  # ensure dispatch schema exists
        dashboard_db.init_db()  # ensure dashboard.db schema exists
        auth_db.init_db()  # ensure auth.db schema exists
        _mark("db_init (dispatch+dashboard+auth)")
        # First-launch bootstrap: ensure data/orgs/{autonomy,personal}.db
        # exist. Idempotent — pre-existing DBs are left untouched. See
        # graph://d970d946-f95.
        try:
            from tools.graph import org_ops
            org_ops.ensure_bootstrap_orgs()
        except LocalStoreUnreadableError:
            _refuse_to_start_on_unreadable_store()
            raise
        except Exception:
            logger.exception("ensure_bootstrap_orgs() failed; continuing startup")
        _mark("org_ops.ensure_bootstrap_orgs")
        await web_push.start_worker()
        _mark("web_push.start_worker")
        await web_push_worker.start_worker()
        _mark("web_push_worker.start_worker")
        await image_build_worker.start_worker()
        _mark("image_build_worker.start_worker")
        await web_gateway_supervisor.start_worker(event_bus)
        _mark("web_gateway_supervisor.start_worker")
        await service_certificate_manager.start_worker(event_bus)
        _mark("service_certificate_manager.start_worker")
        try:
            await web_push.reconcile_approval_attention(
                approvals_routes.push_eligible_kind,
            )
        except Exception:
            logger.exception(
                "Web Push approval reconciliation failed; periodic sends remain active"
            )
        _mark("web_push.reconcile_approval_attention")

    # A node booted with AUTONOMY_FLEET_INVITE is a Fleet member from first
    # boot: mark it joining now, before the serving supervisor can evaluate
    # eligibility, so it fails CLOSED on tunnel serving during the window before
    # its enrollment ceremony runs (machine_boot.mark_joining_from_env).
    # Best-effort and non-fatal — a marker failure must not down startup.
    try:
        from tools.network import machine_boot
        if machine_boot.mark_joining_from_env():
            logger.info("fleet invite present: marked machine fleet-joining "
                        "(fails closed on tunnel serving until enrolled)")
    except Exception:
        logger.exception("marking machine fleet-joining from AUTONOMY_FLEET_INVITE failed")
    _mark("machine_boot.mark_joining_from_env")

    # Personal fleet synchronization owns one in-process scheduler. It starts
    # idle before unlock/runtime credentials are available, so zero-peer,
    # mock, and cold-vault Dashboards pay no database or network cost.
    from tools.network.fleet_sync_scheduler import (
        dashboard_fleet_sync_service,
        set_settings_materialization_hook,
    )
    set_settings_materialization_hook(attention_routes.emit_personal_sync_change)
    await dashboard_fleet_sync_service.start()
    _mark("fleet_sync_scheduler.start")

    # Replay the Dashboard's OWN Fleet runtime credential from the warm ramfs
    # cache so a restarted machine re-arms Fleet sync with nobody present
    # (auto-5er0n). The credential is delivered by the browser at unlock and
    # held only in process memory, so every restart lost it until a human
    # unlocked again. Best-effort: a replay failure must not down startup, and a
    # machine with no cached payload simply stays locked. The scheduler above is
    # already started, so the replayed credential's consumers configure onto a
    # live service.
    with contextlib.suppress(Exception):
        fleet_enrollment_routes.rearm_local_runtime_from_cache()
    _mark("fleet_enrollment_routes.rearm_local_runtime_from_cache")

    # Restore EventBus sequence/buffer state from the prior process. restore()
    # advances the persisted epoch so clients show the existing reload banner
    # while still retaining gap-replay continuity.
    # Some tests substitute a MockEventBus without snapshot/restore;
    # treat absence of the attribute as a no-op.
    # Mock-mode servers skip restore entirely: under pytest all fixture
    # servers on one xdist worker inherit the same DASHBOARD_EVENT_BUS_STATE,
    # so restoring replays a *previous test file's* cached topics (e.g. its
    # session:registry / session:messages) into this file's subscribers —
    # observed as cross-file SSE pollution. Production never runs mock.
    restore_fn = getattr(event_bus, "restore", None)
    if os.environ.get("DASHBOARD_MOCK"):
        restore_fn = None
    if callable(restore_fn):
        try:
            restore_fn(EVENT_BUS_STATE_PATH)
        except Exception:
            logger.exception("event_bus.restore() raised unexpectedly; continuing")
    # A restart status is a live interruption, not application state. Drop a
    # notice left by an older process before any fresh browser can subscribe.
    _discard_restart_event_cache()
    _mark("event_bus.restore")
    try:
        scrubbed = attention_routes.scrub_private_cached_events(event_bus)
        if scrubbed:
            snapshot_fn = getattr(event_bus, "snapshot", None)
            if callable(snapshot_fn):
                snapshot_fn(EVENT_BUS_STATE_PATH)
            logger.info(
                "removed %d private Central Attention entries from EventBus replay",
                scrubbed,
            )
    except Exception:
        logger.critical(
            "private Central Attention EventBus scrub failed; refusing to serve",
            exc_info=True,
        )
        raise
    await attention_routes.start()
    _mark("attention_routes.start+event_bus_scrub")
    # Wire the terminal CrossTalk notifier before the monitor starts so
    # any initial-refresh cache write in ``start()`` can fire transitions
    # for already-armed rows.
    worktree_monitor.set_terminal_notifier(_terminal_crosstalk_notifier)
    # Register the row→JSON renderer before the initial refresh so the very
    # first sweep pre-encodes the /api/worktrees payload on its worker
    # thread (auto-yq27f) — the request path then serves bytes, never git
    # or json.dumps on the event loop.
    worktree_monitor.set_row_renderer(_worktree_state_json)
    if os.environ.get("DASHBOARD_MOCK"):
        await worktree_monitor.start()
        # Mock mode: skip real database init and session monitor.
        # Broadcast initial SSE events from fixture data so SSE-dependent
        # pages (dispatch) render without waiting.
        # NOTE: session:registry is NOT broadcast here — pages fetch fresh
        # data from /api/dao/active_sessions, and broadcasting cached data
        # in the event bus breaks test isolation when fixtures are swapped
        # dynamically between page loads.
        runs = dao_dispatch.get_running_with_stats()
        for r in runs:
            if r.get("last_activity") and isinstance(r["last_activity"], str):
                r["last_activity"] = datetime.fromisoformat(r["last_activity"]).replace(tzinfo=timezone.utc).timestamp()
        await event_bus.broadcast("dispatch", {"active": runs, "waiting": [], "blocked": []})
        # Nav counts from fixture beads
        counts = dao_beads.get_bead_counts()
        running_count = len(runs)
        await event_bus.broadcast("nav", {
            "open_beads": counts.get("total_open_count", 0),
            "running_agents": running_count,
        })
        if os.environ.get("DASHBOARD_MOCK_EVENTS"):
            from tools.dashboard.dao.mock import mock_event_watcher
            _mock_event_watcher_task = asyncio.create_task(mock_event_watcher())
        await _emit_restart_complete()
        return

    # Materialize Setting *schema* meta rows (autonomy.schema#1 +
    # autonomy.schema.synopsis#1) into every org DB. Decoupled from
    # _SCHEMA_USER_VERSION (auto-06ziz): the hot-reload restarts this process on
    # every code change, and the schema registry only changes when code loads,
    # so flushing once here makes schema/synopsis edits live with no version bump
    # and no full table re-init. Must run after ensure_bootstrap_orgs so the
    # freshly created autonomy/personal DBs are covered on first launch. All the
    # eager schema-module imports at the top of this file guarantee the registry
    # is fully populated before this runs.
    try:
        from tools.graph.schemas.registry import flush_schema_meta_machine_store
        n = await asyncio.to_thread(flush_schema_meta_machine_store)
        logger.info(
            "flush_schema_meta_machine_store: machine store %s",
            "flushed" if n else "NOT flushed",
        )
    except Exception:
        logger.exception("flush_schema_meta_machine_store() failed; continuing startup")
    _mark("flush_schema_meta_machine_store")
    try:
        await asyncio.to_thread(_warm_personal_settings_store)
    except Exception:
        logger.exception(
            "personal settings store warm-open failed; continuing startup"
        )
    _mark("warm_personal_settings_store")
    try:
        await asyncio.to_thread(attention_routes.sync_registrations)
    except Exception:
        logger.exception("Central Attention registration sync failed; continuing startup")
    _mark("attention_routes.sync_registrations")
    try:
        from tools.graph.commit_policy import seed_default_workspace_policies
        seed_default_workspace_policies(workspace_settings.load_workspaces())
        workspace_settings.invalidate_caches()
    except Exception:
        logger.exception("commit policy default seed failed; continuing startup")
    _mark("seed_default_workspace_policies")
    try:
        await asyncio.to_thread(
            plugin_loader.reconcile_declared_settings,
            PLUGIN_REGISTRY,
        )
    except Exception:
        logger.exception("plugin declared-settings reconcile failed; continuing startup")
    _mark("plugin_loader.reconcile_declared_settings")
    # Seed from filesystem on first run (one-time), then start background tasks
    await session_monitor.seed_from_filesystem()
    _mark("session_monitor.seed_from_filesystem")
    await session_monitor.start(
        event_bus=event_bus,
        entry_parser=CLAUDE_HARNESS.parse_line,
        entry_enricher=_task_state_tracker.enrich,
        harness=CLAUDE_HARNESS,
        todo_snapshot=_task_state_tracker.snapshot,
    )
    _mark("session_monitor.start")
    try:
        await asyncio.to_thread(load_row_cache, WORKTREE_ROW_CACHE_PATH)
    except Exception:
        logger.exception("worktree row-cache restore failed; continuing startup")
    await worktree_monitor.start()
    _mark("worktree_monitor.start")
    # Resource collector: skip under mock/test servers — it polls the real
    # dashboard.db live-session set and attempts docker/tmux resolution per
    # session, which is pure noise (and timing jitter for browser tests)
    # in those environments. Same gate as the harness-usage poller.
    if _should_run_harness_usage_poller():
        # Carry sparkline ring buffers across the hot reload — the prior
        # process snapshots them in _on_shutdown, mirroring the event bus.
        resource_monitor.load_state(RESOURCE_MONITOR_STATE_PATH)
        await resource_monitor.start(event_bus=event_bus)
    _mark("resource_monitor.start")
    try:
        _ensure_dispatcher_service_token()
    except Exception:
        logger.exception("dispatcher monitor token provisioning failed")
    try:
        from agents.dispatch_db import fail_stale_prelaunch_runs
        swept = await asyncio.to_thread(fail_stale_prelaunch_runs)
        if swept:
            logger.warning(
                "failed %d agentic run(s) stranded in QUEUED/PREPARING by "
                "the previous process", swept)
    except Exception:
        logger.exception("stale prelaunch sweep failed")
    _dispatch_watcher_task = asyncio.create_task(_dispatch_watcher())
    global _agentic_queue_task
    _agentic_queue_task = asyncio.create_task(
        _agentic_queue_drainer(), name="agentic-queue-drainer")
    _event_loop_watchdog_task = asyncio.create_task(_event_loop_watchdog())
    global _stall_sampler_started
    if not _stall_sampler_started:
        _stall_sampler_started = True
        threading.Thread(
            target=_loop_stall_sampler, name="loop-stall-sampler", daemon=True,
        ).start()
    _mark("watcher_tasks_created (dispatch/stall/recent_sessions)")
    # Vault release reconciliation + sweeper (auto-pw9bs.5). Reconcile FIRST,
    # before the sweeper loop and before traffic: a delivered secret whose
    # cleanup was owed when the prior process died is still present in ramfs
    # (which survives a dashboard restart, only a reboot wipes it), and the
    # durable record is the only state that points at it. Reconciliation
    # destroys the ones now overdue or whose session is gone and reclaims
    # orphaned session directories; the loop then keeps the rest to their
    # deadlines. Best-effort: a reconciliation failure must not stop startup,
    # but it is logged loudly because an unswept secret is the thing this
    # subsystem exists to prevent.
    try:
        from tools.dashboard import vault_release_sweeper as _vault_sweeper
        _live_at_start = await asyncio.to_thread(_live_session_names_or_none)
        await asyncio.to_thread(
            _vault_sweeper.reconcile_on_startup,
            session_exists=(
                (lambda s, _live=_live_at_start: s in _live)
                if _live_at_start is not None else None
            ),
        )
    except Exception:
        logger.exception(
            "vault release reconciliation failed on startup; the periodic "
            "sweeper will still run and catch outstanding releases",
        )
    _vault_release_sweeper_task = asyncio.create_task(_vault_release_sweeper())
    _mark("vault_release_sweeper.reconcile_on_startup")
    # Restore a WARM vault from a graceful hot-reload snapshot before traffic
    # (auto-a1pub). Present only after a graceful shutdown wrote it; a cold boot
    # or a crash finds nothing and the vault stays locked until a human unlock.
    # The snapshot files are cleared once consumed.
    try:
        from tools.dashboard.unlock_routes import restore_vault_across_hot_reload
        await asyncio.to_thread(restore_vault_across_hot_reload)
    except Exception:
        logger.exception(
            "vault hot-reload restore raised on startup; the vault stays locked"
        )
    _mark("restore_vault_across_hot_reload")
    # Session lifecycle worker (FSM redesign 2026-06-18): start the single
    # off-loop thread that owns workspace start/stop/retry. It sits idle until
    # api_session_create is rewired to enqueue — starting it now is additive and
    # lets the create cutover land as a separate, verifiable step.
    # Every lifecycle transition broadcasts the registry so the session
    # cards track the worker's states live — without this the chip only
    # moved when some unrelated event happened to broadcast.
    _lifecycle_loop = asyncio.get_running_loop()

    def _lifecycle_transition_hook(transition) -> None:
        async def _publish_transition() -> None:
            # A terminal transition carries its own card.  The registry is a
            # live-only roster, so making the browser infer an ended card from
            # an omission loses the transition whenever its local Active view
            # is stale or reconnecting.
            if transition.state in ("ENDED", "FAILED"):
                await session_monitor.broadcast_terminal_session(transition.tmux_name)
            await session_monitor._broadcast_registry()

        try:
            asyncio.run_coroutine_threadsafe(
                _publish_transition(), _lifecycle_loop,
            )
        except Exception:
            logger.debug("session_lifecycle: transition broadcast failed", exc_info=True)

    _SESSION_LIFECYCLE_WORKER.state_writer.set_transition_hook(_lifecycle_transition_hook)
    _SESSION_LIFECYCLE_WORKER.start()
    logger.info("session_lifecycle: worker started from _on_startup (idle until create enqueues)")
    _mark("lifecycle_worker.start")
    # Recover rows a restart froze mid-launch — must run before traffic so
    # the first registry broadcast the clients see is already repaired.
    await _recover_stuck_lifecycle_rows()
    _mark("recover_stuck_lifecycle_rows")
    if _should_run_harness_usage_poller():
        _harness_usage_poller_task = asyncio.create_task(_harness_usage_poller())
    if _claude_credentials_refresh.should_run_credentials_refresh_poller():
        _claude_credentials_refresh_task = asyncio.create_task(
            _claude_credentials_refresh.credentials_refresh_poller()
        )
    if _codex_credentials_refresh.should_run_codex_credentials_refresh_poller():
        _codex_credentials_refresh_task = asyncio.create_task(
            _codex_credentials_refresh.codex_credentials_refresh_poller()
        )
    if os.environ.get("DASHBOARD_MOCK_EVENTS"):
        from tools.dashboard.dao.mock import mock_event_watcher
        _mock_event_watcher_task = asyncio.create_task(mock_event_watcher())
    _mark("poller_tasks_created (harness_usage/claude_creds/codex_creds)")
    # Settings-mediator action loop: walks per-set cursors and dispatches
    # registered handlers on new rows. Mock-mode dashboards skip this —
    # the loop reads through ``settings_ops`` against the real graph DB,
    # which is unavailable in fixture-driven runs.
    global _settings_mediator_started
    try:
        from tools.dashboard import settings_mediator
        settings_mediator.start_action_loop(
            _build_settings_mediator_services(),
            event_bus=event_bus,
        )
        _settings_mediator_started = True
    except Exception:
        logger.exception(
            "settings_mediator.start_action_loop() failed; "
            "continuing without action dispatch"
        )
    _mark("settings_mediator.start_action_loop")

    # auto.network serving supervisor: bring up the tunnel connector for any
    # org whose serve-cert is provisioned and whose links are live, and arm the
    # watchdog. So a restart re-establishes serving on its own, without waiting
    # for the next publish. Skipped in mock mode (no real settings DB).
    #
    # FIRE-AND-FORGET: bootstrap() reconciles serving per org, which now does
    # real connector bring-up (network work) and can block for tens of seconds
    # once grants are live. AWAITing it here held the whole startup lifespan
    # hostage — the server accepted connections but returned no HTTP response
    # until it finished (a ~55s dead window). Scheduling it as a background
    # task lets the lifespan return immediately; serving reconciles a beat
    # later and the watchdog it arms keeps it converged. The task ref is
    # retained (else the loop may GC it) and a done-callback logs any fault.
    if not os.environ.get("DASHBOARD_MOCK"):
        from tools.dashboard import link_serving_supervisor

        def _log_bootstrap_result(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.error(
                    "link_serving_supervisor.bootstrap() failed; serving "
                    "recovers on the next publish or watchdog tick",
                    exc_info=exc,
                )

        _serving_bootstrap_task = asyncio.create_task(
            asyncio.to_thread(link_serving_supervisor.bootstrap)
        )
        _serving_bootstrap_task.add_done_callback(_log_bootstrap_result)

        # The dashboard half of the event proxy: carry our own bus events to
        # the connector that holds the guest channels, over its loopback
        # control listener. Runs for the life of the process and never
        # blocks the requests that emit the events -- it drains a queue of
        # its own. Independent of bootstrap above: if no connector is up,
        # delivery simply fails per event and serving is untouched.
        #
        # proxy_events_to_connectors() is self-supervising (retries its own
        # startup/loop failures with backoff — see its docstring), so this
        # done-callback is a backstop for visibility, not the retry itself:
        # if the task ever DOES end with an exception, that's not a
        # transient failure it already recovered from, it's worth a loud log.
        from tools.dashboard import link_serving

        def _log_event_proxy_result(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.error(
                    "link_serving.proxy_events_to_connectors() ended "
                    "unexpectedly; live guest delivery is down until the "
                    "next reload",
                    exc_info=exc,
                )

        _event_proxy_task = asyncio.create_task(
            link_serving.proxy_events_to_connectors(event_bus)
        )
        _event_proxy_task.add_done_callback(_log_event_proxy_result)
    _mark("serving_supervisor_bootstrap+event_proxy tasks_created")
    logger.info(
        "startup phase: TOTAL %.1fms", (time.monotonic() - _startup_t0) * 1000,
    )

    # The final lifecycle event is intentionally last: receiving it means this
    # process has completed its synchronous warm-up and can serve the browser,
    # not merely that a Python process has bound the port.
    await _emit_restart_complete()

async def _on_shutdown():
    global _dispatch_watcher_task, _mock_event_watcher_task
    global _harness_usage_poller_task, _claude_credentials_refresh_task
    global _codex_credentials_refresh_task
    global _settings_mediator_started, _serving_bootstrap_task
    global _event_proxy_task
    global _vault_release_sweeper_task
    # Uvicorn closes SSE sockets before it calls this lifespan hook. Its parent
    # watcher has already called the authenticated endpoint and waited three
    # seconds. Direct shutdowns cannot warn a browser, but still leave timing
    # state for the next process.
    if _restart_notice_payload is None:
        _write_restart_notice({"started_at_ms": int(time.time() * 1000)})
    try:
        await web_push_worker.stop_worker()
    except Exception:
        logger.exception("error stopping the Central Web Push delivery worker")
    try:
        await image_build_worker.stop_worker()
    except Exception:
        logger.exception("error stopping the workspace image build worker")
    try:
        await service_certificate_manager.stop_worker()
    except Exception:
        logger.exception("error stopping the Service certificate manager")
    try:
        await web_gateway_supervisor.stop_worker()
    except Exception:
        logger.exception("error stopping the Service gateway reconciliation worker")
    if _agentic_queue_task is not None and not _agentic_queue_task.done():
        _agentic_queue_task.cancel()
        try:
            await _agentic_queue_task
        except (asyncio.CancelledError, Exception):
            pass
    try:
        await web_push.stop_worker()
    except Exception:
        logger.exception("error stopping the Web Push worker")
    try:
        from tools.network.fleet_sync_scheduler import set_settings_materialization_hook
        set_settings_materialization_hook(None)
    except Exception:
        logger.exception("error clearing the personal-sync Settings hint")
    try:
        await attention_routes.stop()
    except Exception:
        logger.exception("error stopping the private Central Attention hub")
    # Clear the emit hook so a subsequent process / test reload doesn't
    # leak a stale binding into a swapped module-level event_bus.
    try:
        from tools.graph import settings_ops as _settings_ops
        _settings_ops.set_emit_hook(None)
    except Exception:
        logger.exception("settings_ops.set_emit_hook(None) failed; continuing")
    try:
        from tools.network.fleet_sync_scheduler import dashboard_fleet_sync_service
        await dashboard_fleet_sync_service.stop()
    except Exception:
        logger.exception("error stopping the personal fleet sync scheduler")
    # Cancel a still-running background bootstrap FIRST, so it cannot spawn a
    # connector in the window between stop_all() and process exit.
    if _serving_bootstrap_task is not None and not _serving_bootstrap_task.done():
        _serving_bootstrap_task.cancel()
        try:
            await _serving_bootstrap_task
        except (asyncio.CancelledError, Exception):
            pass
    _serving_bootstrap_task = None
    if _event_proxy_task is not None and not _event_proxy_task.done():
        _event_proxy_task.cancel()
        try:
            await _event_proxy_task
        except (asyncio.CancelledError, Exception):
            pass
    _event_proxy_task = None
    # Stop the serving watchdog and terminate any connector subprocesses so a
    # reload cycle doesn't leak them (a fresh process re-establishes serving in
    # _on_startup).
    try:
        from tools.dashboard import link_serving_supervisor
        link_serving_supervisor.get_supervisor().stop_all()
    except Exception:
        logger.exception("error stopping the serving supervisor")
    # Drain settings-mediator BEFORE cancelling the dispatcher tasks so
    # any in-flight action handler that calls back into the dashboard
    # (tmux_send / crosstalk) still has those primitives available. The
    # loop's stop() awaits the in-flight tick — handlers complete
    # naturally instead of being cancelled mid-call.
    if _settings_mediator_started:
        try:
            from tools.dashboard import settings_mediator
            await settings_mediator.stop_action_loop()
        except Exception:
            logger.exception("error during settings_mediator.stop_action_loop()")
        _settings_mediator_started = False
    tasks = [
        t for t in (
            _dispatch_watcher_task,
            _mock_event_watcher_task,
            _harness_usage_poller_task,
            _claude_credentials_refresh_task,
            _codex_credentials_refresh_task,
            _event_loop_watchdog_task,
            _vault_release_sweeper_task,
        )
        if t and not t.done()
    ]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _dispatch_watcher_task = None
    _mock_event_watcher_task = None
    _harness_usage_poller_task = None
    _claude_credentials_refresh_task = None
    _codex_credentials_refresh_task = None
    _vault_release_sweeper_task = None
    try:
        await session_monitor.stop()
    except Exception:
        logger.exception("error during session_monitor.stop()")
    try:
        await worktree_monitor.stop()
        await asyncio.to_thread(save_row_cache, WORKTREE_ROW_CACHE_PATH)
    except Exception:
        logger.exception("error during worktree_monitor.stop()")
    try:
        await resource_monitor.stop()
        resource_monitor.save_state(RESOURCE_MONITOR_STATE_PATH)
    except Exception:
        logger.exception("error during resource_monitor.stop()")
    try:
        _SESSION_LIFECYCLE_WORKER.shutdown()
    except Exception:
        logger.exception("error during session lifecycle worker shutdown")
    # Snapshot bus state after monitors stop so the next process boots into
    # the same seq + buffer state and an epoch restore() can advance.
    # Best-effort: snapshot() itself
    # logs and swallows any exception. Some tests substitute a MockEventBus
    # without snapshot/restore; treat absence of the attribute as a no-op.
    snapshot_fn = getattr(event_bus, "snapshot", None)
    if callable(snapshot_fn):
        try:
            snapshot_fn(EVENT_BUS_STATE_PATH)
        except Exception:
            logger.exception("event_bus.snapshot() raised unexpectedly; continuing")
    # Hand a WARM vault to the next process across a graceful reload
    # (auto-a1pub): the delegate signing key and the persona KEM private key go
    # to the ramfs key cache, and the next boot re-derives the generation keys
    # from the on-disk grants with that KEM key. A CRASH skips this hook, so a
    # non-graceful restart writes nothing and boots locked — fail-closed.
    try:
        from tools.dashboard.unlock_routes import save_vault_across_hot_reload
        save_vault_across_hot_reload()
    except Exception:
        logger.exception(
            "vault hot-reload snapshot raised; the next process boots locked"
        )

@asynccontextmanager
async def _lifespan(app):
    await _on_startup()
    try:
        yield
    finally:
        await _on_shutdown()

class _RequestDurationMiddleware(BaseHTTPMiddleware):
    # A request slower than this almost always means the event loop was
    # blocked (sync work on the loop, or CPU-bound to_thread holding the GIL),
    # NOT that the endpoint is intrinsically heavy — so log it LOUD and
    # greppable instead of burying it at INFO. Pair with the event-loop
    # watchdog below: when the loop stalls, the watchdog names the stall and
    # every request caught in it logs as SLOW-REQUEST, so cause and victims
    # correlate in one grep.
    _SLOW_MS = 1000.0
    _HANG_MS = 5000.0

    async def dispatch(self, request, call_next):
        t0 = time.monotonic()
        response = await call_next(request)
        dur_ms = (time.monotonic() - t0) * 1000
        if dur_ms >= self._HANG_MS:
            logger.error(
                "SLOW-REQUEST(HANG) %s %s %d %.0fms — event loop likely blocked",
                request.method, request.url.path, response.status_code, dur_ms,
            )
        elif dur_ms >= self._SLOW_MS:
            logger.warning(
                "SLOW-REQUEST %s %s %d %.0fms",
                request.method, request.url.path, response.status_code, dur_ms,
            )
        else:
            logger.info("%s %s %d %.1fms", request.method, request.url.path, response.status_code, dur_ms)
        return response


class _FleetJoiningMiddleware(BaseHTTPMiddleware):
    """While this machine is mid-join, every page is the home page.

    A node booted with ``AUTONOMY_FLEET_INVITE`` has exactly one thing the
    operator needs — the comparison code — and it renders on ``/``. Every other
    page is not merely unhelpful but actively misleading: the Machines page
    calls an API that requires global authority the node cannot have yet and so
    reports "Fleet is unavailable", and the onboarding entry offers to create a
    NEW identity, which is the opposite of joining an existing fleet.

    So while ``machine_boot.is_joining()`` holds, redirect page navigations to
    ``/``. ``/api`` is untouched (the join itself runs over it), as are static
    assets and websockets.
    """

    async def dispatch(self, request, call_next):
        path = request.url.path
        if (
            path == "/"
            or path.startswith("/api/")
            or path.startswith("/static/")
            or path.startswith("/ws/")
            # /unlock and /welcome ARE the join-completion flow, not stray
            # navigations to funnel home. Once enrollment delivers the personal
            # armor, the human gate flips on and sends "/" -> "/unlock"; the
            # welcome page's own "Unlock to continue" also targets "/unlock"
            # (next=/welcome?fleet_sync=1). Funnelling those two back to "/"
            # bounced the operator between "/" and "/unlock" forever, so the
            # join could never be finished. Let them through.
            or path == "/unlock"
            or path == "/welcome"
        ):
            return await call_next(request)
        try:
            from tools.network import machine_boot
            # A machine that already holds an identity has finished enrolling,
            # regardless of the marker. Checking the identity here stops a stale
            # marker — one nothing cleared — from redirecting every page and
            # locking the operator out of a fully-enrolled machine's dashboard.
            joining = (
                machine_boot.machine_id(org="machine") is None
                and machine_boot.is_joining()
            )
        except Exception:
            # Fail OPEN: an unreadable marker must never make the dashboard
            # unnavigable. A node that is not joining is the common case.
            # LOUD, though — a silent fail-open here is indistinguishable from
            # "not joining", which is exactly how this middleware appeared to
            # do nothing while the marker was in fact set.
            logger.exception("fleet-joining check failed; not redirecting")
            joining = False
        if joining:
            return RedirectResponse(url="/", status_code=307)
        return await call_next(request)


class _CSPMiddleware(BaseHTTPMiddleware):
    """Phase 1 CSP: blocks dangerous injections while permitting existing inline scripts."""

    _CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'none'"
    )

    _CSP_FRAMEABLE = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'self'"
    )

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        # Allow same-origin framing for attachment URLs (rich-content iframes)
        # and mission screens (the SPA hosts /missions/ documents in a
        # same-origin frame so entering/leaving a mission never unloads the
        # app; 'self' still refuses every foreign embedder).
        if (request.url.path.startswith("/api/attachment/")
                or request.url.path.startswith("/missions/")
                or request.url.path.startswith("/api/mission/screen/")):
            response.headers["Content-Security-Policy"] = self._CSP_FRAMEABLE
        else:
            response.headers["Content-Security-Policy"] = self._CSP
        # The app-shell HTML must NEVER be cached. Static assets are cache-busted
        # via ?v=<static_version>, but that only works if the browser fetches
        # FRESH html to see the new ?v=. The shell had no cache headers, so an
        # iOS home-screen PWA (and plain Safari) could pin a stale shell —
        # serving an old ?v= and therefore a stale voice-capture.js etc., which
        # made client fixes silently not reach the operator. Force-revalidate all
        # HTML; static JS/CSS keep their own (?v=-busted) caching.
        ctype = response.headers.get("content-type", "")
        if ctype.startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


async def _protected_setting_handler(request, exc):
    """A generic-settings mutation tried to write a protected identity
    set without the identity-route capability — refuse with 403 across
    every settings route at once (the guard lives in settings_ops)."""
    return JSONResponse({"error": str(exc)}, status_code=403)


app = Starlette(
    routes=routes,
    lifespan=_lifespan,
    exception_handlers={
        settings_ops.ProtectedSettingError: _protected_setting_handler,
    },
    middleware=[
        Middleware(_RequestDurationMiddleware),
        # Nothing was compressed. A mission screen is one self-contained
        # document -- its state, the platform runtime and the author's own
        # markup all inline, by design, because over a share link there is no
        # origin to fetch a second file from. That document went over the wire
        # at its full size on every open, and mission pages are deliberately
        # no-store, so every open paid it again: seconds of white screen on a
        # phone before anything drew.
        #
        # Inside the duration middleware so the time spent compressing is
        # inside the number that reports how long the request took.
        #
        # Streaming is safe rather than lucky: Starlette excludes
        # `text/event-stream` by default and strips the `; charset` parameter
        # before matching, so the live-update stream is passed straight
        # through uncompressed. Non-HTTP scopes -- websockets -- never reach
        # the responder at all.
        Middleware(GZipMiddleware, minimum_size=1024),
        # Establish one API principal and one trusted graph scope. Dashboard
        # cookies and positively local host tokens have global authority;
        # org-stamped session tokens are forced to their own org regardless of
        # X-Graph-Org. Compatibility traffic is classified but remains open
        # while route policies are migrated.
        Middleware(
            api_auth.ApiIdentityMiddleware,
            authenticate_bearer=authenticate_session_request,
            verify_cookie=unlock_routes.verify_session_token,
            cookie_name=unlock_routes.SESSION_COOKIE,
            authenticate_service=authenticate_service,
        ),
        Middleware(_CSPMiddleware),
        # Mid-join, every page is the home page — see the class docstring.
        # Outside the unlock gate: a joining node has no identity to unlock
        # with, so the gate must not get a chance to send it somewhere else.
        Middleware(_FleetJoiningMiddleware),
        # Innermost: the human unlock gate (fail-open-then-enforce).
        # Covers page loads, fragments, and browser websockets; the
        # agent/container ``/api`` surface passes through untouched.
        # Its enrollment read deliberately IGNORES the request's caller
        # org (X-Graph-Org is client-controlled — honouring it would let
        # anyone name an un-enrolled org and fail the lock open), pinning
        # to the dashboard's own org instead. It sits inside _CSPMiddleware
        # so its redirect/401 responses still carry the standard headers.
        Middleware(unlock_routes.HumanGateMiddleware),
    ],
)


def _timestamped_log_config():
    """Build a uvicorn logging config whose lines carry timestamps.

    Uvicorn's own stdout/stderr lines (startup, shutdown, and the
    "WatchFiles detected changes ... Reloading..." hot-reload notice, all
    emitted via the ``uvicorn.error`` logger) default to a bare
    ``%(levelprefix)s %(message)s`` format with no timestamp, so they land
    in data/dashboard.log untimed — unlike Python-level lines, which carry
    an asctime via ``logging.basicConfig`` above. Prepend ``%(asctime)s`` to
    each uvicorn formatter so every line is timestamped and restarts can be
    counted/timed from the log. The datefmt matches basicConfig's default
    (``YYYY-MM-DD HH:MM:SS,mmm``) for consistency across line sources.
    """
    import copy
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    datefmt = "%Y-%m-%d %H:%M:%S"
    for name, formatter in config.get("formatters", {}).items():
        fmt = formatter.get("fmt", "%(message)s")
        if "%(asctime)s" not in fmt:
            formatter["fmt"] = "%(asctime)s " + fmt
        formatter.setdefault("datefmt", datefmt)
    return config


def main():
    import uvicorn
    uvicorn.run(
        "tools.dashboard.server:app",
        host="0.0.0.0",
        port=8080,
        log_level="info",
        log_config=_timestamped_log_config(),
        reload=True,
        reload_dirs=["tools/dashboard"],
        reload_excludes=["tools/dashboard/tests"],
    )
