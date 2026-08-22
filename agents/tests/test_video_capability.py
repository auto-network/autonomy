"""Functional checks for the mounted video command surface."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path


TOOLS = Path("agents/capabilities/video/tools")


def _fake_package(tmp_path: Path) -> Path:
    package = tmp_path / "video"
    shutil.copytree(TOOLS, package / "tools")
    binary_dir = package / "bin"
    binary_dir.mkdir()
    for name in ("ffmpeg", "ffprobe"):
        binary = binary_dir / name
        binary.write_text(f'#!/bin/sh\nprintf "%s\\n" "{name}:$*"\n')
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return package


def test_raw_ffmpeg_and_ffprobe_commands_delegate_to_pinned_package(tmp_path):
    package = _fake_package(tmp_path)

    for name in ("ffmpeg", "ffprobe"):
        result = subprocess.run(
            [package / "tools" / name, "-version"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == f"{name}:-version"


def test_video_probe_resolves_binary_from_package_root(tmp_path):
    package = _fake_package(tmp_path)

    result = subprocess.run(
        [package / "tools" / "video-probe", "recording.webm"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == (
        "ffprobe:-v error -print_format json -show_format -show_streams "
        "recording.webm"
    )
