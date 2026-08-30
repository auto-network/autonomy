"""Run the Sessions launch-menu workspace filtering contract tests.

The .js file existed without this wrapper and silently rotted (the module-scope
SSE listener broke its window stub and nothing noticed) — node tests only run
in CI through a same-named pytest wrapper.
"""

from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[3]
NODE_TEST = Path(__file__).with_suffix(".js")


def test_sessions_workspace_filter():
    subprocess.run(["node", "--test", str(NODE_TEST)], cwd=REPO_ROOT, check=True)
