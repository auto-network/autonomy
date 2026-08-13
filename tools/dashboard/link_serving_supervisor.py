"""Managed lifecycle for the auto.network serving connector (§5.1).

The serving connector (``tools/dashboard/link_serving`` → the B2
``TunnelConnector``) is an UNATTENDED subprocess: it dials out to the relay
and serves grant-gated targets over E2E channels, with no human present. This
module owns its lifecycle so a publish's promise — "the link actually serves"
— holds without the operator babysitting a process.

The rule, from the operator's model: *if a valid serve-cert is provisioned AND
any link is live, the connector should be running.* So :meth:`ServingSupervisor.ensure`
is an idempotent reconciler — given an org, it brings the world to match that
rule (start if it should run and isn't; stop if it shouldn't and is) — driven
from three triggers:

* **dashboard startup** — ``ensure_all()`` over the orgs it manages, so a
  restart with a provisioned cert + live grants brings serving back up on its
  own;
* **after a publish** — the publish executor calls ``ensure(org)`` once it has
  cached a new grant, so the first publish that provisions a cert also starts
  serving;
* **a watchdog** — a daemon thread re-runs ``ensure_all()`` on an interval, so
  a connector that dies (crash, OOM) is restarted and an expired cert or a
  fully-revoked org is torn down, with no operator involvement.

The signing key never leaves its mode-0600 file (see the ``serve-cert``
schema): the supervisor reads the cert from the settings row, VERIFIES the
on-disk key matches the cert's ``child_pub`` before launching (the layer that
actually holds the key enforces the match the schema validator could not), and
hands both to the connector as ``--key-file`` / ``--cert-file``.

The subprocess ``spawn`` is a seam so the reconciliation logic is testable
without real processes; the default spawns ``python -m
tools.dashboard.link_serving``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from tools.data_paths import resolve_store
from tools.dashboard import link_serving
from tools.dashboard.link_approvals import _load_binding
from tools.dashboard.link_probe import registry_to_relay_ws
from tools.graph import settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_LINK_GRANT_REVISION,
    NETWORK_LINK_GRANT_SET_ID,
    NETWORK_SERVE_CERT_SET_ID,
    SERVE_CERT_SCOPE,
)


# ── provisioning state (also the enrich precondition) ─────────


def _resolve_key_path(stored: object) -> tuple[str | None, str | None]:
    """Resolve a portable key basename, preserving legacy absolute rows.

    New rows contain exactly one filename and move with the node volume.
    Existing absolute rows remain valid in place. Any other relative shape is
    rejected before filesystem access so ``..`` or a nested path cannot
    escape the manifest-rooted serving-key directory.
    """
    if not isinstance(stored, str) or not stored:
        return None, "serve-cert key_path is missing"
    path = Path(stored)
    if path.is_absolute():
        return str(path), None
    if (
        path.name != stored
        or stored in {".", ".."}
        or "/" in stored
        or "\\" in stored
    ):
        return None, "serve-cert key_path must be a bare filename"
    try:
        root = resolve_store("serving_keys").resolve()
        resolved = (root / stored).resolve()
        resolved.relative_to(root)
    except Exception:
        return (
            None,
            "serve-cert key_path does not resolve inside the serving-key store",
        )
    return str(resolved), None


def serve_cert_state(org: str | None, *, now: float | None = None) -> dict:
    """The org's serve-cert provisioning state → ``{status, ...}``.

    Owning-scope read (P2): a peer's serve-cert must never make this org look
    provisioned. ``status`` is one of:

    * ``ok`` — a non-expired cert whose key file is present (the browser need
      NOT mint; the connector CAN run);
    * ``missing`` — no serve-cert row at all;
    * ``expired`` — a row whose delegate has passed ``not_after``;
    * ``key-invalid`` — a relative key locator is not a safe basename or its
      manifest root cannot be resolved;
    * ``key-missing`` — a row whose ``key_path`` file is gone.

    This doubles as the publish-time precondition: the approve step mints a
    fresh delegate iff the status is anything but ``ok``.
    """
    now = time.time() if now is None else now
    try:
        members = settings_ops.read_owned_set(NETWORK_SERVE_CERT_SET_ID, org=org).members
    except Exception:
        return {"status": "missing"}
    rows = [m.payload for m in members if isinstance(m.payload, dict)]
    if not rows:
        return {"status": "missing"}
    # v1 keeps one delegate per org; if several rows exist, the freshest
    # (latest not_after) is the one to serve with.
    row = max(rows, key=lambda p: p.get("not_after") or 0)
    not_after = row.get("not_after")
    if not isinstance(not_after, int) or now >= not_after:
        return {"status": "expired", "row": row}
    key_path, key_error = _resolve_key_path(row.get("key_path"))
    if key_error is not None:
        return {"status": "key-invalid", "row": row, "error": key_error}
    if not os.path.isfile(key_path):
        return {"status": "key-missing", "row": row}
    return {
        "status": "ok",
        "row": row,
        "cert": row.get("cert"),
        "key_path": key_path,
        "not_after": not_after,
    }


def serve_cert_ok(org: str | None, *, now: float | None = None) -> bool:
    """True iff a usable serve-cert is provisioned — the enrich precondition."""
    return serve_cert_state(org, now=now)["status"] == "ok"


def _has_live_grant(org: str | None, now: float) -> bool:
    """Any non-expired grant in the org's own cache — the 'links are live'
    half of the run condition (reuses the I9 validity check)."""
    try:
        members = settings_ops.read_owned_set(
            NETWORK_LINK_GRANT_SET_ID,
            org=org,
            target_revision=NETWORK_LINK_GRANT_REVISION,
        ).members
    except Exception:
        return False
    for m in members:
        if link_serving._grant_valid(m.payload, m.key, now) is not None:
            return True
    return False


# ── key/cert materialization for the subprocess ───────────────


def _verify_key_matches(cert_wire: str, key_path: str) -> tuple[bool, str]:
    """The on-disk key must be the one the cert delegates to, or we do not
    launch — the schema validator could not see the file, so the check lives
    here, at the layer that holds the key."""
    from tools.network.idkit import DelegationCert, KeyPair
    try:
        with open(key_path) as fh:
            key = KeyPair.from_private_hex(fh.read().strip())
    except Exception as e:
        return False, f"serve key file unreadable ({key_path}): {e}"
    try:
        cert = DelegationCert.from_json(cert_wire)
    except Exception as e:
        return False, f"serve cert does not parse: {e}"
    if SERVE_CERT_SCOPE not in cert.scope:
        return False, f"serve cert lacks {SERVE_CERT_SCOPE} scope"
    if key.public_hex != cert.child_pub:
        return False, "serve key does not match the cert's child_pub"
    return True, "ok"


def _cert_path_for(key_path: str) -> str:
    return os.path.splitext(key_path)[0] + ".cert"


def _log_path_for(key_path: str) -> str:
    return os.path.splitext(key_path)[0] + ".log"


def _control_path_for(key_path: str) -> str:
    """The loopback control-listener descriptor file the connector writes
    ({port, auth}) so the dashboard can drive D19 control ops on its
    tunnel. Sits next to the serving key, same convention as .cert/.log."""
    return os.path.splitext(key_path)[0] + ".ctl"


class TunnelUnavailable(RuntimeError):
    """No live serving tunnel to carry a control op — names the remedy.

    ``kind`` classifies WHERE the op failed so a caller can safely retry only
    the pre-write cases (the connector/tunnel is still coming up and no control
    frame ever reached the registry): ``no-listener``, ``unreachable``, and
    ``no-tunnel`` are pre-write and retryable; ``no-delegate`` and ``closed``
    are not (``closed`` is ambiguous — the frame may have been sent)."""

    def __init__(self, message: str, *, kind: str | None = None):
        super().__init__(message)
        self.kind = kind


def control(org: str | None, op: str, args: dict, *, timeout: float = 12.0) -> dict:
    """Drive one D19 control op on *org*'s serving tunnel (register §3/§4).

    Reads the connector's ``.ctl`` descriptor, opens the loopback control
    listener, and returns the connector's reply. Raises
    :class:`TunnelUnavailable` when no connector/tunnel is up — the caller
    (the publish executor) turns that into ``ensure(org)`` + a retry."""
    state = serve_cert_state(org)
    key_path = state.get("key_path")
    if not key_path:
        raise TunnelUnavailable(
            "no serving delegate is provisioned for this org — provision "
            "serving, then retry", kind="no-delegate")
    ctl_path = _control_path_for(key_path)
    try:
        with open(ctl_path) as fh:
            descriptor = json.load(fh)
        port = int(descriptor["port"])
        auth = descriptor["auth"]
    except (OSError, ValueError, KeyError) as exc:
        raise TunnelUnavailable(
            "the serving connector is not running (no control listener) — "
            f"start serving and retry ({exc})", kind="no-listener") from exc

    request = json.dumps({"auth": auth, "op": op, "args": args}) + "\n"
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.sendall(request.encode("utf-8"))
            sock.settimeout(timeout)
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except OSError as exc:
        raise TunnelUnavailable(
            f"could not reach the serving connector's control listener ({exc})",
            kind="unreachable") from exc
    if b"\n" not in buf:
        raise TunnelUnavailable("serving connector closed the control connection",
                               kind="closed")
    reply = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    if reply.get("error_kind") == "no-tunnel":
        raise TunnelUnavailable(reply.get("error", "no live tunnel"), kind="no-tunnel")
    return reply


def _materialize_cert(key_path: str, cert_wire: str) -> str:
    """Write the (public) cert next to the key file for ``--cert-file``.
    Atomic replace so a concurrent launch never reads a half-written cert."""
    cert_path = _cert_path_for(key_path)
    tmp = cert_path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(cert_wire)
    os.replace(tmp, cert_path)
    return cert_path


def _connector_command(binding: dict, org: str | None, key_path: str,
                       cert_path: str) -> tuple[list, dict]:
    """The argv + env to launch the serving connector against *binding*."""
    argv = [
        sys.executable, "-m", "tools.dashboard.link_serving",
        "--relay", registry_to_relay_ws(binding["registry_url"]),
        "--org", binding["org_uuid"],
        "--key-file", key_path,
        "--cert-file", cert_path,
        "--control-file", _control_path_for(key_path),
    ]
    if org:
        argv += ["--graph-org", org]
    # Inherit the environment so the subprocess reads the SAME GRAPH_DB (its
    # grant cache) and PYTHONPATH as the dashboard.
    return argv, dict(os.environ)


# ── the managed subprocess ────────────────────────────────────


class _Proc:
    """A spawned connector process. ``alive``/``stop`` are all the supervisor
    needs; the connector self-reconnects to the relay on its own, so the
    supervisor only supervises the OS process, not the tunnel."""

    def __init__(self, popen, ctl_path: str | None = None):
        self._p = popen
        self._ctl_path = ctl_path

    def alive(self) -> bool:
        return self._p.poll() is None

    def stop(self) -> None:
        try:
            if self._p.poll() is None:
                self._p.terminate()
                try:
                    self._p.wait(timeout=5)
                except Exception:
                    self._p.kill()
        finally:
            # The connector removes its own .ctl on a clean exit; on a kill
            # it cannot, so the supervisor sweeps it — a stale descriptor
            # would otherwise point control() at a dead listener.
            if self._ctl_path:
                with contextlib.suppress(OSError):
                    os.remove(self._ctl_path)


def _default_spawn(argv: list, env: dict, *, log_path: str | None = None,
                   ctl_path: str | None = None):
    out = open(log_path, "ab") if log_path else subprocess.DEVNULL
    return _Proc(subprocess.Popen(argv, env=env, stdout=out, stderr=out),
                 ctl_path=ctl_path)


class ServingSupervisor:
    """Reconciles serving connectors to the "valid cert + live grants" rule.

    Thread-safe: ``ensure``/``ensure_all`` take a lock, so the watchdog thread
    and the post-publish call cannot race a double-spawn. *spawn* is the
    subprocess seam (``spawn(argv, env, log_path=...)`` → handle with
    ``alive()``/``stop()``); *now* is the injectable clock.
    """

    def __init__(self, *, spawn=None, now=None):
        self._spawn = spawn or _default_spawn
        self._now = now or time.time
        self._procs: dict = {}       # org -> handle
        self._managed: set = set()   # orgs seen via ensure(), re-checked by the watchdog
        self._started_at: dict = {}  # org -> launch time; fresh-tunnel grace
        self._grace_s: float = 20.0  # one watchdog interval; a fresh tunnel is skipped once
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._watchdog: threading.Thread | None = None

    def ensure(self, org: str | None) -> dict:
        """Bring serving for *org* to match the rule. Idempotent; returns
        ``{running, reason}``."""
        with self._lock:
            self._managed.add(org)
            return self._reconcile(org)

    def ensure_all(self) -> None:
        with self._lock:
            for org in list(self._managed):
                try:
                    self._reconcile(org)
                except Exception:
                    pass  # one org's failure must not stall the others

    def start(self, org: str | None) -> dict:
        """Launch serving for an imminent first publish WITHOUT requiring a live
        grant — on a first publish the grant is created BY riding this tunnel,
        so it cannot pre-exist. The watchdog stays the only teardown: the
        fresh-tunnel grace keeps it from reaping a connector during the one
        interval it takes the publish to cache its grant. Idempotent."""
        with self._lock:
            self._managed.add(org)
            proc = self._procs.get(org)
            if proc is not None and proc.alive():
                return {"running": True, "reason": "already-running"}
            self._procs.pop(org, None)
            state = serve_cert_state(org, now=self._now())
            if state["status"] != "ok":
                return {"running": False, "reason": state["status"]}
            return self._launch(org, state)

    def _reconcile(self, org: str | None) -> dict:
        now = self._now()
        state = serve_cert_state(org, now=now)
        proc = self._procs.get(org)
        should_run = state["status"] == "ok" and _has_live_grant(org, now)

        if not should_run:
            # Fresh-tunnel grace: a connector just launched for a first publish
            # has no live grant YET — the grant is created by riding it. Skip
            # the "no live links" teardown for one watchdog interval so the
            # publish can cache that grant first. Pure in-memory launch-time
            # comparison; a bad/expired cert is torn down immediately regardless.
            if (proc is not None and proc.alive() and state["status"] == "ok"
                    and now - self._started_at.get(org, 0.0) < self._grace_s):
                return {"running": True, "reason": "fresh-grace"}
            if proc is not None:
                proc.stop()
                self._procs.pop(org, None)
            self._started_at.pop(org, None)
            reason = state["status"] if state["status"] != "ok" else "no-live-grants"
            return {"running": False, "reason": reason}

        if proc is not None and proc.alive():
            return {"running": True, "reason": "already-running"}
        # A dead handle: drop it and relaunch below.
        self._procs.pop(org, None)
        return self._launch(org, state)

    def _launch(self, org: str | None, state: dict) -> dict:
        """Spawn the connector for *org* (``state`` must be an ``ok``
        serve-cert state) and record its launch time for the fresh-tunnel
        grace. Callers hold the lock."""
        ok, detail = _verify_key_matches(state["cert"], state["key_path"])
        if not ok:
            return {"running": False, "reason": detail}
        binding, binding_error = _load_binding(org)
        if binding_error:
            return {"running": False, "reason": binding_error}
        cert_path = _materialize_cert(state["key_path"], state["cert"])
        ctl_path = _control_path_for(state["key_path"])
        # A stale descriptor from a previous killed connector would mislead
        # control() until the new connector rewrites it; clear it up front.
        with contextlib.suppress(OSError):
            os.remove(ctl_path)
        argv, env = _connector_command(binding, org, state["key_path"], cert_path)
        self._procs[org] = self._spawn(
            argv, env, log_path=_log_path_for(state["key_path"]), ctl_path=ctl_path)
        self._started_at[org] = self._now()
        return {"running": True, "reason": "launched"}

    def start_watchdog(self, interval: float = 20.0) -> None:
        """Periodically re-reconcile every managed org — restart the dead,
        tear down the expired/revoked. No-op if already running."""
        self._grace_s = interval  # a fresh tunnel is skipped for exactly one interval
        if self._watchdog is not None:
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(interval):
                self.ensure_all()

        self._watchdog = threading.Thread(
            target=loop, name="serving-supervisor", daemon=True)
        self._watchdog.start()

    def stop_all(self) -> None:
        """Stop the watchdog and every managed connector (dashboard shutdown)."""
        self._stop.set()
        watchdog = self._watchdog
        with self._lock:
            for proc in self._procs.values():
                try:
                    proc.stop()
                except Exception:
                    pass
            self._procs.clear()
            self._managed.clear()
            self._started_at.clear()
        if watchdog is not None and watchdog is not threading.current_thread():
            watchdog.join(timeout=2)
        self._watchdog = None

    def running_orgs(self) -> list:
        with self._lock:
            return [org for org, p in self._procs.items() if p.alive()]


# ── process-wide singleton ────────────────────────────────────
#
# One supervisor per dashboard process: the startup hook, the post-publish
# ensure, and the watchdog all reconcile the SAME set of connectors.

_SINGLETON: ServingSupervisor | None = None
_SINGLETON_LOCK = threading.Lock()


def get_supervisor() -> ServingSupervisor:
    global _SINGLETON
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = ServingSupervisor()
    return _SINGLETON


def _discover_startup_orgs() -> list[str | None]:
    """Return every local Settings scope that may own serving state.

    A test-pinned ``GRAPH_DB`` is one physical database, so only its resolved
    scope is meaningful. A real dashboard owns multiple per-org databases and
    must reconcile all of them after a reload; limiting startup to the caller
    org strands every other org's connector outside the watchdog.
    """
    configured = os.environ.get("GRAPH_ORG") or None
    if os.environ.get("GRAPH_DB"):
        # A pinned GRAPH_DB is the single-database test world; per-org DBs
        # alongside it mean a mis-shaped deployment whose org connectors can
        # never reconcile (auto-sb0g8) — say so instead of failing silently.
        try:
            from tools.graph import org_ops as _org_ops
            if _org_ops.list_orgs():
                logging.getLogger(__name__).warning(
                    "GRAPH_DB is pinned but per-org databases exist under the "
                    "orgs dir; serving reconciles ONLY the pinned scope %r — "
                    "org connectors will not start (unset GRAPH_DB on "
                    "multi-org nodes)", configured,
                )
        except Exception:
            pass
        return [configured]

    from tools.graph import org_ops

    discovered: list[str | None] = [None]
    discovered.extend(ref.slug for ref in org_ops.list_orgs())
    if configured is not None and configured not in discovered:
        discovered.append(configured)
    return discovered


def bootstrap(orgs=None) -> ServingSupervisor:
    """Dashboard-startup entry: reconcile serving for each org, then arm the
    watchdog. So a restart with a provisioned cert + live grants brings serving
    back up on its own, and the watchdog keeps it reconciled thereafter.

    *orgs* defaults to every local org database plus the legacy scopeless
    database. Never raises: startup must not be held hostage by a serving
    hiccup.
    """
    supervisor = get_supervisor()
    if orgs is None:
        orgs = _discover_startup_orgs()
    for org in orgs:
        try:
            supervisor.ensure(org)
        except Exception:
            pass
    supervisor.start_watchdog()
    return supervisor
