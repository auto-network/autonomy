from tools.network.acceptance import (
    service_certificate_rotation,
    service_gateway,
    service_gateway_lifecycle,
)


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


def test_header_lookup_is_case_insensitive_for_http2_curl_output():
    headers = "HTTP/2 302\r\nlocation: /final\r\nvia: 1.1 Caddy\r\n\r\n"

    assert service_gateway._header_value(headers, "Location") == "/final"


def test_lifecycle_p95_uses_the_nearest_rank():
    assert service_gateway_lifecycle._p95([float(value) for value in range(1, 21)]) == 19


def test_lifecycle_status_observation_retains_the_runtime_error():
    observations = []

    current = service_gateway_lifecycle._observe_gateway_status(
        lambda: {
            "state": "backoff",
            "reason": "backoff",
            "error": "start Service gateway failed (1): bind source missing",
        },
        observations,
        now=lambda: 123.5,
    )

    assert current["state"] == "backoff"
    assert observations == [
        {
            "at": 123.5,
            "gateway": current,
        }
    ]


def test_lifecycle_status_observation_retains_transport_failures():
    observations = []

    current = service_gateway_lifecycle._observe_gateway_status(
        lambda: (_ for _ in ()).throw(RuntimeError("dashboard restarting")),
        observations,
        now=lambda: 456.0,
    )

    assert current == {}
    assert observations == [
        {
            "at": 456.0,
            "transport_error": "dashboard restarting",
        }
    ]


def test_rotation_driver_staging_command_cannot_activate():
    command = service_certificate_rotation._certificate_command(
        "anchore", "persona-abc", staging=True
    )

    assert command == [
        "python3",
        "-m",
        "tools.dashboard.service_certificate",
        "--org",
        "anchore",
        "--persona-label",
        "persona-abc",
        "--staging",
    ]
