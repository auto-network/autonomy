"""Share-link token acceptance: 128 bits, hex, non-derivable (invariant I2)."""

from __future__ import annotations

import inspect
import string

from tools.network.idkit import TOKEN_BITS, TOKEN_HEX_LEN, generate_token


def test_token_format_is_128_bit_lowercase_hex():
    token = generate_token()
    assert len(token) == TOKEN_HEX_LEN == 32
    assert set(token) <= set(string.hexdigits.lower())
    assert 0 <= int(token, 16) < 2**TOKEN_BITS


def test_i2_no_target_input_in_api_signature():
    """I2 is enforced by construction: the generator accepts NO arguments,
    so a token *cannot* be derived from target content or UUID."""
    assert inspect.signature(generate_token).parameters == {}


def test_tokens_are_unique():
    tokens = {generate_token() for _ in range(2000)}
    assert len(tokens) == 2000


def test_tokens_look_uniform():
    """Statistical sanity, not proof of CSPRNG quality: over 256k bits the
    one-bit fraction must sit deep inside [0.48, 0.52] (>10 sigma margin),
    and every hex symbol must appear at roughly its expected rate. A
    target-derived or counter-based implementation with structure this
    coarse would fail; secrets.token_hex passes with astronomic margin."""
    tokens = [generate_token() for _ in range(2000)]

    total_bits = len(tokens) * TOKEN_BITS
    one_bits = sum(bin(int(t, 16)).count("1") for t in tokens)
    assert 0.48 < one_bits / total_bits < 0.52

    counts = {c: 0 for c in string.hexdigits.lower()[:16]}
    for t in tokens:
        for c in t:
            counts[c] += 1
    expected = len(tokens) * TOKEN_HEX_LEN / 16
    for c, n in counts.items():
        assert 0.85 * expected < n < 1.15 * expected, f"hex symbol {c!r} count {n} far from {expected}"
