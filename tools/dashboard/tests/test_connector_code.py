"""auto-j6ssc: a serving connector is replaced only when code it loaded
changed, not on every commit."""
from __future__ import annotations

import hashlib
import os
import sys
import time
import types

import pytest

from tools.dashboard import connector_code


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(connector_code, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(connector_code, "_DIGEST_CACHE", {})
    return tmp_path


def _module(monkeypatch, name, path):
    mod = types.ModuleType(name)
    mod.__file__ = str(path)
    monkeypatch.setitem(sys.modules, name, mod)


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def test_loaded_files_lists_repository_modules_with_their_digest(repo, monkeypatch):
    (repo / "tools").mkdir()
    (repo / "tools" / "a.py").write_text("A = 1\n")
    _module(monkeypatch, "fake_a", repo / "tools" / "a.py")
    known = {}
    files = connector_code.loaded_files(time.time() + 60, known)
    assert ["tools/a.py", _sha("A = 1\n")] in files
    assert all(not path.startswith("/") for path, _ in files)


def test_a_module_loaded_later_is_added_and_one_modified_after_start_has_no_digest(repo, monkeypatch):
    (repo / "b.py").write_text("B = 1\n")
    started = time.time() - 60
    known = {}
    connector_code.loaded_files(started, known)
    _module(monkeypatch, "fake_b", repo / "b.py")   # imported lazily, later
    files = dict(map(tuple, connector_code.loaded_files(started, known)))
    # b.py's mtime is after the process start: the digest cannot vouch for
    # what was loaded, so it is reported as unknown.
    assert files["b.py"] is None


def test_changed_files(repo):
    (repo / "c.py").write_text("C = 1\n")
    digest = _sha("C = 1\n")
    assert connector_code.changed_files([["c.py", digest]]) == []
    (repo / "c.py").write_text("C = 2\n")
    assert connector_code.changed_files([["c.py", digest]]) == ["c.py"]
    assert connector_code.changed_files([["gone.py", digest]]) == ["gone.py"]
    assert connector_code.changed_files([["c.py", None]]) == ["c.py"]


@pytest.mark.parametrize("report", [None, [], "x", [["c.py"]], [["../etc/passwd", "d"]],
                                    [["/etc/passwd", "d"]]])
def test_a_missing_or_malformed_fingerprint_is_stale(repo, report):
    assert connector_code.changed_files(report) is None


def test_the_digest_cache_follows_the_file(repo, monkeypatch):
    # ctime cannot be set from userspace, so switch off the freshness window
    # to exercise the cache key itself.
    monkeypatch.setattr(connector_code, "_FRESH_S", -1.0)
    (repo / "d.py").write_text("D = 1\n")
    os.utime(repo / "d.py", (1, 1))
    first = connector_code.current_digest("d.py")
    assert connector_code._DIGEST_CACHE
    # File times come from a coarse kernel clock (a few ms per tick); two
    # writes inside one tick share every timestamp, which is why the real
    # code never caches a file touched in the last _FRESH_S seconds.
    time.sleep(0.05)
    (repo / "d.py").write_text("D = 2\n")     # same size
    os.utime(repo / "d.py", (1, 1))            # same mtime; ctime still moves
    assert connector_code.current_digest("d.py") != first


def test_a_same_size_rewrite_within_one_tick_is_seen(repo, monkeypatch):
    """The cache must not hide an edit that leaves mtime and size equal."""
    (repo / "e.py").write_text("E = 1\n")
    first = connector_code.current_digest("e.py")
    (repo / "e.py").write_text("E = 2\n")
    assert connector_code.current_digest("e.py") != first
