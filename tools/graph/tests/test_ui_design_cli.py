"""Focused CLI coverage for ``graph ui-design`` publishing ergonomics."""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import urllib.error
import urllib.request

import pytest

from tools.graph import cli


class _Response:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body


def _args(tmp_path, **overrides):
    design_dir = tmp_path / "design"
    design_dir.mkdir()
    (design_dir / "index.html").write_text("<h1>Deck</h1>")
    values = {
        "title": "Test deck",
        "dir": str(design_dir),
        "design": None,
        "description": None,
        "fixture": None,
        "api": "https://dashboard.example:8443",
        "present": False,
        "once": True,
        "list": None,
        "pull": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _parse_ui_args(monkeypatch, argv):
    captured = {}

    def capture(args):
        captured.update(vars(args))

    monkeypatch.setattr(cli, "cmd_ui_design", capture)
    monkeypatch.setattr(sys, "argv", ["graph", *argv])
    cli.main()
    return captured


def test_once_publishes_with_timeout_and_skips_watch(tmp_path, monkeypatch, capsys):
    calls = []

    def urlopen(req, *, context, timeout):
        calls.append((req.full_url, timeout))
        return _Response({"id": "revision-1"})

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    cli.cmd_ui_design(_args(tmp_path))

    captured = capsys.readouterr()
    assert calls == [("https://dashboard.example:8443/api/design", 30)]
    assert "Publishing to https://dashboard.example:8443" in captured.out
    assert "Created design revision: revision-1" in captured.out
    assert "Watching" not in captured.out
    assert captured.err == ""


def test_watch_remains_the_default(tmp_path, monkeypatch, capsys):
    def stop_watch(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda _req, *, context, timeout: _Response({"id": "revision-1"}),
    )
    monkeypatch.setattr(time, "sleep", stop_watch)

    cli.cmd_ui_design(_args(tmp_path, once=False))

    captured = capsys.readouterr()
    assert "Watching" in captured.out
    assert "Stopped watching. Design: revision-1" in captured.out


def test_unreachable_create_has_one_actionable_error(tmp_path, monkeypatch, capsys):
    def urlopen(*_args, **_kwargs):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_ui_design(_args(tmp_path))

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert captured.err.count("\n") == 1
    assert "Couldn't reach the dashboard at https://dashboard.example:8443" in captured.err
    assert "--api https://host.docker.internal:8080" in captured.err
    assert "Traceback" not in captured.err


def test_unreachable_present_activation_exits_cleanly(tmp_path, monkeypatch, capsys):
    calls = 0

    def urlopen(req, *, context, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _Response({"id": "revision-1"})
        raise OSError(113, "No route to host")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_ui_design(_args(tmp_path, present=True))

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert captured.err.count("\n") == 1
    assert "Couldn't reach the dashboard" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("in_container", "graph_api", "expected"),
    [
        (True, "https://configured.example:9443", "https://configured.example:9443"),
        (True, None, "https://host.docker.internal:8080"),
        (False, None, "https://localhost:8080"),
    ],
)
def test_api_default_precedence(monkeypatch, in_container, graph_api, expected):
    monkeypatch.setattr(cli, "_in_container", lambda: in_container)
    if graph_api is None:
        monkeypatch.delenv("GRAPH_API", raising=False)
    else:
        monkeypatch.setenv("GRAPH_API", graph_api)

    args = _parse_ui_args(monkeypatch, ["ui-design", "Title", "/tmp/deck", "--once"])

    assert args["api"] == expected
    assert args["once"] is True


def test_legacy_alias_supports_no_watch_and_explicit_api(monkeypatch):
    monkeypatch.setattr(cli, "_in_container", lambda: True)
    monkeypatch.setenv("GRAPH_API", "https://configured.example:9443")

    args = _parse_ui_args(
        monkeypatch,
        [
            "ui-exp", "Title", "/tmp/deck", "--no-watch",
            "--api", "https://explicit.example:7443",
        ],
    )

    assert args["api"] == "https://explicit.example:7443"
    assert args["once"] is True


def test_duplicate_name_conflict_explains_revision_and_force_paths(tmp_path, monkeypatch, capsys):
    payload = {
        "error": "duplicate_design_name",
        "message": "A design named 'Test deck' already exists.",
        "existing": [{
            "design_id": "design-1",
            "latest_revision_id": "revision-2",
            "title": "Test deck",
            "revision_count": 2,
        }],
    }

    def urlopen(req, *, context, timeout):
        raise urllib.error.HTTPError(
            req.full_url,
            409,
            "Conflict",
            hdrs=None,
            fp=io.BytesIO(json.dumps(payload).encode()),
        )

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_ui_design(_args(tmp_path))

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "Existing design: design-1" in captured.err
    assert "--design design-1" in captured.err
    assert "Intentional duplicate: re-run with --force" in captured.err
    assert "Couldn't reach" not in captured.err


def test_force_is_sent_to_design_and_present_activation(tmp_path, monkeypatch):
    requests = []

    def urlopen(req, *, context, timeout):
        requests.append(req)
        if req.full_url.endswith("/api/design"):
            return _Response({"id": "revision-1"})
        return _Response({"ok": True})

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    cli.cmd_ui_design(_args(tmp_path, force=True, present=True))

    create_payload = json.loads(requests[0].data)
    assert create_payload["force"] is True
    assert requests[-1].full_url.endswith(
        "/api/presentations/deck/revision-1/shown?force=true"
    )


def test_force_flag_is_available_on_primary_and_legacy_commands(monkeypatch):
    monkeypatch.setattr(cli, "_in_container", lambda: False)
    primary = _parse_ui_args(monkeypatch, ["ui-design", "Title", "/tmp/deck", "--force"])
    legacy = _parse_ui_args(monkeypatch, ["ui-exp", "Title", "/tmp/deck", "--force"])

    assert primary["force"] is True
    assert legacy["force"] is True


# ── Read modes: --list and --pull ─────────────────────────────────────────


def test_list_mode_prints_the_org_scoped_library(tmp_path, monkeypatch, capsys):
    def urlopen(req, *, context, timeout):
        assert "/api/design-studio/designs" in req.full_url
        return _Response({"designs": [
            {"design_id": "d1", "status": "pending", "org": "autonomy", "title": "Alpha"},
            {"design_id": "d2", "status": "completed", "org": "anchore", "title": "Beta"},
        ]})

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    cli.cmd_ui_design(_args(tmp_path, list="", title=None, dir=None))

    out = capsys.readouterr().out
    assert "d1" in out and "Alpha" in out and "[autonomy]" in out
    assert "d2" in out and "Beta" in out and "[anchore]" in out
    assert "2 design(s)." in out


def test_list_mode_forwards_the_query(tmp_path, monkeypatch, capsys):
    seen = {}

    def urlopen(req, *, context, timeout):
        seen["url"] = req.full_url
        return _Response({"designs": []})

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    cli.cmd_ui_design(_args(tmp_path, list="session wall", title=None, dir=None))

    assert "q=session%20wall" in seen["url"]
    assert "No designs match." in capsys.readouterr().out


def test_pull_mode_materializes_variants_and_fixture(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "pulled"

    def urlopen(req, *, context, timeout):
        url = req.full_url
        if url.endswith("/api/design-studio/designs/design-1"):
            return _Response({"latest_revision_id": "rev-9", "title": "Alpha"})
        if url.endswith("/api/design/rev-9/full"):
            return _Response({
                "variants": [
                    {"id": "main", "html": "<h1>Main</h1>"},
                    {"id": "alt", "html": "<h1>Alt</h1>"},
                ],
                "fixture": {"states": {"Default": {"count": 1}}},
            })
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    # The destination arrives via the first positional (argparse fills `title`
    # before `dir` with two optional positionals); the command must still write
    # to it. Pass it as `title` to prove that resolution.
    cli.cmd_ui_design(_args(tmp_path, pull="design-1", title=str(dest), dir=None))

    assert (dest / "main.html").read_text() == "<h1>Main</h1>"
    assert (dest / "alt.html").read_text() == "<h1>Alt</h1>"
    assert json.loads((dest / "fixture.json").read_text())["states"]["Default"]["count"] == 1
    out = capsys.readouterr().out
    assert "Pulled design design-1 (revision rev-9)" in out
    assert "--design design-1" in out


def test_pull_mode_reports_a_design_with_no_revisions(tmp_path, monkeypatch, capsys):
    def urlopen(req, *, context, timeout):
        return _Response({"latest_revision_id": ""})

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_ui_design(_args(tmp_path, pull="empty-design", title=str(tmp_path / "d"), dir=None))

    assert exc_info.value.code == 2
    assert "no revisions" in capsys.readouterr().err.lower()
