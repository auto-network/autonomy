"""Managed lifecycle for the auto.network serving connector (§5.1).

The serving connector (``tools/dashboard/link_serving`` → the B2
``TunnelConnector``) is an UNATTENDED subprocess: it dials out to the relay
and serves grant-gated targets over E2E channels, with no human present. This
module owns its lifecycle so a publish's promise — "the link actually serves"
— holds without the operator babysitting a process.

The rule, from the operator's model: *if a valid serve-cert is provisioned AND
any artifact grant or bound Service publication is live, the connector should
be running.* So :meth:`ServingSupervisor.ensure`
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
schema): the supervisor reads both context-specific certs from the settings
row, VERIFIES the on-disk key matches their shared ``child_pub`` before
launching, and hands them to the connector separately. ``--cert-file`` is
registry admission; ``--channel-cert-file`` is the identity-neutral viewer
handshake.

The subprocess ``spawn`` is a seam so the reconciliation logic is testable
without real processes; the default spawns ``python -m
tools.dashboard.link_serving``.

Several workers of one dashboard installation may share one org store.  A
non-blocking file lock beside the control descriptor elects exactly one
connector owner per org; the other local workers use that owner's loopback
listener and never touch its descriptor or create a competing relay tunnel.
Mock dashboards do not start this supervisor at all; isolated containers do
not share this lock and must carry their own mock or node identity boundary.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import fcntl
import json
import logging
import os
import re
import signal
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
    NETWORK_SERVE_CERT_REVISION,
    NETWORK_SERVE_CERT_SET_ID,
    SERVE_CERT_SCOPE,
)


_PERSONA_PUB_RE = re.compile(r"^[0-9a-f]{64}$")
CONNECTOR_STARTUP_TIMEOUT_S = 60.0

#: How long a connector that HAS served may stay unreachable before the
#: supervisor replaces it. A connector owns its own reconnect loop, so a
#: brief gap is normal and restarting into it would fight that loop --
#: hence a bound far longer than startup rather than the same one. But a
#: child that served once and then wedged used to be trusted forever:
#: the startup deadline only ever applied to one that had NEVER served,
#: so the single failure that leaves a process alive and permanently
#: unable to serve was the single failure nothing reaped.
CONNECTOR_RECONNECT_TIMEOUT_S = 600.0

_log = logging.getLogger(__name__)

#: ``prctl`` option number (``linux/prctl.h``): send a signal when the parent
#: DIES, however it dies. This is the lifeline ``stop_all()`` cannot be — that
#: only runs on a graceful shutdown, and a crash/SIGKILL/reload-without-cleanup
#: is exactly the case that orphaned five generations of connectors onto
#: systemd. Set in the child at spawn so the kernel reaps it with its dashboard.
_PR_SET_PDEATHSIG = 1

#: The libc handle is opened ONCE at import, never inside the post-fork
#: ``preexec_fn``: ``dlopen`` after ``fork()`` in a threaded process can deadlock
#: on the loader's own locks. Only the two syscalls below run in the child.
try:
    _LIBC = ctypes.CDLL(
        ctypes.util.find_library("c") or "libc.so.6", use_errno=True
    )
except OSError:  # pragma: no cover - libc is always present on Linux
    _LIBC = None

#: The connector's module invocation, matched in ``/proc/<pid>/cmdline`` when
#: reaping strays. A leaked connector is ``python -m tools.dashboard.link_serving
#: --org <uuid> ...`` reparented to systemd — the supervisor cannot see it in
#: ``self._procs`` (it is not its child), so it finds it by this signature.
_CONNECTOR_MODULE = "tools.dashboard.link_serving"


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

This is the cheap pre-unlock check: every organization-root sign-on mints a
fresh serving credential iff the status is anything but ``ok``.
    """
    now = time.time() if now is None else now
    try:
        members = settings_ops.read_owned_set(
            NETWORK_SERVE_CERT_SET_ID,
            org=org,
            target_revision=NETWORK_SERVE_CERT_REVISION,
        ).members
    except Exception:
        return {"status": "missing"}
    rows = [m.payload for m in members if isinstance(m.payload, dict)]
    if not rows:
        return {"status": "missing"}
    # The keyed set keeps one credential per org. If corrupt storage contains
    # several rows, select the freshest valid candidate deterministically.
    row = max(rows, key=lambda p: p.get("not_after") or 0)
    not_after = row.get("not_after")
    if not isinstance(not_after, int) or now >= not_after:
        return {"status": "expired", "row": row}
    if not isinstance(row.get("viewer_cert"), str):
        return {
            "status": "identity-invalid",
            "row": row,
            "error": (
                "serving credential lacks the required identity-neutral "
                "viewer certificate; reprovision serving"
            ),
        }
    try:
        from tools.network.idkit import DelegationCert

        cert = DelegationCert.from_json(row.get("cert"))
        viewer_cert = DelegationCert.from_json(row.get("viewer_cert"))
    except Exception as exc:
        return {
            "status": "identity-invalid",
            "row": row,
            "error": f"serve cert does not parse: {exc}",
        }
    if (
        tuple(cert.scope) != (SERVE_CERT_SCOPE,)
        or cert.parent_cert is not None
        or cert.subject.kind != "persona"
        or _PERSONA_PUB_RE.fullmatch(cert.subject.id) is None
    ):
        return {
            "status": "identity-invalid",
            "row": row,
            "error": (
                "serve cert must be a direct root-issued, persona-scoped "
                "tunnel:serve delegate; reprovision serving"
            ),
        }
    if (
        tuple(viewer_cert.scope) != (SERVE_CERT_SCOPE,)
        or viewer_cert.parent_cert is not None
        or viewer_cert.subject.kind != "operator"
        or viewer_cert.subject.id != viewer_cert.child_pub
        or viewer_cert.child_pub != cert.child_pub
        or viewer_cert.org != cert.org
        or viewer_cert.not_before != cert.not_before
        or viewer_cert.not_after != cert.not_after
    ):
        return {
            "status": "identity-invalid",
            "row": row,
            "error": (
                "viewer cert must be a direct root-issued, identity-neutral "
                "certificate over the same serving child, org, and lifetime; "
                "reprovision serving"
            ),
        }
    key_path, key_error = _resolve_key_path(row.get("key_path"))
    if key_error is not None:
        return {"status": "key-invalid", "row": row, "error": key_error}
    if not os.path.isfile(key_path):
        return {"status": "key-missing", "row": row}
    return {
        "status": "ok",
        "row": row,
        "cert": row.get("cert"),
        "viewer_cert": row.get("viewer_cert"),
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


def _has_live_service_publication(org: str | None) -> bool:
    """True when *org* has an active/paused Service with a bound target.

    Services deliberately have no visitor-grant layer: application HTTP auth
    remains the application's responsibility.  Consequently their connector
    lifecycle cannot be inferred from ``network.link-grant``.  The durable
    reservation plus target Settings are the existing publication authority.
    """
    if org is None:
        return False
    try:
        from tools.dashboard import service_publication

        target_ids = {
            row.get("reservation_id")
            for row in service_publication.list_service_targets(org)
            if isinstance(row, dict)
        }
        return any(
            isinstance(row, dict)
            and row.get("state") in {"active", "paused"}
            and row.get("reservation_id") in target_ids
            for row in service_publication.list_reservations(org)
        )
    except Exception:
        # Settings read failures remain fail-closed: do not keep a public
        # connector alive based on state we could not establish.
        return False


def _is_personal_fleet_scope(org: str | None) -> bool:
    """True when *org* is the personal fleet's own serving scope.

    The personal fleet serves under the personal store (``org=None`` /
    ``'personal'``) and, once the personal root is registered as its own org,
    the deterministic ``personal_org_uuid`` (both stable, both never change).
    A collaborative org — a real slug/uuid — is never matched, so its serving
    still requires a genuine published grant.
    """
    from tools.graph import settings_ops

    if settings_ops._resolve_org_arg(org) is None:
        return True
    try:
        from tools.network import fleet_runtime, fleet_tunnel_server

        root_pub = fleet_tunnel_server._personal_root_pub()
        return bool(root_pub) and org == fleet_runtime.personal_org_uuid(root_pub)
    except Exception:
        return False


# ── key/cert materialization for the subprocess ───────────────


def _verify_key_matches(cert_wire: str, viewer_cert_wire: str,
                        key_path: str) -> tuple[bool, str]:
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
        viewer_cert = DelegationCert.from_json(viewer_cert_wire)
    except Exception as e:
        return False, f"serve cert does not parse: {e}"
    if tuple(cert.scope) != (SERVE_CERT_SCOPE,):
        return False, f"serve cert scope is not exactly {SERVE_CERT_SCOPE}"
    if cert.parent_cert is not None:
        return False, "serve cert is not issued directly by the org root"
    if (
        cert.subject.kind != "persona"
        or _PERSONA_PUB_RE.fullmatch(cert.subject.id) is None
    ):
        return False, "serve cert does not carry a canonical persona subject"
    if key.public_hex != cert.child_pub:
        return False, "serve key does not match the cert's child_pub"
    if (
        viewer_cert.child_pub != cert.child_pub
        or tuple(viewer_cert.scope) != (SERVE_CERT_SCOPE,)
        or viewer_cert.parent_cert is not None
        or viewer_cert.org != cert.org
        or viewer_cert.not_before != cert.not_before
        or viewer_cert.not_after != cert.not_after
        or viewer_cert.subject.kind != "operator"
        or viewer_cert.subject.id != viewer_cert.child_pub
    ):
        return False, (
            "viewer cert is not the identity-neutral direct-root certificate "
            "for the same serving key, org, and lifetime"
        )
    return True, "ok"


def _cert_path_for(key_path: str) -> str:
    return os.path.splitext(key_path)[0] + ".cert"


def _viewer_cert_path_for(key_path: str) -> str:
    return os.path.splitext(key_path)[0] + ".viewer.cert"


def _log_path_for(key_path: str) -> str:
    return os.path.splitext(key_path)[0] + ".log"


def _control_path_for(key_path: str) -> str:
    """The loopback control-listener descriptor file the connector writes
    ({port, auth}) so the dashboard can drive D19 control ops on its
    tunnel. Sits next to the serving key, same convention as .cert/.log."""
    return os.path.splitext(key_path)[0] + ".ctl"


def _lock_path_for(key_path: str) -> str:
    """Cross-process ownership lock for one org's serving connector."""
    return _control_path_for(key_path) + ".lock"


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
                       cert_path: str, viewer_cert_path: str) -> tuple[list, dict]:
    """The argv + env to launch the serving connector against *binding*."""
    argv = [
        sys.executable, "-m", "tools.dashboard.link_serving",
        "--relay", registry_to_relay_ws(binding["registry_url"]),
        "--org", binding["org_uuid"],
        "--key-file", key_path,
        "--cert-file", cert_path,
        "--channel-cert-file", viewer_cert_path,
        "--control-file", _control_path_for(key_path),
    ]
    if org:
        argv += ["--graph-org", org]
    # Inherit the environment so the subprocess reads the SAME GRAPH_DB (its
    # grant cache) and PYTHONPATH as the dashboard.
    return argv, dict(os.environ)


