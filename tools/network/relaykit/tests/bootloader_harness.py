"""Standalone headless-browser acceptance harness for the B3 bootloader.

Brings up the real stack — registry (uvicorn) + connector (serve-file
mode) — publishes a link to a 1.55 MB+ HTML artifact, then drives the
bootloader through ``agent-browser`` and asserts:

- the artifact renders: inner document title (via the sandbox postMessage
  bridge) + a client-computed SHA-256 of the received body both match;
- a dead token shows the single generic error page, leaking no token;
- the §5.4 endpoints[] seam behaves (empty → relay fallback; a working
  endpoint short-circuits) in page context.

Kept OUT of the default pytest sweep (agent-browser under the parallel
`-n 8` load is flaky) — run it directly:

    python -m tools.network.relaykit.tests.bootloader_harness

Exit 0 = all assertions passed. The pytest wrapper
(``test_bootloader_browser.py``) invokes this as a subprocess only when
``AUTONET_BROWSER_TEST=1``.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.registry.signing import sign_request

REPO = Path(__file__).resolve().parents[4]
ORG = "77777777-7777-4777-8777-777777777777"
TARGET = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_artifact(path: Path) -> str:
    """A >1.55 MB self-contained HTML deck that posts its title upward."""
    filler = "<p>binder section lorem ipsum dolor sit amet consectetur.</p>" * 26000
    html = (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<title>OSS Insights Binder — Anchore</title></head><body>"
        "<h1>OSS Insights Briefing</h1>" + filler +
        "<script>parent.postMessage({autonet_title: document.title}, '*');</script>"
        "</body></html>"
    ).encode()
    path.write_bytes(html)
    assert len(html) > 1_550_000, len(html)
    return hashlib.sha256(html).hexdigest()


def ab_eval(js: str, timeout: float = 30.0) -> object:
    result = subprocess.run(
        ["agent-browser", "--json", "eval", js],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"agent-browser eval failed: {result.stderr[:500]}")
    payload = json.loads(result.stdout)
    # agent-browser --json wraps the eval result as {success, data:{result}, error}.
    value = payload
    for key in ("data", "result"):
        if isinstance(value, dict) and key in value:
            value = value[key]
    return json.loads(value) if isinstance(value, str) else value


def main() -> int:
    tmp = Path("/tmp/b3-harness")
    tmp.mkdir(exist_ok=True)
    art_path = tmp / "artifact.html"
    art_sha = build_artifact(art_path)
    art_len = art_path.stat().st_size

    reg_port = free_port()
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    db = tmp / "registry.db"
    if db.exists():
        db.unlink()

    procs = []
    try:
        registry = subprocess.Popen(
            [sys.executable, "-m", "tools.network.registry", "--db", str(db),
             "--host", "127.0.0.1", "--port", str(reg_port)],
            cwd=str(REPO), env=env,
            stdout=open(tmp / "registry.log", "wb"), stderr=subprocess.STDOUT,
        )
        procs.append(registry)
        for _ in range(60):
            try:
                if httpx.get(f"http://127.0.0.1:{reg_port}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            raise RuntimeError("registry did not come up")

        root, session = KeyPair.generate(), KeyPair.generate()
        now = int(time.time())
        session_cert = issue_cert(
            root, session.public_hex, scope=("link:publish", "tunnel:serve"), org=ORG,
            subject=Subject("operator", "op-1"), not_before=now - 300, not_after=now + 7 * 86_400,
        )
        with httpx.Client(base_url=f"http://127.0.0.1:{reg_port}") as client:
            assert client.post("/v1/orgs", json=sign_request(
                root, "POST", "/v1/orgs",
                {"org_uuid": ORG, "root_pub": root.public_hex, "recovery_policy": "none"},
                ts=now)).status_code == 201
            token = client.post("/v1/links", json=sign_request(
                session, "POST", "/v1/links",
                {"org": ORG, "target_uuid": TARGET, "target_type": "present"},
                ts=now, cert=session_cert)).json()["token"]

        key_file, cert_file = tmp / "key.hex", tmp / "cert.json"
        key_file.write_text(session.private_hex)
        cert_file.write_text(session_cert.to_json().decode("ascii"))
        connector = subprocess.Popen(
            [sys.executable, "-m", "tools.network.relaykit.connector",
             "--relay", f"ws://127.0.0.1:{reg_port}", "--org", ORG,
             "--key-file", str(key_file), "--cert-file", str(cert_file),
             "--mode", "serve-file", "--file", str(art_path),
             "--content-type", "text/html", "--min-backoff", "0.1"],
            cwd=str(REPO), env=env,
            stdout=open(tmp / "connector.log", "wb"), stderr=subprocess.STDOUT,
        )
        procs.append(connector)
        time.sleep(2.5)  # connector dials in

        base = f"http://127.0.0.1:{reg_port}"
        failures = []

        # 1. Render the 1.55MB artifact.
        subprocess.run(["agent-browser", "open", f"{base}/l/{token}"],
                       capture_output=True, timeout=30)
        deadline = time.time() + 25
        rendered = None
        while time.time() < deadline:
            state = ab_eval("JSON.stringify(window.autonet ? window.autonet.state : {phase:'noload'})")
            if state.get("phase") in ("rendered", "error"):
                rendered = state
                break
            time.sleep(0.5)
        if not rendered or rendered.get("phase") != "rendered":
            failures.append(f"artifact did not render: {rendered}")
        else:
            if rendered.get("bodySha256") != art_sha:
                failures.append(f"body sha mismatch: {rendered.get('bodySha256')} != {art_sha}")
            if rendered.get("bodyLength") != art_len:
                failures.append(f"body length mismatch: {rendered.get('bodyLength')} != {art_len}")
            if rendered.get("artifactTitle") != "OSS Insights Binder — Anchore":
                failures.append(f"inner title not asserted: {rendered.get('artifactTitle')!r}")
            if rendered.get("transport") != "relay":
                failures.append(f"unexpected transport: {rendered.get('transport')}")

        # 2. Dead token → generic error page, no token leak.
        subprocess.run(["agent-browser", "open", f"{base}/l/{'0' * 32}"],
                       capture_output=True, timeout=30)
        time.sleep(1.5)
        err = ab_eval(
            "JSON.stringify({phase: window.autonet.state.phase,"
            " errVisible: !document.getElementById('error-view').hidden,"
            " leaksToken: document.body.innerText.includes('00000000')})")
        if not (err.get("phase") == "error" and err.get("errVisible") and not err.get("leaksToken")):
            failures.append(f"error page wrong: {err}")

        # 3. endpoints[] seam in page context.
        seam = ab_eval(
            "(async () => {"
            " const A = window.autonet;"
            " const empty = await A.attemptEndpoints([], async () => null);"
            " const stubbed = await A.attemptEndpoints(['a','b'], A.attemptDirectEndpoint);"
            " const shortCircuit = await A.attemptEndpoints(['a','b'],"
            "   async (e) => e === 'a' ? {ok:true} : null);"
            " return JSON.stringify({empty, stubbed, shortCircuit});"
            "})()")
        if not (seam.get("empty") is None and seam.get("stubbed") is None
                and isinstance(seam.get("shortCircuit"), dict)):
            failures.append(f"endpoints seam wrong: {seam}")

        if failures:
            for f in failures:
                print("FAIL:", f)
            return 1
        print(f"PASS: artifact {art_len} bytes rendered (sha {art_sha[:12]}), "
              f"title asserted, error-page anti-enumeration, endpoints[] seam.")
        return 0
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        subprocess.run(["agent-browser", "close"], capture_output=True)


if __name__ == "__main__":
    sys.exit(main())
