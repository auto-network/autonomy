"""Dynamic per-plugin CLI registration (tools/graph/plugin_cli.py).

Covers the substrate contract end to end against a temp plugins dir:
discovery reads only manifests; enablement mirrors the dashboard's rules
on the ``dashboard.plugin`` Setting (payload wins, then manifest
``default_enabled``, then the underscore convention); the optional
``cli_workspaces`` allow-list gates on ``$AUTONOMY_WORKSPACE``; a broken
plugin is contained; a verb collision is contained; and the whole
mechanism is generic — two toy plugins mount independently.
"""
from __future__ import annotations

import argparse
import os
import sys
import textwrap
from pathlib import Path

import pytest

from tools.graph.plugin_cli import (
    discover_cli_plugins,
    register_plugin_commands,
)


def _write_plugin(root: Path, dirname: str, *, pid: str | None = None,
                  cli: str | None = None, org: str = "autonomy",
                  default_enabled: str | None = None) -> Path:
    d = root / dirname
    d.mkdir(parents=True)
    lines = [f"id: {pid or dirname}", "api_version: 1", f"org: {org}"]
    if default_enabled is not None:
        lines.append(f"default_enabled: {default_enabled}")
    if cli is not None:
        lines.append("entrypoints:")
        lines.append(f"  cli: {cli}")
    (d / "plugin.yaml").write_text("\n".join(lines) + "\n")
    return d


def _toy_module(tmp_path: Path, name: str, body: str) -> str:
    """Write an importable module and return its import path."""
    mod = tmp_path / f"{name}.py"
    mod.write_text(textwrap.dedent(body))
    if str(tmp_path) not in sys.path:
        sys.path.insert(0, str(tmp_path))
    return name


@pytest.fixture()
def sub():
    parser = argparse.ArgumentParser(prog="graph")
    return parser.add_subparsers(dest="cmd")


def test_discovery_reads_only_cli_manifests(tmp_path):
    _write_plugin(tmp_path, "alpha", cli="alpha_cli:register")
    _write_plugin(tmp_path, "beta")                      # no cli entrypoint
    (tmp_path / "stray").mkdir()                         # no manifest at all
    _write_plugin(tmp_path, "broken", cli="not-a-spec")  # missing ':'
    found = discover_cli_plugins(tmp_path)
    assert [p["id"] for p in found] == ["alpha"]
    assert found[0]["spec"] == "alpha_cli:register"
    assert found[0]["org"] == "autonomy"


def test_enabled_plugin_mounts_and_disabled_contributes_nothing(tmp_path, sub):
    _toy_module(tmp_path, "toy_cli", """
        def register(sub):
            q = sub.add_parser("toyverb", help="toy")
            q.set_defaults(func=lambda a: "toy ran")
    """)
    _write_plugin(tmp_path, "toy", cli="toy_cli:register")

    mounted = register_plugin_commands(
        sub, tmp_path, payload_reader=lambda org: {})
    assert mounted == ["toy"]
    assert "toyverb" in sub.choices

    # Disabling through the Setting removes the verb on the next build.
    sub2 = argparse.ArgumentParser(prog="graph").add_subparsers(dest="cmd")
    mounted2 = register_plugin_commands(
        sub2, tmp_path,
        payload_reader=lambda org: {"toy": {"enabled": False}})
    assert mounted2 == []
    assert "toyverb" not in (sub2.choices or {})


def test_bootstrap_defaults_without_setting_row(tmp_path, sub):
    _toy_module(tmp_path, "dormant_cli", """
        def register(sub):
            sub.add_parser("dormantverb")
    """)
    _toy_module(tmp_path, "optout_cli", """
        def register(sub):
            sub.add_parser("optoutverb")
    """)
    # Underscore dirs stay dormant; explicit default_enabled wins over it.
    _write_plugin(tmp_path, "_sample", pid="sample", cli="dormant_cli:register")
    _write_plugin(tmp_path, "optout", cli="optout_cli:register",
                  default_enabled="false")
    mounted = register_plugin_commands(
        sub, tmp_path, payload_reader=lambda org: {})
    assert mounted == []


def test_workspace_allow_list_gates_mounting(tmp_path, sub, monkeypatch):
    _toy_module(tmp_path, "ws_cli", """
        def register(sub):
            sub.add_parser("wsverb")
    """)
    _write_plugin(tmp_path, "wsplug", cli="ws_cli:register")
    payloads = {"wsplug": {"enabled": True,
                           "cli_workspaces": ["autonomy-developer"]}}

    monkeypatch.delenv("AUTONOMY_WORKSPACE", raising=False)
    assert register_plugin_commands(
        sub, tmp_path, payload_reader=lambda org: payloads) == []

    monkeypatch.setenv("AUTONOMY_WORKSPACE", "autonomy-developer")
    sub2 = argparse.ArgumentParser(prog="graph").add_subparsers(dest="cmd")
    assert register_plugin_commands(
        sub2, tmp_path, payload_reader=lambda org: payloads) == ["wsplug"]


def test_broken_plugin_and_collision_are_contained(tmp_path, sub, capsys):
    _toy_module(tmp_path, "boom_cli", """
        def register(sub):
            raise RuntimeError("boom")
    """)
    _toy_module(tmp_path, "good_cli", """
        def register(sub):
            sub.add_parser("goodverb")
    """)
    _toy_module(tmp_path, "clash_cli", """
        def register(sub):
            sub.add_parser("goodverb")   # collides with good_cli's verb
    """)
    _write_plugin(tmp_path, "aboom", cli="boom_cli:register")
    _write_plugin(tmp_path, "bgood", cli="good_cli:register")
    _write_plugin(tmp_path, "cclash", cli="clash_cli:register")

    mounted = register_plugin_commands(
        sub, tmp_path, payload_reader=lambda org: {})
    assert mounted == ["bgood"]
    assert "goodverb" in sub.choices
    err = capsys.readouterr().err
    assert "aboom" in err and "cclash" in err


def test_http_fallback_when_org_db_absent(monkeypatch):
    """Containers hold no org database: the local Setting read raises and
    _read_payloads falls back to the dashboard's enabled-plugin list."""
    from tools.graph import ops as graph_ops
    from tools.graph import plugin_cli

    def raising_read_set(set_id, *, org=None, peers=None):
        raise RuntimeError("no org db in container")

    monkeypatch.setattr(graph_ops, "read_set", raising_read_set)
    monkeypatch.setattr(plugin_cli, "_http_enabled_ids",
                        lambda: {"fbplug": {"enabled": True}})
    assert plugin_cli._read_payloads(None) == {"fbplug": {"enabled": True}}

    # HTTP also unreachable -> empty map, never an exception
    monkeypatch.setattr(plugin_cli, "_http_enabled_ids", lambda: None)
    assert plugin_cli._read_payloads(None) == {}


def test_two_plugins_mount_independently(tmp_path, sub):
    _toy_module(tmp_path, "one_cli", """
        def register(sub):
            sub.add_parser("oneverb")
    """)
    _toy_module(tmp_path, "two_cli", """
        def register(sub):
            sub.add_parser("twoverb")
    """)
    _write_plugin(tmp_path, "one", cli="one_cli:register")
    _write_plugin(tmp_path, "two", cli="two_cli:register")
    mounted = register_plugin_commands(
        sub, tmp_path, payload_reader=lambda org: {})
    assert sorted(mounted) == ["one", "two"]
    assert {"oneverb", "twoverb"} <= set(sub.choices)
