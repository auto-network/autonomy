"""Release-pipeline contract: immutable refs, project key, sovereign fallback."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PUBLISH = ROOT / "deploy" / "publish-images.sh"
SIGN = ROOT / "deploy" / "sign-image-lock.sh"
VERIFY = ROOT / "deploy" / "verify-image.sh"
DIGEST = "a" * 64


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def release_env(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "calls.log"
    public_key = tmp_path / "cosign.pub"
    public_key.write_text("temporary test public key\n", encoding="utf-8")
    private_key = tmp_path / "cosign.key"
    private_key.write_text("temporary test armored key\n", encoding="utf-8")

    _write_executable(
        fake_bin / "docker",
        f"""#!/usr/bin/env bash
set -euo pipefail
printf 'docker' >>"$RELEASE_TEST_LOG"
printf ' %q' "$@" >>"$RELEASE_TEST_LOG"
printf '\\n' >>"$RELEASE_TEST_LOG"
if [[ "${{1:-}}" == image && "${{2:-}}" == inspect ]]; then
    ref="${{@: -1}}"
    printf '%s@sha256:{DIGEST}\\n' "${{ref%:*}}"
fi
""",
    )
    _write_executable(
        fake_bin / "cosign",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'cosign' >>"$RELEASE_TEST_LOG"
printf ' %q' "$@" >>"$RELEASE_TEST_LOG"
printf '\\n' >>"$RELEASE_TEST_LOG"
if [[ "${1:-}" == verify && "${*: -1}" == *@sha256:0000000000000000000000000000000000000000000000000000000000000000 ]]; then
    exit 1
fi
""",
    )
    agent_build = tmp_path / "agent-build.sh"
    _write_executable(
        agent_build,
        """#!/usr/bin/env bash
set -euo pipefail
printf 'agent-build' >>"$RELEASE_TEST_LOG"
printf ' %q' "$@" >>"$RELEASE_TEST_LOG"
printf '\\n' >>"$RELEASE_TEST_LOG"
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "RELEASE_TEST_LOG": str(log),
            "AUTONOMY_REGISTRY": "registry.test:5000",
            "AUTONOMY_IMAGE_NAMESPACE": "operator/project",
            "AUTONOMY_RELEASE_TAG": "v1.2.3",
            "AUTONOMY_COSIGN_PUBLIC_KEY": str(public_key),
            "AUTONOMY_COSIGN_PRIVATE_KEY": str(private_key),
            "AUTONOMY_AGENT_BUILD_SCRIPT": str(agent_build),
            "AUTONOMY_IMAGE_LOCK": str(tmp_path / "image-lock.env"),
        }
    )
    return env, log, tmp_path / "image-lock.env", public_key


def test_publish_builds_pushes_and_records_exact_digests_without_signing(release_env):
    env, log, lock_file, _ = release_env
    subprocess.run(["bash", str(PUBLISH)], env=env, check=True)

    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls[0].startswith("docker build --pull ")
    # The version stamp is NOT a build arg: the Dockerfile builder stage reads
    # the commit hash + date from the checkout's .git itself (auto-m7vh7). So the
    # release build passes no AUTONOMY_VERSION/AUTONOMY_BUILD_TIME.
    node_build = calls[0]
    assert "AUTONOMY_VERSION" not in node_build, node_build
    assert "AUTONOMY_BUILD_TIME" not in node_build, node_build
    # Built from a throwaway clone, NOT the repo/worktree directly — so a release
    # run from a linked worktree (this suite runs from one) still self-stamps the
    # committed HEAD. The build context is the clone dir, never ROOT (auto-m7vh7).
    ctx = node_build.split()[-1]
    assert ctx != str(ROOT), f"publish must build from a temp clone, not {ROOT}"
    assert ctx.endswith("/repo"), f"expected a clone context dir, got {ctx}"
    assert "agent-build --pull --core-only" in calls
    assert sum(line.startswith("docker push ") for line in calls) == 4
    assert not any(line.startswith("cosign ") for line in calls)

    lock = lock_file.read_text(encoding="utf-8").splitlines()
    assert lock[:2] == [
        "AUTONOMY_IMAGE_LOCK_VERSION=1",
        "AUTONOMY_RELEASE_TAG=v1.2.3",
    ]
    image_lines = [line for line in lock if line.startswith("AUTONOMY_")][2:]
    assert len(image_lines) == 4
    assert all(f"@sha256:{DIGEST}" in line for line in image_lines)


def test_operator_signing_requires_confirmation_and_signs_exact_digests(release_env):
    env, log, lock_file, _ = release_env
    subprocess.run(["bash", str(PUBLISH)], env=env, check=True)
    log.unlink()

    cancelled = subprocess.run(
        ["bash", str(SIGN), str(lock_file)],
        env=env,
        input="no\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert cancelled.returncode == 2
    assert not log.exists()

    subprocess.run(
        ["bash", str(SIGN), str(lock_file)],
        env=env,
        input="SIGN v1.2.3\n",
        text=True,
        check=True,
    )
    calls = log.read_text(encoding="utf-8").splitlines()
    signed = [line for line in calls if line.startswith("cosign sign ")]
    verified = [line for line in calls if line.startswith("cosign verify ")]
    assert len(signed) == len(verified) == 4
    for line in signed + verified:
        assert f"@sha256:{DIGEST}" in line
        assert ":v1.2.3" not in line
    assert all("--tlog-upload=false" in line for line in signed)
    assert all("--insecure-ignore-tlog" in line for line in verified)
    for sign_line in signed:
        digest_ref = sign_line.split()[-1]
        sign_index = calls.index(sign_line)
        verify_index = next(
            i for i, line in enumerate(calls)
            if i > sign_index
            and line.startswith("cosign verify ")
            and line.split()[-1] == digest_ref
        )
        assert sign_index < verify_index


def test_operator_signing_fails_closed_without_public_key(release_env):
    env, log, lock_file, public_key = release_env
    subprocess.run(["bash", str(PUBLISH)], env=env, check=True)
    log.unlink()
    public_key.unlink()
    result = subprocess.run(
        ["bash", str(SIGN), str(lock_file)],
        env=env,
        input="SIGN v1.2.3\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "tracked cosign public key not found" in result.stderr
    assert not log.exists()


def test_operator_signing_refuses_environment_backed_or_hot_password(release_env):
    env, log, lock_file, _ = release_env
    subprocess.run(["bash", str(PUBLISH)], env=env, check=True)
    log.unlink()
    env["AUTONOMY_COSIGN_PRIVATE_KEY"] = "env://COSIGN_PRIVATE_KEY"
    result = subprocess.run(
        ["bash", str(SIGN), str(lock_file)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "refusing environment-backed signing key" in result.stderr
    assert not log.exists()

    env["AUTONOMY_COSIGN_PRIVATE_KEY"] = str(Path(env["AUTONOMY_COSIGN_PUBLIC_KEY"]).with_name("cosign.key"))
    env["COSIGN_PASSWORD"] = "hot-secret"
    result = subprocess.run(
        ["bash", str(SIGN), str(lock_file)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "refusing COSIGN_PASSWORD" in result.stderr
    assert not log.exists()


def test_verify_rejects_tags_and_propagates_tamper_failure(release_env):
    env, log, _, _ = release_env

    mutable = subprocess.run(
        ["bash", str(VERIFY), "registry.test/operator/autonomy-node:latest"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert mutable.returncode == 2
    assert "refusing mutable image reference" in mutable.stderr
    assert not log.exists()

    genuine = f"registry.test/operator/autonomy-node@sha256:{DIGEST}"
    subprocess.run(["bash", str(VERIFY), genuine], env=env, check=True)
    assert genuine in log.read_text(encoding="utf-8")

    tampered = "registry.test/operator/autonomy-node@sha256:" + ("0" * 64)
    refused = subprocess.run(
        ["bash", str(VERIFY), tampered],
        env=env,
        check=False,
    )
    assert refused.returncode != 0


def test_workflow_uses_project_key_not_keyless_identity():
    workflow = (ROOT / ".github/workflows/publish-images.yml").read_text(
        encoding="utf-8"
    )
    assert "COSIGN_PRIVATE_KEY" not in workflow
    assert "COSIGN_PASSWORD" not in workflow
    assert "deploy/publish-images.sh" in workflow
    assert "deploy/sign-image-lock.sh" not in workflow
    assert "id-token: write" not in workflow
    assert "certificate-identity" not in workflow
    assert "keyless" not in workflow.lower()


def test_compose_keeps_sovereign_source_build_and_digest_override():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "dockerfile: deploy/Dockerfile" in compose
    assert "${AUTONOMY_IMAGE:-autonomy-dashboard:local}" in compose
    assert "docker compose up -d --no-build" in compose


@pytest.mark.skipif(shutil.which("cosign") is None, reason="cosign unavailable")
def test_real_cosign_ephemeral_key_rejects_tampered_blob(tmp_path):
    """The release runner executes this with cosign; no production key used."""
    env = os.environ.copy()
    env["COSIGN_PASSWORD"] = "ephemeral-test-password"
    prefix = tmp_path / "ephemeral"
    subprocess.run(
        ["cosign", "generate-key-pair", "--output-key-prefix", str(prefix)],
        env=env,
        check=True,
    )
    payload = tmp_path / "payload"
    signature = tmp_path / "payload.sig"
    payload.write_bytes(b"exact release artifact")
    subprocess.run(
        [
            "cosign", "sign-blob", "--yes", "--tlog-upload=false",
            "--key", str(prefix) + ".key",
            "--output-signature", str(signature),
            str(payload),
        ],
        env=env,
        check=True,
    )
    verify = [
        "cosign", "verify-blob",
        "--insecure-ignore-tlog",
        "--key", str(prefix) + ".pub",
        "--signature", str(signature),
        str(payload),
    ]
    subprocess.run(verify, env=env, check=True)
    payload.write_bytes(b"tampered release artifact")
    assert subprocess.run(verify, env=env, check=False).returncode != 0
