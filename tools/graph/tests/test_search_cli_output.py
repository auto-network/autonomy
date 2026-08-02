"""CLI search ergonomics: bounded source cards and zero-hit broadening."""

from __future__ import annotations

from argparse import Namespace

from tools.graph import cli


def _row(
    sid: str,
    *,
    result_type: str,
    content: str,
    turn: int | None = None,
) -> dict:
    return {
        "id": sid if result_type == "source" else f"{sid}-{turn}",
        "source_id": sid,
        "source_title": f"Source {sid}",
        "source_type": "note",
        "platform": "local",
        "source_created_at": "2026-08-01T12:00:00Z",
        "result_type": result_type,
        "content": content,
        "turn_number": turn,
        "hit_count": 3,
        "org": "autonomy",
    }


def test_compact_output_prints_one_excerpt_per_source(capsys):
    rows = [
        _row("aaaa1111", result_type="source", content="Source aaaa1111"),
        _row(
            "aaaa1111", result_type="thought",
            content="best excerpt\nwith multiple lines", turn=3,
        ),
        _row(
            "aaaa1111", result_type="derivation",
            content="lower excerpt that should stay hidden", turn=4,
        ),
        _row("bbbb2222", result_type="source", content="Source bbbb2222"),
        _row(
            "bbbb2222", result_type="thought",
            content="second source excerpt", turn=8,
        ),
    ]

    cli._print_search_compact(rows, width=200)
    out = capsys.readouterr().out

    assert out.count("  [S]") == 2
    assert "best excerpt with multiple lines" in out
    assert "second source excerpt" in out
    assert "lower excerpt that should stay hidden" not in out
    assert "3 matches" in out


class _FakeHttpClient:
    def __init__(self, broad_rows: list[dict]):
        self.broad_rows = broad_rows
        self.or_modes: list[bool] = []

    def search(self, _query, *, or_mode=False, **_kwargs):
        self.or_modes.append(or_mode)
        return self.broad_rows if or_mode else []


def _args(**overrides) -> Namespace:
    values = {
        "query": "alpha beta",
        "limit": 10,
        "width": 200,
        "or_mode": False,
        "tag": None,
        "state": None,
        "include": None,
        "only_org": None,
        "org_mode": None,
        "source_type": None,
        "json": False,
        "verbose": False,
        "db": None,
    }
    values.update(overrides)
    return Namespace(**values)


def test_interactive_zero_hit_search_falls_back_to_any_term(
    monkeypatch, capsys,
):
    broad = [_row("aaaa1111", result_type="thought", content="alpha", turn=1)]
    client = _FakeHttpClient(broad)
    monkeypatch.setattr(cli, "HttpClient", _FakeHttpClient)
    monkeypatch.setattr(cli, "get_client", lambda: client)

    cli.cmd_search(_args())
    out = capsys.readouterr().out

    assert client.or_modes == [False, True]
    assert "No all-term matches" in out
    assert "Source aaaa1111" in out


def test_quoted_and_json_queries_keep_strict_empty_semantics(
    monkeypatch, capsys,
):
    client = _FakeHttpClient([])
    monkeypatch.setattr(cli, "HttpClient", _FakeHttpClient)
    monkeypatch.setattr(cli, "get_client", lambda: client)

    cli.cmd_search(_args(query='"alpha beta"'))
    assert client.or_modes == [False]
    assert capsys.readouterr().out == "No results found.\n"

    client.or_modes.clear()
    cli.cmd_search(_args(json=True))
    assert client.or_modes == [False]
    assert capsys.readouterr().out == "[]\n"