# ── the managed subprocess ────────────────────────────────────


class _Proc:
    """A spawned connector process plus its authenticated serving readiness.

    The connector self-reconnects after it has served successfully.  A PID
    which never completes its first tunnel hello is different: without the
    local readiness probe it can remain alive forever while publishing is
    impossible.
    """

    def __init__(self, popen, ctl_path: str | None = None):
        self._p = popen
        self._ctl_path = ctl_path

    def pid(self) -> int | None:
        return getattr(self._p, "pid", None)

    def alive(self) -> bool:
        return self._p.poll() is None

    def serving(self) -> bool:
        """True only when the child reports a completed tunnel handshake."""
        if not self.alive() or not self._ctl_path:
            return False
        try:
            with open(self._ctl_path) as fh:
                descriptor = json.load(fh)
            request = json.dumps({
                "auth": descriptor["auth"],
                "op": "connector-status",
                "args": {},
            }) + "\n"
            with socket.create_connection(
                ("127.0.0.1", int(descriptor["port"])), timeout=0.5
            ) as sock:
                sock.sendall(request.encode("utf-8"))
                sock.settimeout(0.5)
                buf = b""
                while b"\n" not in buf:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
            reply = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
            return reply.get("ok") is True and reply.get("serving") is True
        except (OSError, ValueError, KeyError, TypeError):
            return False

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


