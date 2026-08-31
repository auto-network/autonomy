from tools.network.acceptance import service_gateway


def test_acceptance_certificate_uses_full_hostname_only_in_san():
    hostname = (
        "phase1c-gateway-live."
        "persona-77827e972ba4c37d4215.serve.auto.network"
    )

    command = service_gateway.acceptance_certificate_command(hostname)

    subject = command[command.index("-subj") + 1]
    san = command[command.index("-addext") + 1]
    assert subject == "/CN=Autonomy Service Gateway Acceptance"
    assert san == f"subjectAltName=DNS:{hostname}"
    assert hostname not in subject


def test_dashboard_exec_preserves_the_named_users_supplementary_groups(monkeypatch):
    seen = {}

    def fake_exec(container, argv, *, timeout=30.0, user=None):
        seen.update(container=container, argv=argv, timeout=timeout, user=user)
        return "ok"

    monkeypatch.setattr(service_gateway, "_docker_exec", fake_exec)

    result = service_gateway._dashboard_exec("dashboard", ["docker", "inspect"])

    assert result == "ok"
    assert seen == {
        "container": "dashboard",
        "argv": ["docker", "inspect"],
        "timeout": 30.0,
        "user": "autonomy",
    }
