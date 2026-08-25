from tools.network.fleet_sync.codec import Mutation, decode_stream
from tools.network.fleet_sync.segments import encode_segments


def test_segments_are_independent_ordered_codec_objects() -> None:
    mutations = [
        Mutation(
            "sources", (f"s{number}",), number, False,
            (("id", f"s{number}"), ("title", "x" * 80)),
        )
        for number in range(20)
    ]
    segments = encode_segments(reversed(mutations), target_bytes=700)
    assert len(segments) > 1
    assert [segment.ordinal for segment in segments] == list(range(len(segments)))
    decoded = [
        mutation
        for segment in segments
        for mutation in decode_stream(segment.payload)
    ]
    assert decoded == mutations
    assert all(len(segment.payload) <= 700 for segment in segments)
    assert len({segment.digest for segment in segments}) == len(segments)
