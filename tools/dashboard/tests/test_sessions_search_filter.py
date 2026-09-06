"""Run the sessions-page on-screen search contract tests (node --test)."""

from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_TEST = Path(__file__).with_suffix(".js")


def test_sessions_search_filter():
    subprocess.run(["node", "--test", str(NODE_TEST)], cwd=REPO_ROOT, check=True)
