"""Real-browser acceptance for the relay rich-render viewer.

The harness starts a registry, a real dashboard grant handler, and a controlled
remote-image origin. It publishes a note and a design, drives the registry
bootloader with ``agent-browser``, and checks the rendered null-origin frames.

Run directly, or through the opt-in pytest wrapper::

    python -m tools.network.relaykit.tests.bootloader_harness
    AUTONET_BROWSER_TEST=1 pytest tools/network/relaykit/tests/test_bootloader_browser.py
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from tools.network.idkit import KeyPair, Subject, issue_cert
from tools.network.registry.signing import sign_request


REPO = Path(__file__).resolve().parents[4]
ORG_UUID = "77777777-7777-4777-8777-777777777777"
GRAPH_ORG = "richviewer"
AB = ["agent-browser", "--session", "autonet-rich-harness"]
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RemoteHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str | None]] = []
    viewer_bytes: bytes = b""
    embed_url: str = ""

    def do_GET(self):
        type(self).requests.append((self.path, self.headers.get("Referer")))
        if self.path == "/viewer.html":
            body, mime = type(self).viewer_bytes, "text/html"
        elif self.path == "/embed.html":
            body = (
                "<!doctype html><title>embedder</title>"
                f"<iframe src={json.dumps(type(self).embed_url)}></iframe>"
            ).encode()
            mime = "text/html"
        elif self.path.endswith(".png"):
            body, mime = PNG, "image/png"
        else:
            body, mime = b"external page", "text/plain"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def ab(*args: str, timeout: float = 30.0, json_output: bool = False):
    command = [*AB]
    if json_output:
        command.append("--json")
    command.extend(args)
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"agent-browser {' '.join(args)} failed: {result.stderr[:800]} {result.stdout[:800]}"
        )
    return result.stdout


def ab_eval(js: str, timeout: float = 30.0) -> object:
    payload = json.loads(ab("eval", js, timeout=timeout, json_output=True))
    value = payload
    for key in ("data", "result"):
        if isinstance(value, dict) and key in value:
            value = value[key]
    return json.loads(value) if isinstance(value, str) else value


def tab_count() -> int:
    payload = json.loads(ab("tab", "list", json_output=True))
    return len(payload.get("data", {}).get("tabs", []))


def wait_main_phase(expected: str, timeout: float = 25.0) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = ab_eval(
            "JSON.stringify(window.autonet ? window.autonet.state : {phase:'noload'})"
        )
        if last.get("phase") == expected:
            return last
        if last.get("phase") == "error":
            return last
        time.sleep(0.25)
    return last or {}


def publish(client, session, cert, now, target_uuid: str, target_type: str) -> str:
    response = client.post("/v1/links", json=sign_request(
        session, "POST", "/v1/links",
        {"org": ORG_UUID, "target_uuid": target_uuid, "target_type": target_type},
        ts=now, cert=cert,
    ))
    assert response.status_code == 201, response.text
    return response.json()["token"]


def cache_grant(settings_ops, schema, token: str, target_uuid: str, target_type: str):
    settings_ops.add_setting(
        schema.NETWORK_LINK_GRANT_SET_ID,
        schema.NETWORK_LINK_GRANT_REVISION,
        token,
        {
            "token": token,
            "target_uuid": target_uuid,
            "target_type": target_type,
            "meta": {},
            "subject": {"kind": "operator", "id": "browser-harness"},
            "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        org=GRAPH_ORG,
    )


def main() -> int:
    tmp = Path("/tmp/autonet-rich-harness")
    tmp.mkdir(exist_ok=True)
    for name in ("registry.db", "graph.db", "designs.db"):
        path = tmp / name
        if path.exists():
            path.unlink()

    remote_port = free_port()
    remote = ThreadingHTTPServer(("127.0.0.1", remote_port), RemoteHandler)
    remote_thread = threading.Thread(target=remote.serve_forever, daemon=True)
    remote_thread.start()
    RemoteHandler.requests.clear()
    RemoteHandler.viewer_bytes = (
        REPO / "tools" / "dashboard" / "relay_viewer" / "note-viewer.html"
    ).read_bytes()

    env = {
        **os.environ,
        "PYTHONPATH": str(REPO),
        "GRAPH_DB": str(tmp / "graph.db"),
        "GRAPH_ORG": GRAPH_ORG,
        "EXPERIMENTS_DB": str(tmp / "designs.db"),
    }
    os.environ.update({
        "GRAPH_DB": env["GRAPH_DB"],
        "GRAPH_ORG": GRAPH_ORG,
        "EXPERIMENTS_DB": env["EXPERIMENTS_DB"],
    })

    from agents import design_db
    from tools.graph import ops as graph_ops
    from tools.graph import settings_ops
    from tools.graph.schemas import network_identity as schema
    from tools.graph.schemas.org import ORG_REVISION, ORG_SET_ID
    from tools.graph.db import GraphDB
    from agents import workspace_settings

    GraphDB.close_all_pooled()
    design_db.DB_PATH = tmp / "designs.db"
    design_db._initialized = False
    settings_ops.add_setting(
        ORG_SET_ID, ORG_REVISION, GRAPH_ORG,
        {
            "name": "Rich Viewer Org", "color": "#315E81",
            "favicon": "/static/icon-192.png", "type": "shared",
        },
        org=GRAPH_ORG,
    )
    workspace_settings.invalidate_caches()

    own_image = tmp / "own.png"
    own_image.write_bytes(PNG)
    note_body = f"""## Rich heading

