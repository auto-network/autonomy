"""Evidence harness for full-personal-graph fleet synchronization.

This package is deliberately simulation-scoped.  It freezes and exercises the
logical replication contract before the production sync engine owns database
write interception, scheduling, and lifecycle integration.
"""

from .policies import TABLE_POLICIES, audit_schema

__all__ = ["TABLE_POLICIES", "audit_schema"]
