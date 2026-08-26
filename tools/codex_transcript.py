"""Codex transcript error types.

This module used to be the authoritative Codex transcript-version lookup
(cached ``codex_cli_version`` reads of the leading ``session_meta`` and the
``codex_uses_response_item_chat`` gate that arbitrated between the two chat
record shapes).  Chat parsing is version-free now — ``response_item.message``
is the single chat source in every measured codex version — so the version
machinery is gone.  The error types remain because transcript consumers
still catch them at their boundaries (as ``TranscriptParseContextError`` /
``MissingCodexVersionError`` aliases).
"""

from __future__ import annotations


class TranscriptVersionError(RuntimeError):
    """A transcript cannot be parsed faithfully without its format version."""


class CodexTranscriptVersionError(TranscriptVersionError):
    """The authoritative Codex CLI version is missing or malformed."""
