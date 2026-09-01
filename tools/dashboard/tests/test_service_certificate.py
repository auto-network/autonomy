import json

from tools.dashboard import service_certificate as certs


def test_certbot_command_is_exact_scope_ephemeral_job(monkeypatch, tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setenv("AUTONOMY_CONTAINER_ROOT", str(tmp_path))
    command = certs._certbot_command(
        "persona-abc.serve.auto.network", "service-order", staging=True,
    )
    assert command[:8] == [
        "docker", "compose", "--project-name", "autonomy",
        "--project-directory", str(tmp_path), "-f", str(compose),
    ]
    assert "--rm" in command and "--no-deps" in command
    assert command.count("-d") == 2
    assert "persona-abc.serve.auto.network" in command
    assert "*.persona-abc.serve.auto.network" in command
    assert "--staging" in command
    assert not any("private" in token.lower() for token in command)


def test_compose_environment_derives_host_code_root_for_fresh_exec(monkeypatch):
    monkeypatch.delenv("AUTONOMY_HOST_ROOT", raising=False)
    monkeypatch.setenv("AUTONOMY_HOST_DATA_ROOT", "/opt/autonomy")
    assert certs._compose_environment()["AUTONOMY_HOST_ROOT"] == "/opt/autonomy/code"


def test_status_requires_pair_and_unexpired_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(certs, "GATEWAY_CERT", tmp_path / "tls.crt")
    monkeypatch.setattr(certs, "GATEWAY_KEY", tmp_path / "tls.key")
    monkeypatch.setattr(certs, "STATUS_PATH", tmp_path / "status.json")
    assert certs.status() == {"status": "missing"}
    certs.GATEWAY_CERT.write_text("cert")
    certs.GATEWAY_KEY.write_text("key")
    certs.STATUS_PATH.write_text(json.dumps({"not_after": 1, "apex": "x"}))
    assert certs.status()["status"] == "expired"


def test_atomic_copy_never_overwrites_until_complete(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    certs._atomic_copy(source, destination, 0o600)
    assert destination.read_bytes() == b"new"
    assert destination.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".destination.*.tmp"))