def _make_pdeathsig_preexec(expected_ppid: int):
    """Build the child's ``preexec_fn`` that ties its life to the dashboard.

    Runs in the forked child *before* exec, so ``PR_SET_PDEATHSIG`` survives
    into the connector image (the disposition is cleared on ``fork`` but
    preserved across ``execve``). Two things it must get right:

    * **The kernel watches the forking THREAD, not the process.** If a
      short-lived thread spawns the connector, its exit would fire the death
      signal prematurely. The supervisor only ever spawns from long-lived
      threads (the watchdog, or the asyncio default executor's persistent
      workers), which keeps that disposition tied to the dashboard's lifetime.
    * **The parent can die between ``fork`` and this call.** Then the death
      signal is armed against an already-dead thread and never arrives, leaving
      the very orphan this exists to prevent. Re-check ``getppid()`` against the
      pid we forked from and exit immediately if we were already reparented.
    """

    def _preexec() -> None:  # pragma: no cover - runs only in the forked child
        if _LIBC is not None:
            _LIBC.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
        if os.getppid() != expected_ppid:
            os._exit(0)

    return _preexec


def _default_spawn(argv: list, env: dict, *, log_path: str | None = None,
                   ctl_path: str | None = None):
    out = open(log_path, "ab") if log_path else subprocess.DEVNULL
    child_env = dict(env)
    # A supervised connector's diagnostics must reach its file immediately;
    # otherwise a crash/reconnect loop looks like a frozen healthy process.
    child_env["PYTHONUNBUFFERED"] = "1"
    try:
        popen = subprocess.Popen(
            argv, env=child_env, stdout=out, stderr=out,
            # Tie the connector's life to this dashboard's: the kernel signals
            # it whenever the parent dies, so a crash/SIGKILL/reload can no
            # longer leave it orphaned and fighting for the relay slot.
            preexec_fn=_make_pdeathsig_preexec(os.getpid()),
        )
    finally:
        if out is not subprocess.DEVNULL:
            out.close()  # the child retains its duplicated descriptor
    return _Proc(popen, ctl_path=ctl_path)


