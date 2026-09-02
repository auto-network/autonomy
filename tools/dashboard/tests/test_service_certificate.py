import json
import sys

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


def test_production_lineage_name_is_stable_and_renew_uses_certbot_renew(
    monkeypatch, tmp_path
):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setenv("AUTONOMY_CONTAINER_ROOT", str(tmp_path))
    first = certs.certificate_name("anchore", "persona-abc")
    second = certs.certificate_name("anchore", "persona-abc")
    assert first == second
    command = certs._certbot_command(
        "persona-abc.serve.auto.network", first, staging=False, renew=True
    )
    service_index = max(i for i, token in enumerate(command) if token == "service-certbot")
    assert command[service_index + 1] == "renew"
    assert command[command.index("--cert-name") + 1] == first
    assert "--no-random-sleep-on-renew" in command
    assert "-d" not in command


def test_acme_bundle_round_trip_and_prunes_old_lineage_generations(
    monkeypatch, tmp_path
):
    root = tmp_path / "acme"
    archive = root / "config" / "archive" / "service-lineage"
    live = root / "config" / "live" / "service-lineage"
    account = root / "config" / "accounts" / "account-id"
    for directory in (archive, live, account):
        directory.mkdir(parents=True)
    (account / "private_key.json").write_text("account-secret")
    for generation in (1, 2, 3):
        for kind in ("cert", "chain", "fullchain", "privkey"):
            (archive / f"{kind}{generation}.pem").write_text(
                f"{kind}-{generation}"
            )
    (live / "cert.pem").symlink_to("../../archive/service-lineage/cert3.pem")
    monkeypatch.setattr(certs, "ACME_ROOT", root)
    stored = {}
    monkeypatch.setattr(
        certs.settings_ops,
        "write_by_key",
        lambda _set, _rev, key, payload, **_kwargs: stored.update(
            {"key": key, "payload": payload}
        ),
    )

    certs._write_acme_bundle()

    assert stored["key"] == certs.ACME_VAULT_KEY
    assert not list(archive.glob("*1.pem"))
    assert len(list(archive.glob("*.pem"))) == 8
    monkeypatch.setattr(
        certs.settings_ops,
        "read_set_key",
        lambda *_args, **_kwargs: {"payload": stored["payload"]},
    )
    import shutil
    shutil.rmtree(root / "config")

    assert certs._restore_acme_bundle() is True
    assert (account / "private_key.json").read_text() == "account-secret"
    assert (live / "cert.pem").is_symlink()


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


def test_staging_cli_proves_issuance_without_activation(monkeypatch, capsys):
    calls = []

    async def obtain(org, persona, *, staging):
        calls.append(("obtain", org, persona, staging))
        return {"staging": True, "serial": "abc"}, b"cert", b"key"

    async def issue(*_args, **_kwargs):
        calls.append(("issue",))
        return {}

    monkeypatch.setattr(certs, "obtain", obtain)
    monkeypatch.setattr(certs, "issue", issue)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "service_certificate",
            "--org",
            "anchore",
            "--persona-label",
            "persona-abc",
            "--staging",
        ],
    )

    certs.main()

    assert calls == [("obtain", "anchore", "persona-abc", True)]
    assert json.loads(capsys.readouterr().out)["staging"] is True
