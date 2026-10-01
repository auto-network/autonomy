"""auto-1qj12: an organization member's launch on a runner carries every
secret it needs (graph://7eb29bc8-31a v6 §9.8, D9). The launcher then reads
nothing from this machine -- no vault, no account picker, no host
environment -- and no carried value becomes a ``docker run -e`` argument: each
is delivered into the container's private ramfs and an environment variable
is exported from its file by the argv prefix."""

from __future__ import annotations

import pytest

from agents import session_launcher
from agents.session_launcher import CarriedCredentials
from agents.tests.test_session_launcher import (  # noqa: F401 -- fixtures
    _run,
    captured_run,
    fake_crosstalk,
    neutralize_launch_preflight,
    platform_snapshot,
    signin_deliveries,
)
from types import SimpleNamespace

BUNDLE = session_launcher.CLAUDE_BUNDLE_FILENAME


@pytest.fixture
def no_local_secrets(monkeypatch):
    """Every way this machine could supply a secret fails the test."""
    def forbidden(*_a, **_k):
        raise AssertionError("a carried launch read this machine's secrets")

    for name in ("_resolve_credentials", "_resolve_credentials_via_substrate",
                 "_resolve_credential", "_pick_account", "_signin_payloads",
                 "_grok_vault_key_available"):
        monkeypatch.setattr(session_launcher, name, forbidden)


def _carried(**kw):
    return CarriedCredentials(
        kw.get("credentials", {"github.token": "ghp_CARRIED"}),
        kw.get("env", {}),
        kw.get("signins", {BUNDLE: b'{"claudeAiOauth": {}}'}))


def test_a_carried_launch_reads_nothing_local_and_passes_no_secret_as_an_argument(
        tmp_path, fake_crosstalk, captured_run, signin_deliveries, no_local_secrets):
    _run(name="auto-m", output_dir=str(tmp_path / "run"), harness="claude",
         extra_env={"GH_TOKEN": "credential:github.token", "PLAIN": "x"},
         carried=_carried(env={"FROM_HOME": "home-value"}))
    cmd = captured_run[0]
    joined = " ".join(cmd)
    assert "ghp_CARRIED" not in joined and "home-value" not in joined
    env_args = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-e"]
    assert not any(a.startswith(("GH_TOKEN=", "FROM_HOME=", "CLAUDE_CODE_OAUTH_TOKEN="))
                   for a in env_args)
    assert "PLAIN=x" in joined                      # literals still pass as -e
    assert signin_deliveries["now"] == [
        ("auto-m", sorted([BUNDLE, "env.GH_TOKEN", "env.FROM_HOME"]))]
    script = cmd[cmd.index("session-widgets") + 3]
    assert 'GH_TOKEN="$(cat "$f")"; export GH_TOKEN' in script
    assert 'FROM_HOME="$(cat "$f")"; export FROM_HOME' in script


def test_the_delivered_files_carry_the_values(tmp_path, fake_crosstalk, captured_run,
                                              monkeypatch, no_local_secrets):
    got = {}
    monkeypatch.setattr(session_launcher, "deliver_signins",
                        lambda name, payloads, **_k: got.update(payloads) or [])
    _run(name="auto-v", output_dir=str(tmp_path / "run"), harness="claude",
         extra_env={"GH_TOKEN": "credential:github.token"},
         carried=_carried(env={"CLAUDE_CODE_OAUTH_TOKEN": "tok-1"}, signins={}))
    assert got == {"env.GH_TOKEN": b"ghp_CARRIED", "env.CLAUDE_CODE_OAUTH_TOKEN": b"tok-1"}


def test_a_credential_the_workspace_names_but_was_not_carried_refuses(
        tmp_path, fake_crosstalk, captured_run, no_local_secrets, capsys):
    out = _run(output_dir=str(tmp_path / "run"), harness="claude",
               extra_env={"GH_TOKEN": "credential:github.token"},
               carried=_carried(credentials={}))
    assert out is None and captured_run == []
    assert "credential-refused" in capsys.readouterr().err


@pytest.mark.parametrize("binding, secret_files", [
    ({"TOKEN": "host:RUNNER_TOKEN"}, {}),
    ({"TOKEN": "file:/etc/runner:TOKEN"}, {}),
    ({}, {"/run/x": "/home/owner/secret"}),
])
def test_a_capability_reading_the_runner_refuses(tmp_path, fake_crosstalk, captured_run,
                                                 no_local_secrets, binding, secret_files):
    cap = SimpleNamespace(implementation="cap-x", env_bindings=binding,
                          secret_file_bindings=secret_files)
    out = _run(output_dir=str(tmp_path / "run"), harness="claude",
               capabilities=(cap,), carried=_carried())
    assert out is None and captured_run == []


def test_a_sign_in_or_name_the_launcher_does_not_know_refuses(
        tmp_path, fake_crosstalk, captured_run, no_local_secrets):
    assert _run(output_dir=str(tmp_path / "run"), harness="claude",
                carried=_carried(signins={"../../etc/x": b"1"})) is None
    assert _run(output_dir=str(tmp_path / "run"), harness="claude",
                carried=_carried(env={"BAD NAME": "1"})) is None
    assert captured_run == []


def test_requirements_name_credentials_host_env_and_refusals(tmp_path):
    cap = SimpleNamespace(implementation="cap-y", secret_file_bindings={},
                          env_bindings={"A": "credential:cap.key", "B": "host:X", "C": "literal"})
    keys, host, refusals = session_launcher.carried_requirements(
        {"GH_TOKEN": "credential:github.token", "P": "plain"}, ["OPENROUTER_API_KEY"], (cap,))
    assert keys == {"github.token", "cap.key"}
    assert host == ["OPENROUTER_API_KEY"]
    assert refusals == ["capability cap-y: B is read from the runner's own environment"]


def test_carried_repr_names_no_value():
    text = repr(_carried(env={"X": "secret-value"}))
    assert "ghp_CARRIED" not in text and "secret-value" not in text
