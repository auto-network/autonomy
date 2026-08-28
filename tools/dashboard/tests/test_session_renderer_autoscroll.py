"""Run the dependency-free SessionRenderer autoscroll contract tests."""

from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_TEST = Path(__file__).with_suffix(".js")


def test_session_renderer_autoscroll_contract():
    subprocess.run(["node", "--test", str(NODE_TEST)], cwd=REPO_ROOT, check=True)
