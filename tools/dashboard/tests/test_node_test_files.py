"""Every ``*.test.mjs`` in this directory runs under pytest (auto-pw295).

Node suites were wired one wrapper at a time, and twelve of them had none:
approval_adapters, approval_dialog, the vault/fleet/email/service approvals,
founding registration and others passed under ``node --test`` but never ran in
agent-test, so a regression in them was invisible. This runs each file found
here, so a new suite is enforced the day it lands. Some files also have a
dedicated wrapper elsewhere; running those twice costs seconds and keeps this
rule a glob rather than a list someone must remember to extend.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
# Older suites use the ``test_*.js`` name and plugin suites live beside their
# plugin; both were missed by the ``*.test.mjs`` glob and never ran. The two
# ceremony suites are named because their siblings take fixture arguments
# from dedicated Python wrappers and cannot run bare.
SUITES = sorted(
    [*HERE.glob("*.test.mjs"), *HERE.glob("test_*.js")]
) + sorted(REPO_ROOT.glob("tools/dashboard/plugins/*/tests/test_*.js")) + [
    HERE.parent / "static/js/ceremony/tests/factor-policy.test.mjs",
    HERE.parent / "static/js/ceremony/tests/organization.test.mjs",
]


def test_the_glob_finds_the_suites():
    assert len(SUITES) >= 20, [p.name for p in SUITES]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
@pytest.mark.parametrize("suite", SUITES, ids=[p.name if p.parent == HERE else str(p.relative_to(REPO_ROOT)) for p in SUITES])
def test_node_suite_passes(suite: Path):
    proc = subprocess.run(
        ["node", "--test", str(suite)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-4000:]
