"""CLI tests for ``graph turn-correction suggest`` (delivery: auto-hmow2).

The command now delivers a correction as ONE authenticated dashboard API call
(``POST /api/session/turn-corrections/suggest``) using ``GRAPH_API`` and the
shared session bearer token. It sends only the corrected replacement text plus
optional metadata — never session/target/hash identity. Its stdout is a receipt
and may be redirected or discarded without affecting delivery. There is no
transcript/stdout delivery path and no direct-DB fallback.
"""

from __future__ import annotations

import inspect
import io
import json
import sys
import urllib.error
import urllib.request
from contextlib import redirect_stdout, redirect_stderr

import pytest

from tools.graph import cli


SUGGEST_PATH = "/api/session/turn-corrections/suggest"


def _run_cli(argv: list[str], stdin_text: str | None = None) -> tuple[int, str, str]:
    """Drive ``cli.main`` end-to-end. Returns (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    rc = 0
    saved_argv = sys.argv
    saved_stdin = sys.stdin
    sys.argv = ["graph"] + argv
    if stdin_text is not None:
        sys.stdin = io.StringIO(stdin_text)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as e:
                rc = int(e.code) if e.code is not None else 0
    finally:
        sys.argv = saved_argv
        sys.stdin = saved_stdin
    return rc, out.getvalue(), err.getvalue()


class _CapturingPoster:
    """Captures the outgoing urllib Request and returns a canned 201 body."""

    def __init__(self, response_body: dict | None = None, raise_exc: Exception | None = None):
        self.requests: list[urllib.request.Request] = []
        self.bodies: list[bytes] = []
        self._raise = raise_exc
        self._response_body = response_body if response_body is not None else {
            "ok": True,
            "session_id": "auto-0803-151510",
            "correction": {
                "session_uuid": "uuid-1",
                "target_message_id": "msg-42",
                "status": "pending",
                "original_sha256": "abc",
                "corrected_text": "…",
            },
        }

    def __call__(self, req, timeout=None, context=None):
        self.requests.append(req)
        self.bodies.append(req.data)
        if self._raise is not None:
            raise self._raise
        return io.BytesIO(json.dumps(self._response_body).encode())


@pytest.fixture
def bearer(monkeypatch):
    """Force a known session bearer via the shared resolver; pin GRAPH_API."""
    monkeypatch.setattr(cli, "_resolve_crosstalk_token", lambda: "session-token-xyz")
    monkeypatch.setenv("GRAPH_API", "https://dash.example:9999")
    return "session-token-xyz"


def _install_poster(monkeypatch, poster: _CapturingPoster) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", poster)


def _sent_json(poster: _CapturingPoster) -> dict:
    assert poster.bodies, "no request was sent"
    return json.loads(poster.bodies[-1].decode())


# ── body is posted verbatim ────────────────────────────────────


def test_positional_body_posted_verbatim(bearer, monkeypatch):
    poster = _CapturingPoster()
    _install_poster(monkeypatch, poster)
    rc, out, err = _run_cli([
        "turn-correction", "suggest", "The full replacement message",
    ])
    assert rc == 0, err
    body = _sent_json(poster)
    assert body == {"corrected_text": "The full replacement message"}
    # Identity is NEVER sent — the server derives it.
    for forbidden in ("session_id", "session_uuid", "target_message_id", "original_sha256"):
        assert forbidden not in body


def test_multiline_stdin_body_posted_verbatim(bearer, monkeypatch):
    poster = _CapturingPoster()
    _install_poster(monkeypatch, poster)
    long_text = "\n\n".join(f"Paragraph {i}: " + ("x" * 400) for i in range(20))
    assert len(long_text) > 8000
    rc, out, err = _run_cli(
        ["turn-correction", "suggest", "--stdin"],
        stdin_text=long_text,
    )
    assert rc == 0, err
    assert _sent_json(poster)["corrected_text"] == long_text


def test_stdin_preserves_trailing_newline(bearer, monkeypatch):
    poster = _CapturingPoster()
    _install_poster(monkeypatch, poster)
    rc, out, err = _run_cli(
        ["turn-correction", "suggest", "--stdin"],
        stdin_text="line one\nline two\n",
    )
    assert rc == 0, err
    assert _sent_json(poster)["corrected_text"] == "line one\nline two\n"


def test_content_stdin_alias_posts_body(bearer, monkeypatch):
    poster = _CapturingPoster()
    _install_poster(monkeypatch, poster)
    rc, out, err = _run_cli(
        ["turn-correction", "suggest", "-c", "-"],
        stdin_text="alias body",
    )
    assert rc == 0, err
    assert _sent_json(poster)["corrected_text"] == "alias body"


def test_optional_metadata_transmitted(bearer, monkeypatch):
    poster = _CapturingPoster()
    _install_poster(monkeypatch, poster)
    rc, out, err = _run_cli([
        "turn-correction", "suggest", "fixed",
        "--mode", "balanced",
        "--reason", "dictation cleanup",
        "--confidence", "0.9",
    ])
    assert rc == 0, err
    body = _sent_json(poster)
    assert body["mode"] == "balanced"
    assert body["reason"] == "dictation cleanup"
    assert body["confidence"] == pytest.approx(0.9)


# ── auth + endpoint ────────────────────────────────────────────


def test_uses_session_bearer_and_graph_api(bearer, monkeypatch):
    poster = _CapturingPoster()
    _install_poster(monkeypatch, poster)
    rc, out, err = _run_cli(["turn-correction", "suggest", "x"])
    assert rc == 0, err
    req = poster.requests[-1]
    assert req.full_url == "https://dash.example:9999" + SUGGEST_PATH
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == f"Bearer {bearer}"
    # The session name is never carried in the URL or body.
    assert "auto-" not in req.full_url


def test_no_correction_specific_token_or_legacy_alias():
    """The command must use the shared resolver only — no bespoke credential,
    legacy-token branch, or old-environment-name alias around it."""
    src = inspect.getsource(cli.cmd_turn_correction_suggest)
    assert "_resolve_crosstalk_token()" in src
    # No correction-specific / legacy token env names.
    for banned in (
        "TURN_CORRECTION_TOKEN",
        "CORRECTION_TOKEN",
        "SESSION_TOKEN",  # renamed resolver is auto-2hfrq's job, not a local alias
        "os.environ.get(\"CROSSTALK_TOKEN\"",
    ):
        assert banned not in src, f"unexpected token handling: {banned!r}"


# ── redirect-safe delivery ─────────────────────────────────────


def test_success_when_stdout_discarded(bearer, monkeypatch):
    """Delivery must succeed even if the receipt is thrown away entirely."""
    poster = _CapturingPoster()
    _install_poster(monkeypatch, poster)
    # _run_cli captures stdout into a StringIO we ignore — the equivalent of a
    # redirect to /dev/null from the caller's perspective.
    rc, out, err = _run_cli(["turn-correction", "suggest", "still delivered"])
    assert rc == 0, err
    # The POST fired regardless of what happened to stdout.
    assert _sent_json(poster)["corrected_text"] == "still delivered"


# ── failure paths exit nonzero, never claim success ────────────


def test_http_failure_exits_nonzero(bearer, monkeypatch):
    err_body = io.BytesIO(json.dumps({"error": "invalid or revoked token"}).encode())
    http_err = urllib.error.HTTPError(
        "https://dash.example:9999" + SUGGEST_PATH, 401, "Unauthorized", {}, err_body)
    poster = _CapturingPoster(raise_exc=http_err)
    _install_poster(monkeypatch, poster)
    rc, out, err = _run_cli(["turn-correction", "suggest", "x"])
    assert rc != 0
    assert "invalid or revoked token" in err
    assert "✓" not in out  # no success receipt


def test_network_failure_exits_nonzero(bearer, monkeypatch):
    poster = _CapturingPoster(raise_exc=urllib.error.URLError("connection refused"))
    _install_poster(monkeypatch, poster)
    rc, out, err = _run_cli(["turn-correction", "suggest", "x"])
    assert rc != 0
    assert "✓" not in out


# ── no direct DB write / no transcript fallback ────────────────


def test_no_direct_db_write_or_transcript_fallback():
    src = inspect.getsource(cli.cmd_turn_correction_suggest)
    # Delivery is the POST; there is no DAO write and no stdout-event contract.
    assert "upsert_turn_correction" not in src
    assert "dashboard_db" not in src
    assert "sqlite" not in src.lower()
    # It no longer prints a machine-readable event to stdout as the transport;
    # json.dumps(payload) is now the POST body, never a stdout print.
    assert 'print(json.dumps(payload))' not in src


# ── --json remains a hidden no-op receipt flag ─────────────────


def test_json_flag_is_accepted_noop(bearer, monkeypatch):
    """Frozen primers may still pass --json; it must not fail and must still
    deliver via the API (never select an output-as-transport contract)."""
    poster = _CapturingPoster()
    _install_poster(monkeypatch, poster)
    rc, out, err = _run_cli(["turn-correction", "suggest", "x", "--json"])
    assert rc == 0, err
    assert _sent_json(poster)["corrected_text"] == "x"


# ── validation (rejected before any network call) ──────────────


def test_requires_corrected_text():
    rc, _, err = _run_cli(["turn-correction", "suggest"])
    assert rc != 0
    assert "corrected text" in err.lower()


def test_rejects_both_argv_and_stdin():
    rc, _, err = _run_cli(
        ["turn-correction", "suggest", "from-argv", "--stdin"],
        stdin_text="from-stdin",
    )
    assert rc != 0
    assert "stdin" in err.lower()


def test_rejects_multiple_input_forms():
    rc, _, err = _run_cli(
        ["turn-correction", "suggest", "from-argv", "-c", "-"],
        stdin_text="from-stdin",
    )
    assert rc != 0
    assert "multiple ways" in err.lower()


def test_rejects_invalid_mode():
    rc, _, _ = _run_cli(["turn-correction", "suggest", "x", "--mode", "wild"])
    assert rc != 0


@pytest.mark.parametrize("bad", ["-0.1", "1.5"])
def test_rejects_out_of_range_confidence(bad):
    rc, _, err = _run_cli(["turn-correction", "suggest", "x", "--confidence", bad])
    assert rc != 0
    assert "confidence" in err.lower()
