"""Browser-parent attachment driver tests (auto-2cmzd).

The production code is dependency-free browser JavaScript.  These tests load
that exact source in Node with WebCrypto, then attack the state machine,
record-stream adapter, and opaque-child trust boundary with deterministic
in-memory storage.  Real OPFS persistence is covered separately in Chrome.
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

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node not on PATH",
)


def _run_node(scenario: str) -> dict:
    loader = f"""
const webcrypto = require("crypto").webcrypto;
let src = require({json.dumps(str(AUTONET_TEST_SOURCE))}).loadAutonetTestSource();
src = src.replace(/window\\.autonet = autonet;[\\s\\S]*$/, "return autonet;");
src = src.replace(/^const autonet = \\(\\(\\) => \\{{/, "");
const A = new Function(
  "TextEncoder", "TextDecoder", "crypto", src
)(TextEncoder, TextDecoder, webcrypto);
"""
    result = subprocess.run(
        ["node", "-e", loader + scenario],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


_FAKES = r"""
const CHUNK = 1024 * 1024;
const WINDOW = 8 * CHUNK;

function bodyFrame(offset, flags, chunk) {
  const out = new Uint8Array(9 + chunk.length);
  new DataView(out.buffer).setBigUint64(0, BigInt(offset));
  out[8] = flags;
  out.set(chunk, 9);
  return out;
}

class MemorySink {
  constructor(id = "sink") {
    this.sinkId = id;
    this.data = new Uint8Array();
    this.removed = false;
  }
  async size() { return this.data.length; }
  async writeAt(offset, bytes) {
    if (offset !== this.data.length) throw new Error("non-contiguous");
    const next = new Uint8Array(offset + bytes.length);
    next.set(this.data);
    next.set(bytes, offset);
    this.data = next;
  }
  async truncate(size) { this.data = this.data.slice(0, size); }
  async flush() {}
  async file() { return new Blob([this.data]); }
  async remove() { this.removed = true; }
}

class MemoryCursors {
  constructor() { this.records = new Map(); this.active = new Set(); }
  async get(key) { return this.records.has(key) ? structuredClone(this.records.get(key)) : null; }
  async put(key, value) { this.records.set(key, structuredClone(value)); }
  async delete(key) { this.records.delete(key); }
  acquire(key) {
    if (this.active.has(key)) return false;
    this.active.add(key);
    return true;
  }
  release(key) { this.active.delete(key); }
}

function chunks(start, limit, total) {
  return (async function* () {
    for (let offset = start; offset < limit || (total === 0 && offset === 0);) {
      const end = total === 0 ? 0 : Math.min(offset + CHUNK, limit);
      const bytes = new Uint8Array(end - offset);
      for (let i = 0; i < bytes.length; i++) bytes[i] = (offset + i) % 251;
      const flags = (end === limit ? 1 : 0) | (end === total ? 2 : 0);
      yield bodyFrame(offset, flags, bytes);
      if (total === 0) return;
      offset = end;
    }
  })();
}

function entry(total) {
  return {
    ref: "attachment-ref", name: "report.bin",
    mime: "application/octet-stream", raw_sha256: "a".repeat(64),
    total_size: total, oversize: false,
  };
}
"""


def test_streamed_secure_channel_accepts_message_boundaries_and_final():
    scenario = r"""
(async () => {
  const te = new TextEncoder();
  const keyBytes = new Uint8Array(32).fill(7);
  const key = await webcrypto.subtle.importKey(
    "raw", keyBytes, "AES-GCM", false, ["encrypt", "decrypt"]);
  const transcript = new Uint8Array(32).fill(11);
  const direction = te.encode("s2c\0");
  function concat(...parts) {
    const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
    let at = 0;
    for (const part of parts) { out.set(part, at); at += part.length; }
    return out;
  }
  async function record(sequence, flags, text) {
    const seq = new Uint8Array(8);
    new DataView(seq.buffer).setBigUint64(0, BigInt(sequence));
    const plaintext = concat(new Uint8Array([flags]), te.encode(text));
    const ciphertext = new Uint8Array(await webcrypto.subtle.encrypt({
      name: "AES-GCM", iv: concat(direction, seq),
      additionalData: concat(transcript, direction, seq),
    }, key, plaintext));
    return concat(seq, ciphertext);
  }
  const records = [
    await record(0, 2, "first"),
    await record(1, 3, "second"),
  ];
  const ws = {recvBinary: async () => records.shift(), send() {}, close() {}};
  const channel = new A.SecureChannel(ws, key, key, transcript);
  const messages = [];
  for await (const message of channel.recvMessageStream()) {
    messages.push(new TextDecoder().decode(message));
  }

  const bad = await record(0, 4, "bad");
  let closed = false;
  const badChannel = new A.SecureChannel(
    {recvBinary: async () => bad, send() {}, close() { closed = true; }},
    key, key, transcript);
  let rejected = false;
  try {
    for await (const _message of badChannel.recvMessageStream()) {}
  } catch (err) {
    rejected = /unknown flags/.test(String(err.message));
  }
  process.stdout.write(JSON.stringify({messages, rejected, closed}));
})();
"""
    assert _run_node(scenario) == {
        "messages": ["first", "second"],
        "rejected": True,
        "closed": True,
    }


def test_resume_fetches_only_durable_suffix_and_identity_mismatch_restarts():
    scenario = _FAKES + r"""
(async () => {
  const total = WINDOW + CHUNK + 17;
  const sink = new MemorySink();
  const cursors = new MemoryCursors();
  const requests = [];
  let disconnect = true;
  const fetchWindow = async (request) => {
    requests.push({...request});
    const limit = Math.min(request.offset + request.length, total);
    if (!disconnect) return chunks(request.offset, limit, total);
    disconnect = false;
    return (async function* () {
      let seen = 0;
      for await (const message of chunks(request.offset, limit, total)) {
        if (seen++ === 2) throw new Error("forced disconnect");
        yield message;
      }
    })();
  };
  const args = {
    entry: entry(total), cursorId: "cursor", noteId: "note",
    sink, cursors, fetchWindow,
  };
  let disconnected = false;
  try { await new A.AttachmentDownloader(args).run(); }
  catch (err) { disconnected = /forced disconnect/.test(String(err.message)); }
  const afterDrop = sink.data.length;
  const result = await new A.AttachmentDownloader(args).run();
  let bytesMatch = sink.data.length === total;
  for (const at of [0, CHUNK - 1, 2 * CHUNK, WINDOW, total - 1]) {
    bytesMatch &&= sink.data[at] === at % 251;
  }

  const mismatchSink = new MemorySink("mismatch");
  mismatchSink.data = new Uint8Array(CHUNK).fill(99);
  const mismatchCursors = new MemoryCursors();
  const mismatchRequests = [];
  const mismatch = new A.AttachmentDownloader({
    entry: entry(CHUNK + 3), cursorId: "cursor", noteId: "note",
    sink: mismatchSink, cursors: mismatchCursors,
    fetchWindow: async (request) => {
      mismatchRequests.push({...request});
      return chunks(request.offset, CHUNK + 3, CHUNK + 3);
    },
  });
  const stale = mismatch._record(CHUNK);
  stale.raw_sha256 = "b".repeat(64);
  await mismatchCursors.put(mismatch.cursorKey, stale);
  await mismatch.run();

  process.stdout.write(JSON.stringify({
    disconnected, afterDrop, requestOffsets: requests.map(r => r.offset),
    result, bytesMatch, mismatchFirstOffset: mismatchRequests[0].offset,
  }));
})();
"""
    assert _run_node(scenario) == {
        "disconnected": True,
        "afterDrop": 2 * 1024 * 1024,
        "requestOffsets": [0, 2 * 1024 * 1024],
        "result": {
            "status": "ready",
            "received": 9 * 1024 * 1024 + 17,
        },
        "bytesMatch": True,
        "mismatchFirstOffset": 0,
    }


def test_malformed_frame_rolls_back_to_last_durable_chunk():
    scenario = _FAKES + r"""
(async () => {
  const total = 3 * CHUNK;
  const sink = new MemorySink();
  const cursors = new MemoryCursors();
  const downloader = new A.AttachmentDownloader({
    entry: entry(total), cursorId: "cursor", noteId: "note",
    sink, cursors,
    fetchWindow: async () => (async function* () {
      yield bodyFrame(0, 0, new Uint8Array(CHUNK));
      yield bodyFrame(CHUNK + 1, 0, new Uint8Array(CHUNK));
    })(),
  });
  let code = null;
  try { await downloader.run(); }
  catch (err) { code = err.code; }
  const record = await cursors.get(downloader.cursorKey);

  const revokedSink = new MemorySink("revoked");
  const revokedCursors = new MemoryCursors();
  const revoked = new A.AttachmentDownloader({
    entry: entry(total), cursorId: "cursor", noteId: "note",
    sink: revokedSink, cursors: revokedCursors,
    fetchWindow: async () => (async function* () {
      yield bodyFrame(0, 0, new Uint8Array(CHUNK));
      yield new TextEncoder().encode(
        '{"code":"not_authorized","op":"error","ref":"attachment-ref","v":1}');
    })(),
  });
  let revokedCode = null;
  try { await revoked.run(); }
  catch (err) { revokedCode = err.code; }
  const revokedRecord = await revokedCursors.get(revoked.cursorKey);
  process.stdout.write(JSON.stringify({
    code, size: sink.data.length, committed: record.committed_offset,
    revokedCode, revokedSize: revokedSink.data.length,
    revokedCommitted: revokedRecord.committed_offset,
  }));
})();
"""
    assert _run_node(scenario) == {
        "code": "invalid_message",
        "size": 1024 * 1024,
        "committed": 1024 * 1024,
        "revokedCode": "not_authorized",
        "revokedSize": 1024 * 1024,
        "revokedCommitted": 1024 * 1024,
    }


def test_parent_bridge_rejects_forged_intents_and_exposes_only_state():
    scenario = _FAKES + r"""
(async () => {
  const sent = [];
  const child = {postMessage: (message) => sent.push(message)};
  const frame = {contentWindow: child};
  const listeners = new Map();
  const host = {
    addEventListener: (name, fn) => listeners.set(name, fn),
    removeEventListener: (name, fn) => {
      if (listeners.get(name) === fn) listeners.delete(name);
    },
    showSaveFilePicker: async () => { throw new Error("not invoked"); },
    setTimeout: () => {},
    URL: {createObjectURL: () => "blob:test", revokeObjectURL() {}},
  };
  const sink = new MemorySink();
  const requests = [];
  const channel = {
    async sendMessage(bytes) {
      requests.push(JSON.parse(new TextDecoder().decode(bytes)));
    },
    recvMessageStream() { return chunks(0, 4, 4); },
  };
  const controller = new A.AttachmentController({
    frame, channel, token: "secret-link-token", noteId: "note",
    manifest: [entry(4)], cursors: new MemoryCursors(),
    sinkFactory: async () => sink, hostWindow: host,
    documentObject: {},
  });
  const valid = {v: 1, type: "attachment.select", ref: "attachment-ref"};
  await controller.handleEvent({source: {}, data: valid});
  await controller.handleEvent({
    source: child, data: {...valid, path: "/tmp/evil"},
  });
  await controller.handleEvent({
    source: child, data: {...valid, ref: "unknown"},
  });
  const beforeValid = {states: sent.length, requests: requests.length};
  await controller.handleEvent({source: child, data: valid});
  const forbidden = sent.some((message) =>
    ["token", "path", "offset", "name", "filename", "destination", "bytes"]
      .some((key) => Object.prototype.hasOwnProperty.call(message, key)));
  const progressValid = sent.every((message) =>
    !("received" in message) ||
    (Number.isSafeInteger(message.received)
      && Number.isSafeInteger(message.total)
      && message.received <= message.total));
  controller.dispose();
  process.stdout.write(JSON.stringify({
    beforeValid, requests, states: sent, forbidden, progressValid,
    listenerRemoved: !listeners.has("message"),
  }));
})();
"""
    result = _run_node(scenario)
    assert result["beforeValid"] == {"states": 0, "requests": 0}
    assert result["requests"] == [{
        "length": 8 * 1024 * 1024,
        "offset": 0,
        "op": "attachment.fetch",
        "ref": "attachment-ref",
        "v": 1,
    }]
    assert [state["state"] for state in result["states"]] == [
        "queued", "downloading", "downloading", "ready",
    ]
    assert result["forbidden"] is False
    assert result["progressValid"] is True
    assert result["listenerRemoved"] is True


def test_export_requires_parent_gesture_and_streams_after_picker_choice():
    scenario = _FAKES + r"""
(async () => {
  const sent = [];
  const child = {postMessage: (message) => sent.push(message)};
  const listeners = new Map();
  const buttonListeners = new Map();
  const button = {
    hidden: true, disabled: true, textContent: "", focused: false,
    addEventListener: (name, fn) => buttonListeners.set(name, fn),
    removeEventListener: (name, fn) => {
      if (buttonListeners.get(name) === fn) buttonListeners.delete(name);
    },
    focus() { this.focused = true; },
  };
  let settleDestination;
  let pickerCalls = 0;
  let fileCalls = 0;
  let suggestedName = null;
  const saved = [];
  const destination = {
    createWritable: async () => new WritableStream({
      write(chunk) { saved.push(...chunk); },
    }),
  };
  const host = {
    addEventListener: (name, fn) => listeners.set(name, fn),
    removeEventListener: () => {},
    showSaveFilePicker(options) {
      pickerCalls += 1;
      suggestedName = options.suggestedName;
      return new Promise((resolve, reject) => {
        settleDestination = (accept) => accept
          ? resolve(destination) : reject(new Error("picker cancelled"));
      });
    },
    setTimeout: () => {},
    URL: {createObjectURL: () => "blob:test", revokeObjectURL() {}},
  };
  const cursors = new MemoryCursors();
  await cursors.put("cursor:key", {kept: true});
  const sink = new MemorySink();
  sink.data = new Uint8Array([1, 2, 3, 4]);
  sink.file = async () => {
    fileCalls += 1;
    return new Blob([sink.data]);
  };
  const controller = new A.AttachmentController({
    frame: {contentWindow: child}, channel: {}, token: "secret",
    noteId: "note", manifest: [entry(4)], exportButton: button,
    cursors, sinkFactory: async () => sink, hostWindow: host,
    documentObject: {},
  });
  const item = {
    entry: {...entry(4), name: "../../unsafe:name.bin"},
    sink, downloader: {cursorKey: "cursor:key"}, objectUrl: null,
  };
  controller.ready.set(item.entry.ref, item);
  controller._selectExport(item.entry.ref);

  // The opaque child's intent may focus the parent control, but cannot open
  // a picker because postMessage carries no transient user activation.
  await controller.handleEvent({
    source: child,
    data: {v: 1, type: "attachment.export", ref: item.entry.ref},
  });
  const afterIntent = {pickerCalls, focused: button.focused};

  // Model the subsequent real click. showSaveFilePicker must be the first
  // awaited operation: staging is not read until the destination resolves.
  const exporting = controller.exportSelected();
  const beforeChoice = {pickerCalls, fileCalls};
  settleDestination(false);
  await exporting;
  const afterCancel = {
    ready: controller.ready.has(item.entry.ref),
    cursor: await cursors.get("cursor:key"),
    fileCalls,
  };
  const retry = controller.exportSelected();
  const beforeRetryChoice = {pickerCalls, fileCalls};
  settleDestination(true);
  await retry;
  process.stdout.write(JSON.stringify({
    afterIntent, beforeChoice, afterCancel, beforeRetryChoice,
    fileCalls, suggestedName, saved,
    states: sent.map((message) => message.state),
    readyAfter: controller.ready.has(item.entry.ref),
    cursorAfter: await cursors.get("cursor:key"),
  }));
})();
"""
    assert _run_node(scenario) == {
        "afterIntent": {"pickerCalls": 0, "focused": True},
        "beforeChoice": {"pickerCalls": 1, "fileCalls": 0},
        "afterCancel": {
            "ready": True,
            "cursor": {"kept": True},
            "fileCalls": 0,
        },
        "beforeRetryChoice": {"pickerCalls": 2, "fileCalls": 0},
        "fileCalls": 1,
        "suggestedName": "_.._unsafe_name.bin",
        "saved": [1, 2, 3, 4],
        "states": ["exporting", "ready", "exporting", "complete"],
        "readyAfter": False,
        "cursorAfter": None,
    }


def test_export_anchor_fallback_uses_parent_sanitized_name():
    scenario = _FAKES + r"""
(async () => {
  const sent = [];
  const child = {postMessage: (message) => sent.push(message)};
  const clicked = [];
  const body = {appendChild() {}};
  const documentObject = {
    body,
    createElement(tag) {
      if (tag !== "a") throw new Error("unexpected element");
      return {
        href: "", download: "", hidden: false,
        click() { clicked.push({href: this.href, download: this.download}); },
        remove() {},
      };
    },
  };
  const host = {
    addEventListener() {}, removeEventListener() {}, setTimeout() {},
    URL: {createObjectURL: () => "blob:staged-file", revokeObjectURL() {}},
  };
  const cursors = new MemoryCursors();
  const sink = new MemorySink();
  const controller = new A.AttachmentController({
    frame: {contentWindow: child}, channel: {}, token: "secret",
    noteId: "note", manifest: [entry(4)], cursors,
    sinkFactory: async () => sink, hostWindow: host, documentObject,
  });
  const item = {
    entry: {...entry(4), name: "../fallback?.txt"},
    sink, downloader: {cursorKey: "cursor:key"},
    objectUrl: "blob:staged-file",
  };
  controller.ready.set(item.entry.ref, item);
  controller.exportCandidate = item.entry.ref;
  await controller.exportSelected();
  process.stdout.write(JSON.stringify({
    clicked, states: sent.map((message) => message.state),
  }));
})();
"""
    assert _run_node(scenario) == {
        "clicked": [{
            "href": "blob:staged-file",
            "download": "_fallback_.txt",
        }],
        "states": ["exporting", "complete"],
    }


def test_cursor_hmac_and_parent_filename_sanitization_are_frozen():
    scenario = r"""
(async () => {
  const cursor = await A.attachmentCursorId("x".repeat(48));
  const names = [
    A.safeAttachmentName("../../evil:name?.txt"),
    A.safeAttachmentName(".."),
    A.safeAttachmentName("ok.pdf"),
  ];
  process.stdout.write(JSON.stringify({cursor, names}));
})();
"""
    assert _run_node(scenario) == {
        "cursor": "6958209cd709171caa477b57946d44da0f887992071a161669750ab4ebeb034e",
        "names": ["_.._evil_name_.txt", "attachment", "ok.pdf"],
    }