- one
- two

```python
print('highlighted')
```

![own image]({{1}})

![remote image](http://127.0.0.1:{remote_port}/remote.png)

[external link](http://127.0.0.1:{remote_port}/external)
[internal link](graph://aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa)

![[bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb]]

<script>window.noteScriptRan = true;document.getElementById('note-title').textContent='SCRIPT EXECUTED'</script>
<img src=x onerror="window.noteHandlerRan = true;document.getElementById('note-title').textContent='HANDLER EXECUTED'">
"""
    note = graph_ops.create_note(
        note_body, title="Relay Rich Viewer Acceptance",
        attachments=[str(own_image)], org=GRAPH_ORG,
    )
    design = design_db.create_design(
        title="Executable relay design",
        variants=[{"id": "v1", "html": (
            "<!doctype html><html><body><p id='ran'>not run</p>"
            "<script>window.designRan=true;document.getElementById('ran').textContent="
            "'design javascript ran';</script></body></html>"
        )}],
    )

    reg_port = free_port()
    registry_db = tmp / "registry.db"
    procs = []
    failures: list[str] = []
    try:
        registry = subprocess.Popen(
            [sys.executable, "-m", "tools.network.registry", "--db", str(registry_db),
             "--host", "127.0.0.1", "--port", str(reg_port)],
            cwd=str(REPO), env=env,
            stdout=open(tmp / "registry.log", "wb"), stderr=subprocess.STDOUT,
        )
        procs.append(registry)
        for _ in range(60):
            try:
                if httpx.get(
                    f"http://127.0.0.1:{reg_port}/healthz", timeout=1
                ).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            raise RuntimeError("registry did not start")

        root, session = KeyPair.generate(), KeyPair.generate()
        now = int(time.time())
        cert = issue_cert(
            root, session.public_hex,
            scope=("link:publish", "tunnel:serve"), org=ORG_UUID,
            subject=Subject("operator", "browser-harness"),
            not_before=now - 300, not_after=now + 7 * 86_400,
        )
        with httpx.Client(base_url=f"http://127.0.0.1:{reg_port}") as client:
            registered = client.post("/v1/orgs", json=sign_request(
                root, "POST", "/v1/orgs",
                {"org_uuid": ORG_UUID, "root_pub": root.public_hex,
                 "recovery_policy": "none"}, ts=now,
            ))
            assert registered.status_code == 201, registered.text
            note_token = publish(client, session, cert, now, note["id"], "note")
            design_token = publish(client, session, cert, now, design, "design")

        cache_grant(settings_ops, schema, note_token, note["id"], "note")
        cache_grant(settings_ops, schema, design_token, design, "design")

        key_file, cert_file = tmp / "key.hex", tmp / "cert.json"
        key_file.write_text(session.private_hex)
        cert_file.write_text(cert.to_json().decode("ascii"))
        connector = subprocess.Popen(
            [sys.executable, "-m", "tools.dashboard.link_serving",
             "--relay", f"ws://127.0.0.1:{reg_port}", "--org", ORG_UUID,
             "--key-file", str(key_file), "--cert-file", str(cert_file),
             "--graph-org", GRAPH_ORG, "--min-backoff", "0.1"],
            cwd=str(REPO), env=env,
            stdout=open(tmp / "connector.log", "wb"), stderr=subprocess.STDOUT,
        )
        procs.append(connector)
        time.sleep(2)

        base = f"http://127.0.0.1:{reg_port}"

        # First exercise the exact generated viewer as a top-level page, where
        # its DOM is directly inspectable. The full relay run below proves the
        # same bytes arrive through the sandboxed frame and authenticated bridge.
        ab("open", f"http://127.0.0.1:{remote_port}/viewer.html")
        png_b64 = base64.b64encode(PNG).decode("ascii")
        direct_markdown = (
            "## Direct heading\n\n```python\nprint('highlighted')\n```\n\n"
            "![own](cid:own)\n\n"
            "![missing](cid:missing)\n\n"
            f"![remote](http://127.0.0.1:{remote_port}/direct.png)\n\n"
            f"[external](http://127.0.0.1:{remote_port}/external) "
            "[internal](graph://aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa)\n\n"
            "<script>window.noteScriptRan=true</script>"
            "<img src=x onerror=\"window.noteHandlerRan=true\">"
        )
        ab_eval(
            "(() => {"
            f"const raw=atob({json.dumps(png_b64)});"
            "const bytes=new Uint8Array(raw.length);"
            "for(let i=0;i<raw.length;i++)bytes[i]=raw.charCodeAt(i);"
            "window.postMessage({v:1,op:'content',"
            f"title:'Direct Viewer',markdown:{json.dumps(direct_markdown)},"
            "parts:[{ref:'own',mime:'image/png',bytes:bytes.buffer}]},'*',[bytes.buffer]);"
            "return true;})()"
        )
        direct = None
        deadline = time.time() + 10
        while time.time() < deadline:
            direct = ab_eval("""JSON.stringify((() => {
              const links = [...document.querySelectorAll('a')];
              const external = links.find(a => a.textContent === 'external');
              const own = [...document.images].find(i => i.alt === 'own');
              const remote = [...document.images].find(i => i.alt === 'remote');
              const code = document.querySelector('pre code');
              return {
                title: document.title,
                heading: document.querySelector('h2')?.textContent,
                highlighted: !!(code && code.classList.contains('hljs') && code.children.length),
                ownBlob: !!(own && own.src.startsWith('blob:') && own.complete && own.naturalWidth),
                remoteLoaded: !!(remote && remote.complete && remote.naturalWidth),
                missingPlaceholder: [...document.querySelectorAll('.image-unavailable')]
                  .some(el => el.textContent === 'Image unavailable: missing'),
                externalTarget: external?.target,
                externalRel: external?.rel,
                internalInert: (() => {
                  const internal = links.find(a => a.textContent === 'internal');
                  return document.body.innerText.includes('internal')
                    && (!internal || !internal.hasAttribute('href'));
                })(),
                scriptRan: window.noteScriptRan === true,
                handlerRan: window.noteHandlerRan === true,
                scriptsRemain: document.querySelectorAll('#note-body script').length,
              };
            })())""")
            if direct.get("ownBlob") and direct.get("remoteLoaded"):
                break
            time.sleep(0.2)
        direct_expected = {
            "title": "Direct Viewer", "heading": "Direct heading",
            "highlighted": True, "ownBlob": True, "remoteLoaded": True,
            "missingPlaceholder": True,
            "externalTarget": "_blank", "externalRel": "noopener noreferrer",
            "internalInert": True, "scriptRan": False, "handlerRan": False,
            "scriptsRemain": 0,
        }
        if direct != direct_expected:
            failures.append(f"direct viewer behavior mismatch: {direct}")

        ab("open", f"{base}/l/{note_token}")
        state = wait_main_phase("rendered")
        if state.get("phase") != "rendered":
            failures.append(f"note outer frame did not render: {state}")
        else:
            actual_title = None
            deadline = time.time() + 5
            while time.time() < deadline:
                state = ab_eval("JSON.stringify(window.autonet.state)")
                actual_title = state.get("artifactTitle")
                if actual_title == "Relay Rich Viewer Acceptance":
                    break
                time.sleep(0.1)
            if actual_title != "Relay Rich Viewer Acceptance":
                failures.append(f"note title bridge failed: {actual_title!r}")
            brand = ab_eval(
                "JSON.stringify((() => {"
                "const el=document.getElementById('brand');const img=el.querySelector('img');"
                "return {title:el.title,text:el.textContent,imageAlt:img?.alt,"
                "imageLoaded:!!(img && img.complete && img.naturalWidth)};})())"
            )
            if brand != {
                "title": "Rich Viewer Org", "text": "",
                "imageAlt": "Rich Viewer Org", "imageLoaded": True,
            }:
                failures.append(f"authenticated org favicon failed: {brand}")
            snapshot = ab("snapshot")
            required = (
                'heading "Relay Rich Viewer Acceptance"',
                'heading "Rich heading"',
                "print('highlighted')", 'image "own image"',
                'image "remote image"', 'link "external link"',
                'StaticText " internal link"',
                '![[bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb]]',
            )
            missing = [item for item in required if item not in snapshot]
            if missing:
                failures.append(f"relay note snapshot missing {missing}: {snapshot}")
            if "SCRIPT EXECUTED" in snapshot or "HANDLER EXECUTED" in snapshot:
                failures.append("sanitized note code executed in the relay frame")
            before = ab_eval("JSON.stringify(window.autonet.state.artifactTitle)")
            ab_eval("window.postMessage({v:1,op:'title',title:'forged'},'*'); true")
            time.sleep(0.1)
            after = ab_eval("JSON.stringify(window.autonet.state.artifactTitle)")
            if before != after:
                failures.append("foreign window changed the authenticated title bridge")
            external_ref = re.search(
                r'link "external link" \[ref=(e\d+)\]', snapshot
            )
            if not external_ref:
                failures.append("external link had no browser interaction target")
            else:
                tabs_before = tab_count()
                ab("click", "@" + external_ref.group(1))
                time.sleep(0.3)
                tabs_after = tab_count()
                if tabs_after != tabs_before + 1:
                    failures.append(
                        f"external link did not open a new tab: {tabs_before} -> {tabs_after}"
                    )
                elif tabs_after > tabs_before:
                    ab("tab", "close")

        RemoteHandler.embed_url = f"{base}/l/{note_token}"
        ab("open", f"http://127.0.0.1:{remote_port}/embed.html")
        time.sleep(1)
        framed_snapshot = ab("snapshot")
        if "Relay Rich Viewer Acceptance" in framed_snapshot or "Rich heading" in framed_snapshot:
            failures.append(
                "frame-ancestors policy allowed a third-party page to render the share"
            )

        image_requests = [ref for path, ref in RemoteHandler.requests if path.endswith(".png")]
        if not image_requests:
            failures.append("controlled remote image was not requested")
        elif any(ref is not None for ref in image_requests):
            failures.append(f"remote request leaked Referer: {RemoteHandler.requests}")

        ab("open", f"{base}/l/{design_token}")
        state = wait_main_phase("rendered")
        if state.get("phase") != "rendered":
            failures.append(f"design outer frame did not render: {state}")
        else:
            snapshot = ab("snapshot")
            if "design javascript ran" not in snapshot:
                failures.append(f"design javascript did not run: {snapshot}")

        connector.terminate()
        connector.wait(timeout=5)
        time.sleep(0.5)
        ab("open", f"{base}/l/{note_token}")
        offline = wait_main_phase("error")
        offline_view = ab_eval(
            "JSON.stringify({phase:window.autonet.state.phase,"
            "kind:window.autonet.state.errorKind,"
            "status:document.getElementById('status-line').textContent,"
            "visible:!document.getElementById('disconnected-view').hidden})"
        )
        if offline_view != {
            "phase": "error", "kind": "disconnected", "status": "",
            "visible": True,
        }:
            failures.append(f"disconnected error state failed: {offline_view}, {offline}")

        ab("open", f"{base}/l/{'0' * 32}")
        time.sleep(0.5)
        error = ab_eval(
            "JSON.stringify({phase:window.autonet.state.phase,"
            "kind:window.autonet.state.errorKind,"
            "status:document.getElementById('status-line').textContent,"
            "visible:!document.getElementById('invalid-link-view').hidden,"
            "leaks:document.body.innerText.includes('00000000')})"
        )
        if error != {
            "phase": "error", "kind": "invalid", "status": "",
            "visible": True, "leaks": False,
        }:
            failures.append(f"invalid-link error view failed: {error}")

        if failures:
            for failure in failures:
                print("FAIL:", failure)
            return 1
        print(
            "PASS: rich note, own and remote images, highlighted code, sanitation, "
            "link policy, authenticated org favicon, source-authenticated bridge, "
            "frame-ancestor blocking, "
            "executable design, and truthful invalid/disconnected error states"
        )
        return 0
    finally:
        try:
            ab("close")
        except Exception:
            pass
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        remote.shutdown()
        remote.server_close()
        GraphDB.close_all_pooled()


if __name__ == "__main__":
    sys.exit(main())
