"""Per-order Unix socket presented to Certbot's manual DNS hooks."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path


class Dns01HookServer:
    """Bind one private socket to one already-authorized ACME order.

    The hook can present or clean only the TXT value Certbot gives it.  Org,
    persona, order identity, record name, and signing authority never cross
    this boundary.
    """

    def __init__(self, client, order: str, path: str | Path):
        self._client = client
        self._order = order
        self._path = Path(path)
        self._server = None

    async def __aenter__(self):
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(self._path),
        )
        os.chmod(self._path, 0o600)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()

    async def _handle(self, reader, writer):
        reply = {"ok": False, "error": "invalid request"}
        try:
            raw = await reader.readline()
            if len(raw) > 2048:
                raise ValueError("request too large")
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict) or set(request) != {"action", "value"}:
                raise ValueError("invalid fields")
            action = request["action"]
            value = request["value"]
            if action not in {"present", "cleanup"} or not isinstance(value, str):
                raise ValueError("invalid operation")
            if action == "present":
                result = await asyncio.to_thread(
                    self._client.present, self._order, value,
                )
                reply = {"ok": True, **result}
            else:
                await asyncio.to_thread(
                    self._client.cleanup, self._order, value,
                )
                reply = {"ok": True}
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        except Exception:
            # The hook needs a useful success bit, not authority or relay
            # diagnostics. Full detail remains in the Dashboard log.
            reply = {"ok": False, "error": "operation failed"}
        try:
            writer.write((json.dumps(reply, separators=(",", ":")) + "\n").encode())
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
