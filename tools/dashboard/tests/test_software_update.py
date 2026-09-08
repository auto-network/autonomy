"""Guards for the one-click software update (follow-origin fast-forward).

The point of these tests is the safety property: ``perform_update`` must NEVER
``reset --hard`` away work that origin does not have. The guard is git history
itself — an author machine is always ahead of origin — so the tests build real
tiny git repos and assert the follower fast-forwards while the author and a
dirty tree are refused.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tools.dashboard import software_update as su


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout


def _identity(cwd: Path) -> None:
    # Local, hermetic identity — and never sign: a global commit.gpgsign=true
    # would otherwise fail these throwaway commits with "gpg failed to sign".
    _git(cwd, "config", "user.email", "t@t")
    _git(cwd, "config", "user.name", "t")
    _git(cwd, "config", "commit.gpgsign", "false")


def _commit(cwd: Path, name: str, text: str) -> None:
    (cwd / name).write_text(text)
    _git(cwd, "add", name)
    _git(cwd, "commit", "-q", "-m", f"add {name}")


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch):
    """A bare 'origin' with 3 commits, plus a checkout the tests reposition."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "-b", "master", str(seed))
    _identity(seed)
    for i in range(3):
        _commit(seed, f"f{i}.txt", str(i))
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))

    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    _identity(checkout)
    # Point the module at this checkout and disable the network fetch (the bare
    # origin is local; a fetch of a local remote is fine, but keep it explicit).
    monkeypatch.setattr(su, "_REPO_ROOT", checkout)
    return {"origin": origin, "checkout": checkout}


def test_follower_fast_forwards(fleet):
    checkout = fleet["checkout"]
    _git(checkout, "reset", "--hard", "-q", "HEAD~2")   # 2 behind, clean

    status = su.update_status(fetch=True)
    assert status["mode"] == "follower"
    assert status["behind"] == 2 and status["ahead"] == 0
    assert status["can_update"] is True

    result = su.perform_update()
    assert result["updated"] is True
    assert result["count"] == 2
    assert su.update_status(fetch=False)["behind"] == 0


def test_author_is_refused(fleet):
    checkout = fleet["checkout"]
    _commit(checkout, "local.txt", "unshipped")          # 1 ahead, clean

    status = su.update_status(fetch=True)
    assert status["mode"] == "author"
    assert status["ahead"] == 1
    assert status["can_update"] is False

    with pytest.raises(su.SoftwareUpdateError):
        su.perform_update()


def test_dirty_tree_is_refused(fleet):
    checkout = fleet["checkout"]
    _git(checkout, "reset", "--hard", "-q", "HEAD~1")     # behind, but…
    (checkout / "f0.txt").write_text("edited but uncommitted")

    status = su.update_status(fetch=True)
    assert status["dirty"] is True
    assert status["can_update"] is False
    with pytest.raises(su.SoftwareUpdateError):
        su.perform_update()


def test_up_to_date_is_a_noop(fleet):
    status = su.update_status(fetch=True)
    assert status["behind"] == 0
    result = su.perform_update()
    assert result["updated"] is False
