"""autonet.js's viewer-socket demultiplexer.

This exists because nothing tested it. The Python suite drives ViewerChannel,
which has its own copy of the same rule, so when autonet.js was left on an
older framing scheme every test still passed and every relay link broke in
production: the handshake received [0x00][SERVER_HELLO] with the kind byte
still attached and failed to parse it as JSON, surfacing as "could not verify
the Autonomy dashboard serving this link".
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

AUTONET_JS = Path(__file__).resolve().parents[1] / "bootloader" / "autonet.js"
AUTONET_TEST_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "relaykit" / "tests" / "autonet_test_source.cjs"
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")


def _demux(frames: list[list[int]]) -> dict:
    script = r"""
let src = require(%s).loadAutonetTestSource();
src = src.replace(/window\.autonet = autonet;[\s\S]*$/, 'return autonet;');
src = src.replace(/^const autonet = \(\(\) => \{/, '');
let made = null;
class FakeWS {
  constructor() { made = this; this.binaryType = 'blob'; setTimeout(() => this.onopen && this.onopen(), 0); }
  send() {} close() { this.closed = true; }
}
const A = new Function('TextEncoder','TextDecoder','crypto','WebSocket',
  src)(TextEncoder, TextDecoder, require('crypto').webcrypto, FakeWS);
(async () => {
  const t = await A.openSocket('ws://x');
  const frames = JSON.parse(process.argv[1]);
  const records = [], feeds = [];
  const pumpR = () => t.recvBinary().then(b => { records.push([...b]); pumpR(); }, () => {});
  const pumpF = () => t.recvFeed().then(b => { feeds.push([...b]); pumpF(); }, () => {});
  pumpR(); pumpF();
  await new Promise(r => setTimeout(r, 10));
  for (const f of frames) {
    made.onmessage({ data: new Uint8Array(f).buffer });
  }
  await new Promise(r => setTimeout(r, 20));
  process.stdout.write(JSON.stringify({records, feeds, closed: made.closed === true}));
  process.exit(0);
})();
    """ % json.dumps(str(AUTONET_TEST_SOURCE))
    r = subprocess.run(["node", "-e", script, json.dumps(frames)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_the_kind_byte_is_stripped_before_the_payload_is_used():
    """The exact production failure: a record delivered with its kind byte
    still attached is not valid JSON, so the handshake cannot verify."""
    out = _demux([[0x00, 0x7b, 0x7d]])          # 0x00 + "{}"
    assert out["records"] == [[0x7b, 0x7d]], "kind byte was not stripped"
    assert out["feeds"] == []


def test_feed_frames_route_to_the_feed_queue():
    out = _demux([[0x01, 1, 2, 3]])
    assert out["feeds"] == [[1, 2, 3]]
    assert out["records"] == []


def test_records_and_feeds_interleave_without_crossing():
    out = _demux([[0x00, 10], [0x01, 20], [0x00, 30], [0x01, 40]])
    assert out["records"] == [[10], [30]]
    assert out["feeds"] == [[20], [40]]


def test_an_unknown_kind_poison_closes_the_socket():
    out = _demux([[0x7f, 9], [0x00, 5]])
    assert out["records"] == []
    assert out["feeds"] == []
    assert out["closed"] is True
