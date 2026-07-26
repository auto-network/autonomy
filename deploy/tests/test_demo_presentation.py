"""Presentation-layer contracts over the real B6 phase interface."""

from __future__ import annotations

import json
import ssl
import subprocess
from pathlib import Path

import pytest

from deploy import demo
from deploy.demo import Demo, DemoConfig
from deploy.harness.driver import CommandResult, HarnessConfig, HarnessError
from deploy.harness.topology import TopologyConfig, compose_model


IMAGE = "registry.test/autonomy-node@sha256:" + "a" * 64
PROJECT = "autonomy-harness-demo-test"


class TimelineRunner:
    def __init__(self, timeline: list[str], *, fail_verify: bool = False):
        self.timeline = timeline
        self.fail_verify = fail_verify
        self.calls: list[dict] = []

    def run(self, argv, *, env=None, input_text=None, check=True):
        self.calls.append({"argv": list(argv), "env": dict(env or {})})
        if argv[:2] == ["bash", str(demo.VERIFY_IMAGE)]:
            self.timeline.append("verify")
            if self.fail_verify:
                raise HarnessError("signature refused")
        return CommandResult()


class VisibleHarness:
    def __init__(self, tmp_path: Path, timeline: list[str]):
        self.config = HarnessConfig(
            project=PROJECT,
            nodes=3,
            relay_port=24477,
            node_port_base=24880,
            image=IMAGE,
            artifacts_dir=tmp_path,
            build=False,
            secure_dashboard=True,
        )
        self.run_dir = tmp_path / PROJECT
        self.compose_file = self.run_dir / "compose.json"
        self.log_file = self.run_dir / "compose.log"
        self._found = {"content_id": "content-id"}
        self._content_channel_token = "public-content-channel"
        self._claim_token = "crown-jewel-claim-bearer"
        self._password = "crown-jewel-personal-password"
        self.timeline = timeline

    @property
    def content_source_id(self):
        return self._found["content_id"]

    @property
    def relay_content_url(self):
        return (
            f"{self.config.relay_http}/l/{self._content_channel_token}"
        )

    def _action(self, name: str) -> None:
        self.timeline.append(name)

    phase_topology = lambda self: self._action("topology")
    phase_found = lambda self: self._action("found")
    phase_join = lambda self: self._action("join")
    phase_admit = lambda self: self._action("admit")
    phase_portability = lambda self: self._action("portability")
    phase_prove = lambda self: self._action("prove")
    _capture_logs = lambda self: self._action("logs")
    teardown = lambda self: self._action("teardown")


def test_secure_topology_uses_existing_tls_initializer_and_pinned_healthcheck():
    secure = compose_model(
        TopologyConfig(
            project=PROJECT,
            secure_dashboard=True,
            from_source=False,
            image=IMAGE,
        )
    )
    node = secure["services"]["node-a"]
    assert "DASHBOARD_TLS" not in node["environment"]
    assert node["environment"]["DASHBOARD_DOMAIN"] == "localhost"
    assert node["healthcheck"]["test"] == [
        "CMD",
        "curl",
        "--cacert",
        "/app/data/tls.crt",
        "-fs",
        "https://localhost:8080/api/ping",
    ]

    ordinary = compose_model(
        TopologyConfig(project=PROJECT, from_source=False, image=IMAGE)
    )
    node = ordinary["services"]["node-a"]
    assert node["environment"]["DASHBOARD_TLS"] == "off"
    assert node["healthcheck"]["test"][-1].startswith("http://")


def test_pinned_context_trusts_only_the_captured_self_signed_certificate(
    tmp_path,
):
    cert = tmp_path / "tls.crt"
    key = tmp_path / "tls.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256",
            "-days", "1", "-nodes",
            "-keyout", str(key), "-out", str(cert),
            "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )

    context = demo.Harness._pinned_ssl_context(cert)
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert len(context.get_ca_certs(binary_form=True)) == 1


