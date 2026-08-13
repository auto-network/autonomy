"""Subprocess runner for the no-Docker serving-bootstrap acceptance.

Executes the exact node-a founding sequence the multi-node harness drives —
first-run init, the ``found`` fixture, registry seeding, then the
dashboard-startup serving reconciliation (``link_serving_supervisor.bootstrap``)
— against a REAL registry+relay subprocess, and performs node-b's first act:
fetching the org:join context over the production ``ViewerJoinTransport``.

Runs in a subprocess so the environment under test (built by the caller from
the generated harness topology) is applied before any settings/db module
resolves a store. argv: ``<tmp_dir> <env_json>``. Exit 0 iff the join context
comes back ``status == "ok"``; the reply JSON is the last stdout line.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

TMP = Path(sys.argv[1]).resolve()
ENV_FILE = Path(sys.argv[2])
REPO = Path(__file__).resolve().parents[2]

# The environment under test replaces every ambient store/scope variable.
for var in list(os.environ):
    if var.startswith(("AUTONOMY_", "DASHBOARD_", "GRAPH_")) or var in (
        "AUTH_DB", "DISPATCH_DB", "APPROVAL_REQUESTS_DB", "COMMIT_WORKFLOW_DB",
    ):
        os.environ.pop(var)
os.environ.update(json.loads(ENV_FILE.read_text()))
os.environ["PYTHONPATH"] = str(REPO)
sys.path.insert(0, str(REPO))

import httpx  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


port = free_port()
registry_log = TMP / "registry.log"
registry = subprocess.Popen(
    [sys.executable, "-m", "tools.network.registry",
     "--db", str(TMP / "registry.db"), "--host", "127.0.0.1", "--port", str(port)],
    cwd=str(REPO), env=dict(os.environ),
    stdout=open(registry_log, "ab"), stderr=subprocess.STDOUT,
)
try:
    deadline = time.time() + 20
    while True:
        try:
            if httpx.get(
                f"http://127.0.0.1:{port}/healthz", timeout=1
            ).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if time.time() > deadline:
            print(registry_log.read_text()[-2000:])
            sys.exit("registry never came up")
        time.sleep(0.2)

    init = subprocess.run(
        [sys.executable, "-m", "tools.init", "--org", "demo", "--no-tls",
         "--root", str(TMP)],
        cwd=str(REPO), env=dict(os.environ), capture_output=True, text=True,
    )
    if init.returncode != 0:
        print(init.stdout[-2000:], init.stderr[-2000:])
        sys.exit("tools.init failed")

    from deploy.harness import fixture_ops

    claim_token = secrets.token_hex(16)
    join_channel_token = secrets.token_hex(16)
    found = fixture_ops.found_node({
        "org": "demo",
        "password": secrets.token_hex(8),
        "claim_token": claim_token,
        "join_channel_token": join_channel_token,
        "content_channel_token": secrets.token_hex(16),
    })

    os.environ["AUTONOMY_HARNESS_REGISTRY_DB"] = str(TMP / "registry.db")
    seeded = fixture_ops.seed_registry({
        key: found[key]
        for key in ("org_uuid", "root_pub", "invite_ref", "invite_expiry",
                    "content_id")
    } | {
        "join_channel_token": join_channel_token,
        "content_channel_token": secrets.token_hex(16),
    })

    from tools.graph import settings_ops
    from tools.graph.schemas.network_identity import (
        NETWORK_BINDING_REVISION, NETWORK_BINDING_SET_ID,
    )
    binding = next(
        m.payload for m in
        settings_ops.read_owned_set(NETWORK_BINDING_SET_ID, org="demo").members
        if m.key == "relay"
    )
    binding["registry_url"] = f"http://127.0.0.1:{port}"
    settings_ops.upsert_by_key(
        NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION, "relay", binding,
        org="demo",
    )

    # The post-restart production path: dashboard-startup reconciliation.
    from tools.dashboard import link_serving_supervisor
    link_serving_supervisor.bootstrap()

    from tools.init.join import ViewerJoinTransport
    from tools.network.invitation import invitation_from_join_url
    invitation = invitation_from_join_url(
        org=found["org_uuid"], root_pub=found["root_pub"],
        invite_ref=found["invite_ref"],
        join_url=seeded["join_url"] + "#t="
        + urllib.parse.quote(claim_token, safe=""),
    )
    reply, last = None, None
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            reply = ViewerJoinTransport(
                invitation, relay_url=f"ws://127.0.0.1:{port}", timeout=5,
            ).request({"v": 1, "op": "context"})
            break
        except Exception as exc:  # the connector may still be dialing
            last = repr(exc)
            time.sleep(0.5)

    ok = isinstance(reply, dict) and reply.get("status") == "ok"
    if not ok:
        for log in sorted((TMP / "data" / "network").glob("*.log")):
            print(f"── connector log {log.name}:\n{log.read_text()[-3000:]}")
        print(f"── last transport error: {last}")
    print(json.dumps(reply))
    sys.exit(0 if ok else 1)
finally:
    try:
        from tools.dashboard.link_serving_supervisor import get_supervisor
        get_supervisor().stop_all()
    except Exception:
        pass
    registry.terminate()
