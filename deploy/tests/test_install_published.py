"""Published-image installer: embedded trust root, refusal before any pull."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALL = ROOT / "deploy" / "install-published.sh"
PUBLIC_KEY = ROOT / "deploy" / "cosign.pub"
GOOD = "a" * 64
BAD = "0" * 64
PRIVATE = "b" * 64  # the fake registry refuses the anonymous pull of this digest


def _exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _lock(tmp_path: Path, node_digest: str = GOOD, node_ref: str | None = None,
          host_terminal: bool = True, service_gateway: bool = True) -> Path:
    node = node_ref or f"ghcr.io/example/autonomy-node@sha256:{node_digest}"
    lock = tmp_path / "image-lock.env"
    lock.write_text(
        "AUTONOMY_IMAGE_LOCK_VERSION=1\n"
        "AUTONOMY_RELEASE_TAG=test\n"
        f"AUTONOMY_NODE_IMAGE={node}\n"
        f"AUTONOMY_SESSION_IMAGE=ghcr.io/example/autonomy-session@sha256:{GOOD}\n"
        f"AUTONOMY_SESSION_PLATFORM_IMAGE=ghcr.io/example/autonomy-session-platform@sha256:{GOOD}\n"
        f"AUTONOMY_SESSION_DIND_IMAGE=ghcr.io/example/autonomy-session-dind@sha256:{GOOD}\n"
        + (f"AUTONOMY_HOST_TERMINAL_IMAGE=ghcr.io/example/autonomy-host-terminal@sha256:{GOOD}\n"
           if host_terminal else "")
        + (f"AUTONOMY_SERVICE_GATEWAY_IMAGE=ghcr.io/example/autonomy-service-gateway@sha256:{GOOD}\n"
           if service_gateway else ""),
        encoding="utf-8",
    )
    return lock


def _run(tmp_path: Path, lock: Path, *extra: str,
         env_extra: dict | None = None) -> tuple[subprocess.CompletedProcess, str]:
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    # The stand-in daemon answers the engine API floor check (auto-8pohz) and
    # records every other call; see _FAKE_DOCKER for the volumes.
    _exe(fake / "docker", _FAKE_DOCKER.replace("PYTHON", sys.executable))
    _exe(
        fake / "cosign",
        f'#!/usr/bin/env bash\necho "cosign $*" >>"$T_LOG"\n'
        f'[[ "$*" == *@sha256:{BAD}* ]] && {{ echo "Error: no matching signatures: signature not found" >&2; exit 1; }}\n'
        f'[[ "$*" == *@sha256:{PRIVATE}* ]] && {{ echo "Error: GET https://ghcr.io/token?scope=repository:example/autonomy-node:pull: UNAUTHORIZED" >&2; exit 1; }}\n'
        'exit 0\n',
    )
    env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", T_LOG=str(log),
               AUTONOMY_COSIGN_BIN=str(fake / "cosign"), HOME=str(tmp_path),
               AUTONOMY_READY_TIMEOUT="1")
    env.update(env_extra or {})
    result = subprocess.run(
        ["bash", str(INSTALL), "--lock", str(lock), "--dir", str(tmp_path / "node"), "--yes",
         *extra],
        capture_output=True, text=True, env=env, timeout=60,
    )
    return result, log.read_text(encoding="utf-8") if log.exists() else ""


# The volumes of the stand-in daemon are directories named by the environment:
# T_CODE_VOLUME is the autonomy-code volume (absent: it does not exist yet, a
# first install), T_RELEASE the node image's /app, T_DATA_VOLUME autonomy-data.
# A one-shot `docker run` against a volume runs the installer's own shell
# script with those directories in place of the container paths, so the git
# rules are exercised on real repositories.
_FAKE_DOCKER = r"""#!PYTHON
import os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["T_LOG"], "a") as log:
    log.write("docker " + " ".join(args).replace("\n", " ") + "\n")
if args[:1] == ["version"]:
    print("1.47")
if args[:2] == ["volume", "inspect"]:
    sys.exit(0 if os.environ.get("T_CODE_VOLUME") else 1)
if args[:1] == ["ps"] and os.environ.get("T_CODE_VOLUME"):
    print("c0ffee")  # the running node's container
