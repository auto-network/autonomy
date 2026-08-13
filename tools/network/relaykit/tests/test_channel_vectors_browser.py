"""Browser-JS side of the frozen channel fixture (auto-25pky).

Loads the SHIPPED bootloader client (autonet.js) with real WebCrypto and
proves it implements exactly the same protocol as the fixture: canonical JSON
byte-compat, cert-chain verification, the Ed25519 SERVER_HELLO signature, HKDF
key derivation, and SecureChannel record open/seal — reproducing positives and
refusing negatives. Same bytes Python (test_channel_vectors.py) and Go
(auto-64fht) validate against. Skipped when node is absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

AUTONET = Path(__file__).resolve().parents[1].parent / "registry" / "bootloader" / "autonet.js"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "channel_v1.json"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")

_JS = r"""
const fs = require('fs');
const webcrypto = require('crypto').webcrypto;
let src = fs.readFileSync(process.argv[2], 'utf8');
src = src.replace(/window\.autonet = autonet;[\s\S]*$/, 'return autonet;');
src = src.replace(/^const autonet = \(\(\) => \{/, '');
const A = new Function('TextEncoder', 'crypto', src)(TextEncoder, webcrypto);
const V = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

const te = new TextEncoder();
const hex = (u8) => Buffer.from(u8).toString('hex');
const unhex = (h) => new Uint8Array(Buffer.from(h, 'hex'));
async function importAes(h) { return webcrypto.subtle.importKey('raw', unhex(h), { name: 'AES-GCM' }, false, ['encrypt','decrypt']); }
const KEYS_INFO = te.encode('autonomy.network.channel.keys.v1');
const HANDSHAKE_DOMAIN = te.encode('autonomy.network.channel.handshake.v1\n');
const results = [];
const check = (name, cond) => results.push({ name, ok: !!cond });

async function ed25519Verify(pubHex, sigHex, data) {
  const key = await webcrypto.subtle.importKey('raw', unhex(pubHex), 'Ed25519', false, ['verify']);
  return webcrypto.subtle.verify('Ed25519', key, unhex(sigHex), data);
}
async function hkdf(sharedHex, saltHex) {
  const ikm = await webcrypto.subtle.importKey('raw', unhex(sharedHex), 'HKDF', false, ['deriveBits']);
  const bits = await webcrypto.subtle.deriveBits(
    { name: 'HKDF', hash: 'SHA-256', salt: unhex(saltHex), info: KEYS_INFO }, ikm, 64 * 8);
  return hex(new Uint8Array(bits));
}
function signedPayload(org, token, clientEph, serverEph) {
  return new Uint8Array([...HANDSHAKE_DOMAIN, ...te.encode(
    A.canonicalJson({ v: 1, org, token, client_eph: clientEph, server_eph: serverEph }))]);
}
function serverHelloObj(wireHex) { return JSON.parse(Buffer.from(unhex(wireHex)).toString('utf8')); }

// A fake ws feeding fixed records to SecureChannel.recvRecord / draining sends.
function fakeWs(records) {
  const q = (records || []).map(unhex);
  return { sent: [], recvBinary: async () => { if (!q.length) throw new Error('eof'); return q.shift(); },
           send: function (b) { this.sent.push(hex(b)); }, close() {} };
}

(async () => {
  // 1) canonical JSON byte-compat + Ed25519 sig + HKDF keys, per handshake.
  for (const hs of V.handshakes) {
    const helloClient = te.encode(A.canonicalJson({ v: 1, eph_pub: hs.client_public_hex }));
    check('canonical_client_hello:' + hs.name, hex(helloClient) === hs.client_hello_utf8_hex);
    const sp = signedPayload(hs.org, hs.token, hs.client_public_hex, hs.server_public_hex);
    check('canonical_signed_payload:' + hs.name, hex(sp) === hs.signed_payload_hex);
    const leafPub = hs.delegates[hs.delegates.length - 1].public_hex;
    check('hello_sig_verifies:' + hs.name, await ed25519Verify(leafPub, hs.hello_signature_hex, sp));
    check('hkdf_keys:' + hs.name,
      (await hkdf(hs.shared_secret_hex, hs.transcript_hash_hex)) === hs.hkdf_okm_hex);
    // Cert chain verifies to the pinned root with tunnel:serve.
    let chainOk = false;
    try { await A.verifyChain(serverHelloObj(hs.server_hello_utf8_hex).cert,
      hs.root.public_hex, hs.org, 'tunnel:serve', hs.now); chainOk = true; } catch (e) {}
    check('verify_chain:' + hs.name, chainOk);
  }

  // 2) SecureChannel opens the s2c (server->client, the browser's recv dir) records + streams.
  const hsByName = Object.fromEntries(V.handshakes.map(h => [h.name, h]));
  for (const rec of V.records.filter(r => r.direction === 's2c')) {
    const hs = hsByName[rec.handshake];
    const ch = new A.SecureChannel(fakeWs([rec.wire_record_hex]), await importAes(hs.key_c2s_hex),
      await importAes(hs.key_s2c_hex), unhex(hs.transcript_hash_hex));
    ch.recvSeq = rec.sequence;
    const { flags, chunk } = await ch.recvRecord();
    check('record_open:' + rec.name, flags === rec.flags && hex(chunk) === rec.chunk_hex);
  }
  for (const st of V.streams) {
    const hs = hsByName[st.handshake];
    for (const step of st.steps.filter(s => s.sender === 'server')) {
      const ch = new A.SecureChannel(fakeWs(step.records_hex), await importAes(hs.key_c2s_hex),
        await importAes(hs.key_s2c_hex), unhex(hs.transcript_hash_hex));
      ch.recvSeq = step.start_sequence;
      const msg = await ch.recvMessage();
      check('stream_open:' + st.name, hex(msg) === step.message_hex);
    }
    // Browser SENDS c2s. Its sender marks a final single-chunk record with
    // STREAM_FINAL only (0x01); channel.py adds MSG_END too (0x03). Both are
    // wire-compatible — every recv treats either bit as message-end — so seal
    // is NOT byte-identical across languages. We prove cross-language SEND two
    // sound ways instead of by fragile byte-equality:
    //   (a) the browser OPENS channel.py's c2s records (dir-swapped receiver);
    //   (b) the browser's OWN sealed records round-trip through a browser reader.
    const C2S = te.encode('c2s\x00');
    for (const step of st.steps.filter(s => s.sender === 'client')) {
      const hs2 = hsByName[st.handshake];
      // (a) read the channel.py-sealed c2s bytes from the fixture.
      const rx = new A.SecureChannel(fakeWs(step.records_hex), await importAes(hs2.key_s2c_hex),
        await importAes(hs2.key_c2s_hex), unhex(hs2.transcript_hash_hex));
      rx.recvDir = C2S; rx.recvSeq = step.start_sequence;
      const opened = await rx.recvMessage();
      check('client_stream_open:' + st.name, hex(opened) === step.message_hex);
      // (b) browser seals the same message, then reads its own records back.
      const ws = fakeWs([]);
      const tx = new A.SecureChannel(ws, await importAes(hs2.key_c2s_hex), await importAes(hs2.key_s2c_hex),
        unhex(hs2.transcript_hash_hex));
      tx.sendSeq = step.start_sequence;
      await tx.sendMessage(unhex(step.message_hex));
      check('client_stream_record_limit:' + st.name,
        ws.sent.every(h => Buffer.from(h, 'hex').length <= 65536));
      const rx2 = new A.SecureChannel(fakeWs(ws.sent), await importAes(hs2.key_s2c_hex),
        await importAes(hs2.key_c2s_hex), unhex(hs2.transcript_hash_hex));
      rx2.recvDir = C2S; rx2.recvSeq = step.start_sequence;
      const rt = await rx2.recvMessage();
      check('client_stream_roundtrip:' + st.name, hex(rt) === step.message_hex);
    }
  }

  // 3) Negatives, routed through the browser primitive that ACTUALLY gates
  //    each class — never a reimplementation. Cases whose only gate is inside
  //    performHandshake's envelope parser (needs a live socket + a fresh client
  //    ephemeral, so it can't be fixture-driven) or the hardcoded message cap
  //    are skipped here and covered by the Python suite (test_channel_vectors).
  const skipped = [];
  //  a) cert-chain tampering -> the shipped verifyChain rejects it (wrong root/
  //     org, expiry, noncanonical cert, missing required scope, broken narrowing).
  const CHAIN_ERR = new Set(['wrong_root', 'wrong_org', 'invalid_time', 'noncanonical_cert',
    'invalid_scope', 'invalid_narrowing']);
  //  b) signature/eph/token tampering -> the cert is valid but the Ed25519
  //     signed-payload check (the exact composition performHandshake runs at the
  //     sig step: canonicalJson + WebCrypto Ed25519) fails.
  //  c) envelope parse (version/fields/encoding/ephemeral) -> performHandshake-only.
  const ENVELOPE_ERR = new Set(['unsupported_version', 'invalid_fields',
    'invalid_encoding', 'invalid_ephemeral']);
  for (const nc of V.negative_cases) {
    const inp = nc.input;
    if (nc.stage === 'hello') {
      if (CHAIN_ERR.has(nc.expected_error)) {
        let refused = false;
        try {
          const obj = serverHelloObj(inp.wire_utf8_hex);
          await A.verifyChain(obj.cert, inp.root_public_hex, inp.org, 'tunnel:serve', inp.now);
        } catch (e) { refused = true; }
        check('neg_hello_chain:' + nc.name, refused);
      } else if (nc.expected_error === 'invalid_signature') {
        // cert itself is valid; only the signed payload is tampered.
        let refused = false;
        try {
          const obj = serverHelloObj(inp.wire_utf8_hex);
          await A.verifyChain(obj.cert, inp.root_public_hex, inp.org, 'tunnel:serve', inp.now);
          const childPub = JSON.parse(obj.cert).child_pub;
          const sp = signedPayload(inp.org, inp.token, inp.client_public_hex, obj.eph_pub);
          if (!(await ed25519Verify(childPub, obj.sig, sp))) refused = true;
        } catch (e) { refused = true; }
        check('neg_hello_sig:' + nc.name, refused);
      } else if (ENVELOPE_ERR.has(nc.expected_error)) {
        skipped.push(nc.name);   // performHandshake-only; Python covers it.
      } else {
        check('neg_hello_unclassified:' + nc.name, false);  // fail loudly on a new category
      }
    } else if (nc.stage === 'record_open') {
      if (nc.expected_error === 'message_too_large') {
        skipped.push(nc.name);   // cap is a build constant; recvMessageStream size guard, Python covers it.
        continue;
      }
      // Read every record with the SHIPPED SecureChannel.recvRecord so replays /
      // reorders (which need >1 record) are actually exercised.
      let refused = false;
      try {
        const dir = inp.recv_direction === 'c2s' ? te.encode('c2s\x00') : te.encode('s2c\x00');
        const ch = new A.SecureChannel(fakeWs(inp.records_hex), await importAes('00'.repeat(32)),
          await importAes(inp.recv_key_hex), unhex(inp.transcript_hash_hex));
        ch.recvDir = dir;
        ch.recvSeq = inp.start_sequence;
        for (const _ of inp.records_hex) await ch.recvRecord();
      } catch (e) { refused = true; }
      check('neg_record_open:' + nc.name, refused);
    } else {
      skipped.push(nc.name);   // record_seal: sender-side, Python covers it.
    }
  }

  const failed = results.filter(r => !r.ok);
  process.stdout.write(JSON.stringify({ total: results.length, failed, skipped }));
})().catch(e => { process.stdout.write(JSON.stringify({ fatal: String(e && e.stack || e) })); });
"""


def test_browser_reproduces_and_refuses_channel_fixture(tmp_path):
    script = tmp_path / "validate.js"
    script.write_text(_JS, encoding="utf-8")
    out = subprocess.run(
        ["node", str(script), str(AUTONET), str(FIXTURE)],
        capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr[-3000:]
    report = json.loads(out.stdout)
    assert "fatal" not in report, report.get("fatal")
    assert report["failed"] == [], report["failed"]
    assert report["total"] > 20  # sanity: the checks actually ran
