"""The refresh pollers rotate the harness sign-ins sealed in the vault."""
from __future__ import annotations

from types import SimpleNamespace

from tools.dashboard import claude_credentials_refresh as claude_refresh
from tools.dashboard import codex_credentials_refresh as codex_refresh
from tools.graph import harness_credentials as hv


def _vault(monkeypatch, values: dict) -> dict:
    """A stub vault that opens every key in *values* and records seals."""
    class _Members:
        @property
        def members(self):
            return [SimpleNamespace(key=k, payload={"value": v}, vault_error=None)
                    for k, v in values.items()]
    monkeypatch.setattr(hv.graph_ops, "read_set", lambda *a, **kw: _Members())
    sealed: dict = {}

    def seal(key, value, **kw):
        sealed[key] = value
        values[key] = value
        return "id"
    monkeypatch.setattr(hv, "seal", seal)
    return sealed


def test_claude_vault_sign_in_rotates_when_near_expiry(monkeypatch):
    now = 1_000_000
    sealed = _vault(monkeypatch, {
        hv.CLAUDE_REFRESH: "rt-old", hv.CLAUDE_EXPIRES: str(now + 60_000),
    })
    result = claude_refresh.RefreshResult(
        kind="ok", access_token="at-new", refresh_token="rt-new", expires_in=3600,
    )
    kind = claude_refresh.refresh_vault_sign_in(now_ms=now, refresh=lambda tok: result)
    assert kind == "ok"
    assert sealed[hv.CLAUDE_ACCESS] == "at-new"
    assert sealed[hv.CLAUDE_REFRESH] == "rt-new"
    assert sealed[hv.CLAUDE_EXPIRES] == str(now + 3600 * 1000)


def test_claude_vault_sign_in_left_alone_with_life_left(monkeypatch):
    now = 1_000_000
    sealed = _vault(monkeypatch, {
        hv.CLAUDE_REFRESH: "rt", hv.CLAUDE_EXPIRES: str(now + 10 * 3600 * 1000),
    })
    assert claude_refresh.refresh_vault_sign_in(now_ms=now, refresh=lambda tok: 1 / 0) is None
    assert sealed == {}


def test_claude_vault_sign_in_absent_is_skipped(monkeypatch):
    sealed = _vault(monkeypatch, {})
    assert claude_refresh.refresh_vault_sign_in(now_ms=1, refresh=lambda tok: 1 / 0) is None
    assert sealed == {}


def test_codex_vault_sign_in_rotates_the_triple(monkeypatch):
    now = 1_000_000
    sealed = _vault(monkeypatch, {
        hv.CODEX_REFRESH: "cr-old", hv.CODEX_EXPIRES: str(now + 60_000),
    })
    result = codex_refresh.RefreshResult(
        kind="ok", access_token="ct-new", refresh_token="cr-new",
        id_token="id-new", expires_at_ms=now + 7_200_000,
    )
    kind = codex_refresh.refresh_vault_sign_in(now_ms=now, refresh=lambda tok: result)
    assert kind == "ok"
    assert sealed[hv.CODEX_ID] == "id-new"
    assert sealed[hv.CODEX_ACCESS] == "ct-new"
    assert sealed[hv.CODEX_REFRESH] == "cr-new"
    assert sealed[hv.CODEX_EXPIRES] == str(now + 7_200_000)


def test_revoked_sign_in_is_reported_not_overwritten(monkeypatch):
    now = 1_000_000
    sealed = _vault(monkeypatch, {
        hv.CLAUDE_REFRESH: "rt", hv.CLAUDE_EXPIRES: str(now + 60_000),
    })
    result = claude_refresh.RefreshResult(kind="revoked", error="invalid_grant")
    assert claude_refresh.refresh_vault_sign_in(now_ms=now, refresh=lambda tok: result) == "revoked"
    assert sealed == {}