if args[:1] == ["run"] and "--entrypoint" in args and "sh" in args:
    env = dict(os.environ)
    for i, a in enumerate(args):
        if a == "-e":
            k, _, v = args[i + 1].partition("=")
            env[k] = v
    if "autonomy-code:/volume" in args:
        script = sys.stdin.read().replace(
            "release=/app code=/volume",
            f"release={env['T_RELEASE']} code={env['T_CODE_VOLUME']}")
        sys.exit(subprocess.run(["sh", "-s"], input=script, text=True, env=env).returncode)
    if "autonomy-data:/data" in args:
        script = args[args.index("-c") + 1].replace("/data/", env["T_DATA_VOLUME"] + "/")
        sys.exit(subprocess.run(["sh", "-c", script], env=env).returncode)
"""


def test_embedded_key_is_the_committed_project_key():
    body = INSTALL.read_text(encoding="utf-8")
    embedded = re.search(r"PROJECT_PUBLIC_KEY='(.*?)'", body, re.S).group(1)
    assert embedded.strip() == PUBLIC_KEY.read_text(encoding="utf-8").strip()


def test_unsigned_image_is_refused_before_any_pull(tmp_path):
    result, calls = _run(tmp_path, _lock(tmp_path, node_digest=BAD))
    assert result.returncode == 4, result.stderr
    assert "SIGNATURE CHECK FAILED" in result.stderr
    assert "docker pull" not in calls
    assert "compose up" not in calls


def test_a_private_package_is_named_as_unreachable_not_as_a_bad_signature(tmp_path):
    """Windows run 6 (2026-09-27): GHCR created the new gateway package
    private, cosign could not fetch its signature, and the installer said
    SIGNATURE CHECK FAILED. A registry refusal is named as such."""
    result, calls = _run(tmp_path, _lock(tmp_path, node_digest=PRIVATE))
    assert result.returncode == 9, result.stderr
    assert "IMAGE UNREACHABLE" in result.stderr
    assert "cannot be pulled anonymously" in result.stderr
    assert "SIGNATURE CHECK FAILED" not in result.stderr
    assert "docker pull" not in calls


def test_floating_tag_in_lock_is_refused(tmp_path):
    result, calls = _run(tmp_path, _lock(tmp_path, node_ref="ghcr.io/example/autonomy-node:latest"))
    assert result.returncode == 2
    assert "refusing non-digest lock entry" in result.stderr
    assert "cosign verify" not in calls


def test_every_image_is_verified_with_the_embedded_key(tmp_path):
    result, calls = _run(tmp_path, _lock(tmp_path))
    assert result.returncode == 5  # fakes never answer /api/ping
    verifies = [line for line in calls.splitlines() if line.startswith("cosign verify")]
    assert len(verifies) == 6
    assert all("--key" in line for line in verifies)
    lines = calls.splitlines()
    last_verify = max(i for i, line in enumerate(lines) if line.startswith("cosign verify"))
    first_pull = min(i for i, line in enumerate(lines) if line.startswith("docker pull"))
    assert last_verify < first_pull
    assert any(line.startswith("docker compose up -d --no-build") for line in lines)
    # The launcher starts the host terminal from this local name.
    assert any(line.startswith("docker tag ") and line.endswith(" autonomy-host-terminal")
               for line in lines)


def test_a_release_lock_without_the_host_terminal_image_still_installs(tmp_path):
    """deploy/releases/2026.09.26-0d46057.env pins four images; its node
    predates the in-node host terminal and needs no host-terminal image."""
    result, calls = _run(tmp_path, _lock(tmp_path, host_terminal=False, service_gateway=False))
    assert result.returncode == 5, result.stderr  # fakes never answer /api/ping
    verifies = [line for line in calls.splitlines() if line.startswith("cosign verify")]
    assert len(verifies) == 4
    assert not any(line.endswith(" autonomy-host-terminal") for line in calls.splitlines())
    assert "AUTONOMY_SERVICE_GATEWAY_IMAGE" not in _env_file(tmp_path)


def test_the_service_gateway_image_is_verified_pulled_and_recorded_for_the_dashboard(tmp_path):
    """The dashboard starts the gateway from its own Compose run, where .env
    is not read: the pinned reference is recorded in .env, which
    docker-compose.yml passes into the dashboard's environment (Windows run 5:
    "No such image: autonomy-service-gateway:local")."""
    result, calls = _run(tmp_path, _lock(tmp_path))
    assert result.returncode == 5, result.stderr
    ref = f"ghcr.io/example/autonomy-service-gateway@sha256:{GOOD}"
    lines = calls.splitlines()
    assert f"cosign verify --insecure-ignore-tlog --key {tmp_path / 'node' / 'tools' / 'cosign.pub'} {ref}" in lines \
        or any(line.startswith("cosign verify") and line.endswith(ref) for line in lines)
    assert any(line.startswith("docker pull") and line.endswith(ref) for line in lines)
    assert _env_file(tmp_path)["AUTONOMY_SERVICE_GATEWAY_IMAGE"] == ref


def _env_file(tmp_path: Path) -> dict[str, str]:
    lines = (tmp_path / "node" / ".env").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def test_host_home_defaults_to_the_invoking_user(tmp_path):
    result, _ = _run(tmp_path, _lock(tmp_path))
    assert result.returncode == 5, result.stderr
    assert _env_file(tmp_path)["AUTONOMY_HOST_HOME"] == str(tmp_path)


def test_host_home_under_sudo_is_the_sudo_users_home_not_root(tmp_path):
    """Windows walkthrough, 2026-09-26: run as root, the node was pointed at
    /root, which holds none of the operator's sign-ins and which the
    dashboard (uid 1000) cannot read. Under sudo the invoking user's home is
    used. ``id`` and ``getent`` are faked so the test needs no root."""
    alice = tmp_path / "home-alice"
    alice.mkdir()
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    _exe(fake / "id", '#!/usr/bin/env bash\n[[ "$1" == -u ]] && { echo 0; exit 0; }\n'
                      'exec /usr/bin/id "$@"\n')
    _exe(fake / "getent", f'#!/usr/bin/env bash\necho "alice:x:1000:1000::{alice}:/bin/bash"\n')
    result, _ = _run(tmp_path, _lock(tmp_path), env_extra={"SUDO_USER": "alice"})
    assert result.returncode == 5, result.stderr
    assert _env_file(tmp_path)["AUTONOMY_HOST_HOME"] == str(alice)


def test_host_home_option_wins(tmp_path):
    chosen = tmp_path / "elsewhere"
    chosen.mkdir()
    result, _ = _run(tmp_path, _lock(tmp_path), "--host-home", str(chosen))
    assert result.returncode == 5, result.stderr
    assert _env_file(tmp_path)["AUTONOMY_HOST_HOME"] == str(chosen)


def test_missing_host_home_is_refused_before_any_pull(tmp_path):
    result, calls = _run(tmp_path, _lock(tmp_path), "--host-home", str(tmp_path / "nope"))
    assert result.returncode == 2
    assert "does not exist" in result.stderr
    assert "docker pull" not in calls


# ── upgrade: an existing node moves to a new release (auto-d8jf5.1) ─────────
def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example", "commit", "-qm", name)
    return _git(repo, "rev-parse", "HEAD")


def _node(tmp_path: Path) -> dict:
    """A node installed from release N, with the image of release N+1.

    The release image's /app is a repository at the new commit with /app/VERSION
    naming it; the code volume is a clone of it at the previous commit, the
    shape a node seeded from the older image has."""
    release = tmp_path / "release-app"
    release.mkdir()
    _git(release, "init", "-q")
    old = _commit(release, "app.py", "old\n")
    new = _commit(release, "app.py", "new\n")
    (release / "VERSION").write_text(f"commit={new}\ncommit_date=2026-10-02\n", encoding="utf-8")
    code = tmp_path / "code-volume"
    subprocess.run(["git", "clone", "-q", str(release), str(code)], check=True)
    _git(code, "reset", "-q", "--hard", old)
    (code / "VERSION").write_text(f"commit={old}\n", encoding="utf-8")  # untracked, as seeded
    data = tmp_path / "data-volume"
    data.mkdir()
    node = tmp_path / "node"
    node.mkdir()
    (node / ".env").write_text(
        "AUTONOMY_IMAGE=ghcr.io/example/autonomy-node@sha256:" + "c" * 64 + "\n"
        f"AUTONOMY_HOST_HOME={tmp_path}\n"
        "DASHBOARD_PORT=9443\n"
        "DASHBOARD_HTTP_PORT=8099\n"
        "AUTONOMY_SUBNET=10.213.0.0/24\n"
        "TZ=America/Los_Angeles\n", encoding="utf-8")
    (node / "docker-compose.override.yml").write_text("services: {dashboard: {}}\n", encoding="utf-8")
    return {"release": release, "code": code, "data": data, "node": node, "old": old, "new": new,
            "env": {"T_RELEASE": str(release), "T_CODE_VOLUME": str(code), "T_DATA_VOLUME": str(data)}}


def _ping_ok(tmp_path: Path) -> None:
    (tmp_path / "bin").mkdir(exist_ok=True)
    _exe(tmp_path / "bin" / "curl", '#!/usr/bin/env bash\necho "curl $*" >>"$T_LOG"\necho 200\n')


def test_an_upgrade_keeps_the_nodes_values_and_override_and_updates_the_images(tmp_path):
    n = _node(tmp_path)
    override = (n["node"] / "docker-compose.override.yml").read_text(encoding="utf-8")
    result, calls = _run(tmp_path, _lock(tmp_path), env_extra=n["env"])
    assert result.returncode == 5, result.stderr  # fakes never answer /api/ping
    env = _env_file(tmp_path)
    assert env["AUTONOMY_IMAGE"] == f"ghcr.io/example/autonomy-node@sha256:{GOOD}"
    assert env["AUTONOMY_SERVICE_GATEWAY_IMAGE"] == f"ghcr.io/example/autonomy-service-gateway@sha256:{GOOD}"
    assert env["DASHBOARD_PORT"] == "9443"
    assert env["DASHBOARD_HTTP_PORT"] == "8099"
    assert env["AUTONOMY_SUBNET"] == "10.213.0.0/24"
    assert env["AUTONOMY_HOST_HOME"] == str(tmp_path)
    assert env["TZ"] == "America/Los_Angeles"
    assert (n["node"] / "docker-compose.override.yml").read_text(encoding="utf-8") == override
    assert "network_preflight" not in calls


def test_an_explicit_flag_still_replaces_the_kept_value(tmp_path):
    n = _node(tmp_path)
    home = tmp_path / "other-home"
    home.mkdir()
    result, _ = _run(tmp_path, _lock(tmp_path), "--port", "9555", "--host-home", str(home),
                     env_extra=n["env"])
    assert result.returncode == 5, result.stderr
    env = _env_file(tmp_path)
    assert env["DASHBOARD_PORT"] == "9555"
    assert env["AUTONOMY_HOST_HOME"] == str(home)


def test_an_upgrade_moves_the_code_volume_to_the_release_commit_before_compose_up(tmp_path):
    n = _node(tmp_path)
    result, calls = _run(tmp_path, _lock(tmp_path), env_extra=n["env"])
    assert result.returncode == 5, result.stderr
    assert _git(n["code"], "rev-parse", "HEAD") == n["new"]
    assert (n["code"] / "app.py").read_text(encoding="utf-8") == "new\n"
    assert (n["code"] / "VERSION").read_text(encoding="utf-8") == (n["release"] / "VERSION").read_text(encoding="utf-8")
    lines = calls.splitlines()

    def at(pred) -> int:
        return next(i for i, line in enumerate(lines) if pred(line))
    check = at(lambda line: "AUTONOMY_CODE_STEP=check" in line)
    stop = at(lambda line: line == "docker stop c0ffee")
    move = at(lambda line: "AUTONOMY_CODE_STEP=move" in line)
    tag = at(lambda line: line.startswith("docker tag "))
    compose_up = at(lambda line: line.startswith("docker compose up"))
    # Checked before anything the node uses changes; moved only once the node
    # is stopped, so its code never changes under a running container.
    assert check < stop < move < tag < compose_up
    assert "label=com.docker.compose.project=autonomy" in lines[stop - 1]
    # As the volume's owner, never root, and from the image just verified.
    for i in (check, move):
        assert "--user 1000:1000" in lines[i]
        assert f"autonomy-node@sha256:{GOOD}" in lines[i]
    assert f"code volume: {n['old']} -> {n['new']}" in result.stdout


def _refused(tmp_path: Path, n: dict, result, calls: str) -> None:
    assert result.returncode == 10, result.stderr
    # Nothing the running node uses has changed: not its containers, not the
    # session image tags it launches from, not $DIR.
    assert "docker stop" not in calls
    assert "docker tag" not in calls
    assert "compose up" not in calls
    assert "docker cp" not in calls
    assert _env_file(tmp_path)["AUTONOMY_IMAGE"].endswith("c" * 64)  # .env untouched
    assert _git(n["code"], "rev-parse", "HEAD") != n["new"]


def test_a_dirty_code_volume_is_refused_and_nothing_is_recreated(tmp_path):
    n = _node(tmp_path)
    (n["code"] / "app.py").write_text("a hand edit on the node\n", encoding="utf-8")
    result, calls = _run(tmp_path, _lock(tmp_path), env_extra=n["env"])
    _refused(tmp_path, n, result, calls)
    assert "uncommitted changes" in result.stderr
    assert (n["code"] / "app.py").read_text(encoding="utf-8") == "a hand edit on the node\n"


def test_a_code_volume_with_local_commits_is_refused_and_nothing_is_recreated(tmp_path):
    n = _node(tmp_path)
    local = _commit(n["code"], "local.py", "a developer node's own commit\n")
    result, calls = _run(tmp_path, _lock(tmp_path), env_extra=n["env"])
    _refused(tmp_path, n, result, calls)
    assert "is not an ancestor of release commit" in result.stderr
    assert "--allow-downgrade" in result.stderr
    assert _git(n["code"], "rev-parse", "HEAD") == local


def test_a_downgrade_is_refused_unless_allowed(tmp_path):
    n = _node(tmp_path)
    newer = _commit(n["code"], "app.py", "newer than the release\n")
    result, calls = _run(tmp_path, _lock(tmp_path), env_extra=n["env"])
    _refused(tmp_path, n, result, calls)
    assert _git(n["code"], "rev-parse", "HEAD") == newer
    result, calls = _run(tmp_path, _lock(tmp_path), "--allow-downgrade", env_extra=n["env"])
    assert result.returncode == 5, result.stderr
    assert _git(n["code"], "rev-parse", "HEAD") == n["new"]


def test_a_first_install_has_no_code_step(tmp_path):
    result, calls = _run(tmp_path, _lock(tmp_path))
    assert result.returncode == 5, result.stderr
    assert "autonomy-code:/volume" not in calls
    assert _env_file(tmp_path)["DASHBOARD_PORT"] == "8080"


def test_the_installed_release_is_recorded_with_its_predecessor_kept(tmp_path):
    n = _node(tmp_path)
    _ping_ok(tmp_path)
    first = _lock(tmp_path)
    result, calls = _run(tmp_path, first, env_extra=n["env"])
    assert result.returncode == 0, result.stderr
    assert "https://localhost:9443/api/ping" in calls  # the kept port is the one waited on
    installed = n["data"] / "release" / "installed.env"
    body = installed.read_text(encoding="utf-8")
    assert body.startswith(first.read_text(encoding="utf-8"))
    assert re.search(r"^AUTONOMY_INSTALLED_AT=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", body, re.M)

    later = tmp_path / "later"
    later.mkdir()
    second = _lock(later)
    second.write_text(second.read_text(encoding="utf-8").replace("RELEASE_TAG=test", "RELEASE_TAG=next"),
                      encoding="utf-8")
    result, _ = _run(tmp_path, second, env_extra=n["env"])
    assert result.returncode == 0, result.stderr
    assert "AUTONOMY_RELEASE_TAG=next" in installed.read_text(encoding="utf-8")
    history = list((n["data"] / "release" / "history").iterdir())
    assert [h.read_text(encoding="utf-8") for h in history] == [body]
