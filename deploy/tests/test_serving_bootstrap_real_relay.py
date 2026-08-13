"""auto-sb0g8 regression: the founding node serves its join channel after a
restart, under the EXACT node environment the harness topology generates.

The multi-node ladder's phase_found path — found → restart → startup
reconciliation → org:join context over the real relay — broke because the
generated node environment pinned ``GRAPH_DB``, collapsing every settings
scope into one file while the ledger and org enumeration stayed per-org. The
fake-runner unit tests could not see it; this test drives the real serving
stack (registry+relay subprocess, real connector process, production
``ViewerJoinTransport``) with the environment taken straight from
``compose_model``, so any future env-shape change that breaks serving fails
here, without Docker.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from deploy.harness.topology import TopologyConfig, compose_model

RUNNER = Path(__file__).with_name("serving_bootstrap_runner.py")
REPO = Path(__file__).resolve().parents[2]


def _node_env_from_topology(tmp_path: Path) -> dict:
    """node-a's generated environment, rebased from /app/data into tmp."""
    model = compose_model(TopologyConfig(
        project="autonomy-harness-serving-bootstrap",
        nodes=3,
        relay_port=21477,
        node_port_base=21880,
    ))
    environment = dict(model["services"]["node-a"]["environment"])
    rebased = {}
    for key, value in environment.items():
        if key in ("AUTONOMY_FIRST_ORG", "AUTONOMY_FIRST_ORG_NAME"):
            continue  # the runner founds via tools.init + the found fixture
        if isinstance(value, str) and value.startswith("/app/data"):
            value = str(tmp_path / "data") + value[len("/app/data"):]
        rebased[key] = value
    return rebased


def test_generated_node_env_serves_join_context_after_bootstrap(tmp_path):
    env_file = tmp_path / "node-env.json"
    env_file.write_text(json.dumps(_node_env_from_topology(tmp_path)))

    result = subprocess.run(
        [sys.executable, str(RUNNER), str(tmp_path), str(env_file)],
        cwd=str(REPO), capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"join context was not served under the generated node env:\n"
        f"{result.stdout[-4000:]}\n{result.stderr[-2000:]}"
    )
    reply = json.loads(result.stdout.strip().splitlines()[-1])
    assert reply["status"] == "ok"
    assert reply["granted_role"] == "member"
    assert reply["binding"] == "token"
