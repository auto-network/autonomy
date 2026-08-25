import math

import pytest

from tools.network.fleet_sync.codec import (
    MAGIC,
    CodecError,
    Mutation,
    decode_stream,
    decode_value,
    encode_stream,
    encode_value,
)


def _source(*, timestamp: int, title: str) -> Mutation:
    return Mutation(
        table="sources",
        address=("source-1",),
        timestamp_ns=timestamp,
        tombstone=False,
        values=(
            ("metadata", {"b": [2, 1], "a": True}),
            ("title", title),
            ("weight", 0.5),
        ),
    )


def test_typed_values_have_one_round_trip_representation() -> None:
    value = {
        "blob": b"\x00\xff",
        "float": 1.25,
        "integer": -(1 << 80),
        "list": [None, False, True, "text"],
    }
    encoded = encode_value(value)
    assert decode_value(encoded) == value
    assert encode_value(decode_value(encoded)) == encoded
    assert encode_value(-0.0) == encode_value(0.0)
    with pytest.raises(CodecError, match="non-finite"):
        encode_value(math.inf)


def test_stream_bytes_ignore_input_order_and_sort_exact_ties() -> None:
    older = _source(timestamp=10, title="old")
    concurrent_a = _source(timestamp=20, title="a")
    concurrent_b = _source(timestamp=20, title="b")
    left = encode_stream([concurrent_b, older, concurrent_a])
    right = encode_stream([concurrent_a, concurrent_b, older])
    assert left == right
    decoded = decode_stream(left)
    assert decoded[0] == older
    assert [item.candidate_hash for item in decoded[1:]] == sorted(
        [concurrent_a.candidate_hash, concurrent_b.candidate_hash]
    )
    assert encode_stream([older, older]) == encode_stream([older])


def test_decoder_rejects_noncanonical_or_tampered_streams() -> None:
    encoded = encode_stream([_source(timestamp=10, title="ok")])
    assert decode_stream(encoded)
    with pytest.raises(CodecError):
        decode_stream(MAGIC + encoded[len(MAGIC):] + b"junk")
    damaged = bytearray(encoded)
    damaged[-1] ^= 1
    with pytest.raises(CodecError):
        decode_stream(bytes(damaged))
    with pytest.raises(CodecError, match="domain/version"):
        decode_stream(encoded.replace(b"\x00v1\n", b"\x00v2\n", 1))
    # A map with keys in descending encoded order is syntactically complete
    # but has no canonical representation.
    noncanonical_map = (
        b"d\x00\x00\x00\x02"
        b"s\x00\x00\x00\x01z" b"n"
        b"s\x00\x00\x00\x01a" b"n"
    )
    with pytest.raises(CodecError, match="strictly ordered"):
        decode_value(noncanonical_map)


def test_codec_refuses_local_state_and_machine_local_columns() -> None:
    with pytest.raises(CodecError, match="not replicating"):
        Mutation("orgs", ("id",), 1, False, (("slug", "personal"),))
    identity = Mutation(
        "settings",
        ("autonomy.identity.personal", 1, "root", "raw", "base"),
        1,
        False,
        (("payload", {"armor": "ciphertext"}),),
    )
    assert decode_stream(encode_stream([identity])) == [identity]
    with pytest.raises(CodecError, match="machine-local"):
        Mutation(
            "attachments",
            ("attachment-1",),
            1,
            False,
            (("file_path", "/one/machine/path"),),
        )


def test_tombstone_is_an_ordered_candidate_without_values() -> None:
    tombstone = Mutation("sources", ("source-1",), 30, True)
    assert decode_stream(encode_stream([tombstone])) == [tombstone]
    with pytest.raises(CodecError, match="do not carry"):
        Mutation("sources", ("source-1",), 30, True, (("title", "bad"),))
