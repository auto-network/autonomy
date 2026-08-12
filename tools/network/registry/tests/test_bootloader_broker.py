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
// A transport whose feed queue yields the frames given, then blocks.
function fakeTransport(frames) {
  let i = 0;
  return {
    async recvFeed() {
      if (i < frames.length) return frames[i++];
      return new Promise(() => {});      // no more: never resolves
    },
  };
}

// Seal a payload the way relay.py does: [12B nonce][AES-256-GCM ct], with
// the stream domain as AAD.
async function seal(keyHex, payload) {
  const raw = Uint8Array.from(keyHex.match(/../g).map((h) => parseInt(h, 16)));
  const key = await require('crypto').webcrypto.subtle.importKey(
    'raw', raw, {name: 'AES-GCM'}, false, ['encrypt']);
  const iv = new Uint8Array(12);
  const ct = await require('crypto').webcrypto.subtle.encrypt(
    {name: 'AES-GCM', iv,
     additionalData: new TextEncoder().encode('autonomy.network.channel.stream.v1')},
    key, new TextEncoder().encode(JSON.stringify(payload)));
  return new Uint8Array([...iv, ...new Uint8Array(ct)]);
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


# ── ported from the deleted mission bridge tests ────────────────────────
#
# These pin the properties whose absence caused real outages on this epic.
# The bridge that used to carry them is gone; the behaviour is not, so
# neither is the coverage.


def test_a_feed_frame_reaches_the_viewer_as_an_event():
    """A pushed frame is an event, never mistaken for someone's response.

    On the old single-queue design a frame arriving mid-request was
    swallowed as if it were that request's answer, and the seq check then
    closed the channel -- the page never rendered at all.
    """
    out = _node("""
(async () => {
  const key = 'ab'.repeat(32);
  const frame = await seal(key, {kind:'conversation', question:{entry_id:'e1'}});
  const channel = fakeChannel([{v:1, status:'ok', stream_key: key}]);
  const broker = new A.ChannelBroker(channel, fakeTransport([frame]));
  const pair = new MessageChannel();
  broker.attach(pair.port1);
  const seen = [];
  pair.port2.on('message', (m) => seen.push(m));
  pair.port2.postMessage({v:1, type:'request', id:'s', op:'subscribe'});
  await wait(150);
  process.stdout.write(JSON.stringify({seen}));
  process.exit(0);
})();
""")
    kinds = [(m["type"], m.get("topic")) for m in out["seen"]]
    assert ("response", None) in kinds
    assert ("event", "conversation") in kinds
    event = [m for m in out["seen"] if m["type"] == "event"][0]
    assert event["body"]["question"]["entry_id"] == "e1"


def test_a_frame_sealed_under_another_key_is_dropped():
    """Authentication is the ONLY thing that decides whether a frame is
    ours. A frame that does not open is dropped silently and the feed keeps
    running -- indistinguishable from noise, on purpose."""
    out = _node("""
(async () => {
  const key = 'ab'.repeat(32), other = 'cd'.repeat(32);
  const foreign = await seal(other, {kind:'conversation'});
  const mine = await seal(key, {kind:'presence'});
  const channel = fakeChannel([{v:1, status:'ok', stream_key: key}]);
  const broker = new A.ChannelBroker(channel, fakeTransport([foreign, mine]));
  const pair = new MessageChannel();
  broker.attach(pair.port1);
  const seen = [];
  pair.port2.on('message', (m) => seen.push(m));
  pair.port2.postMessage({v:1, type:'request', id:'s', op:'subscribe'});
  await wait(200);
  process.stdout.write(JSON.stringify({
    topics: seen.filter((m) => m.type === 'event').map((m) => m.topic),
  }));
  process.exit(0);
})();
""")
    # The foreign frame is gone; the one after it still arrives.
    assert out["topics"] == ["presence"]


def test_a_refused_subscribe_leaves_the_viewer_static_not_broken():
    """No live updates is a degraded page, not a dead one. The viewer is
    told, keeps its content, and can still issue ordinary ops."""
    out = _node("""
(async () => {
  const channel = fakeChannel([{v:1, status:'unavailable'}, {v:1, status:'ok', n:7}]);
  const broker = new A.ChannelBroker(channel, fakeTransport([]));
  const pair = new MessageChannel();
  broker.attach(pair.port1);
  const seen = [];
  pair.port2.on('message', (m) => seen.push(m));
  pair.port2.postMessage({v:1, type:'request', id:'s', op:'subscribe'});
  await wait(80);
  pair.port2.postMessage({v:1, type:'request', id:'after', op:'read'});
  await wait(120);
  process.stdout.write(JSON.stringify({
    seen: seen.map((m) => ({id: m.id, ok: m.ok, n: m.body && m.body.n})),
    hasKey: broker.streamKey !== null,
  }));
  process.exit(0);
})();
""")
    assert out["hasKey"] is False
    after = [m for m in out["seen"] if m["id"] == "after"]
    assert after and after[0]["n"] == 7


def test_a_failing_op_does_not_silence_the_channel():
    """One rejected exchange must not poison the queue.

    The chain is kept alive explicitly; without that, every later op
    inherits the rejection and the viewer goes quiet with no error.
    """
    out = _node("""
(async () => {
  let call = 0;
  const channel = {
    async sendMessage() {},
    async recvMessage() {
      call++;
      if (call === 1) throw new Error('channel hiccup');
      return new TextEncoder().encode(JSON.stringify({v:1,status:'ok',n:call}) + '\\n');
    },
  };
  const broker = new A.ChannelBroker(channel, null);
  const pair = new MessageChannel();
  broker.attach(pair.port1);
  const seen = [];
  pair.port2.on('message', (m) => seen.push(m));
  pair.port2.postMessage({v:1, type:'request', id:'boom', op:'read'});
  await wait(80);
  pair.port2.postMessage({v:1, type:'request', id:'after', op:'read'});
  await wait(120);
  process.stdout.write(JSON.stringify({
    seen: seen.map((m) => ({id: m.id, ok: m.ok})),
  }));
  process.exit(0);
})();
""")
    by_id = {m["id"]: m["ok"] for m in out["seen"]}
    assert by_id["boom"] is False       # the caller is told
    assert by_id["after"] is True       # and the next op still works


# ── the shell's own chrome, claimed by the viewer ───────────────────────


def test_the_shell_hides_its_header_only_when_a_viewer_claims_the_surface():
    """Which viewers own their surface is not the shell's business.

    This used to be `artifact.kind === "mission"`. A viewer that renders
    its own top bar now says so, so the shell never learns what it is
    showing -- and a new viewer of any kind gets the behaviour without a
    registry deploy.
    """
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "bootloader"
    script = (root / "autonet.js").read_text()
    css = (root / "bootloader.html").read_text()

    assert "body.surface-owned > header { display: none; }" in css
    assert 'event.data.op === "chrome"' in script
    assert 'event.data.own === true' in script
    # The export button lives in that header, so the claim is refused while
    # a download is on offer -- otherwise it becomes unreachable.
    assert "exportOffered" in script
    # And no viewer kind decides it.
    assert 'artifact.kind === "mission"' not in script
