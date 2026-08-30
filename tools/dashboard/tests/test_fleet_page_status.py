"""Run the Fleet page card status-derivation contract tests (bead auto-9yp8r)."""

from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_TEST = Path(__file__).with_suffix(".js")


def test_fleet_page_status_derivation():
    subprocess.run(["node", "--test", str(NODE_TEST)], cwd=REPO_ROOT, check=True)
