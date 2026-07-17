"""swarmkit — content-addressed swarm bulk fetch (G2, spec §8).

Large artifacts move as verified blocks pulled from ANY org peer that
holds them; the transport (relaykit channels over any rung of the
fallback chain) is untrusted — hashes are integrity, signatures are
authorship.
"""

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
