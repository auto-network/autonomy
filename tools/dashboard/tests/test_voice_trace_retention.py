"""Hard bounds for explicitly enabled client voice traces."""

from __future__ import annotations

import json
import os
import time

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import server


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(server, "_REPO_ROOT", tmp_path)
    app = Starlette(routes=[
        Route("/api/voice/trace", server.api_voice_trace, methods=["POST"]),
    ])
    return TestClient(app)


def _payload(frames: int = 1) -> dict:
    return {
        "reason": "clear+window",
        "bind": "auto-test",
        "ua": "trace-test",
        "frames": [{"t": n, "kind": "reset"} for n in range(frames)],
    }


def test_trace_upload_persists_the_valid_bounded_payload(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/voice/trace", json=_payload())

    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is True
    assert result["frames"] == 1
    traces = list((tmp_path / "data" / "voice-traces").glob("voice-trace-*.json"))
    assert len(traces) == 1
    assert json.loads(traces[0].read_text()) == _payload()


def test_trace_upload_rejects_oversized_body_and_frame_count(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "VOICE_TRACE_MAX_BODY_BYTES", 128)
    with _client(tmp_path, monkeypatch) as client:
        oversized = client.post(
            "/api/voice/trace",
            content=b"x" * 129,
            headers={"Content-Type": "application/json"},
        )
        monkeypatch.setattr(server, "VOICE_TRACE_MAX_BODY_BYTES", 2 * 1024 * 1024)
        too_many_frames = client.post(
            "/api/voice/trace",
            json=_payload(server.VOICE_TRACE_MAX_FRAMES + 1),
        )

    assert oversized.status_code == 413
    assert too_many_frames.status_code == 413
    assert not (tmp_path / "data" / "voice-traces").exists()


def test_trace_retention_prunes_age_count_and_total_bytes(tmp_path, monkeypatch):
    traces_dir = tmp_path / "data" / "voice-traces"
    traces_dir.mkdir(parents=True)
    now = time.time()
    planted = []
    for index, age in enumerate((120, 40, 30, 20, 10)):
        path = traces_dir / f"voice-trace-planted-{index}.json"
        path.write_bytes(b"x" * 220)
        os.utime(path, (now - age, now - age))
        planted.append(path)
    unrelated = traces_dir / "keep-me.txt"
    unrelated.write_text("not a managed trace")

    monkeypatch.setattr(server, "VOICE_TRACE_MAX_AGE_S", 60)
    monkeypatch.setattr(server, "VOICE_TRACE_MAX_FILES", 3)
    monkeypatch.setattr(server, "VOICE_TRACE_MAX_TOTAL_BYTES", 550)
    with _client(tmp_path, monkeypatch) as client:
        response = client.post("/api/voice/trace", json=_payload())

    assert response.status_code == 200
    managed = list(traces_dir.glob("voice-trace-*.json"))
    assert len(managed) <= 3
    assert sum(path.stat().st_size for path in managed) <= 550
    assert planted[0].exists() is False  # age expiry
    assert unrelated.read_text() == "not a managed trace"
    assert os.path.exists(response.json()["path"])
