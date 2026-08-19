"""Evidence harness for full-personal-graph fleet synchronization.

This package is deliberately simulation-scoped.  It freezes and exercises the
logical replication contract before the production sync engine owns database
write interception, scheduling, and lifecycle integration.
"""

from .alpha import ALPHA_VERSION, FleetSyncAlpha, install_checkpoint
from .policies import TABLE_POLICIES, audit_schema

__all__ = [
    "ALPHA_VERSION", "FleetSyncAlpha", "TABLE_POLICIES", "audit_schema",
    "install_checkpoint",
]
