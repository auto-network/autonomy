"""Cross-language acceptance for the JavaScript ledger-event seam."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools.network.ledger.events import Event

REPO_ROOT = Path(__file__).resolve().parents[3]
CEREMONY_TESTS = (
    REPO_ROOT / "tools" / "dashboard" / "static" / "js"
    / "ceremony" / "tests"
)
VECTOR_GENERATOR = CEREMONY_TESTS / "generate_ledger_event_vectors.py"
NODE_TEST = CEREMONY_TESTS / "ledger-event.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_node_ledger_events_match_and_verify_through_python(tmp_path):
    fixture_path = tmp_path / "ledger-event-vectors.json"
    subprocess.run(
        [sys.executable, str(VECTOR_GENERATOR), str(fixture_path)],
        cwd=REPO_ROOT,
        check=True,
    )
    result = subprocess.run(
        ["node", str(NODE_TEST), str(fixture_path)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    node_output = json.loads(result.stdout)
    assert node_output["persona_public_hex"] == fixture["persona"]["public_hex"]
    assert len(node_output["events"]) == len(fixture["vectors"]) == 4

    for node_event, vector in zip(
        node_output["events"],
        fixture["vectors"],
        strict=True,
    ):
        event = Event.from_dict(node_event)
        event.verify_sig()
        assert event.event_id == vector["event_id"]
        assert event.signing_input().hex() == vector["signing_input_hex"]
