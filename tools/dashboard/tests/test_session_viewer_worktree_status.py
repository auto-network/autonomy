"""Run the session-viewer worktree SSE contract through Agent Test."""

from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_TEST = Path(__file__).with_suffix(".js")


def test_session_viewer_worktree_status():
    result = subprocess.run(
        ["node", "--test", str(NODE_TEST)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
