"""Development serve-file seam follows the production artifact grammar."""

from __future__ import annotations

import asyncio
import json

import pytest

from tools.network.idkit import canonical_json
from tools.network.relaykit.connector import file_handler


def _call(handler, request: dict) -> tuple[dict, bytes]:
    raw = asyncio.run(handler("ignored", canonical_json(request)))
    header, _, body = raw.partition(b"\n")
    return json.loads(header), body


def test_file_handler_serves_html_as_design_artifact(tmp_path):
    path = tmp_path / "viewer.html"
    path.write_bytes(b"<h1>viewer</h1>")
    handler = file_handler(str(path), "text/html; charset=utf-8")

    header, body = _call(handler, {"v": 1, "op": "fetch"})
    assert header == {
        "v": 1,
        "status": "ok",
        "kind": "design",
        "viewer": {"offset": 0, "length": len(body)},
    }
    assert body == path.read_bytes()
    assert _call(handler, {"v": 1, "op": "head"}) == (
        {"v": 1, "status": "ok", "serialized_size": len(body)},
        b"",
    )


def test_file_handler_rejects_non_html(tmp_path):
    path = tmp_path / "data.bin"
    path.write_bytes(b"data")
    with pytest.raises(ValueError, match="requires text/html"):
        file_handler(str(path), "application/octet-stream")
