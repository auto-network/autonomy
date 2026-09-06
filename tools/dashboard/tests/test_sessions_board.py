"""Run the Session Board logic contract tests (test_sessions_board.js).

Node tests only run in CI through a same-named pytest wrapper; a bare .js file
silently rots without one.
"""

from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_TEST = Path(__file__).with_suffix(".js")


def test_sessions_board_logic():
    subprocess.run(["node", "--test", str(NODE_TEST)], cwd=REPO_ROOT, check=True)
