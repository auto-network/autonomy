"""Presence must survive crossing a process boundary.

The bar said "nobody here" while every row sat exactly where it had been
written. The writer named an org literally; the readers used the
caller-derived sentinel, which resolves through a contextvar, then GRAPH_ORG,
then None. In the dashboard process -- which sets neither -- the two sides
addressed different databases.

Reading back in the SAME process would have passed throughout and proved
nothing, because one process resolves the sentinel one way. So these tests
write and read in separate interpreters, with deliberately mismatched
GRAPH_ORG environments: if where presence lands depends on the environment of
whoever asks, that is the bug.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]

WRITE = """
import sys; sys.path.insert(0, {repo!r})
from tools.dashboard.plugins.mission_control import compose
from tools.graph.surface import Presence
with Presence(surface_id="mission:probe-mission", participant_kind="agent",
              participant_id="probe-writer", label="probe-writer",
              org=compose.PRESENCE_ORG):
    pass
print("wrote")
"""

READ = """
import sys, json; sys.path.insert(0, {repo!r})
from tools.dashboard.plugins.mission_control import compose
here = compose._presence("mission:probe-mission", 0)
print(json.dumps([p["participant_id"] for p in here]))
"""


def _run(code: str, env_extra: dict) -> str:
    env = {**os.environ, **env_extra}
    r = subprocess.run([sys.executable, "-c", code.format(repo=str(REPO))],
                       capture_output=True, text=True, timeout=90, env=env)
    assert r.returncode == 0, r.stderr[-1500:]
    return r.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize("reader_org", [None, "", "personal", "somewhere-else"])
def test_presence_is_readable_whatever_the_readers_environment_says(reader_org, tmp_path):
    """The writer's environment and the reader's differ on purpose.

    Each parameter is a GRAPH_ORG value the dashboard process might have --
    including unset, which is what it actually has.
    """
    graph_db = tmp_path / "graph.db"
    writer_env = {"GRAPH_DB": str(graph_db)}
    writer_env.pop("GRAPH_ORG", None)
    reader_env = {"GRAPH_DB": str(graph_db)}
    if reader_org is None:
        writer_env["GRAPH_ORG"] = "autonomy"      # writer knows who it is
        reader_env.pop("GRAPH_ORG", None)         # reader does not
    else:
        reader_env["GRAPH_ORG"] = reader_org

    _run(WRITE, writer_env)
    seen = json.loads(_run(READ, reader_env))
    assert "probe-writer" in seen, (
        f"presence written by one process was invisible to a reader whose "
        f"GRAPH_ORG is {reader_org!r} — the two sides are addressing "
        f"different stores again"
    )
