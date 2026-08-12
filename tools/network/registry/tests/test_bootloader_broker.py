"""The generic channel broker: one MessagePort per viewer document.

Drives ChannelBroker directly in Node against a fake channel, because the
behaviour worth pinning is the contract -- correlation, key custody, and
what happens to a previous document's in-flight work -- not rendering.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

AUTONET_JS = Path(__file__).resolve().parents[1] / "bootloader" / "autonet.js"

_PRELUDE = """
const fs = require('fs');
let src = fs.readFileSync(%s, 'utf8');
src = src.replace(/window\\.autonet = autonet;[\\s\\S]*$/, 'return autonet;');
src = src.replace(/^const autonet = \\(\\(\\) => \\{/, '');
const A = new Function('TextEncoder', 'TextDecoder', 'crypto', 'MessageChannel', src)(
  TextEncoder, TextDecoder, require('crypto').webcrypto, require('worker_threads').MessageChannel);

// A channel that answers each request with a canned JSON line.
function fakeChannel(replies) {
  let i = 0;
  const sent = [];
  return {
    sent,
    async sendMessage(bytes) { sent.push(JSON.parse(new TextDecoder().decode(bytes))); },
    async recvMessage() {
      const reply = replies[Math.min(i++, replies.length - 1)];
      return new TextEncoder().encode(JSON.stringify(reply) + '\\n');
    },
  };
}
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
"""


def _node(body: str) -> dict:
    script = _PRELUDE % json.dumps(str(AUTONET_JS)) + body
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")


def test_requests_are_correlated_and_forwarded_opaquely():
    """Two concurrent calls both get answered, matched by id.

    The singleton pendingOp this replaces dropped the first of any two
    overlapping calls -- which is how a pillar roster silently arrived
    empty while a presence poll succeeded.
    """
    out = _node("""
(async () => {
  const channel = fakeChannel([{v:1,status:'ok',n:1}, {v:1,status:'ok',n:2}]);
  const broker = new A.ChannelBroker(channel, null);
  const pair = new MessageChannel();
  broker.attach(pair.port1);
  const seen = [];
  pair.port2.on('message', (m) => seen.push(m));
  pair.port2.postMessage({v:1, type:'request', id:'a', op:'read', body:{kind:'pillars'}});
  pair.port2.postMessage({v:1, type:'request', id:'b', op:'read', body:{kind:'presence'}});
  await wait(120);
  process.stdout.write(JSON.stringify({
    replies: seen.map((m) => ({id: m.id, ok: m.ok, n: m.body && m.body.n})),
    forwarded: channel.sent.map((m) => [m.op, m.body && m.body.kind]),
  }));
  process.exit(0);
})();
""")
    assert sorted(r["id"] for r in out["replies"]) == ["a", "b"]
    assert all(r["ok"] for r in out["replies"])
    assert out["forwarded"] == [["read", "pillars"], ["read", "presence"]]


def test_the_stream_key_never_reaches_the_viewer():
    """subscribe is answered by the host, which keeps the key.

    A viewer holding the stream key could open fan-out traffic for the
    link directly, which would make the sandbox mean only "cannot read the
    parent's DOM".
    """
    out = _node("""
(async () => {
  const key = 'ab'.repeat(32);
  const channel = fakeChannel([{v:1, status:'ok', stream_key: key}]);
  const broker = new A.ChannelBroker(channel, null);
  const pair = new MessageChannel();
  broker.attach(pair.port1);
  const seen = [];
  pair.port2.on('message', (m) => seen.push(m));
  pair.port2.postMessage({v:1, type:'request', id:'s', op:'subscribe'});
  await wait(120);
  process.stdout.write(JSON.stringify({
    reply: seen[0],
    hostHasKey: broker.streamKey instanceof Uint8Array && broker.streamKey.length === 32,
  }));
  process.exit(0);
})();
""")
    assert out["reply"]["ok"] is True
    assert "stream_key" not in out["reply"]["body"]
    assert out["hostHasKey"] is True


def test_a_replacement_port_supersedes_the_previous_document():
    """After document.open/write/close the new document gets a new port.

    The old document's in-flight reply must not be delivered to it: the
    replacement never issued that request and carries its own fresh state.
    """
    out = _node("""
(async () => {
  const channel = fakeChannel([{v:1,status:'ok',which:'first'}, {v:1,status:'ok',which:'second'}]);
  const broker = new A.ChannelBroker(channel, null);
  const first = new MessageChannel();
  broker.attach(first.port1);
  const firstSeen = [], secondSeen = [];
  first.port2.on('message', (m) => firstSeen.push(m));
  first.port2.postMessage({v:1, type:'request', id:'old', op:'read'});

  const second = new MessageChannel();   // the replacement document
  broker.attach(second.port1);
  second.port2.on('message', (m) => secondSeen.push(m));
  second.port2.postMessage({v:1, type:'request', id:'new', op:'read'});
  await wait(150);
  process.stdout.write(JSON.stringify({
    firstIds: firstSeen.map((m) => m.id),
    secondIds: secondSeen.map((m) => m.id),
  }));
  process.exit(0);
})();
""")
    assert out["secondIds"] == ["new"]
    assert "new" not in out["firstIds"]


def test_malformed_viewer_messages_are_ignored():
    out = _node("""
(async () => {
  const channel = fakeChannel([{v:1,status:'ok'}]);
  const broker = new A.ChannelBroker(channel, null);
  const pair = new MessageChannel();
  broker.attach(pair.port1);
  for (const bad of [null, {}, {v:2,type:'request',op:'read'},
                     {v:1,type:'event',op:'read'}, {v:1,type:'request'},
                     {v:1,type:'request',op:5}]) {
    pair.port2.postMessage(bad);
  }
  await wait(100);
  process.stdout.write(JSON.stringify({forwarded: channel.sent.length}));
  process.exit(0);
})();
""")
    assert out["forwarded"] == 0
