"""Fan-out stream frames (auto-albp6.8).

A different shape from the record layer for a different job: sealed ONCE
under a key every viewer of a link holds, so the relay can copy one
ciphertext to all of them instead of the origin re-sealing per viewer.

What this deliberately does NOT provide is origin authentication -- every
viewer holds the key, so a valid tag proves only that someone holding it
sealed the frame. Per-entry signing is the answer and is deferred by
operator decision (auto-albp6.2). The last test here pins that limitation
explicitly so nobody later mistakes the encryption for proof of origin.
"""

from __future__ import annotations

import os

import pytest

from tools.network.relaykit.channel import (
    STREAM_KEY_LEN,
    RecordError,
    open_stream_frame,
    seal_stream_frame,
)


def key() -> bytes:
    return os.urandom(STREAM_KEY_LEN)


def test_roundtrip():
    k = key()
    assert open_stream_frame(k, seal_stream_frame(k, b"live update")) == b"live update"


def test_every_holder_of_the_key_opens_the_same_frame():
    """The property fan-out depends on: ONE sealed frame, N readers."""
    k = key()
    sealed = seal_stream_frame(k, b"one frame")
    assert [open_stream_frame(k, sealed) for _ in range(50)] == [b"one frame"] * 50


def test_another_links_key_cannot_open_it():
    assert open_stream_frame(key(), seal_stream_frame(key(), b"secret")) is None


def test_nonces_differ_per_frame():
    """One party seals with a given stream key, so a random nonce is
    safe -- but it must actually be random per frame, or identical
    plaintexts would produce identical ciphertexts."""
    k = key()
    sealed = {seal_stream_frame(k, b"same") for _ in range(100)}
    assert len(sealed) == 100


def test_a_tampered_frame_is_rejected():
    k = key()
    sealed = bytearray(seal_stream_frame(k, b"authentic"))
    sealed[-1] ^= 0x01
    assert open_stream_frame(k, bytes(sealed)) is None


def test_a_tampered_nonce_is_rejected():
    k = key()
    sealed = bytearray(seal_stream_frame(k, b"authentic"))
    sealed[0] ^= 0x01
    assert open_stream_frame(k, bytes(sealed)) is None


def test_empty_plaintext_roundtrips():
    k = key()
    assert open_stream_frame(k, seal_stream_frame(k, b"")) == b""


def test_a_large_frame_roundtrips():
    k = key()
    body = os.urandom(3 * 1024 * 1024)
    assert open_stream_frame(k, seal_stream_frame(k, body)) == body


# Fixed byte strings, never os.urandom: a random parametrize value gets a
# different test ID on every xdist worker, so collection disagrees and the
# whole run errors out.
@pytest.mark.parametrize("bad_key", [b"", b"short", b"k" * 31, b"k" * 33, "notbytes", None])
def test_sealing_requires_a_full_length_key(bad_key):
    with pytest.raises(RecordError):
        seal_stream_frame(bad_key, b"x")


@pytest.mark.parametrize("bad_key", [b"", b"short", b"k" * 31, "notbytes", None])
def test_opening_with_a_malformed_key_fails_closed(bad_key):
    assert open_stream_frame(bad_key, seal_stream_frame(key(), b"x")) is None


@pytest.mark.parametrize("bad", [b"", b"tooshort", b"n" * 12, "notbytes", None])
def test_a_truncated_or_malformed_frame_fails_closed(bad):
    assert open_stream_frame(key(), bad) is None


def test_the_tag_does_not_prove_who_sealed_it():
    """The recorded limitation, pinned: any holder of the stream key can
    produce a frame that authenticates. This is why per-entry signing
    exists in the design (auto-albp6.2, deferred) -- if this test ever
    starts failing, the threat model changed and that decision needs
    revisiting."""
    shared = key()
    forged = seal_stream_frame(shared, b"i am not the coordinator")
    assert open_stream_frame(shared, forged) == b"i am not the coordinator"