def _iter_connector_pids(org_uuid: str):
    """Yield pids of running serving connectors for *org_uuid* — ours or not.

    Scans ``/proc`` for ``python -m tools.dashboard.link_serving --org
    <org_uuid>``. This is how the supervisor finds the leaked generations it
    did not spawn (reparented to systemd, invisible in ``self._procs``); the
    caller filters out the pids it owns before terminating the rest.
    """
    proc_root = "/proc"
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"{proc_root}/{entry}/cmdline", "rb") as fh:
                parts = fh.read().split(b"\0")
        except OSError:
            continue  # the process exited mid-scan, or is not ours to read
        args = [p.decode("utf-8", "replace") for p in parts if p]
        if _CONNECTOR_MODULE not in args:
            continue
        try:
            org_at = args.index("--org")
            if args[org_at + 1] != org_uuid:
                continue
        except (ValueError, IndexError):
            continue
        try:
            yield int(entry)
        except ValueError:
            continue


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not signalable by us
    return True


def _terminate_pid(pid: int) -> None:
    """SIGTERM a stray connector, escalating to SIGKILL if it lingers.

    Bounded so a wedged orphan cannot stall reconciliation: a short poll after
    the term, then a kill. A stray that already exited is a no-op.
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        _log.warning("cannot terminate stray serving connector pid=%s "
                     "(not permitted)", pid)
        return
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)


class ServingSupervisor:
    """Reconciles serving connectors to the "valid cert + live grants" rule.

    Thread-safe: ``ensure``/``ensure_all`` take a lock, so the watchdog thread
    and the post-publish call cannot race a double-spawn. *spawn* is the
    subprocess seam (``spawn(argv, env, log_path=...)`` → handle with
    ``alive()``/``serving()``/``stop()``); *now* is the injectable clock.
    """

    def __init__(self, *, spawn=None, now=None):
        self._spawn = spawn or _default_spawn
        self._now = now or time.time
        self._procs: dict = {}       # org -> handle
        self._credentials: dict = {} # org -> exact (both cert wires, key path) launched
        self._managed: set = set()   # orgs seen via ensure(), re-checked by the watchdog
        self._started_at: dict = {}  # org -> launch time; fresh-tunnel grace
        self._grace_s: float = CONNECTOR_STARTUP_TIMEOUT_S
        self._last_served: dict = {}  # org -> last time observed serving
        self._locks: dict = {}       # org -> open file holding flock ownership
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
            eligibility = self._fleet_eligibility()
            if not eligibility.allowed:
                return self._stop_for_fleet_assignment(org, eligibility)
            proc = self._procs.get(org)
            if proc is not None and proc.alive():
                state = serve_cert_state(org, now=self._now())
                if (
                    state["status"] == "ok"
                    and self._credentials.get(org)
                    == (state["cert"], state["viewer_cert"], state["key_path"])
                ):
                    current = self._live_process_state(org, proc)
                    if current is not None:
                        return current
                proc.stop()
                self._procs.pop(org, None)
                self._credentials.pop(org, None)
                self._last_served.pop(org, None)
            self._procs.pop(org, None)
            state = serve_cert_state(org, now=self._now())
            if state["status"] != "ok":
                return {"running": False, "reason": state["status"]}
            return self._launch(org, state)

    def _reconcile(self, org: str | None) -> dict:
        now = self._now()
        eligibility = self._fleet_eligibility()
        if not eligibility.allowed:
            return self._stop_for_fleet_assignment(org, eligibility)
        state = serve_cert_state(org, now=now)
        proc = self._procs.get(org)
        # The personal fleet's tunnel must stay online whenever the fleet has
        # members, independent of any transient invite grant: fleet sync rides
        # the STABLE personal_org_uuid + machine_id, and the invite link is only
        # the bootstrap (it expires and is torn down). Operator directive
        # 2026-08-23 — a personal fleet with members always serves. Collaborative
        # orgs are unaffected: _is_personal_fleet_scope excludes them, so they
        # still require a genuine live grant.
        fleet_has_members = (eligibility.active_machine_count or 0) >= 2
        should_run = state["status"] == "ok" and (
            _has_live_grant(org, now)
            or _has_live_service_publication(org)
            or (fleet_has_members and _is_personal_fleet_scope(org))
        )

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
                self._credentials.pop(org, None)
            # While we still hold the ownership lock, sweep any orphan for this
            # org: a connector left serving on a revoked/expired org keeps
            # displacing everyone else's relay slot exactly like the leaked
            # generations. Only when we own the lock — otherwise a live sibling
            # dashboard's connector is not ours to kill.
            if org in self._locks:
                self._reap_strays(org)
            self._started_at.pop(org, None)
            self._last_served.pop(org, None)
            self._release_lock(org)
            reason = state["status"] if state["status"] != "ok" else "no-live-grants"
            return {"running": False, "reason": reason}

        if proc is not None and proc.alive():
            if self._credentials.get(org) == (
                state["cert"], state["viewer_cert"], state["key_path"]
            ):
                current = self._live_process_state(org, proc)
                if current is not None:
                    return current
                # The child stayed alive but never completed its first hello
                # before the startup deadline.  Replace this genuinely wedged
                # process; a connector observed serving once owns its normal
                # reconnect lifecycle and is not churned here.
                proc.stop()
                self._procs.pop(org, None)
                self._credentials.pop(org, None)
                self._last_served.pop(org, None)
                return self._launch(org, state)
            # Provisioning replaced this org's credential. Keeping the old
            # process alive would leave it presenting the superseded cert
            # forever, because ensure() otherwise treats any live PID as
            # healthy. Stop and relaunch on the exact new credential.
            proc.stop()
            self._procs.pop(org, None)
            self._credentials.pop(org, None)
            self._last_served.pop(org, None)
        # A dead handle: drop it and relaunch below.
        self._procs.pop(org, None)
        self._credentials.pop(org, None)
        return self._launch(org, state)

    @staticmethod
    def _fleet_eligibility():
        """Read the temporary personal Fleet assignment at reconciliation.

        Kept behind one lazy import so deleting the compatibility mechanism in
        ``auto-clune.7`` is a literal removal of this seam rather than a
        permanent dependency hidden across the supervisor.
        """
        from tools.network import fleet_tunnel_server

        return fleet_tunnel_server.state()

    def _stop_for_fleet_assignment(self, org, eligibility) -> dict:
        """Stop local ownership when another roster machine is selected."""
        proc = self._procs.pop(org, None)
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.stop()
        self._credentials.pop(org, None)
        self._started_at.pop(org, None)
        self._last_served.pop(org, None)
        self._release_lock(org)
        result = {"running": False, "reason": eligibility.reason}
        if eligibility.selected_machine_id is not None:
            result["selected_machine_id"] = eligibility.selected_machine_id
        return result

    def _live_process_state(self, org, proc) -> dict | None:
        """Classify an alive, correctly credentialed child.

        ``None`` means the caller must replace it, for either of the two ways
        a live PID can be useless: it never served and is past the startup
        deadline, or it served once and has now been unreachable past the
        reconnect deadline.

        Between those, a child that has served owns its own reconnect loop and
        is left alone -- restarting into a reconnect would fight it with a
        second competing retry loop. That deference is why the bound exists:
        unbounded, it made "alive and permanently unable to serve" the one
        state nothing recovered from.
        """
        if proc.serving():
            self._last_served[org] = self._now()
            return {"running": True, "reason": "already-running"}
        last_served = self._last_served.get(org)
        if last_served is not None:
            if self._now() - last_served < CONNECTOR_RECONNECT_TIMEOUT_S:
                return {"running": True, "reason": "reconnecting"}
            # Served once, then stopped and stayed stopped past every window
            # its own reconnect loop should have needed. Treat it as wedged.
            return None
        if self._now() - self._started_at.get(org, 0.0) < self._grace_s:
            return {"running": True, "reason": "starting"}
        return None

    def _launch(self, org: str | None, state: dict) -> dict:
        """Spawn the connector for *org* (``state`` must be an ``ok``
        serve-cert state) and record its launch time for the fresh-tunnel
        grace. Callers hold the lock."""
        ok, detail = _verify_key_matches(
            state["cert"], state["viewer_cert"], state["key_path"])
        if not ok:
            return {"running": False, "reason": detail}
        binding, binding_error = _load_binding(org)
        if binding_error:
            return {"running": False, "reason": binding_error}
        # Several workers of one dashboard installation can legitimately
        # share the same org store.  Exactly one may own its connector.
        # Acquire BEFORE touching the shared cert or control descriptor; a
        # non-owner simply uses the owner's loopback control listener when
        # publish calls control().  This is local process coordination, not
        # cross-container isolation; mock dashboards never bootstrap serving.
        if not self._acquire_lock(org, state["key_path"]):
            return {"running": True, "reason": "owned-by-other-dashboard"}
        # We hold the per-org ownership lock, so every OTHER connector for this
        # org is a stray — an orphan a dead dashboard left behind (a live sibling
        # would still hold this lock). Reap them before spawning ours, so the new
        # connector does not just join the crowd fighting for the relay slot.
        self._reap_strays(org)
        try:
            cert_path = _materialize_cert(state["key_path"], state["cert"])
            viewer_cert_path = _viewer_cert_path_for(state["key_path"])
            viewer_tmp = viewer_cert_path + ".tmp"
            with open(viewer_tmp, "w") as fh:
                fh.write(state["viewer_cert"])
            os.replace(viewer_tmp, viewer_cert_path)
            ctl_path = _control_path_for(state["key_path"])
            # A stale descriptor from a previous killed connector would
            # mislead control() until the new connector rewrites it; clear it
            # up front.  All shared-file preparation stays inside this guard:
            # failure must release ownership so another dashboard can serve.
            with contextlib.suppress(OSError):
                os.remove(ctl_path)
            argv, env = _connector_command(
                binding, org, state["key_path"], cert_path, viewer_cert_path)
            self._procs[org] = self._spawn(
                argv, env, log_path=_log_path_for(state["key_path"]),
                ctl_path=ctl_path,
            )
        except Exception:
            self._release_lock(org)
            raise
        self._credentials[org] = (
            state["cert"], state["viewer_cert"], state["key_path"])
        self._started_at[org] = self._now()
        self._last_served.pop(org, None)
        return {"running": True, "reason": "launched"}

    def _owned_pids(self) -> set[int]:
        """The pids of connectors THIS supervisor spawned — the reap exclusion
        set. Includes our own pid so a same-node command line never matches."""
        owned = {os.getpid()}
        for handle in self._procs.values():
            pid = None
            with contextlib.suppress(Exception):
                pid = handle.pid()
            if isinstance(pid, int):
                owned.add(pid)
        return owned

    def _reap_strays(self, org: str | None) -> None:
        """Terminate serving connectors for *org* that this supervisor does not
        own — the leaked generations reparented to systemd when a previous
        dashboard died.

        ``PR_SET_PDEATHSIG`` (see :func:`_default_spawn`) covers future spawns
        but cannot touch a process that already leaked, nor the race where the
        parent died before the child armed the signal. This is that backstop:
        it also self-heals a node that inherited orphans from an earlier crash,
        with no operator involved. Callers hold the lock; best-effort — a scan
        or signal failure must never block reconciliation.
        """
        try:
            binding, binding_error = _load_binding(org)
            if binding_error or not binding:
                return
            org_uuid = binding.get("org_uuid")
            if not org_uuid:
                return
            owned = self._owned_pids()
            for pid in _iter_connector_pids(org_uuid):
                if pid in owned:
                    continue
                _log.warning(
                    "reaping stray serving connector pid=%s for org=%s "
                    "(not owned by this dashboard — a leaked generation)",
                    pid, org_uuid,
                )
                _terminate_pid(pid)
        except Exception:
            _log.warning("stray-connector reap failed for org=%s",
                         org, exc_info=True)

    def _acquire_lock(self, org: str | None, key_path: str) -> bool:
        if org in self._locks:
            return True
        lock_path = _lock_path_for(key_path)
        lock = open(lock_path, "a+")
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            return False
        self._locks[org] = lock
        return True

    def _release_lock(self, org: str | None) -> None:
        lock = self._locks.pop(org, None)
        if lock is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()

    def start_watchdog(self, interval: float = 20.0) -> None:
        """Periodically re-reconcile every managed org — restart the dead,
        tear down the expired/revoked. No-op if already running."""
        # Startup is an observed process property, not a watchdog scheduling
        # property.  Keep a real deadline even when tests/operators choose a
        # shorter reconciliation interval.
        self._grace_s = max(CONNECTOR_STARTUP_TIMEOUT_S, interval)
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
            self._credentials.clear()
            self._managed.clear()
            self._started_at.clear()
            self._last_served.clear()
            for org in list(self._locks):
                self._release_lock(org)
        if watchdog is not None and watchdog is not threading.current_thread():
            watchdog.join(timeout=2)
        self._watchdog = None

    def running_orgs(self) -> list:
        with self._lock:
            return [org for org, p in self._procs.items() if p.alive()]

    def restart(self, org: str | None) -> dict:
        """Stop *org*'s connector (if any) and reconcile it back up.

        A running connector holds its Fleet serving credential ONLY in memory
        and it dies with the process; it also keeps running whatever code it
        imported at launch. Restarting is therefore how the tray's "sync" flag
        clears a stale-code process — a fresh subprocess re-imports the code on
        disk — and the precondition for re-arming it, since the credential must
        be re-installed AFTER the new process is up. Idempotent; returns
        ``{running, reason}`` from the relaunch."""
        with self._lock:
            proc = self._procs.pop(org, None)
            if proc is not None:
                proc.stop()
            self._credentials.pop(org, None)
            self._last_served.pop(org, None)
            self._started_at.pop(org, None)
            self._managed.add(org)
            return self._reconcile(org)


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
    if os.environ.get("GRAPH_DB"):
        # A pinned GRAPH_DB is the single-database test world; per-org DBs
        # alongside it mean a mis-shaped deployment whose org connectors can
        # never reconcile (auto-sb0g8) — say so instead of failing silently.
        try:
            from tools.graph import org_ops as _org_ops
            if _org_ops.list_orgs():
                logging.getLogger(__name__).warning(
                    "GRAPH_DB is pinned but per-org databases exist under the "
                    "orgs dir; serving reconciles ONLY the pinned database — "
                    "org connectors will not start (unset GRAPH_DB on "
                    "multi-org nodes)",
                )
        except Exception:
            pass
        return [None]

    from tools.graph import org_ops

    # org-scope: enumerate — one connector per local org database, plus the
    # scopeless legacy slot; nothing ambient can add or hide a scope.
    discovered: list[str | None] = [None]
    discovered.extend(ref.slug for ref in org_ops.list_orgs())
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
