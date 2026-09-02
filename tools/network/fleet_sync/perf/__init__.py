"""Opt-in performance benchmark suite for the fleet sync engine.

This package is deliberately NOT a pytest suite.  It contains no
``test_*.py`` files, so the default ``testpaths`` sweep, directory runs of
``tools/network/fleet_sync/tests``, and agent-test's changed-line plan
(which globs ``test_*.py`` repo-wide) can never collect or execute it.
Keep it that way: adding a ``test_*.py`` file here would put multi-minute
benchmarks into every changed-plan run that touches the engine.
``tools/network/fleet_sync/tests/test_perf_isolation.py`` trips if this
rule is broken.

The one documented entry point:

    .venv/bin/python -m tools.network.fleet_sync.perf run --scale quick

It runs the consolidated suite, prints a comparison table against the
previously retained baseline for that scale, and retains the new result.
Regressions are reported, never gating.  See ``__main__.py`` for flags.
"""
