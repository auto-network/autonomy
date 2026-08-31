#!/usr/bin/env python3
"""Dependency-free HTTP/WebSocket canary for the local Caddy gateway proof."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class CanaryHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log_path: Path
    log_lock = threading.Lock()

    def _body(self) -> bytes:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = 0
        return self.rfile.read(size) if size > 0 else b""

    def _record(self, body: bytes = b"") -> None:
        row = {
            "method": self.command,
            "path": self.path,
            "body": body.decode("utf-8", errors="replace"),
            "content_type": self.headers.get("Content-Type", ""),
        }
        with self.log_lock:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/ws" and self.headers.get("Upgrade", "").lower() == "websocket":
            self._websocket()
            return
        self._record()
        if self.path == "/":
            self._send(
                200,
                (
                    b'<!doctype html><link rel="stylesheet" href="/assets/site.css">'
                    b'<form method="post" action="/form"><input name="value">'
                    b'<button>send</button></form><a href="/redirect">redirect</a>'
                ),
                "text/html; charset=utf-8",
            )
        elif self.path == "/assets/site.css":
            self._send(200, b"body{color:rgb(1,2,3)}\n", "text/css")
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/final")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/final":
            self._send(200, b"redirect-complete\n", "text/plain")
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk in (b"stream-one\n", b"stream-two\n", b"stream-three\n"):
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        else:
            self._send(404, b"not found\n", "text/plain")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        body = self._body()
        self._record(body)
        self._send(
            200,
            json.dumps(
                {"method": "POST", "path": self.path, "body": body.decode()}
            ).encode(),
            "application/json",
        )

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        body = self._body()
        self._record(body)
        self._send(
            200,
            json.dumps(
                {"method": "PUT", "path": self.path, "body": body.decode()}
            ).encode(),
            "application/json",
        )

    def _websocket(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            self._send(400, b"missing key\n", "text/plain")
            return
        self._record()
        accept = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode()).digest()
        ).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.wfile.flush()

        header = self.rfile.read(2)
        if len(header) != 2:
            return
        opcode = header[0] & 0x0F
        length = header[1] & 0x7F
        masked = bool(header[1] & 0x80)
        if length == 126:
            length = int.from_bytes(self.rfile.read(2), "big")
        elif length == 127:
            length = int.from_bytes(self.rfile.read(8), "big")
        mask = self.rfile.read(4) if masked else b""
        payload = self.rfile.read(length)
        if masked:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        if opcode == 1 and len(payload) <= 125:
            self.wfile.write(bytes((0x81, len(payload))) + payload)
            self.wfile.flush()

    def log_message(self, format: str, *args) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log", default="/tmp/service-gateway-canary.jsonl")
    args = parser.parse_args()
    CanaryHandler.log_path = Path(args.log)
    CanaryHandler.log_path.unlink(missing_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", args.port), CanaryHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
