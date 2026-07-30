"""Generate the cross-language attachment streaming vectors.

The fixture deliberately includes full plaintext messages and sealed records.
That makes the record/application boundary unambiguous for Python, browser,
and Go consumers: ``records_hex`` decrypt and reassemble to ``message_hex``;
``decoded`` then states how the attachment layer interprets those bytes.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from tools.network.idkit import canonical_json
from tools.network.relaykit.channel import (
    CHUNK_SIZE as RECORD_CHUNK_SIZE,
    DIR_C2S,
    DIR_S2C,
    ChannelCrypto,
)

APPLICATION_CHUNK_SIZE = 1024 * 1024
WINDOW_SIZE = 8 * APPLICATION_CHUNK_SIZE
LAST_IN_WINDOW = 0x01
EOF = 0x02

C2S_KEY = bytes(range(32))
S2C_KEY = bytes(range(32, 64))
TRANSCRIPT_HASH = hashlib.sha256(b"autonomy.attachment.v1.fixture").digest()
REF = "att-vector-1"


def _cryptos() -> tuple[ChannelCrypto, ChannelCrypto]:
    return (
        ChannelCrypto(
            C2S_KEY, S2C_KEY, TRANSCRIPT_HASH, DIR_C2S, DIR_S2C
        ),
        ChannelCrypto(
            S2C_KEY, C2S_KEY, TRANSCRIPT_HASH, DIR_S2C, DIR_C2S
        ),
    )


def _body(offset: int, flags: int, chunk: bytes) -> bytes:
    return offset.to_bytes(8, "big") + bytes([flags]) + chunk


def _step(
    sender: str,
    message: bytes,
    records: list[bytes],
    *,
    starting_sequence: int,
    decoded: dict,
) -> dict:
    return {
        "sender": sender,
        "starting_sequence": starting_sequence,
        "message_hex": message.hex(),
        "records_hex": [record.hex() for record in records],
        "decoded": decoded,
    }


def _error_vectors() -> list[dict]:
    vectors = []
    for code in (
        "not_found",
        "not_authorized",
        "oversize",
        "out_of_range",
        "unavailable",
        "internal",
    ):
        _client, server = _cryptos()
        decoded = {"v": 1, "op": "error", "ref": REF, "code": code}
        if code == "internal":
            decoded["detail"] = "fixture detail"
        message = canonical_json(decoded)
        vectors.append(
            _step(
                "server",
                message,
                server.seal_message(message),
                starting_sequence=0,
                decoded=decoded,
            )
        )
    return vectors


def build_vectors() -> dict:
    client, server = _cryptos()
    chunk_a = bytes(range(256)) * (APPLICATION_CHUNK_SIZE // 256)
    chunk_b = bytes(reversed(range(256))) * (APPLICATION_CHUNK_SIZE // 256)
    tail = b"end"
    raw = chunk_a + chunk_b + tail
    raw_sha256 = hashlib.sha256(raw).hexdigest()

    first_request_dict = {
        "v": 1,
        "op": "attachment.fetch",
        "ref": REF,
        "offset": 0,
        "length": 2 * APPLICATION_CHUNK_SIZE,
    }
    first_request = canonical_json(first_request_dict)
    first_request_records = client.seal_message(first_request)

    first_body = _body(0, 0, chunk_a)
    first_body_records = server.seal_message(first_body, stream_final=False)
    second_body = _body(
        APPLICATION_CHUNK_SIZE, LAST_IN_WINDOW, chunk_b
    )
    second_body_records = server.seal_message(second_body, stream_final=True)

    second_request_dict = {
        "v": 1,
        "op": "attachment.fetch",
        "ref": REF,
        "offset": 2 * APPLICATION_CHUNK_SIZE,
        "length": APPLICATION_CHUNK_SIZE,
    }
    second_request = canonical_json(second_request_dict)
    second_request_records = client.seal_message(second_request)
    final_body = _body(
        2 * APPLICATION_CHUNK_SIZE, LAST_IN_WINDOW | EOF, tail
    )
    final_body_records = server.seal_message(final_body, stream_final=True)
    cancel_client, _cancel_server = _cryptos()
    cancel_dict = {"v": 1, "op": "attachment.cancel", "ref": REF}
    cancel_message = canonical_json(cancel_dict)

    return {
        "schema": "autonomy.network.attachment.vectors.v1",
        "constants": {
            "application_chunk_size": APPLICATION_CHUNK_SIZE,
            "window_size": WINDOW_SIZE,
            "record_chunk_size": RECORD_CHUNK_SIZE,
            "body_header_size": 9,
            "body_flags": {
                "last_in_window": LAST_IN_WINDOW,
                "eof": EOF,
            },
            "record_flags": {
                "stream_final": 1,
                "message_end": 2,
            },
        },
        "record_crypto": {
            "c2s_key_hex": C2S_KEY.hex(),
            "s2c_key_hex": S2C_KEY.hex(),
            "transcript_hash_hex": TRANSCRIPT_HASH.hex(),
        },
        "manifest": {
            "ref": REF,
            "name": "vector.bin",
            "mime": "application/octet-stream",
            "raw_sha256": raw_sha256,
            "total_size": len(raw),
            "oversize": False,
        },
        "positive": [
            {
                "name": "two_messages_end_mid_file",
                "stream_steps": [
                    _step(
                        "client",
                        first_request,
                        first_request_records,
                        starting_sequence=0,
                        decoded=first_request_dict,
                    ),
                    _step(
                        "server",
                        first_body,
                        first_body_records,
                        starting_sequence=0,
                        decoded={
                            "kind": "attachment.body",
                            "offset": 0,
                            "flags": 0,
                            "last_in_window": False,
                            "eof": False,
                            "chunk_length": len(chunk_a),
                            "chunk_sha256": hashlib.sha256(chunk_a).hexdigest(),
                        },
                    ),
                    _step(
                        "server",
                        second_body,
                        second_body_records,
                        starting_sequence=len(first_body_records),
                        decoded={
                            "kind": "attachment.body",
                            "offset": APPLICATION_CHUNK_SIZE,
                            "flags": LAST_IN_WINDOW,
                            "last_in_window": True,
                            "eof": False,
                            "chunk_length": len(chunk_b),
                            "chunk_sha256": hashlib.sha256(chunk_b).hexdigest(),
                        },
                    ),
                ],
            },
            {
                "name": "resume_window_reaches_eof",
                "stream_steps": [
                    _step(
                        "client",
                        second_request,
                        second_request_records,
                        starting_sequence=len(first_request_records),
                        decoded=second_request_dict,
                    ),
                    _step(
                        "server",
                        final_body,
                        final_body_records,
                        starting_sequence=(
                            len(first_body_records) + len(second_body_records)
                        ),
                        decoded={
                            "kind": "attachment.body",
                            "offset": 2 * APPLICATION_CHUNK_SIZE,
                            "flags": LAST_IN_WINDOW | EOF,
                            "last_in_window": True,
                            "eof": True,
                            "chunk_length": len(tail),
                            "chunk_sha256": hashlib.sha256(tail).hexdigest(),
                        },
                    ),
                ],
            },
        ],
        "cancel": _step(
            "client",
            cancel_message,
            cancel_client.seal_message(cancel_message),
            starting_sequence=0,
            decoded=cancel_dict,
        ),
        "errors": _error_vectors(),
        "negative": [
            {
                "name": "out_of_range_offset",
                "kind": "request",
                "message_hex": canonical_json(
                    {
                        "v": 1,
                        "op": "attachment.fetch",
                        "ref": REF,
                        "offset": 1,
                        "length": APPLICATION_CHUNK_SIZE,
                    }
                ).hex(),
                "expected_error": "out_of_range",
            },
            {
                "name": "overlapping_frame",
                "kind": "assembly",
                "request": {"offset": 0, "length": APPLICATION_CHUNK_SIZE},
                "total_size": APPLICATION_CHUNK_SIZE + 1,
                "messages_hex": [
                    _body(0, 0, b"ab").hex(),
                    _body(1, LAST_IN_WINDOW, b"c").hex(),
                ],
                "expected_error": "non_contiguous",
            },
            {
                "name": "non_contiguous_frame",
                "kind": "assembly",
                "request": {"offset": 0, "length": APPLICATION_CHUNK_SIZE},
                "total_size": APPLICATION_CHUNK_SIZE + 1,
                "messages_hex": [
                    _body(0, 0, b"a").hex(),
                    _body(2, LAST_IN_WINDOW, b"b").hex(),
                ],
                "expected_error": "non_contiguous",
            },
            {
                "name": "window_ends_early",
                "kind": "assembly",
                "request": {"offset": 0, "length": APPLICATION_CHUNK_SIZE},
                "total_size": APPLICATION_CHUNK_SIZE + 1,
                "messages_hex": [_body(0, LAST_IN_WINDOW, b"a").hex()],
                "expected_error": "wrong_window_end",
            },
            {
                "name": "eof_before_total_size",
                "kind": "assembly",
                "request": {"offset": 0, "length": APPLICATION_CHUNK_SIZE},
                "total_size": 5,
                "messages_hex": [
                    _body(0, LAST_IN_WINDOW | EOF, b"a").hex()
                ],
                "expected_error": "wrong_eof",
            },
        ],
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} OUTPUT.json", file=sys.stderr)
        return 2
    output = Path(argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(build_vectors(), ensure_ascii=True, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
