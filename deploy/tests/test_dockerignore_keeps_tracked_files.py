"""The node image's /app must match git exactly (auto-2uj1b, S7 witness 2026-09-27).

deploy/Dockerfile copies the build context INCLUDING .git and keeps it: /app is a
working checkout that follows origin (tools/dashboard/software_update.py). A
tracked file that .dockerignore keeps out of the context reads as a deletion in
the image, the tree is dirty, and ``can_update`` is false on every node — the
update tile never appears and auto-install never applies. The S7 witness found
exactly that: .gitignore, .dockerignore, agents/images/dashboard/Dockerfile and
data/uploads/.gitkeep missing from a freshly built /app.

The matcher below follows Docker's .dockerignore rules (moby patternmatcher):
patterns are cleaned and anchored at the context root, a pattern matches a path
or any of its parent directories, ``**`` spans any number of segments, ``!``
re-includes, and the LAST matching pattern decides. It covers the pattern forms
.dockerignore uses today; ``[...]`` character classes and backslash escapes are
NOT emulated — extend ``_patterns`` before adding one, or it will mis-parse.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _patterns() -> list[tuple[bool, re.Pattern]]:
    out = []
    for raw in (REPO / ".dockerignore").read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        pat = line[1:] if negate else line
        pat = pat.strip("/")
        regex = ""
        i = 0
        while i < len(pat):
            if pat.startswith("**/", i):
                regex += "(?:.*/)?"
                i += 3
            elif pat.startswith("**", i):
                regex += ".*"
                i += 2
            elif pat[i] == "*":
                regex += "[^/]*"
                i += 1
            elif pat[i] == "?":
                regex += "[^/]"
                i += 1
            else:
                regex += re.escape(pat[i])
                i += 1
        out.append((negate, re.compile("^" + regex + "$")))
    return out


def _excluded(path: str, patterns) -> bool:
    parts = path.split("/")
    candidates = ["/".join(parts[: n + 1]) for n in range(len(parts))]
    excluded = False
    for negate, rx in patterns:
        if any(rx.match(c) for c in candidates):
            excluded = not negate
    return excluded


def test_no_tracked_file_is_kept_out_of_the_image():
    patterns = _patterns()
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert tracked, "git ls-files returned nothing"
    excluded = [p for p in tracked if _excluded(p, patterns)]
    assert excluded == [], (
        "tracked files excluded from the build context make every node's /app "
        f"dirty: {excluded}"
    )


def test_host_state_is_still_kept_out():
    patterns = _patterns()
    for path in ("data/orgs/personal.db", "data/agent-runs/x/sessions/a.jsonl",
                 "agents/images/other/blob.tar", ".venv/bin/python",
                 "tools/dashboard/__pycache__/server.cpython-312.pyc", "agents/bin/claude"):
        assert _excluded(path, patterns), path
    for path in ("data/uploads/.gitkeep", "agents/images/dashboard/Dockerfile",
                 ".gitignore", ".dockerignore", ".git/HEAD"):
        assert not _excluded(path, patterns), path
