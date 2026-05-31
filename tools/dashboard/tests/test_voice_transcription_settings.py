"""Read-helper tests for ``dashboard.voice.transcription``.

Cover :func:`resolve_transcription_config`'s field-level fallback to the
``voice_whisperlive`` module defaults: empty set, explicit override,
malformed/wrong-type values, and the never-raises read-error path. The
graph read is monkeypatched so these stay pure unit tests (no DB).
"""

from types import SimpleNamespace

import pytest

from tools.dashboard import voice_transcription_settings as vts
from tools.dashboard import voice_whisperlive as vw


def _members(payload):
    """Build a fake SetMembers with a single 'default' row."""
    row = SimpleNamespace(key=vts.SINGLETON_KEY, payload=payload)
    return SimpleNamespace(members=[row])


def test_defaults_when_set_is_empty(monkeypatch):
    monkeypatch.setattr(vts.settings_ops, "read_set",
                        lambda *a, **k: SimpleNamespace(members=[]))
    cfg = vts.resolve_transcription_config()
    assert cfg.model == vw.WHISPERLIVE_MODEL
    assert cfg.language == vw.WHISPERLIVE_LANGUAGE
    assert cfg.use_vad == vw.WHISPERLIVE_USE_VAD
    assert cfg.no_speech_thresh == vw.WHISPERLIVE_NO_SPEECH_THRESH
    assert cfg.vad_threshold == vw.WHISPERLIVE_VAD_THRESHOLD


def test_override_is_applied(monkeypatch):
    monkeypatch.setattr(vts.settings_ops, "read_set", lambda *a, **k: _members({
        "model": "small.en",
        "language": "de",
        "use_vad": False,
        "no_speech_thresh": 0.8,
        "vad_threshold": 0.35,
    }))
    cfg = vts.resolve_transcription_config()
    assert cfg.model == "small.en"
    assert cfg.language == "de"
    assert cfg.use_vad is False
    assert cfg.no_speech_thresh == 0.8
    assert cfg.vad_threshold == 0.35


def test_partial_override_keeps_other_defaults(monkeypatch):
    # Only the silence threshold is set; everything else falls back.
    monkeypatch.setattr(vts.settings_ops, "read_set",
                        lambda *a, **k: _members({"no_speech_thresh": 0.9}))
    cfg = vts.resolve_transcription_config()
    assert cfg.no_speech_thresh == 0.9
    assert cfg.model == vw.WHISPERLIVE_MODEL
    assert cfg.vad_threshold == vw.WHISPERLIVE_VAD_THRESHOLD


def test_wrong_types_fall_back(monkeypatch):
    # bool must not satisfy the numeric fields; empty string must not
    # satisfy str fields; a string must not satisfy use_vad.
    monkeypatch.setattr(vts.settings_ops, "read_set", lambda *a, **k: _members({
        "model": "",
        "no_speech_thresh": True,      # bool, not a real number
        "vad_threshold": "high",       # str
        "use_vad": "yes",              # str, not bool
    }))
    cfg = vts.resolve_transcription_config()
    assert cfg.model == vw.WHISPERLIVE_MODEL
    assert cfg.no_speech_thresh == vw.WHISPERLIVE_NO_SPEECH_THRESH
    assert cfg.vad_threshold == vw.WHISPERLIVE_VAD_THRESHOLD
    assert cfg.use_vad == vw.WHISPERLIVE_USE_VAD


def test_int_threshold_is_coerced_to_float(monkeypatch):
    monkeypatch.setattr(vts.settings_ops, "read_set",
                        lambda *a, **k: _members({"no_speech_thresh": 1}))
    cfg = vts.resolve_transcription_config()
    assert cfg.no_speech_thresh == 1.0
    assert isinstance(cfg.no_speech_thresh, float)


def test_read_error_returns_all_defaults(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("graph unavailable")
    monkeypatch.setattr(vts.settings_ops, "read_set", _boom)
    cfg = vts.resolve_transcription_config()
    assert cfg.no_speech_thresh == vw.WHISPERLIVE_NO_SPEECH_THRESH
    assert cfg.model == vw.WHISPERLIVE_MODEL


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
