"""Log channels (C): routing, no duplication, stdout = WARNING+, the map line."""

from __future__ import annotations

import logging
import logging.handlers

import pytest

from tools.dashboard import log_channels as lc


@pytest.fixture
def channels(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_LOG_CHANNELS", "on")
    monkeypatch.delenv("DASHBOARD_LOG_STDOUT_LEVEL", raising=False)
    monkeypatch.delenv("DASHBOARD_LOG_MAX_BYTES", raising=False)
    monkeypatch.delenv("DASHBOARD_LOG_BACKUPS", raising=False)
    line = lc.configure()
    yield tmp_path, line
    # Leave logging as the rest of the suite expects it: no channel handlers.
    lc.configure_plain()


def _flush():
    for h in logging.getLogger().handlers:
        h.flush()
    for _, prefix in lc._routed_loggers():
        for h in logging.getLogger(prefix).handlers:
            h.flush()


def _read(tmp_path, channel):
    p = tmp_path / f"{channel}.log"
    return p.read_text() if p.exists() else ""


def test_channel_for_routes_by_prefix_and_defaults_to_catch_all():
    assert lc.channel_for("tools.dashboard.server.http") == "http"
    assert lc.channel_for("tools.dashboard.server.stall") == "stalls"
    assert lc.channel_for("tools.dashboard.server.voice") == "voice"
    assert lc.channel_for("tools.dashboard.voice_whisperlive") == "voice"
    assert lc.channel_for("tools.dashboard.session_monitor") == "monitors"
    assert lc.channel_for("agents.workspace_manager") == "monitors"
    assert lc.channel_for("tools.network.fleet_sync.sync") == "fleet"
    assert lc.channel_for("tools.network") == "fleet"
    assert lc.channel_for("tools.dashboard.link_serving_supervisor") == "fleet"
    assert lc.channel_for("tools.dashboard.server") == "dashboard"
    assert lc.channel_for("tools.dashboard.server.httpx") == "dashboard"   # prefix, not substring
    assert lc.channel_for("tools.graph.db") == "dashboard"


def test_routed_records_land_in_their_file_and_nowhere_else(channels):
    tmp_path, _ = channels
    logging.getLogger("tools.dashboard.server.http").info("GET /api/ping 200 0.5ms client=1.2.3.4")
    logging.getLogger("tools.network.fleet_sync.sync").info("fleet delta applied")
    logging.getLogger("tools.dashboard.session_monitor").warning("worktree preserved x")
    logging.getLogger("tools.dashboard.server").info("startup phase: TOTAL 1.0ms")
    _flush()
    assert "GET /api/ping" in _read(tmp_path, "http")
    assert "fleet delta applied" in _read(tmp_path, "fleet")
    assert "worktree preserved" in _read(tmp_path, "monitors")
    catch_all = _read(tmp_path, "dashboard")
    assert "startup phase: TOTAL" in catch_all
    for routed in ("GET /api/ping", "fleet delta applied", "worktree preserved"):
        assert routed not in catch_all, "routed channels must not duplicate into the catch-all"
    assert "GET /api/ping" not in _read(tmp_path, "fleet")


def test_stdout_gets_warnings_only_from_everything(capsys, channels):
    logging.getLogger("tools.dashboard.server.http").info("quiet request line")
    logging.getLogger("tools.dashboard.server.http").warning("SLOW-REQUEST GET /x 200 1500ms client=-")
    logging.getLogger("tools.graph.db").info("quiet catch-all line")
    logging.getLogger("tools.graph.db").error("something broke")
    _flush()
    out = capsys.readouterr().out
    assert "quiet request line" not in out
    assert "quiet catch-all line" not in out
    assert "SLOW-REQUEST GET /x" in out
    assert "something broke" in out


def test_map_line_names_every_channel_and_reaches_stdout(capsys, channels):
    tmp_path, line = channels
    assert line.startswith("log channels: ")
    for channel in ("dashboard", *lc.CHANNELS):
        assert f"{channel}={tmp_path / (channel + '.log')}" in line
    assert "(INFO catch-all)" in line
    assert "stdout=WARNING+" in line
    assert "rotation 50MB x 5" in line
    _flush()
    assert line in capsys.readouterr().out            # discoverable from the stdout capture
    assert line in _read(tmp_path, "dashboard")       # and from the file


def test_rotation_parameters_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_LOG_CHANNELS", "on")
    monkeypatch.setenv("DASHBOARD_LOG_MAX_BYTES", str(7 * 1024 * 1024))
    monkeypatch.setenv("DASHBOARD_LOG_BACKUPS", "3")
    try:
        line = lc.configure()
        assert "rotation 7MB x 3" in line
        http = logging.getLogger("tools.dashboard.server.http")
        files = [h for h in http.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(files) == 1
        assert files[0].maxBytes == 7 * 1024 * 1024 and files[0].backupCount == 3
    finally:
        lc.configure_plain()


def test_reconfigure_is_idempotent(channels):
    tmp_path, _ = channels
    lc.configure()
    lc.configure()
    root = logging.getLogger()
    tagged = [h for h in root.handlers if getattr(h, lc._TAG, None)]
    assert len(tagged) == 2                          # one file, one stdout — not six
    http = logging.getLogger("tools.dashboard.server.http")
    assert len([h for h in http.handlers if getattr(h, lc._TAG, None)]) == 2
    assert http.propagate is False


def test_uvicorn_lifecycle_lines_are_copied_into_the_catch_all(channels):
    tmp_path, _ = channels
    uv = logging.getLogger("uvicorn.error")
    uv.info("hand-off complete: worker [1] -> [2]")
    _flush()
    assert "hand-off complete" in _read(tmp_path, "dashboard")


def test_off_mode_installs_nothing_but_a_plain_stream(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("DASHBOARD_LOG_CHANNELS", "off")
    assert lc.configure() is None
    assert not list(tmp_path.glob("*.log"))
    assert logging.getLogger("tools.dashboard.server.http").propagate is True
    assert logging.getLogger().level == logging.INFO


def test_mock_mode_defaults_off():
    assert lc.enabled({"DASHBOARD_MOCK": "1"}) is False
    assert lc.enabled({"DASHBOARD_MOCK": "1", "DASHBOARD_LOG_CHANNELS": "on"}) is True
    assert lc.enabled({}) is True
    assert lc.enabled({"DASHBOARD_LOG_CHANNELS": "false"}) is False
