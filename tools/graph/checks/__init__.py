"""Lint checks for ``tools.graph``.

These are static-analysis guards that fail the test suite when a
disallowed pattern is reintroduced. They complement (do not replace)
code review and runtime tests.
"""

from tools.graph.checks.force_host import (
    Violation,
    find_violations_in_repo,
    find_violations_in_source,
)

__all__ = ["Violation", "find_violations_in_repo", "find_violations_in_source"]
