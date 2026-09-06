"""The plugin background entrypoint + supervisor (auto-jjqct).

Pins the contract the manifest comment states: tasks are lifespan-owned
by the supervisor, started only while the plugin is enabled (live
reconcile, not start-once), restarted with backoff on crash, cancelled
cleanly on disable and shutdown — and a permanently crashing task never
kills anything but its own cycle.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tools.dashboard.plugin_api import background as bg
from tools.dashboard.plugin_api import loader


# ── Entrypoint resolution (loader-level) ─────────────────────────────

_STARTED = []


async def _fake_background_coro():  # pragma: no cover — never awaited here
    await asyncio.sleep(3600)


def _fake_background():
    return [_fake_background_coro]


def _min_manifest_yaml(plugin_id: str) -> str:
    return (
        f"id: {plugin_id}\n"
        "api_version: 1\n"
        "org: autonomy\n"
        f"paths: [/{plugin_id}]\n"
        "assets: {template: page.html, script: page.js}\n"
        f"nav: {{label: {plugin_id}}}\n"
        f"frontend: {{alpine_root: {plugin_id}Root}}\n"
    )


def _write_plugin(plugins_dir, plugin_id: str, yaml_text: str) -> None:
    plugin_dir = plugins_dir / plugin_id
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(yaml_text)
    (plugin_dir / "page.html").write_text("<div></div>")
    (plugin_dir / "page.js").write_text("// empty")


def test_loader_resolves_background_entrypoint(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    yaml_text = _min_manifest_yaml("bgplug") + (
        "entrypoints:\n"
        "  background: "
        "tools.dashboard.tests.test_plugin_background:_fake_background\n"
    )
    _write_plugin(plugins_dir, "bgplug", yaml_text)
    monkeypatch.setattr(loader, "_read_plugin_settings", lambda org=None: {})
    [plugin] = loader.load_enabled(plugins_dir=plugins_dir)
    assert plugin.background.__name__ == "_fake_background"


def test_loader_downgrades_non_callable_background(tmp_path, monkeypatch,
                                                   caplog):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    yaml_text = _min_manifest_yaml("badbg") + (
        "entrypoints:\n"
        "  background: tools.dashboard.tests.test_plugin_background:_STARTED\n"
    )
    _write_plugin(plugins_dir, "badbg", yaml_text)
    monkeypatch.setattr(loader, "_read_plugin_settings", lambda org=None: {})
    assert loader.load_enabled(plugins_dir=plugins_dir) == []
    assert "downgrading plugin 'badbg'" in caplog.text


# ── Supervisor behavior ──────────────────────────────────────────────


def _plugin(plugin_id: str, factories):
    return SimpleNamespace(id=plugin_id, background=lambda: factories)


def _run(coro):
    return asyncio.run(coro)


def test_disabled_plugin_starts_no_tasks():
    async def scenario():
        ran = asyncio.Event()

        async def work():
            ran.set()
            await asyncio.sleep(3600)

        supervisor = bg.PluginBackgroundSupervisor(
            [_plugin("dormant", [work])], enabled=lambda: {"dormant": False})
        await supervisor.start()
        await asyncio.sleep(0.05)
        assert not ran.is_set()
        assert supervisor._tasks == {}
        await supervisor.stop()

    _run(scenario())


def test_enabled_plugin_runs_and_shutdown_cancels():
    async def scenario():
        ran = asyncio.Event()
        cancelled = asyncio.Event()

        async def work():
            ran.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        supervisor = bg.PluginBackgroundSupervisor(
            [_plugin("live", [work])], enabled=lambda: {"live": True})
        await supervisor.start()
        await asyncio.wait_for(ran.wait(), 2)
        await supervisor.stop()
        assert cancelled.is_set()

    _run(scenario())


def test_live_toggle_reconciles_both_ways():
    async def scenario():
        state = {"toggle": True}
        runs = []

        async def work():
            runs.append("start")
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                runs.append("cancel")
                raise

        supervisor = bg.PluginBackgroundSupervisor(
            [_plugin("toggle", [work])],
            enabled=lambda: dict(state, toggle=state["toggle"]))
        await supervisor.start()
        await asyncio.sleep(0.05)
        assert runs == ["start"]
        state["toggle"] = False
        supervisor.reconcile_once()
        await asyncio.sleep(0.05)
        assert runs == ["start", "cancel"]
        state["toggle"] = True
        supervisor.reconcile_once()
        await asyncio.sleep(0.05)
        assert runs == ["start", "cancel", "start"]
        await supervisor.stop()

    _run(scenario())


def test_crashing_task_restarts_with_backoff_not_spin(monkeypatch):
    async def scenario():
        monkeypatch.setattr(bg, "BACKOFF_BASE_S", 0.05)
        attempts = []

        async def crash():
            attempts.append(1)
            raise RuntimeError("boom")

        supervisor = bg.PluginBackgroundSupervisor(
            [_plugin("crashy", [crash])], enabled=lambda: {"crashy": True})
        await supervisor.start()
        await asyncio.sleep(0.3)
        await supervisor.stop()
        # backoff doubles from 0.05: restarts are bounded, not a spin,
        # and the supervisor itself survived every crash.
        assert 2 <= len(attempts) <= 6

    _run(scenario())


def test_faulting_background_callable_is_contained():
    async def scenario():
        bad = SimpleNamespace(
            id="faulty",
            background=lambda: (_ for _ in ()).throw(RuntimeError("bad")))
        supervisor = bg.PluginBackgroundSupervisor(
            [bad], enabled=lambda: {"faulty": True})
        await supervisor.start()  # must not raise
        assert supervisor._tasks == {}
        await supervisor.stop()

    _run(scenario())


def test_stubborn_task_is_abandoned_within_grace(monkeypatch):
    async def scenario():
        monkeypatch.setattr(bg, "SHUTDOWN_GRACE_S", 0.1)

        async def stubborn():
            # Ignores the supervisor's cancellation (the pathological
            # case under test) but honors the SECOND one — asyncio.run's
            # own loop shutdown must still be able to end it, or the
            # test itself hangs the runner exactly the way the
            # supervisor refuses to.
            ignored = False
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    if ignored:
                        raise
                    ignored = True

        supervisor = bg.PluginBackgroundSupervisor(
            [_plugin("stubborn", [stubborn])],
            enabled=lambda: {"stubborn": True})
        await supervisor.start()
        await asyncio.sleep(0.05)
        await asyncio.wait_for(supervisor.stop(), 5)  # bounded, not wedged

    _run(scenario())