def test_demo_verifies_before_topology_and_drives_real_phase_order(
    tmp_path,
    monkeypatch,
):
    timeline: list[str] = []
    runner = TimelineRunner(timeline)
    harness = VisibleHarness(tmp_path, timeline)
    key = tmp_path / "cosign.pub"
    key.write_text("fixture project public key\n", encoding="utf-8")
    opened: list[str] = []
    monkeypatch.setattr(demo.shutil, "which", lambda _command: "/usr/bin/docker")

    Demo(
        DemoConfig(
            image=IMAGE,
            cosign_public_key=key,
            pace="none",
            open_browser=True,
            recording_path=tmp_path / "operator-recording.mp4",
        ),
        harness,
        runner=runner,
        announce=lambda _line: None,
        open_url=lambda url: opened.append(url) or True,
    ).run()

    assert timeline == [
        "verify",
        "topology",
        "found",
        "join",
        "admit",
        "portability",
        "prove",
        "teardown",
    ]
    verify = runner.calls[0]
    assert verify["argv"] == ["bash", str(demo.VERIFY_IMAGE), IMAGE]
    assert verify["env"]["AUTONOMY_COSIGN_PUBLIC_KEY"] == str(key.resolve())
    assert opened
    assert all(url.startswith(("https://", "http://")) for url in opened)
    assert any("/design" in url for url in opened)
    assert any("/graph/content-id" in url for url in opened)
    assert any("/l/public-content-channel" in url for url in opened)

    manifest = json.loads(harness.run_dir.joinpath("demo-urls.json").read_text())
    assert manifest["recording"] == str(tmp_path / "operator-recording.mp4")
    assert manifest["tls"].startswith("local self-signed")
    assert len(manifest["urls"]["live-node-dashboards"]) == 3
    transcript = harness.run_dir.joinpath("demo-transcript.md").read_text()
    assert "public-CA endorsement" in transcript
    assert "not claimed as relay-published" in transcript
    assert "fabricated" in transcript
    artifact_text = json.dumps(manifest) + transcript
    assert harness._claim_token not in artifact_text
    assert harness._password not in artifact_text


def test_signature_failure_stops_before_any_phase_but_still_tears_down(
    tmp_path,
    monkeypatch,
):
    timeline: list[str] = []
    runner = TimelineRunner(timeline, fail_verify=True)
    harness = VisibleHarness(tmp_path, timeline)
    key = tmp_path / "cosign.pub"
    key.write_text("fixture project public key\n", encoding="utf-8")
    monkeypatch.setattr(demo.shutil, "which", lambda _command: "/usr/bin/docker")

    with pytest.raises(HarnessError, match="signature refused"):
        Demo(
            DemoConfig(
                image=IMAGE,
                cosign_public_key=key,
                pace="none",
                open_browser=False,
            ),
            harness,
            runner=runner,
            announce=lambda _line: None,
        ).run()
    assert timeline == ["verify", "teardown"]


@pytest.mark.parametrize(
    ("build", "secure", "message"),
    [
        (True, True, "refuses source-build mode"),
        (False, False, "requires secure_dashboard"),
    ],
)
def test_demo_refuses_non_presentation_harness_modes(
    tmp_path,
    monkeypatch,
    build,
    secure,
    message,
):
    timeline: list[str] = []
    harness = VisibleHarness(tmp_path, timeline)
    object.__setattr__(harness.config, "build", build)
    object.__setattr__(harness.config, "secure_dashboard", secure)
    key = tmp_path / "cosign.pub"
    key.write_text("key\n")
    monkeypatch.setattr(demo.shutil, "which", lambda _command: "/usr/bin/docker")
    with pytest.raises(HarnessError, match=message):
        Demo(
            DemoConfig(
                image=IMAGE,
                cosign_public_key=key,
                pace="none",
            ),
            harness,
            runner=TimelineRunner(timeline),
        ).run()
    assert timeline == []


def test_browser_command_and_runtime_commands_are_parameterized(tmp_path):
    assert demo._command("docker compose", name="compose") == (
        "docker",
        "compose",
    )
    assert demo._command("open {url}", name="browser") == ("open", "{url}")
    with pytest.raises(ValueError, match="must not be empty"):
        demo._command("   ", name="browser")
