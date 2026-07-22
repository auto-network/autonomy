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

import os
import subprocess
import sys
import threading
import time

from tools.dashboard import link_serving
from tools.dashboard.link_approvals import _load_binding
from tools.dashboard.link_probe import registry_to_relay_ws
from tools.graph import settings_ops
from tools.graph.schemas.network_identity import (
    NETWORK_LINK_GRANT_SET_ID,
    NETWORK_SERVE_CERT_SET_ID,
    SERVE_CERT_SCOPE,
)


# ── provisioning state (also the enrich precondition) ─────────


def serve_cert_state(org: str | None, *, now: float | None = None) -> dict:
    """The org's serve-cert provisioning state → ``{status, ...}``.

    Owning-scope read (P2): a peer's serve-cert must never make this org look
    provisioned. ``status`` is one of:

    * ``ok`` — a non-expired cert whose key file is present (the browser need
      NOT mint; the connector CAN run);
    * ``missing`` — no serve-cert row at all;
    * ``expired`` — a row whose delegate has passed ``not_after``;
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
    key_path = row.get("key_path")
    if not isinstance(key_path, str) or not key_path or not os.path.isfile(key_path):
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
        members = settings_ops.read_owned_set(NETWORK_LINK_GRANT_SET_ID, org=org).members
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

    def __init__(self, popen):
        self._p = popen

    def alive(self) -> bool:
        return self._p.poll() is None

    def stop(self) -> None:
        if self._p.poll() is not None:
            return
        self._p.terminate()
        try:
            self._p.wait(timeout=5)
        except Exception:
            self._p.kill()


def _default_spawn(argv: list, env: dict, *, log_path: str | None = None):
    out = open(log_path, "ab") if log_path else subprocess.DEVNULL
    return _Proc(subprocess.Popen(argv, env=env, stdout=out, stderr=out))


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

    def _reconcile(self, org: str | None) -> dict:
        now = self._now()
        state = serve_cert_state(org, now=now)
        proc = self._procs.get(org)
        should_run = state["status"] == "ok" and _has_live_grant(org, now)

        if not should_run:
            if proc is not None:
                proc.stop()
                self._procs.pop(org, None)
            reason = state["status"] if state["status"] != "ok" else "no-live-grants"
            return {"running": False, "reason": reason}

        if proc is not None and proc.alive():
            return {"running": True, "reason": "already-running"}
        # A dead handle: drop it and relaunch below.
        self._procs.pop(org, None)

        ok, detail = _verify_key_matches(state["cert"], state["key_path"])
        if not ok:
            return {"running": False, "reason": detail}
        binding, binding_error = _load_binding(org)
        if binding_error:
            return {"running": False, "reason": binding_error}
        cert_path = _materialize_cert(state["key_path"], state["cert"])
        argv, env = _connector_command(binding, org, state["key_path"], cert_path)
        handle = self._spawn(argv, env, log_path=_log_path_for(state["key_path"]))
        self._procs[org] = handle
        return {"running": True, "reason": "launched"}

    def start_watchdog(self, interval: float = 20.0) -> None:
        """Periodically re-reconcile every managed org — restart the dead,
        tear down the expired/revoked. No-op if already running."""
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
        with self._lock:
            for proc in self._procs.values():
                try:
                    proc.stop()
                except Exception:
                    pass
            self._procs.clear()

    def running_orgs(self) -> list:
        with self._lock:
            return [org for org, p in self._procs.items() if p.alive()]
