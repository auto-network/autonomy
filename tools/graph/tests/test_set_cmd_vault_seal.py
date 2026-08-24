"""Secret-safe CLI input for secured Setting writes."""

from __future__ import annotations

import argparse
import io

import pytest

from tools.graph import set_cmd


def _args(**overrides):
    values = {
        "key": "mac.ssh",
        "policy_class": "class-1",
        "secret_file": None,
        "secret_fd": None,
        "secret_prompt": False,
        "org": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_multiline_private_value_is_byte_identical_and_never_printed(
    tmp_path, monkeypatch, capsys,
):
    fake = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "ZmFrZS1ub3QtYS1yZWFsLWtleQ==\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
    )
    source = tmp_path / "fake-key"
    source.write_text(fake)
    calls = []

    class Client:
        def add_setting(self, set_id, revision, key, payload, **kwargs):
            calls.append((set_id, revision, key, payload["value"], kwargs))
            return "setting-123456789"

    monkeypatch.setattr(set_cmd, "get_client", lambda: Client())
    set_cmd.cmd_set_seal(_args(secret_file=str(source)))

    output = capsys.readouterr().out
    assert fake not in output
    assert "setting-123" in output
    assert calls == [(
        "autonomy.vault.secured", 1, "mac.ssh", fake,
        {
            "state": "raw",
            "org": set_cmd._org(_args()),
            "vault_policy_class_id": "class-1",
        },
    )]


def test_secret_buffer_is_zeroed_even_when_the_write_fails(monkeypatch):
    secret = bytearray(b"fake-private-key")

    class Client:
        def add_setting(self, *_args, **_kwargs):
            raise RuntimeError("synthetic refusal")

    monkeypatch.setattr(set_cmd, "_read_secret_bytes", lambda _args: secret)
    monkeypatch.setattr(set_cmd, "get_client", lambda: Client())
    with pytest.raises(RuntimeError, match="synthetic"):
        set_cmd.cmd_set_seal(_args())
    assert secret == bytearray(len(secret))


def test_interactive_mode_uses_hidden_prompt(monkeypatch):
    monkeypatch.setattr(set_cmd.getpass, "getpass", lambda _label: "one-line-secret")
    assert set_cmd._read_secret_bytes(_args(secret_prompt=True)) == bytearray(
        b"one-line-secret"
    )


def test_secret_reader_bounds_input_before_rejecting_it(capsys):
    class RecordingStream(io.BytesIO):
        requested = None

        def read(self, size=-1):
            self.requested = size
            return super().read(size)

    stream = RecordingStream(b"x" * (set_cmd._MAX_SECRET_BYTES + 2))
    with pytest.raises(SystemExit):
        set_cmd._read_limited_secret(stream)
    assert stream.requested == set_cmd._MAX_SECRET_BYTES + 1
    assert "exceeds" in capsys.readouterr().err
