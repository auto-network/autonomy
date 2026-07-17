"""relaykit — auto.network relay tunnel + E2E channel crypto (B2).

The pieces of spec §5.1/§5.2 that both ends of the tunnel share:

- ``frames``   — the tunnel mux wire format (Q1: raw WS binary frames)
- ``channel``  — X25519 handshake + AES-256-GCM record layer (I5)
- ``connector``— dashboard-side outbound tunnel client (reconnect/backoff)
- ``viewer``   — bootloader-side reference client (what /l/{token} JS
  will reimplement in WebCrypto)

The registry-side relay endpoint lives in ``tools.network.registry.relay``
— it consumes ``frames`` only, never ``channel``: the relay routes opaque
ciphertext and holds no key material (I5).

G1 adds the network-fabric rungs above the floor (spec
``eb245082-b76`` §8, bead ``auto-57hav``):

- ``peer``     — org peer relay (``relay:serve`` delegation proof, park +
  dial bridging) and the park connector
- ``direct``   — direct-dial listener + candidate client (rung one)
- ``dialer``   — the fallback chain: direct → peer relay → floor
- ``node``     — a member node's composite runtime (listener, parks,
  floor tunnel, reachability announcer)

Spec: graph note ``a17c8657-939`` §5.1, §5.2, §7 (I5).
Beads: ``auto-xbt33`` (B2), ``auto-57hav`` (G1).
"""

from .channel import (
    HANDSHAKE_DOMAIN,
    ChannelCrypto,
    HandshakeError,
    build_client_hello,
    build_server_hello,
    parse_client_hello,
    verify_server_hello,
)
from .frames import (
    FRAME_CLOSE,
    FRAME_DATA,
    FRAME_OPEN,
    Frame,
    decode_frame,
    encode_frame,
)

__all__ = [
    "Frame",
    "encode_frame",
    "decode_frame",
    "FRAME_OPEN",
    "FRAME_DATA",
    "FRAME_CLOSE",
    "ChannelCrypto",
    "HandshakeError",
    "HANDSHAKE_DOMAIN",
    "build_client_hello",
    "build_server_hello",
    "parse_client_hello",
    "verify_server_hello",
]
