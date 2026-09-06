"""Run the focused dashboard voice client tests through Agent Test."""

from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]
VOICE_TESTS = [
    "test_system_auth.js",
    "test_voice_store.js",
    "test_voice_shell.js",
    "test_session_viewer_cross_session.js",
    "test_composer_signal.js",
    "test_voice_capture_health.js",
    "test_voice_capture_audio_watchdog.js",
    "test_voice_capture_switch_buffer.js",
]


@pytest.mark.parametrize("filename", VOICE_TESTS)
def test_voice_client_node_suite(filename: str) -> None:
    result = subprocess.run(
        ["node", "--test", str(ROOT / "tools/dashboard/tests" / filename)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
