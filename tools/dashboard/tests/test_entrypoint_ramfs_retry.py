"""The ramfs provisioner must survive losing a race with the Docker daemon.

Provisioning needs the Docker socket, so on a host reboot it races the daemon.
On 2026-09-09 it died at 13:35:50Z on a `docker inspect` timeout while the
daemon did not become active until 13:37:01Z. It was a single best-effort
attempt with no retry, so the machine had NO ramfs key carrier from then until
it was repaired by hand, and every vault unlock in that window had nowhere to
persist.

These RUN deploy/provision-secret-ramfs.sh, with `python3` and `sleep` shimmed
onto PATH so nothing is mounted and nothing waits.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "deploy" / "provision-secret-ramfs.sh"


def _run(tmp_path, *, fail_times, attempts=10):
    """Invoke the real script with a provisioner that fails *fail_times*."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    counter = tmp_path / "attempts"
    counter.write_text("0")
    slept = tmp_path / "slept"
    slept.write_text("")

    (bin_dir / "python3").write_text(
        "#!/bin/sh\n"
        f'n=$(cat "{counter}")\n'
        "n=$((n+1))\n"
        f'echo "$n" > "{counter}"\n'
        f'[ "$n" -gt {fail_times} ]\n'
    )
    (bin_dir / "sleep").write_text(
        f'#!/bin/sh\necho "$1" >> "{slept}"\n'
    )
    for name in ("python3", "sleep"):
        (bin_dir / name).chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["RAMFS_PROVISION_ATTEMPTS"] = str(attempts)
    env["RAMFS_PROVISION_DELAY"] = "5"
    result = subprocess.run(
        [str(SCRIPT)], capture_output=True, text=True, timeout=30, env=env,
    )
    return (
        result,
        int(counter.read_text().strip()),
        [l for l in slept.read_text().splitlines() if l],
    )


def test_exhaustion_is_loud_and_terminates(tmp_path):
    """The only property here worth a test: a provisioner that never succeeds
    must TERMINATE, not retry forever and hang container start.

    Deliberately the only one. That the loop retries at all, and that it does
    not sleep before its first attempt, are properties of `while` — testing
    them is ceremony. And what would actually matter, whether the real
    `agents.secret_ramfs` fails RECOVERABLY when the Docker daemon is not up
    yet, is not tested here: the provisioner is stubbed. This tests the retry
    wrapper, not the thing it exists to survive.
    """
    result, attempts, slept = _run(tmp_path, fail_times=99, attempts=4)

    assert result.returncode == 1
    assert attempts == 4
    assert len(slept) == 3, "no sleep after the final attempt"
    assert "after 4 attempts" in result.stderr
