"""Generate the tls-stream/1 wire vectors — fixtures/stream_v1.json.

Cross-implementation-stable bytes for the seam frozen with the dashboard
lane (auto-9z1xh): the 0x05 frame header, every STREAM_CTRL payload, and
the tls-stream OPEN payload. Deterministic by construction — no keys, no
clocks — and built by CALLING the real frames/stream_wire primitives so
it can never drift from the code it freezes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tools.network.relaykit.frames import FRAME_STREAM_CTRL, encode_frame
from tools.network.relaykit.stream_wire import (
    RESET_ROUTE_RELEASED,
    STREAM_INITIAL_CREDIT,
    build_ctrl_credit,
    build_ctrl_eof,
    build_ctrl_open_ok,
    build_ctrl_reset,
    build_stream_open,
)

CHANNEL_ID = bytes(range(16))
HOST = "docs.worker-9a2db2e23f1504cd0566.serve.auto.network"
RESERVATION = "d3a99356-6161-5507-81c5-71c89f7fdf57"


def build_fixture() -> dict:
    ctrl_payloads = {
        "open_ok": build_ctrl_open_ok(credit=STREAM_INITIAL_CREDIT),
        "credit": build_ctrl_credit(65536),
        "eof": build_ctrl_eof(),
        "reset_route_released": build_ctrl_reset(RESET_ROUTE_RELEASED),
    }
    return {
        "description": "tls-stream/1 golden vectors (auto-9z1xh seam r4)",
        "frame_type_stream_ctrl": FRAME_STREAM_CTRL,
        "channel_id_hex": CHANNEL_ID.hex(),
        "open_payload": json.loads(
            build_stream_open(host=HOST, reservation=RESERVATION,
                              credit=STREAM_INITIAL_CREDIT)
        ),
        "open_payload_hex": build_stream_open(
            host=HOST, reservation=RESERVATION,
            credit=STREAM_INITIAL_CREDIT,
        ).hex(),
        "ctrl_payloads_hex": {
            name: payload.hex() for name, payload in ctrl_payloads.items()
        },
        "ctrl_frames_hex": {
            name: encode_frame(
                FRAME_STREAM_CTRL, CHANNEL_ID, payload
            ).hex()
            for name, payload in ctrl_payloads.items()
        },
    }


def main() -> int:
    out = Path(__file__).parent / "fixtures" / "stream_v1.json"
    out.write_text(json.dumps(build_fixture(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
