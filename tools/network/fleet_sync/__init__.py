"""Fleet-sync executable contract and production catalog preparation.

``FleetSyncAlpha`` remains the end-to-end evidence harness.  The catalog's
explicit existing-database migration is also the first production integration
seam; write interception, scheduling, and lifecycle integration remain gated
behind their later rollout steps.
"""

from .sync import ALPHA_VERSION, FleetSyncAlpha, install_checkpoint
from .policies import TABLE_POLICIES, audit_schema

__all__ = [
    "ALPHA_VERSION", "FleetSyncAlpha", "TABLE_POLICIES", "audit_schema",
    "install_checkpoint",
]
