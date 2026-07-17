"""Share-link token generation.

Invariant I2 is enforced **by construction**: :func:`generate_token` takes
no arguments at all — there is no way to pass target content, a target
UUID, or anything else that a token could be derived from. Every token is
128 bits straight from the OS CSPRNG (``secrets``), hex-encoded.
"""

from __future__ import annotations

import secrets

TOKEN_BITS = 128
TOKEN_HEX_LEN = TOKEN_BITS // 4  # 32 hex chars


def generate_token() -> str:
    """Return a fresh 128-bit CSPRNG token as 32 lowercase hex chars."""
    return secrets.token_hex(TOKEN_BITS // 8)
