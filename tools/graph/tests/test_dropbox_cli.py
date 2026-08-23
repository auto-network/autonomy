"""The graph dropbox CLI uses session auth and materializes safe files."""

from __future__ import annotations

import io
import hashlib
import json
from types import SimpleNamespace

from tools.graph import dropbox_cmd


class _Response:
    def __init__(self, body: bytes, headers: dict | None = None):
        self._body = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, size: int = -1):
        return self._body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_list_prints_newest_metadata(monkeypatch, capsys):
    seen = {}
    body = json.dumps({"items": [{
        "created_at": "2026-08-23T23:00:00Z",
        "id": "a" * 32,
        "content_type": "image/png",
        "size": 2048,
        "original_filename": "iphone.png",
    }]}).encode()

    def fake_request(path, token):
        seen.update(path=path, token=token)
        return _Response(body)

    monkeypatch.setattr(dropbox_cmd, "_request", fake_request)
    dropbox_cmd.cmd_list(SimpleNamespace(limit=3, _resolve_token=lambda: "session-token"))
    output = capsys.readouterr().out
    assert seen == {"path": "/api/dropbox?limit=3", "token": "session-token"}
    assert "image/png" in output and "2.0 KiB" in output and "iphone.png" in output


def test_get_writes_id_prefixed_file_without_overwrite(monkeypatch, tmp_path, capsys):
    full_id = "b" * 32

    def fake_request(path, token):
        assert path == "/api/dropbox/bbbbbbbb"
        assert token == "session-token"
        return _Response(
            b"pixels",
            {
                "X-Autonomy-Dropbox-Id": full_id,
                "X-Autonomy-SHA256": hashlib.sha256(b"pixels").hexdigest(),
                "Content-Disposition": 'attachment; filename="IMG_1234.png"',
            },
        )

    monkeypatch.setattr(dropbox_cmd, "_request", fake_request)
    args = SimpleNamespace(
        id="bbbbbbbb", output_dir=str(tmp_path),
        _resolve_token=lambda: "session-token",
    )
    dropbox_cmd.cmd_get(args)
    first = capsys.readouterr().out.strip()
    assert first.endswith(f"{full_id}-IMG_1234.png")
    assert (tmp_path / f"{full_id}-IMG_1234.png").read_bytes() == b"pixels"

    dropbox_cmd.cmd_get(args)
    second = capsys.readouterr().out.strip()
    assert second.endswith(f"{full_id}-IMG_1234-1.png")
