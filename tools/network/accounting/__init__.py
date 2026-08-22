"""Official physical-usage accounting primitives for auto.network services.

The package is deliberately independent of relay, TURN, storage, pricing, and
sink implementations.  Producers emit one immutable :class:`UsageBatch` and
persist its exact wire bytes before attempting delivery.  Pricing consumes
settled batches later; it is never part of this contract.
"""

from .batch import (
    BATCH_INTERVAL_SECONDS,
    MAX_COUNTERS,
    USAGE_BATCH_VERSION,
    UsageBatch,
    UsageBatchError,
    UsageBatchMalformed,
    UsageBatchSignatureError,
)
from .spool import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_RECORDS,
    SPOOL_SCHEMA_VERSION,
    SpoolHealth,
    SpoolRecord,
    UsageSpool,
    UsageSpoolAckError,
    UsageSpoolCapacity,
    UsageSpoolConflict,
    UsageSpoolCorrupt,
    UsageSpoolError,
    UsageSpoolIOError,
    UsageSpoolLocked,
    UsageSpoolSequenceError,
)

__all__ = [
    "BATCH_INTERVAL_SECONDS",
    "MAX_COUNTERS",
    "USAGE_BATCH_VERSION",
    "UsageBatch",
    "UsageBatchError",
    "UsageBatchMalformed",
    "UsageBatchSignatureError",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_RECORDS",
    "SPOOL_SCHEMA_VERSION",
    "SpoolHealth",
    "SpoolRecord",
    "UsageSpool",
    "UsageSpoolAckError",
    "UsageSpoolCapacity",
    "UsageSpoolConflict",
    "UsageSpoolCorrupt",
    "UsageSpoolError",
    "UsageSpoolIOError",
    "UsageSpoolLocked",
    "UsageSpoolSequenceError",
]
