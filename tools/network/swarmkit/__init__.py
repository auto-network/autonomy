"""swarmkit — content-addressed swarm bulk fetch (G2, spec §8).

Large artifacts move from ANY org peer over untrusted transport
(relaykit channels, any rung of the fallback chain) — hashes are
integrity, signatures are authorship. The primary transfer is the
RaptorQ fountain path (``fountain*`` — interchangeable coded symbols,
publisher egress ≈1× by construction); the block scheduler
(``store``/``protocol``/``fetch``) is superseded but retained.
"""

from .fountain import (
    DEFAULT_N_STRIPES,
    DEFAULT_SYMBOL_SIZE,
    FountainError,
    FountainStore,
    build_fountain_manifest,
    check_fountain_manifest,
    fountain_id,
    roster_stripe,
    source_symbols,
)
from .fountain_protocol import FountainMetrics, fountain_handler
from .fountain_fetch import (
    FountainFetchError,
    FountainReport,
    fountain_fetch,
)
from .store import (
    BLOCK_SIZE,
    BlockError,
    BlockStore,
    bitmap_hex_to_indices,
    build_manifest,
    check_manifest,
    indices_to_bitmap_hex,
    manifest_id,
)
from .protocol import SwarmMetrics, swarm_handler
from .fetch import (
    FetchReport,
    HandlerLink,
    ChannelLink,
    SwarmFetchError,
    dial_links,
    swarm_fetch,
)

__all__ = [
    "DEFAULT_N_STRIPES",
    "DEFAULT_SYMBOL_SIZE",
    "FountainError",
    "FountainStore",
    "build_fountain_manifest",
    "check_fountain_manifest",
    "fountain_id",
    "roster_stripe",
    "source_symbols",
    "FountainMetrics",
    "fountain_handler",
    "FountainFetchError",
    "FountainReport",
    "fountain_fetch",
    "BLOCK_SIZE",
    "BlockError",
    "BlockStore",
    "bitmap_hex_to_indices",
    "build_manifest",
    "check_manifest",
    "indices_to_bitmap_hex",
    "manifest_id",
    "SwarmMetrics",
    "swarm_handler",
    "FetchReport",
    "HandlerLink",
    "ChannelLink",
    "SwarmFetchError",
    "dial_links",
    "swarm_fetch",
]
