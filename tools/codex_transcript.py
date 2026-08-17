"""Authoritative Codex transcript-version lookup.

Every Codex rollout records its CLI version in the leading ``session_meta``
line.  Consumers never inspect that record themselves: transcript readers call
``codex_cli_version`` and receive a cached, validated version or an explicit
error.  The cache is keyed by file identity so replacing/rolling a transcript
cannot reuse metadata from the prior file at the same path.
"""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path


class TranscriptVersionError(RuntimeError):
    """A transcript cannot be parsed faithfully without its format version."""


class CodexTranscriptVersionError(TranscriptVersionError):
    """The authoritative Codex CLI version is missing or malformed."""


CODEX_RESPONSE_ITEM_CHAT_FROM = (0, 147, 0)


def parse_codex_version(raw: str | None) -> tuple[int, ...]:
    if not raw:
        raise CodexTranscriptVersionError("Codex CLI version is unavailable")
    try:
        version = tuple(int(part) for part in str(raw).split("."))
    except (TypeError, ValueError):
        raise CodexTranscriptVersionError(
            f"Codex CLI version is malformed: {raw!r}"
        ) from None
    if not version:
        raise CodexTranscriptVersionError("Codex CLI version is unavailable")
    return version


@lru_cache(maxsize=4096)
def _read_version(path_text: str, device: int, inode: int) -> str:
    _ = device, inode  # identity-only cache-key fields
    path = Path(path_text)
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            first_line = handle.readline().strip()
        raw = json.loads(first_line) if first_line else None
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexTranscriptVersionError(
            f"Cannot read Codex transcript metadata from {path}: {exc}"
        ) from exc
    payload = raw.get("payload") if isinstance(raw, dict) else None
    version = (
        str(payload.get("cli_version"))
        if raw and raw.get("type") == "session_meta"
        and isinstance(payload, dict) and payload.get("cli_version")
        else None
    )
    parse_codex_version(version)
    return version  # type: ignore[return-value]


def codex_cli_version(path: str | Path) -> str:
    """Return the cached authoritative version for one rollout file."""
    transcript = Path(path)
    try:
        stat = transcript.stat()
    except OSError as exc:
        raise CodexTranscriptVersionError(
            f"Cannot stat Codex transcript {transcript}: {exc}"
        ) from exc
    return _read_version(str(transcript.resolve()), stat.st_dev, stat.st_ino)


def codex_uses_response_item_chat(version: str | None) -> bool:
    """Classify the chat record shape; unknown is always an error."""
    return parse_codex_version(version) >= CODEX_RESPONSE_ITEM_CHAT_FROM
