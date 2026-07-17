"""Exception taxonomy for the authority ledger.

Structural problems (bad bytes, bad schema, missing parents) raise; semantic
invalidity (an author overreaching their authority) never raises — the fold
marks such events invalid and inert, because a replica must be able to hold
and propagate events it considers invalid (other replicas need to see them
to converge on the same judgement).
"""

from __future__ import annotations


class LedgerError(Exception):
    """Base class for all ledger errors."""


class MalformedEventError(LedgerError):
    """Input could not be parsed as a structurally valid ledger event."""


class SchemaError(MalformedEventError):
    """Payload type or fields fall outside the authority-event vocabulary.

    This is the L8 enforcement point: the ledger holds authority only, so
    content-access events (views, reads, anything unrecognised) are rejected
    at the schema layer and never enter the DAG.
    """


class SignatureError(LedgerError):
    """An event or approval signature did not verify against its key."""


class UnknownParentError(LedgerError):
    """An event names a parent hash the ledger does not hold."""


class CausalityError(LedgerError):
    """An event's HLC does not advance past all of its parents' HLCs."""


class GenesisError(LedgerError):
    """Genesis missing, duplicated, or malformed for this ledger."""
