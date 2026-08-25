"""Real-browser contract for the Dashboard's root push-only service worker.

The production worker deliberately has no fetch handler or Cache Storage.  This
test serves the production registration controller and worker from a private
localhost origin, changes only a trailing worker build marker, and proves Chrome
activates the update in the already-open page.  Localhost is a browser secure
context, so the test needs neither a certificate nor a live Dashboard login.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import threading
import urllib.request

import pytest


DASHBOARD = Path(__file__).resolve().parents[1]
CONTROLLER = DASHBOARD / "static" / "js" / "web-push-register.js"
WORKER = DASHBOARD / "static" / "service-worker.js"


def _agent_browser(*args: str, stdin: str | None = None, timeout: int = 30):
    result = subprocess.run(
        ["agent-browser", "--json", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    for line in reversed(result.stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "data" in value:
            data = value["data"]
            if isinstance(data, dict) and "result" in data:
                return data["result"]
            return data
        return value
    raise AssertionError(f"agent-browser returned no JSON: {result.stdout!r}")


def _evaluate(expression: str):
    value = _agent_browser("eval", "--stdin", stdin=expression)
    return json.loads(value) if isinstance(value, str) else value


@pytest.fixture(scope="module")
def worker_origin():
    if shutil.which("agent-browser") is None:
        pytest.skip("agent-browser is not installed")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib HTTP handler contract
            if self.path == "/":
                body = (
                    "<!doctype html><meta charset=utf-8>"
                    "<title>Web Push worker contract</title>"
                    '<script src="/web-push-register.js"></script>'
                ).encode()
                content_type = "text/html; charset=utf-8"
            elif self.path == "/web-push-register.js":
                body = CONTROLLER.read_bytes()
                content_type = "application/javascript"
            elif self.path == "/service-worker.js":
                build = self.server.worker_build
                body = WORKER.read_bytes() + (
                    "\nconst __TEST_WORKER_BUILD__ = " + json.dumps(build) + ";\n"
                    "self.addEventListener('message', (event) => {\n"
                    "  if (event.data && event.data.type === 'worker-build') {\n"
                    "    event.source.postMessage({type: 'worker-build', "
                    "build: __TEST_WORKER_BUILD__});\n"
                    "  }\n"
                    "});\n"
                ).encode()
                content_type = "application/javascript"
            else:
                self.send_error(404)
                return

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if self.path == "/service-worker.js":
                self.send_header(
                    "Cache-Control", "no-cache, no-store, must-revalidate",
                )
                self.send_header("Service-Worker-Allowed", "/")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.worker_build = "first"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        yield origin, server
    finally:
        subprocess.run(
            ["agent-browser", "close"], capture_output=True, timeout=10,
        )
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TestWebPushServiceWorkerBrowser:
    def test_root_worker_updates_without_page_reload_or_fetch_cache(
        self, worker_origin,
    ):
        origin, server = worker_origin

        with urllib.request.urlopen(f"{origin}/service-worker.js") as response:
            assert response.headers["Service-Worker-Allowed"] == "/"
            assert response.headers["Cache-Control"] == (
                "no-cache, no-store, must-revalidate"
            )
            assert response.headers.get_content_type() == "application/javascript"

        _agent_browser("open", origin)
        first = _evaluate(
            """(async () => {
              const registration = await navigator.serviceWorker.ready;
              if (!navigator.serviceWorker.controller) {
                await new Promise((resolve, reject) => {
                  const timer = setTimeout(() => reject(new Error('no controller')), 10000);
                  navigator.serviceWorker.addEventListener('controllerchange', () => {
                    clearTimeout(timer); resolve();
                  }, {once: true});
                });
              }
              const build = await new Promise((resolve, reject) => {
                const timer = setTimeout(() => reject(new Error('no worker reply')), 5000);
                function receive(event) {
                  if (event.data && event.data.type === 'worker-build') {
                    clearTimeout(timer);
                    navigator.serviceWorker.removeEventListener('message', receive);
                    resolve(event.data.build);
                  }
                }
                navigator.serviceWorker.addEventListener('message', receive);
                navigator.serviceWorker.controller.postMessage({type: 'worker-build'});
              });
              const registrations = await navigator.serviceWorker.getRegistrations();
              return JSON.stringify({
                build,
                secure: window.isSecureContext,
                supported: window.AutonomyWebPush.supported(),
                registrations: registrations.length,
                scope: registration.scope,
                updateViaCache: registration.updateViaCache,
                cacheNames: await caches.keys(),
                navigationCount: performance.getEntriesByType('navigation').length,
              });
            })()"""
        )
        assert first == {
            "build": "first",
            "secure": True,
            "supported": True,
            "registrations": 1,
            "scope": f"{origin}/",
            "updateViaCache": "none",
            "cacheNames": [],
            "navigationCount": 1,
        }

        server.worker_build = "second"
        second = _evaluate(
            """(async () => {
              const registration = await navigator.serviceWorker.getRegistration('/');
              const changed = new Promise((resolve, reject) => {
                const timer = setTimeout(() => reject(new Error('update did not activate')), 15000);
                navigator.serviceWorker.addEventListener('controllerchange', () => {
                  clearTimeout(timer); resolve();
                }, {once: true});
              });
              await registration.update();
              await changed;
              const build = await new Promise((resolve, reject) => {
                const timer = setTimeout(() => reject(new Error('no updated worker reply')), 5000);
                function receive(event) {
                  if (event.data && event.data.type === 'worker-build') {
                    clearTimeout(timer);
                    navigator.serviceWorker.removeEventListener('message', receive);
                    resolve(event.data.build);
                  }
                }
                navigator.serviceWorker.addEventListener('message', receive);
                navigator.serviceWorker.controller.postMessage({type: 'worker-build'});
              });
              return JSON.stringify({
                build,
                registrations: (await navigator.serviceWorker.getRegistrations()).length,
                cacheNames: await caches.keys(),
                navigationCount: performance.getEntriesByType('navigation').length,
              });
            })()"""
        )
        assert second == {
            "build": "second",
            "registrations": 1,
            "cacheNames": [],
            "navigationCount": 1,
        }
