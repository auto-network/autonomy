#!/usr/bin/env python3
"""Certbot manual auth/cleanup hook; stdlib-only by design."""

from __future__ import annotations

import json
import os
import socket
import sys


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"present", "cleanup"}:
        return 2
    path = os.environ.get("AUTONOMY_ACME_SOCKET", "")
    value = os.environ.get("CERTBOT_VALIDATION", "")
    if not path or not value:
        return 2
    request = json.dumps(
        {"action": sys.argv[1], "value": value}, separators=(",", ":")
    ) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(70)
            sock.connect(path)
            sock.sendall(request.encode("utf-8"))
            data = b""
            while b"\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        reply = json.loads(data.split(b"\n", 1)[0])
    except Exception as exc:
        print(f"autonomy DNS-01 hook unavailable: {type(exc).__name__}: {exc} (socket {path!r}, action {sys.argv[1]})", file=sys.stderr)
        return 1
    if reply.get("ok") is not True:
        print(f"autonomy DNS-01 hook refused: {reply.get('error', 'no reason')} (action {sys.argv[1]})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
