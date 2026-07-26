"""Production-path multi-node Docker acceptance harness.

The package is intentionally importable.  Demo-day presentation code can
drive the same labelled phases one at a time without scraping a shell script,
while ``python -m deploy.harness`` remains the one-command CI entry point.
"""

from .driver import Harness, HarnessConfig

__all__ = ["Harness", "HarnessConfig"]
