"""The generated views: structurally complete in both directions, and the
committed copies can never silently drift from registry.yaml."""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gen  # noqa: E402
import keyreg  # noqa: E402


@pytest.fixture(scope="module")
def registry():
    return keyreg.load()


@pytest.fixture(scope="module")
def views():
    return gen.generate()


# ── Structural equality: registry -> views ─────────────────────────────────

def test_every_key_is_a_diagram_node(registry, views):
    mmd = views["key-graph.mmd"]
    for key_id in registry["keys"]:
        assert re.search(rf"^\s+{key_id}\[", mmd, re.M), (
            f"{key_id} missing from key-graph.mmd"
        )


def test_every_derivation_edge_is_drawn(registry, views):
    mmd = views["key-graph.mmd"]
    for child, parent, _fn in keyreg.derivation_edges(registry):
        assert re.search(rf"{parent} -->.*\b{child}\b", mmd), (
            f"derivation edge {parent} -> {child} missing from key-graph.mmd"
        )


def test_every_key_id_seal_edge_is_drawn(registry, views):
    mmd = views["key-graph.mmd"]
    for key_id, recipient, _purpose in keyreg.seal_edges(registry):
        if recipient in registry["keys"]:
            assert re.search(rf"{key_id} -\.->.*\b{recipient}\b", mmd), (
                f"seal edge {key_id} -> {recipient} missing from key-graph.mmd"
            )


def test_every_key_has_a_register_section(registry, views):
    register = views["key-register.md"]
    for key_id in registry["keys"]:
        assert f"### {key_id} — " in register, f"{key_id} missing from key-register.md"


def test_every_entry_has_a_coverage_row(registry, views):
    coverage = views["proof-coverage.md"]
    for entry_id in list(registry["keys"]) + list(registry["mutations"]):
        assert f"| {entry_id} |" in coverage, (
            f"{entry_id} missing from proof-coverage.md"
        )


def test_json_round_trips_whole_registry(registry, views):
    assert json.loads(views["registry.json"]) == registry


# ── Structural equality: views -> registry (no orphans) ────────────────────

def test_no_diagram_node_without_a_source_key(registry, views):
    mmd = views["key-graph.mmd"]
    known_clusters = {g.replace("-", "_") for g in gen.GROUP_TITLES}
    for node in re.findall(r"^\s+(\w+)\[", mmd, re.M):
        assert node in registry["keys"] or node in known_clusters, (
            f"orphan node {node} in key-graph.mmd"
        )


def test_no_register_section_without_a_source_key(registry, views):
    register = views["key-register.md"]
    for section in re.findall(r"^### (\w+) — ", register, re.M):
        assert section in registry["keys"], f"orphan section {section} in key-register.md"


def test_no_coverage_row_without_a_source_entry(registry, views):
    coverage = views["proof-coverage.md"]
    known = set(registry["keys"]) | set(registry["mutations"])
    for row in re.findall(r"^\| (\S+) \| (?:key|mutation) \|", coverage, re.M):
        assert row in known, f"orphan row {row} in proof-coverage.md"


# ── Staleness: the committed copies match a fresh generation ───────────────

def test_committed_views_are_current(views):
    for name, content in views.items():
        committed = (gen.GENERATED / name).read_text()
        assert committed == content, (
            f"generated/{name} is stale — run: python3 tools/network/keyreg/gen.py"
        )


# ── Rendering ──────────────────────────────────────────────────────────────

def test_mermaid_source_is_well_formed(views):
    mmd = views["key-graph.mmd"]
    body = [
        line.strip() for line in mmd.splitlines()
        if line.strip() and not line.strip().startswith("%%")
    ]
    assert body[0] == "graph LR"
    subgraphs = sum(1 for line in body if line.startswith("subgraph "))
    ends = sum(1 for line in body if line == "end")
    assert subgraphs == ends and subgraphs > 0, (
        f"unbalanced subgraph/end: {subgraphs} vs {ends}"
    )


def test_mermaid_renders_when_tooling_available(views):
    renderer = shutil.which("mmdc") or shutil.which("mermaid-render")
    if renderer is None:
        pytest.skip("no mermaid renderer on PATH in this environment")
    target = gen.GENERATED / "key-graph.mmd"
    result = subprocess.run(
        [renderer, "-i", str(target), "-o", "/tmp/key-graph.svg"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode == 127 or "not found" in result.stderr:
        # The cap-bin shim exists but its backing binary is not installed in
        # this container — tool unavailability, not a diagram defect.
        pytest.skip(f"mermaid renderer not executable here: {result.stderr.strip()}")
    assert result.returncode == 0, result.stderr[-2000:]
