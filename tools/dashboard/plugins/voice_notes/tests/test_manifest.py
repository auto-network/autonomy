from pathlib import Path

import yaml

from tools.dashboard.plugin_api.manifest import PluginManifest


PLUGIN_DIR = Path(__file__).resolve().parent.parent


def test_voice_notes_manifest_declares_route_scoped_voice_replacement():
    raw = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
    manifest = PluginManifest.model_validate(raw)

    assert manifest.id == "voice_notes"
    assert manifest.paths == ["/voice-notes"]
    assert manifest.frontend.alpine_root == "voiceNotesPage"
    assert manifest.frontend.voice.live_transcript is True
    assert manifest.frontend.voice.replace_caption is True
    assert manifest.frontend.voice.replace_controls is True
    assert manifest.entrypoints.api.endswith("voice_notes.entrypoints.api:routes")
    assert manifest.entrypoints.schemas == [
        "tools.dashboard.plugins.voice_notes.entrypoints.schemas:VoiceNoteV1",
    ]


def test_plugin_surface_has_one_voice_control_and_no_send_or_keyboard_toggle():
    html = (PLUGIN_DIR / "page.html").read_text()

    assert html.count('data-testid="voice-notes-mic"') == 1
    assert "voice-notes-send" not in html
    assert "keyboard" not in html.lower()
    assert "@click=\"toggleTalking()\"" in html
