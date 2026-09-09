import json
import sys

import pytest

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


@pytest.mark.asyncio
async def test_production_issue_refuses_cold_vault_before_contacting_acme(monkeypatch):
    monkeypatch.setattr(
        certs.settings_ops, "personal_delegate_audited_is_warm", lambda: False
    )
    calls = []

    async def obtain(*_args, **_kwargs):
        calls.append(True)

    monkeypatch.setattr(certs, "obtain", obtain)

    with pytest.raises(certs.ServiceCertificateError, match="vault is locked"):
        await certs.issue("anchore", "persona-abc", staging=False)

    assert calls == []


def test_failed_certbot_run_surfaces_real_cause_and_preserves_attempt(monkeypatch, tmp_path):
    """A failing certbot job must (a) name certbot's own complaint, which it
    writes to STDOUT, not Compose's progress line or the generic epilogue on
    STDERR, and (b) keep the attempt (both streams + certbot's log dir) in the
    data root BEFORE the ephemeral ACME root is wiped (auto-0iwrd)."""
    import asyncio
    from tools.dashboard import service_certificate as certs

    acme_root = tmp_path / "acme"
    monkeypatch.setattr(certs, "ACME_ROOT", acme_root)
    data_root = tmp_path / "data"
    data_root.mkdir()
    import tools.data_paths as data_paths
    monkeypatch.setattr(data_paths, "resolve_data_root", lambda: data_root)
    monkeypatch.setattr(certs, "load_dns01_client", lambda org: object())
    monkeypatch.setattr(certs, "_restore_acme_bundle", lambda: None)
    monkeypatch.setattr(certs, "_compose_environment", lambda: {})
    monkeypatch.setattr(certs, "_certbot_command", lambda *a, **k: ["certbot"])
    monkeypatch.setattr(certs, "_dns01_preflight", lambda client, apex, wait=None: None)

    class _Server:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            # certbot writes its log while the responder is up
            (acme_root / "logs").mkdir(parents=True, exist_ok=True)
            (acme_root / "logs" / "letsencrypt.log").write_text("hook says: refused\n")
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(certs, "Dns01HookServer", _Server)

    class _Proc:
        returncode = 1

        async def communicate(self):
            stdout = (
                b"Hook '--manual-auth-hook' for example reported error code 1\n"
                b"Hook '--manual-auth-hook' ran with error output:\n"
                b" autonomy DNS-01 hook unavailable: FileNotFoundError: [Errno 2] (socket '/run/autonomy-acme/dns01.sock', action present)\n"
            )
            stderr = (
                b"\x1b[?25l Container autonomy-service-certbot-run-abc Creating\r\n"
                b" Container autonomy-service-certbot-run-abc Created\n"
                b"Ask for help or search for solutions at https://community.letsencrypt.org.\n"
                b"See the logfile /run/autonomy-acme/logs/letsencrypt.log for more details.\n"
            )
            return stdout, stderr

    async def fake_exec(*a, **k):
        return _Proc()

    monkeypatch.setattr(certs.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(certs.ServiceCertificateError) as excinfo:
        asyncio.run(certs.obtain("autonomy", "jeremy-77827e972ba4c37d4215"))
    message = str(excinfo.value)
    assert "hook unavailable: FileNotFoundError" in message
    assert "Creating" not in message and "See the logfile" not in message
    attempts = list((data_root / "service-certs" / "attempts").iterdir())
    assert len(attempts) == 1 and attempts[0].name.endswith("-jeremy-77827e972ba4c37d4215")
    assert "attempt kept at" in message and attempts[0].name in message
    assert (attempts[0] / "output.txt").read_bytes().count(b"--- stdout ---") == 1
    assert (attempts[0] / "logs" / "letsencrypt.log").read_text() == "hook says: refused\n"
    assert not (acme_root / "logs").exists()  # ephemeral root still wiped


class _FakeDns01Client:
    def __init__(self, bound_label):
        self.bound_label = bound_label
        self.presented = []
        self.cleaned = []

    def present(self, order, value, *, ttl=60, lifetime=600):
        self.presented.append((order, value))
        return {"name": f"_acme-challenge.{self.bound_label}.serve.auto.network", "expires_at": 1}

    def cleanup(self, order, value):
        self.cleaned.append((order, value))


def test_dns01_preflight_refuses_a_label_the_relay_did_not_bind_before_any_acme_order(monkeypatch):
    """The relay derives the challenge name from the persona's bound serving
    label. If that is not the apex we are about to order for, ACME would look
    in the wrong place five times and rate-limit us (2026-09-07). Fail here,
    name both labels, place no order, and always clean the canary."""
    from tools.dashboard import service_certificate as certs
    client = _FakeDns01Client("persona-77827e972ba4c37d4215")
    waited = []
    with pytest.raises(certs.ServiceCertificateError) as excinfo:
        certs._dns01_preflight(client, "jeremy-77827e972ba4c37d4215.serve.auto.network",
                               wait=lambda name, value: waited.append(name))
    msg = str(excinfo.value)
    assert "bound serving label is 'persona-77827e972ba4c37d4215'" in msg
    assert "'jeremy-77827e972ba4c37d4215'" in msg and "No ACME order was placed" in msg
    assert waited == []  # never waited on a name ACME will not query
    assert len(client.cleaned) == 1 and client.cleaned[0] == client.presented[0]


def test_dns01_preflight_passes_when_our_nameservers_answer_the_right_name():
    from tools.dashboard import service_certificate as certs
    client = _FakeDns01Client("persona-77827e972ba4c37d4215")
    waited = []
    certs._dns01_preflight(client, "persona-77827e972ba4c37d4215.serve.auto.network",
                           wait=lambda name, value: waited.append((name, value)))
    assert waited[0][0] == "_acme-challenge.persona-77827e972ba4c37d4215.serve.auto.network"
    assert waited[0][1] == client.presented[0][1]
    assert client.cleaned == client.presented


def test_obtain_runs_the_preflight_before_starting_certbot(monkeypatch, tmp_path):
    import asyncio
    from tools.dashboard import service_certificate as certs
    monkeypatch.setattr(certs, "ACME_ROOT", tmp_path / "acme")
    monkeypatch.setattr(certs, "load_dns01_client", lambda org: _FakeDns01Client("persona-77827e972ba4c37d4215"))
    monkeypatch.setattr(certs, "_restore_acme_bundle", lambda: None)
    started = []

    async def fake_exec(*a, **k):
        started.append(a)
        raise AssertionError("certbot must not start after a failed preflight")

    monkeypatch.setattr(certs.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(certs.ServiceCertificateError, match="DNS-01 preflight"):
        asyncio.run(certs.obtain("autonomy", "jeremy-77827e972ba4c37d4215"))
    assert started == []


# ── The certificate record is a FLEET fact (auto-7jhm3) ─────────────────
#
# The bundle has always gone to the audited vault at @home("personal"), so it
# already replicated. The POINTER row was @home("machine"), so a second
# serving machine looked up the identity, found nothing, concluded no
# certificate existed and went to ACME for a duplicate — with the good bundle
# sitting in the shared vault, unreachable because nothing on that machine
# knew its key. Measured 2026-09-09: sjc-2 wanted three identities including
# the same wildcard zone home already held.


def _record(org="autonomy", identity="autonomy.taplink.net", serial="ab"):
    return {
        "org": org, "zone": identity, "apex": identity,
        "sans": [identity, f"*.{identity}"],
        "not_before": 1, "not_after": 2 ** 31, "serial": serial,
        "vault_key": f"vault/{org}/{identity}/{serial}", "staging": False,
        "activated_at": 1,
    }


def test_another_machines_certificate_is_found_instead_of_reissued(monkeypatch):
    """THE ONE THAT MATTERS. A machine that has never issued anything must
    find the fleet's record and materialize from the shared vault. If this
    returns None the manager orders a duplicate from ACME, which is the
    rate-limit burn the operator set this as a precondition to prevent."""
    reads = {}

    def _read(_set_id, key, *, org=None, peers=None):
        reads[org] = key
        return {"payload": _record()} if org is None else None

    monkeypatch.setattr(certs.settings_ops, "read_set_key", _read)

    got = certs.certificate_metadata("autonomy", "autonomy.taplink.net")

    assert got is not None and got["serial"] == "ab"
    assert None in reads, "the fleet store must be consulted"


def test_a_legacy_machine_row_is_promoted_so_other_machines_can_see_it(
    monkeypatch,
):
    """Re-homing strands existing rows, and here that is ACTIVELY harmful: a
    machine that could no longer see its own certificate would issue a
    duplicate. The legacy row is therefore read AND promoted, making the move
    self-healing and one-way."""
    def _read(_set_id, key, *, org=None, peers=None):
        return None if org is None else {"payload": _record(serial="cd")}

    writes = []
    monkeypatch.setattr(certs.settings_ops, "read_set_key", _read)
    monkeypatch.setattr(
        certs.settings_ops, "write_by_key",
        lambda *a, **kw: writes.append({"org": kw.get("org"), "payload": a[3]}))

    got = certs.certificate_metadata("autonomy", "autonomy.taplink.net")

    assert got["serial"] == "cd", "the legacy row must still answer"
    assert len(writes) == 1
    assert writes[0]["org"] is None, "promotion must write to the FLEET store"
    assert writes[0]["payload"]["serial"] == "cd"


def test_a_failed_promotion_still_serves_this_machine(monkeypatch):
    """Promotion is best-effort. If it fails this machine must still get its
    certificate — degrading sharing is acceptable, dropping a certificate this
    machine already holds is not."""
    def _read(_set_id, key, *, org=None, peers=None):
        return None if org is None else {"payload": _record(serial="ef")}

    def _explode(*_a, **_kw):
        raise RuntimeError("fleet store unavailable")

    monkeypatch.setattr(certs.settings_ops, "read_set_key", _read)
    monkeypatch.setattr(certs.settings_ops, "write_by_key", _explode)

    assert certs.certificate_metadata("autonomy", "x.example.com")["serial"] == "ef"


def test_no_record_anywhere_still_returns_none(monkeypatch):
    """NEGATIVE CONTROL: a genuinely new identity must still report absence,
    or nothing would ever be issued at all."""
    monkeypatch.setattr(
        certs.settings_ops, "read_set_key",
        lambda *a, **kw: None)

    assert certs.certificate_metadata("autonomy", "new.example.com") is None
