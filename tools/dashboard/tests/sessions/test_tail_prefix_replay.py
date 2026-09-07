"""auto-3xony: the tail endpoint's gap-fill reconstruction and window reader.

- reconstruction is incremental: a second replay of the same chain extends the
  cached frontier instead of re-parsing from byte 0, and yields exactly the
  state a full replay yields;
- the cache falls back to a full replay when an earlier file changed, the
  file shrank, or the chain rolled over;
- semantic-tile enrichment (a sqlite lookup per graph tile) is off during
  reconstruction and on for the served window;
- the rewritten tail-window reader is byte-identical to the previous
  quadratic implementation on random files, including multi-MB lines.
"""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path

import pytest

from tools.dashboard import server, session_harness
from tools.dashboard.tests.conftest import MOCK_ENTRIES


# ── helpers ───────────────────────────────────────────────────────────────

def _write_jsonl(path: Path, entries) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def _more_entries(n: int, start_minute: int = 10):
    out = []
    for i in range(n):
        ts = f"2026-03-24T12:{start_minute + i:02d}:00Z"
        out.append({"type": "human", "message": {"role": "user", "content": [
            {"type": "text", "text": f"follow-up {i}"}]}, "timestamp": ts})
        out.append({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": f"answer {i}"}]}, "timestamp": ts})
    return out


def _line_offset(path: Path, k: int) -> int:
    """Byte offset right after the k-th newline."""
    data = path.read_bytes()
    off = 0
    for _ in range(k):
        off = data.index(b"\n", off) + 1
    return off


def _normalize(obj):
    if isinstance(obj, dict):
        return {str(k): _normalize(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (list, tuple)):
        return [_normalize(v) for v in obj]
    if isinstance(obj, set):
        return sorted(_normalize(v) for v in obj)
    if hasattr(obj, "__dict__"):
        return _normalize(vars(obj))
    return obj


def _signature(state: dict):
    tracker = state["tracker"]
    return _normalize({
        "parse_ctx": state["parse_ctx"],
        "postprocess_state": state["postprocess_state"],
        "tracker": tracker._sessions,
        "last_enqueue_content": state["last_enqueue_content"],
        "agent_descriptions": state["agent_descriptions"],
        "claimed_subagents": state["claimed_subagents"],
    })


@pytest.fixture
def fresh_cache():
    server._recon_cache.clear()
    server._recon_stats.update(full=0, incremental=0)
    yield
    server._recon_cache.clear()


@pytest.fixture
def session_file(tmp_path):
    path = tmp_path / "sessions" / "u" / "abc.jsonl"
    _write_jsonl(path, MOCK_ENTRIES + _more_entries(6))
    return path


def _full(chain, upto_file, upto_off):
    server._recon_cache.clear()
    return server._reconstruct_read_state(
        chain, server.CLAUDE_HARNESS, upto_file=upto_file, upto_off=upto_off,
    )


# ── incremental reconstruction ────────────────────────────────────────────

def test_incremental_replay_equals_full_replay(session_file, fresh_cache):
    chain = [("abc", session_file)]
    end = server._last_complete_offset_in(session_file)
    mid = _line_offset(session_file, 5)
    assert 0 < mid < end

    expected = _signature(_full(chain, "abc", end))

    server._recon_cache.clear()
    server._recon_stats.update(full=0, incremental=0)
    first = server._reconstruct_read_state(chain, server.CLAUDE_HARNESS, upto_file="abc", upto_off=mid)
    assert server._recon_stats == {"full": 1, "incremental": 0}
    second = server._reconstruct_read_state(chain, server.CLAUDE_HARNESS, upto_file="abc", upto_off=end)
    assert server._recon_stats == {"full": 1, "incremental": 1}
    assert _signature(second) == expected
    # The mid-state was not disturbed by the extension (deep-copied out).
    assert _signature(first) == _signature(_full(chain, "abc", mid))


def test_replay_extends_when_the_file_grows(session_file, fresh_cache):
    chain = [("abc", session_file)]
    end1 = server._last_complete_offset_in(session_file)
    server._reconstruct_read_state(chain, server.CLAUDE_HARNESS, upto_file="abc", upto_off=end1)
    with open(session_file, "a") as fh:
        for e in _more_entries(3, start_minute=40):
            fh.write(json.dumps(e) + "\n")
    end2 = server._last_complete_offset_in(session_file)
    assert end2 > end1
    server._recon_stats.update(full=0, incremental=0)
    got = server._reconstruct_read_state(chain, server.CLAUDE_HARNESS, upto_file="abc", upto_off=end2)
    assert server._recon_stats == {"full": 0, "incremental": 1}
    assert _signature(got) == _signature(_full(chain, "abc", end2))


def test_same_cursor_twice_parses_nothing_new(session_file, fresh_cache):
    chain = [("abc", session_file)]
    end = server._last_complete_offset_in(session_file)
    a = server._reconstruct_read_state(chain, server.CLAUDE_HARNESS, upto_file="abc", upto_off=end)
    b = server._reconstruct_read_state(chain, server.CLAUDE_HARNESS, upto_file="abc", upto_off=end)
    assert server._recon_stats == {"full": 1, "incremental": 1}
    assert _signature(a) == _signature(b)


def test_returned_state_is_a_private_copy(session_file, fresh_cache):
    chain = [("abc", session_file)]
    end = server._last_complete_offset_in(session_file)
    a = server._reconstruct_read_state(chain, server.CLAUDE_HARNESS, upto_file="abc", upto_off=end)
    a["parse_ctx"]["poison"] = True
    a["agent_descriptions"]["poison"] = "x"
    a["tracker"].enrich("_reconstruct", [{"type": "poison"}])
    b = server._reconstruct_read_state(chain, server.CLAUDE_HARNESS, upto_file="abc", upto_off=end)
    assert "poison" not in b["parse_ctx"]
    assert "poison" not in b["agent_descriptions"]


def test_shrunken_file_falls_back_to_full_replay(session_file, fresh_cache):
    chain = [("abc", session_file)]
    end = server._last_complete_offset_in(session_file)
    server._reconstruct_read_state(chain, server.CLAUDE_HARNESS, upto_file="abc", upto_off=end)
    keep = _line_offset(session_file, 6)
    with open(session_file, "r+b") as fh:
        fh.truncate(keep)
    server._recon_stats.update(full=0, incremental=0)
    got = server._reconstruct_read_state(chain, server.CLAUDE_HARNESS, upto_file="abc", upto_off=keep)
    assert server._recon_stats == {"full": 1, "incremental": 0}
    assert _signature(got) == _signature(_full(chain, "abc", keep))


def test_rollover_chain_replays_earlier_files_once(session_file, tmp_path, fresh_cache):
    second = session_file.with_name("def.jsonl")
    _write_jsonl(second, _more_entries(4, start_minute=50))
    chain1 = [("abc", session_file)]
    chain2 = [("abc", session_file), ("def", second)]
    end1 = server._last_complete_offset_in(session_file)
    end2 = server._last_complete_offset_in(second)

    server._reconstruct_read_state(chain1, server.CLAUDE_HARNESS, upto_file="abc", upto_off=end1)
    # Different chain shape → different key → one full replay, then cached.
    server._recon_stats.update(full=0, incremental=0)
    got = server._reconstruct_read_state(chain2, server.CLAUDE_HARNESS, upto_file="def", upto_off=end2)
    assert server._recon_stats == {"full": 1, "incremental": 0}
    assert _signature(got) == _signature(_full(chain2, "def", end2))
    server._recon_cache.clear()
    server._reconstruct_read_state(chain2, server.CLAUDE_HARNESS, upto_file="def", upto_off=_line_offset(second, 2))
    server._recon_stats.update(full=0, incremental=0)
    got2 = server._reconstruct_read_state(chain2, server.CLAUDE_HARNESS, upto_file="def", upto_off=end2)
    assert server._recon_stats == {"full": 0, "incremental": 1}
    assert _signature(got2) == _signature(_full(chain2, "def", end2))
    # An EARLIER file that changed invalidates the extension.
    with open(session_file, "a") as fh:
        fh.write(json.dumps(_more_entries(1, start_minute=55)[0]) + "\n")
    server._recon_stats.update(full=0, incremental=0)
    server._reconstruct_read_state(chain2, server.CLAUDE_HARNESS, upto_file="def", upto_off=end2)
    assert server._recon_stats == {"full": 1, "incremental": 0}


def test_cache_is_bounded(session_file, fresh_cache, monkeypatch):
    monkeypatch.setattr(server, "_RECON_CACHE_MAX", 3)
    end = server._last_complete_offset_in(session_file)
    for i in range(6):
        server._reconstruct_read_state([(f"s{i}", session_file)], server.CLAUDE_HARNESS,
                                       upto_file=f"s{i}", upto_off=end)
    assert len(server._recon_cache) == 3
    assert [k[1] for k in server._recon_cache] == ["s3", "s4", "s5"]


# ── enrichment gate ───────────────────────────────────────────────────────

def test_semantic_enrichment_gate(monkeypatch, tmp_path):
    calls = []

    def connect(*a, **k):
        calls.append(a)
        raise session_harness.sqlite3.OperationalError("spy")
    monkeypatch.setattr(session_harness, "_graph_db_path", lambda: str(tmp_path / "g.db"))
    monkeypatch.setattr(session_harness.sqlite3, "connect", connect)

    with session_harness.semantic_enrichment_disabled():
        session_harness._enrich_semantic_tile({"source_id": "abc"})
        assert session_harness._SEMANTIC_ENRICHMENT.get() is False
    assert calls == []                                   # gated: no sqlite at all
    assert session_harness._SEMANTIC_ENRICHMENT.get() is True
    session_harness._enrich_semantic_tile({"source_id": "abc"})
    assert len(calls) == 1                               # served window: enriched


def test_reconstruction_runs_with_enrichment_disabled(session_file, fresh_cache, monkeypatch):
    seen = []
    real = session_harness.resolve_harness_for_path

    def spy(path, **kw):
        seen.append(session_harness._SEMANTIC_ENRICHMENT.get())
        return real(path, **kw)
    monkeypatch.setattr(server.session_harness, "resolve_harness_for_path", spy)
    end = server._last_complete_offset_in(session_file)
    server._reconstruct_read_state([("abc", session_file)], server.CLAUDE_HARNESS,
                                   upto_file="abc", upto_off=end)
    assert seen and all(v is False for v in seen)
    assert session_harness._SEMANTIC_ENRICHMENT.get() is True


# ── tail window reader ────────────────────────────────────────────────────

def _reference_tail_window(path: Path, *, n: int, before: int | None):
    """The pre-3xony implementation, verbatim, as the oracle."""
    if n <= 0:
        return b"", 0, 0
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        if before is None:
            end = server._last_complete_offset_in(path)
        else:
            end = max(0, min(int(before), size))
        if end <= 0:
            return b"", 0, 0
        chunk_size = 8192
        buf = b""
        offset = end
        while offset > 0 and buf.count(b"\n") <= n:
            read = min(chunk_size, offset)
            offset -= read
            fh.seek(offset)
            buf = fh.read(read) + buf
        if offset > 0:
            nl = buf.find(b"\n")
            if nl != -1:
                offset += nl + 1
                buf = buf[nl + 1:]
        segments = buf.splitlines(keepends=True)
        count = 0
        i = len(segments)
        while i > 0 and count < n:
            i -= 1
            if segments[i].strip():
                count += 1
        if count == 0:
            return b"", end, end
        raw = b"".join(segments[i:])
        return raw, end - len(raw), end


def _random_file(path: Path, rng: random.Random, *, big: bool) -> None:
    lines = []
    for _ in range(rng.randint(1, 60)):
        r = rng.random()
        if r < 0.15:
            lines.append(b"")
        elif big and r < 0.25:
            lines.append(b"x" * rng.randint(9_000, 300_000))
        else:
            lines.append(bytes(rng.choice(b"abcdefghij ") for _ in range(rng.randint(0, 400))))
    data = b"\n".join(lines) + (b"\n" if rng.random() < 0.8 else b"")
    path.write_bytes(data)


@pytest.mark.parametrize("seed", range(12))
def test_tail_window_matches_previous_implementation(tmp_path, seed):
    rng = random.Random(seed)
    path = tmp_path / "s.jsonl"
    _random_file(path, rng, big=(seed % 3 == 0))
    size = path.stat().st_size
    for n in (1, 2, 5, 17, 200):
        for before in (None, 0, 1, size // 3, size // 2, size - 1, size, size + 50):
            assert server._read_jsonl_tail_window(path, n=n, before=before) == \
                _reference_tail_window(path, n=n, before=before), (n, before)


def test_tail_window_over_a_multi_megabyte_line(tmp_path):
    path = tmp_path / "big.jsonl"
    small = b'{"type":"human","x":1}\n'
    huge = b'{"type":"assistant","blob":"' + b"y" * (2 * 1024 * 1024) + b'"}\n'
    path.write_bytes(small * 3 + huge + small * 2)
    raw, start, end = server._read_jsonl_tail_window(path, n=3, before=None)
    assert raw == huge + small * 2
    assert end - start == len(raw) and end == path.stat().st_size
    assert server._read_jsonl_tail_window(path, n=3, before=None) == \
        _reference_tail_window(path, n=3, before=None)
