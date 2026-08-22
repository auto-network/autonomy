"""Official physical-usage accounting for auto.network services.

The batch and spool are independent of relay, TURN, and storage implementations.
Producers persist exact immutable bytes before delivery to the selected v1
ledger. Pricing consumes settled usage later; it is never part of the physical
usage contract.
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
from .ledger import (
    LEDGER_SCHEMA_VERSION,
    IngestReceipt,
    LedgerHealth,
    StreamReconciliation,
    UsageLedger,
    UsageLedgerAuthorizationError,
    UsageLedgerConflict,
    UsageLedgerCorrupt,
    UsageLedgerError,
    UsageLedgerIOError,
    UsageLedgerLocked,
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
    "LEDGER_SCHEMA_VERSION",
    "IngestReceipt",
    "LedgerHealth",
    "StreamReconciliation",
    "UsageLedger",
    "UsageLedgerAuthorizationError",
    "UsageLedgerConflict",
    "UsageLedgerCorrupt",
    "UsageLedgerError",
    "UsageLedgerIOError",
    "UsageLedgerLocked",
]
