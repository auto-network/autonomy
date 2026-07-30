"""Real-Chrome OPFS + IndexedDB resume acceptance for auto-2cmzd."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


AUTONET_JS = Path(__file__).resolve().parents[1] / "bootloader" / "autonet.js"

pytestmark = pytest.mark.skipif(
    shutil.which("agent-browser") is None,
    reason="agent-browser (headless Chrome) not on PATH",
)


def _eval(session: str, javascript: str):
    result = subprocess.run(
        ["agent-browser", "--session", session, "eval", "--stdin"],
        input=javascript,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    parsed = json.loads(result.stdout.strip())
    return json.loads(parsed) if isinstance(parsed, str) else parsed


@contextmanager
def _origin(tmp_path):
    source = AUTONET_JS.read_text(encoding="utf-8")
    source = re.sub(
        r"window\.autonet = autonet;[\s\S]*$",
        "window.A = autonet;",
        source,
    )
    (tmp_path / "index.html").write_text(
        "<!doctype html><meta charset=utf-8><script>"
        + source
        + "</script>",
        encoding="utf-8",
    )

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        lambda *args, **kwargs: QuietHandler(
            *args, directory=str(tmp_path), **kwargs
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


_HARNESS = r"""
const CHUNK = 1024 * 1024;
const WINDOW = 8 * CHUNK;
const TOTAL = WINDOW + CHUNK + 17;
const ENTRY = {
  ref: "browser-opfs-ref", name: "large.bin",
  mime: "application/octet-stream", raw_sha256: "a".repeat(64),
  total_size: TOTAL, oversize: false,
};
const CURSOR = "browser-opfs-cursor";
const NOTE = "browser-opfs-note";
const SINK = "browser-opfs-resume.part";

function frame(offset, flags, bytes) {
  const out = new Uint8Array(9 + bytes.length);
  new DataView(out.buffer).setBigUint64(0, BigInt(offset));
  out[8] = flags;
  out.set(bytes, 9);
  return out;
}
function bytesAt(offset, length) {
  const out = new Uint8Array(length);
  for (let i = 0; i < length; i++) out[i] = (offset + i) % 251;
  return out;
}
function windowMessages(start, limit) {
  return (async function* () {
    for (let offset = start; offset < limit;) {
      const end = Math.min(offset + CHUNK, limit);
      const flags = (end === limit ? 1 : 0) | (end === TOTAL ? 2 : 0);
      yield frame(offset, flags, bytesAt(offset, end - offset));
      offset = end;
    }
  })();
}
"""


def test_opfs_cursor_survives_reload_and_fetches_only_suffix(tmp_path):
    session = "attachment-parent-opfs"
    with _origin(tmp_path) as url:
        subprocess.run(
            ["agent-browser", "--session", session, "open", url],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        first = _eval(
            session,
            _HARNESS
            + r"""
(async () => {
  const cursors = new A.BrowserCursorStore();
  const competing = new A.BrowserCursorStore();
  const firstLock = await cursors.acquire("cross-tab-lock-test");
  const refusedLock = await competing.acquire("cross-tab-lock-test");
  cursors.release("cross-tab-lock-test");
  await new Promise((resolve) => setTimeout(resolve, 0));
  const lockAfterRelease = await competing.acquire("cross-tab-lock-test");
  competing.release("cross-tab-lock-test");
  const sink = await A.OpfsAttachmentSink.open(SINK);
  await sink.truncate(0);
  await cursors.delete(CURSOR + ":" + ENTRY.ref);
  const requests = [];
  const downloader = new A.AttachmentDownloader({
    entry: ENTRY, cursorId: CURSOR, noteId: NOTE, sink, cursors,
    fetchWindow: async (request) => {
      requests.push({...request});
      return (async function* () {
        yield frame(0, 0, bytesAt(0, CHUNK));
        throw new Error("forced browser reload");
      })();
    },
  });
  let disconnected = false;
  try { await downloader.run(); }
  catch (err) { disconnected = /forced browser reload/.test(String(err.message)); }
  const record = await cursors.get(downloader.cursorKey);
  return JSON.stringify({
    disconnected, requests, size: await sink.size(),
    committed: record.committed_offset,
    locks: [firstLock, refusedLock, lockAfterRelease],
  });
})();
""",
        )
        assert first["disconnected"] is True
        assert first["size"] == 1024 * 1024
        assert first["committed"] == 1024 * 1024
        assert first["locks"] == [True, False, True]
        assert [request["offset"] for request in first["requests"]] == [0]

        # A second navigation destroys every in-memory object.  Only OPFS and
        # the committed IndexedDB transaction can carry the prefix forward.
        subprocess.run(
            ["agent-browser", "--session", session, "open", url],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        second = _eval(
            session,
            _HARNESS
            + r"""
(async () => {
  const cursors = new A.BrowserCursorStore();
  const sink = await A.OpfsAttachmentSink.open(SINK);
  const requests = [];
  let inFlight = 0;
  let maxInFlight = 0;
  const downloader = new A.AttachmentDownloader({
    entry: ENTRY, cursorId: CURSOR, noteId: NOTE, sink, cursors,
    fetchWindow: async (request) => {
      requests.push({...request});
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      const limit = Math.min(request.offset + request.length, TOTAL);
      return (async function* () {
        try {
          yield* windowMessages(request.offset, limit);
        } finally {
          inFlight -= 1;
        }
      })();
    },
  });
  const result = await downloader.run();
  const file = await sink.file();
  const bytes = new Uint8Array(await file.arrayBuffer());
  let bytesMatch = bytes.length === TOTAL;
  for (let at = 0; bytesMatch && at < bytes.length; at++) {
    bytesMatch = bytes[at] === at % 251;
  }
  await cursors.delete(downloader.cursorKey);
  await sink.remove();
  return JSON.stringify({
    result, requestOffsets: requests.map((request) => request.offset),
    requestLengths: requests.map((request) => request.length),
    maxInFlight, size: bytes.length, bytesMatch,
  });
})();
""",
        )
        subprocess.run(
            ["agent-browser", "--session", session, "close"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    assert second == {
        "result": {
            "status": "ready",
            "received": 9 * 1024 * 1024 + 17,
        },
        "requestOffsets": [1024 * 1024, 9 * 1024 * 1024],
        "requestLengths": [8 * 1024 * 1024, 8 * 1024 * 1024],
        "maxInFlight": 1,
        "size": 9 * 1024 * 1024 + 17,
        "bytesMatch": True,
    }
