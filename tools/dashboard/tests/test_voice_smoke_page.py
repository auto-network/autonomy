"""Contract tests for the standalone ``/_admin/voice-smoke`` canary page.

This page is intentionally outside the Alpine SPA shell. The tests pin:

1. feature-flag gating via ``voice.pipe_enabled``
2. standalone page scaffolding and no-store headers
3. the operator-facing browser contract: active-session selector,
   AudioWorklet capture, and ``/ws/voice`` wiring
"""

from __future__ import annotations


def test_voice_smoke_page_is_flag_gated(test_client, monkeypatch):
    from tools.dashboard import feature_flags

    monkeypatch.setattr(
        feature_flags,
        "is_enabled",
        lambda name, **kwargs: False,
    )

    resp = test_client.get("/_admin/voice-smoke")
    assert resp.status_code == 403
    assert "voice.pipe_enabled is disabled" in resp.text
    assert "no-store" in resp.headers.get("cache-control", "")


def test_voice_smoke_page_renders_standalone_contract(test_client, monkeypatch):
    from tools.dashboard import feature_flags

    monkeypatch.setattr(
        feature_flags,
        "is_enabled",
        lambda name, **kwargs: True,
    )

    resp = test_client.get("/_admin/voice-smoke")
    assert resp.status_code == 200
    html = resp.text

    assert 'data-testid="voice-smoke-root"' in html
    assert "Voice pipe canary" in html
    assert "Hold to talk" in html
    assert "Grant Mic" in html
    assert "Commit" in html
    assert "Discard" in html
    assert "Reset" in html
    assert "no-store" in resp.headers.get("cache-control", "")

    # Standalone browser contract: vanilla JS, not the Alpine shell.
    assert "AudioWorkletNode" in html
    assert "navigator.mediaDevices.getUserMedia" in html
    assert "/static/js/lib/voice-smoke-worklet.js?v=" in html
    assert "/api/dao/active_sessions" in html
    assert "/ws/voice?bind=" in html


def test_voice_smoke_page_exposes_operator_controls_and_readouts(test_client, monkeypatch):
    from tools.dashboard import feature_flags

    monkeypatch.setattr(
        feature_flags,
        "is_enabled",
        lambda name, **kwargs: True,
    )

    html = test_client.get("/_admin/voice-smoke").text

    for testid in (
        "voice-session-select",
        "voice-manual-bind",
        "voice-bind-button",
        "voice-grant-button",
        "voice-ptt-button",
        "voice-commit-button",
        "voice-discard-button",
        "voice-reset-button",
        "voice-log",
        "voice-buffer-panel",
    ):
        assert f'data-testid="{testid}"' in html, f"missing {testid}"

    assert "Committed buffer" in html
    assert "Live partial" in html
    assert "Event log" in html
