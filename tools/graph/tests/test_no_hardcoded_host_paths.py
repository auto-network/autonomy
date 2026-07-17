"""Portability guard (bead auto-k0z22, H1).

Pins the invariant that no operator-specific host path is baked into the
``tools/`` and ``agents/`` source trees. Historically ~25 ``/home/jeremy``
references had accumulated — proof that a clean-machine bring-up had never
run. Everything must now derive from the repo root (``Path(__file__)``) plus
documented env vars (see DEPLOY.md), so a fresh clone at any path boots.

The guard scans BOTH source and test files: the forbidden tokens are absolute
host *paths*, never legitimate anywhere. Operator *identity* strings (e.g. a
``participant_id`` of ``jeremy``) are a different concern and are not matched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# tools/graph/tests/ → repo root is three parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Absolute-path forms of the operator's real home, in both slash and Claude
# Code project-slug encodings. These are the tokens that must never appear.
_FORBIDDEN = ("/home/jeremy", "-home-jeremy")

# Scanned trees and the noise we never descend into. ``.browser_profile`` is a
# gitignored Chromium profile full of runtime LevelDB logs; the rest is build
# cruft.
_ROOTS = ("tools", "agents")
_SKIP_DIRS = {".browser_profile", "__pycache__", ".git", "node_modules", ".venv"}

# This guard necessarily contains the forbidden substrings itself.
_SELF = Path(__file__).resolve()


def _iter_text_files():
    for root_name in _ROOTS:
        root = _REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if path.resolve() == _SELF:
                continue
            yield path


def test_no_hardcoded_operator_host_paths():
    offenders: list[str] = []
    for path in _iter_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable — not source we can host-pin
        for lineno, line in enumerate(text.splitlines(), 1):
            for token in _FORBIDDEN:
                if token in line:
                    rel = path.relative_to(_REPO_ROOT)
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Hardcoded operator host paths found — derive from the repo root / env "
        "instead (see DEPLOY.md):\n" + "\n".join(offenders)
    )


if __name__ == "__main__":  # pragma: no cover - manual invocation
    raise SystemExit(pytest.main([__file__, "-q"]))
