"""Paced, visible demo-day driver over the real B6 phase machine.

The command deliberately contains no substitute transport or fake recording
path.  It verifies one immutable signed image, drives the production harness
one phase at a time, opens real browser surfaces, and leaves a timestamped
transcript plus URL manifest for the operator's screen recording.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import shutil
import sys
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from deploy.harness.driver import (
    Harness,
    HarnessConfig,
    HarnessError,
    Runner,
    SubprocessRunner,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
VERIFY_IMAGE = REPO_ROOT / "deploy" / "verify-image.sh"


@dataclass(frozen=True)
class DemoConfig:
    image: str
    cosign_public_key: Path
    pace: str = "manual"
    pause_seconds: float = 4.0
    open_browser: bool = True
    browser_command: tuple[str, ...] = ()
    recording_path: Path | None = None


class Demo:
    """Presentation-only orchestration; every state change is a Harness phase."""

    def __init__(
        self,
        config: DemoConfig,
        harness: Harness,
        *,
        runner: Runner | None = None,
        announce: Callable[[str], None] = print,
        read_line: Callable[[str], str] = input,
        sleep: Callable[[float], None] = time.sleep,
        open_url: Callable[[str], bool] = webbrowser.open_new_tab,
    ):
        self.config = config
        self.harness = harness
        self.runner = runner or SubprocessRunner(announce)
        self.announce = announce
        self.read_line = read_line
        self.sleep = sleep
        self.open_url = open_url
        self.transcript_path = harness.run_dir / "demo-transcript.md"
        self.url_manifest_path = harness.run_dir / "demo-urls.json"
        self._urls: dict[str, str | list[str]] = {}

    def _record(self, text: str) -> None:
        self.harness.run_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.transcript_path.open("a", encoding="utf-8") as stream:
            stream.write(f"- `{stamp}` {text}\n")
        self.announce(text)

    def _pause(self, prompt: str) -> None:
        if self.config.pace == "manual":
            self.read_line(prompt + " Press Enter to continue… ")
        elif self.config.pace == "auto":
            self.sleep(self.config.pause_seconds)

    def _beat(self, number: int, title: str, narration: str) -> None:
        self._record(f"\n## Beat {number}: {title}\n\n{narration}")
        self._pause("Frame the dashboard and narrate this beat.")

    def _write_urls(self) -> None:
        payload = {
            "project": self.harness.config.project,
            "generated_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            "tls": (
                "local self-signed certificates pinned by the driver; "
                "accept each browser warning explicitly"
            ),
            "urls": self._urls,
            "recording": (
                str(self.config.recording_path)
                if self.config.recording_path is not None
                else None
            ),
        }
        self.url_manifest_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _show(self, name: str, url: str) -> None:
        self._urls[name] = url
        self._write_urls()
        self._record(f"Visible surface — {name}: {url}")
        if not self.config.open_browser:
            return
        if self.config.browser_command:
            argv = [
                part.replace("{url}", url)
                for part in self.config.browser_command
            ]
            if all("{url}" not in part for part in self.config.browser_command):
                argv.append(url)
            self.runner.run(argv, check=False)
        else:
            self.open_url(url)

    def _verify_signed_image(self) -> None:
        if not self.config.cosign_public_key.is_file():
            raise HarnessError(
                "cosign public key not found: "
                f"{self.config.cosign_public_key}"
            )
        env = dict(os.environ)
        env["AUTONOMY_COSIGN_PUBLIC_KEY"] = str(
            self.config.cosign_public_key.resolve()
        )
        self._record(
            "Preflight: independently verify the exact node image digest "
            "against the operator-provisioned project key."
        )
        self.runner.run(
            ["bash", str(VERIFY_IMAGE), self.config.image],
            env=env,
        )
        self._record(f"Verified immutable image: {self.config.image}")

    def _live_node_urls(self) -> list[str]:
        # A remains deliberately stopped after portability.  B, restored C,
        # and peers 3..N are the live N-node climax.
        urls = [
            self.harness.config.node_http(1),
            self.harness.config.node_http(self.harness.config.nodes),
        ]
        urls.extend(
            self.harness.config.node_http(index - 1)
            for index in range(3, self.harness.config.nodes + 1)
        )
        return urls

    def run(self) -> None:
        if self.harness.config.build:
            raise HarnessError(
                "demo presentation refuses source-build mode; supply a "
                "verified image@sha256 and run the harness with build=False"
            )
        if not self.harness.config.secure_dashboard:
            raise HarnessError(
                "demo presentation requires secure_dashboard=True"
            )
        if shutil.which(self.harness.config.docker_command[0]) is None:
            raise HarnessError(
                "Docker is required for the live presentation; run it on "
                "the operator's Docker host"
            )

        failure: BaseException | None = None
        try:
            self._verify_signed_image()

            self._beat(
                1,
                "Sovereign signed boot",
                "The node image is digest-pinned and verified with the "
                "project key before Docker starts anything. The dashboard "
                "uses its own local self-signed TLS keypair; this is real "
                "HTTPS, not a claim of public-CA endorsement.",
            )
            self.harness.phase_topology()
            self._show("node-a-dashboard", self.harness.config.node_http(0))

            self._beat(
                2,
                "Platform and organization in one container",
                "Found the organization with locally armored roots, then "
                "show the graph and Design Studio as tools running inside "
                "this sovereign node. They are not claimed as relay-published.",
            )
            self.harness.phase_found()
            node_a = self.harness.config.node_http(0)
            self._show(
                "node-a-graph",
                f"{node_a}/graph/{self.harness.content_source_id}",
            )
            self._show("node-a-design-studio", f"{node_a}/design")
            self._show("real-relay-note", self.harness.relay_content_url)

            self._beat(
                3,
                "Second node joins through the real relay",
                "Node B creates its own personal identity locally. The relay "
                "sees only its channel grant token; the ledger claim bearer "
                "stays in the client-only invitation domain.",
            )
            self.harness.phase_join()
            self._show("node-b-dashboard", self.harness.config.node_http(1))

            self._beat(
                4,
                "Two-party admission",
                "Two independently signed approvals satisfy the real ledger "
                "policy. B restarts, polls status, and finalizes its own claim.",
            )
            self.harness.phase_admit()

            self._beat(
                5,
                "Portability is sovereignty",
                "A is quiesced and kept stopped. Its consistent snapshot is "
                "restored into fresh C rather than copying a running store.",
            )
            self.harness.phase_portability()
            self._show(
                "restored-node-c-dashboard",
                self.harness.config.node_http(self.harness.config.nodes),
            )

            self._beat(
                6,
                "C is A",
                "C proves the same cryptographic identity, membership, and "
                "byte-identical serving delegate, reconnects, and serves A's "
                "note over the production relay while A remains stopped.",
            )
            self.harness.phase_prove()

            self._beat(
                7,
                "N sovereign nodes side by side",
                "The live dashboards are isolated nodes on one machine: B, "
                "restored C, and the remaining peers. This is the real test "
                "bed the synchronization sprint builds on.",
            )
            live = self._live_node_urls()
            self._urls["live-node-dashboards"] = live
            self._write_urls()
            for index, url in enumerate(live, 1):
                self._show(f"live-node-{index}", url)

            self._record(
                "DEMO COMPLETE. The operator's screen recording is the live "
                "artifact; this command produced only the honest transcript "
                f"({self.transcript_path}) and URL manifest "
                f"({self.url_manifest_path})."
            )
            if self.config.recording_path is not None:
                self._record(
                    "Operator recording destination (not fabricated or "
                    f"created by this command): {self.config.recording_path}"
                )
            self._pause("Finish the operator-side screen recording.")
        except BaseException as exc:
            failure = exc
            if self.harness.compose_file.exists():
                self.harness._capture_logs()
            raise
        finally:
            try:
                self.harness.teardown()
            except Exception:
                if failure is None:
                    raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the real multi-node ladder as a paced visible demo"
    )
    parser.add_argument(
        "--image",
        required=True,
        help="signed node image as image@sha256:<64 lowercase hex>",
    )
    parser.add_argument("--cosign-public-key", type=Path, required=True)
    parser.add_argument("--project")
    parser.add_argument("--nodes", type=int, default=3)
    parser.add_argument("--relay-port", type=int, default=18477)
    parser.add_argument("--node-port-base", type=int, default=18880)
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=Path("harness-artifacts"),
    )
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument(
        "--pace",
        choices=("manual", "auto", "none"),
        default="manual",
    )
    parser.add_argument("--pause-seconds", type=float, default=4.0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--browser-command",
        help="host-specific opener, e.g. 'open {url}' or 'xdg-open {url}'",
    )
    parser.add_argument(
        "--docker-command",
        default="docker",
        help="host Docker CLI command (default: docker)",
    )
    parser.add_argument(
        "--compose-command",
        default="docker compose",
        help="host Compose command (default: 'docker compose')",
    )
    parser.add_argument(
        "--recording-path",
        type=Path,
        help="operator-side recording destination to include in the manifest",
    )
    return parser


def _command(value: str, *, name: str) -> tuple[str, ...]:
    command = tuple(shlex.split(value))
    if not command:
        raise ValueError(f"{name} must not be empty")
    return command


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        docker_command = _command(
            args.docker_command,
            name="--docker-command",
        )
        compose_command = _command(
            args.compose_command,
            name="--compose-command",
        )
        browser_command = (
            _command(args.browser_command, name="--browser-command")
            if args.browser_command
            else ()
        )
        harness = Harness(
            HarnessConfig(
                project=(
                    args.project
                    or f"autonomy-harness-demo-{secrets.token_hex(3)}"
                ),
                nodes=args.nodes,
                relay_port=args.relay_port,
                node_port_base=args.node_port_base,
                image=args.image,
                artifacts_dir=args.artifacts_dir,
                build=False,
                timeout=args.timeout,
                secure_dashboard=True,
                docker_command=docker_command,
                compose_command=compose_command,
            )
        )
        Demo(
            DemoConfig(
                image=args.image,
                cosign_public_key=args.cosign_public_key,
                pace=args.pace,
                pause_seconds=args.pause_seconds,
                open_browser=not args.no_browser,
                browser_command=browser_command,
                recording_path=args.recording_path,
            ),
            harness,
        ).run()
    except (HarnessError, ValueError) as exc:
        print(f"DEMO FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
