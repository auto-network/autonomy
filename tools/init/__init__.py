"""First-run initialization — empty deployment from nothing (H3).

See :mod:`tools.init.first_run` for the library API and ``TOOL.md`` for
the operator-facing story. CLI: ``python -m tools.init``.
"""

from .first_run import InitReport, InitStep, initialize  # noqa: F401
