"""Lint-check tests for ``tools.graph.checks.force_host``.

The repo-level test in this module is the *enforcement path*. A future
edit that introduces ``subprocess.run([..., "--db", ...])`` against the
graph CLI without ``--force-host`` will fail
``test_repo_has_no_graph_cli_subprocess_missing_force_host`` and block
merge. The other tests pin the detector's behaviour on synthetic input
so that pin doesn't drift silently.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tools.graph.checks.force_host import (
    Violation,
    find_violations_in_repo,
    find_violations_in_source,
)


_REPO_ROOT = Path(__file__).resolve().parents[3]


# ── Bad patterns that MUST be flagged ─────────────────────────────


def test_module_form_with_db_no_force_host_is_flagged():
    src = textwrap.dedent(
        '''
        import subprocess, sys

        def go(db):
            subprocess.run(
                [sys.executable, "-m", "tools.graph.cli",
                 "--db", str(db), "note", "x"],
                capture_output=True,
            )
        '''
    )
    vs = find_violations_in_source(src, "synthetic_module_form.py")
    assert len(vs) == 1, vs
    assert "--force-host" in vs[0].reason


def test_binary_form_with_db_no_force_host_is_flagged():
    src = textwrap.dedent(
        '''
        import subprocess

        def go(db):
            subprocess.run(["graph", "--db", str(db), "note", "x"])
        '''
    )
    vs = find_violations_in_source(src, "synthetic_binary_form.py")
    assert len(vs) == 1, vs


def test_dynamic_cmd_extend_with_db_no_force_host_is_flagged():
    """Mirrors the test_rich_content.py idiom but with --force-host removed."""
    src = textwrap.dedent(
        '''
        import subprocess, sys

        def go(db):
            cmd = [sys.executable, "-m", "tools.graph.cli"]
            if db:
                cmd.extend(["--db", str(db)])
            subprocess.run(cmd, capture_output=True)
        '''
    )
    vs = find_violations_in_source(src, "synthetic_dynamic.py")
    assert len(vs) == 1, vs


def test_binary_form_via_variable_with_db_no_force_host_is_flagged():
    src = textwrap.dedent(
        '''
        import subprocess

        def go(db):
            cmd = ["graph"]
            cmd.extend(["--db", str(db), "note", "x"])
            subprocess.run(cmd)
        '''
    )
    vs = find_violations_in_source(src, "synthetic_binary_var.py")
    assert len(vs) == 1, vs


# ── Clean patterns that MUST NOT be flagged ───────────────────────


def test_module_form_with_force_host_passes():
    src = textwrap.dedent(
        '''
        import subprocess, sys

        def go(db):
            cmd = [sys.executable, "-m", "tools.graph.cli", "--force-host"]
            cmd.extend(["--db", str(db), "note", "x"])
            subprocess.run(cmd)
        '''
    )
    assert find_violations_in_source(src, "clean_module.py") == []


def test_binary_form_with_force_host_passes():
    src = textwrap.dedent(
        '''
        import subprocess

        def go(db):
            subprocess.run(
                ["graph", "--force-host", "--db", str(db), "note", "x"],
            )
        '''
    )
    assert find_violations_in_source(src, "clean_binary.py") == []


def test_no_db_no_force_host_passes():
    """Read-only graph operations against the live dashboard are fine
    without --force-host."""
    src = textwrap.dedent(
        '''
        import subprocess

        def go():
            subprocess.run(["graph", "search", "test"], capture_output=True)
        '''
    )
    assert find_violations_in_source(src, "clean_no_db.py") == []


def test_unrelated_subprocess_with_db_passes():
    """Other tools that happen to use a --db flag are not graph-CLI sites."""
    src = textwrap.dedent(
        '''
        import subprocess

        def go(db):
            subprocess.run(["psql", "--db", db, "-c", "select 1"])
        '''
    )
    assert find_violations_in_source(src, "clean_unrelated.py") == []


def test_argparse_definition_alone_is_not_a_subprocess_call():
    """``parser.add_argument("--db", ...)`` is not a subprocess call,
    even when both flag names appear as string literals in main()."""
    src = textwrap.dedent(
        '''
        import argparse

        def main():
            parser = argparse.ArgumentParser()
            parser.add_argument("--db")
            parser.add_argument("--force-host", action="store_true")
            parser.parse_args()
        '''
    )
    assert find_violations_in_source(src, "clean_argparse.py") == []


# ── Repo-level enforcement ────────────────────────────────────────


def test_repo_has_no_graph_cli_subprocess_missing_force_host():
    """Enforcement: scanning the repo produces zero violations.

    A future commit that subprocess-invokes the graph CLI with ``--db``
    but no ``--force-host`` will fail this test. This is the path the
    bead's acceptance hinges on — the check is not advisory.
    """
    roots = [_REPO_ROOT / "tools", _REPO_ROOT / "agents"]
    violations = find_violations_in_repo(roots)
    assert violations == [], (
        "Found graph-CLI subprocess invocations missing --force-host:\n"
        + "\n".join(v.format(_REPO_ROOT) for v in violations)
    )


# ── Sanity: the existing compliant site is recognised ─────────────


def test_existing_test_rich_content_site_is_recognised_as_clean():
    """The known good helper in test_rich_content.py must scan clean.

    If this fails, the detector has regressed: it either no longer
    recognises ``-m tools.graph.cli`` as a graph-CLI site, or it stopped
    accepting ``--force-host`` as the gate.
    """
    target = _REPO_ROOT / "tools" / "dashboard" / "tests" / "test_rich_content.py"
    if not target.exists():
        pytest.skip("test_rich_content.py not present in this checkout")
    src = target.read_text()
    violations = find_violations_in_source(src, str(target))
    assert violations == [], (
        f"detector regressed — flagged a known-clean site: "
        f"{[v.format(_REPO_ROOT) for v in violations]}"
    )
