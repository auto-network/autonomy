"""L2.B browser contract for the Dashboard's root push-only service worker.

The fixture appends a test harness to the served worker, never to the production
asset.  That harness invokes the production worker's payload, IndexedDB, and
refresh functions inside Chrome.  Localhost is a browser secure context, so the
test needs neither a certificate nor a live Dashboard login.
"""

from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
import urllib.parse
import urllib.request

import pytest

from tools.dashboard.approval_kind_registry import PRODUCTION_APPROVAL_REGISTRY


DASHBOARD = Path(__file__).resolve().parents[1]
CONTROLLER = DASHBOARD / "static" / "js" / "web-push-register.js"
WORKER = DASHBOARD / "static" / "service-worker.js"
EVENT_ID = "A" * 43
UPDATE_TOKEN = "t" * 43
NEXT_UPDATE_TOKEN = "u" * 43


_WORKER_COMMAND = """
async function workerCommand(message) {
  const registration = await navigator.serviceWorker.ready;
  const worker = registration.active || navigator.serviceWorker.controller;
  return await new Promise((resolve, reject) => {
    const channel = new MessageChannel();
    const timer = setTimeout(() => reject(new Error('worker command timeout')), 5000);
    channel.port1.onmessage = (event) => {
      clearTimeout(timer);
      channel.port1.close();
      resolve(event.data);
    };
    worker.postMessage(message, [channel.port2]);
  });
}
"""


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


def _browser_ref(*, role: str, name: str) -> str:
    """Resolve one accessibility ref, including Chrome WebUI shadow DOM."""

    snapshot = _agent_browser("snapshot", "-i", "-c")
    assert isinstance(snapshot, dict), snapshot
    refs = snapshot.get("refs")
    assert isinstance(refs, dict), snapshot
    matches = [
        ref for ref, item in refs.items()
        if isinstance(item, dict)
        and item.get("role") == role
        and item.get("name") == name
    ]
    assert len(matches) == 1, (role, name, refs)
    return f"@{matches[0]}"


@contextmanager
def _notification_permission(origin: str):
    """Allow one exact origin through Chrome's own content-settings UI.

    Chrome 152 headless acknowledges ``Browser.setPermission`` but leaves the
    active page at ``Notification.permission === 'default'``.  The browser's
    Notifications settings page updates the same real profile and lets the
    worker exercise ``showNotification``/``getNotifications`` without a mock
    permission branch or a second Chrome process.
    """

    _agent_browser("open", "chrome://settings/content/notifications")
    _agent_browser("wait", "300")
    clicked = _evaluate("""(() => {
      const matches = [];
      function visit(root) {
        for (const item of root.querySelectorAll('*')) {
          if (item.getAttribute('aria-label') === 'Add site to allowed list') {
            matches.push(item);
          }
          if (item.shadowRoot) visit(item.shadowRoot);
        }
      }
      visit(document);
      if (matches.length !== 1) return false;
      matches[0].click();
      return true;
    })()""")
    assert clicked is True
    _agent_browser("wait", "100")
    _agent_browser("fill", _browser_ref(role="textbox", name="Site"), origin)
    _agent_browser("click", _browser_ref(role="button", name="Add"))
    _agent_browser("wait", "200")
    _agent_browser("open", origin)
    try:
        yield
    finally:
        quoted = urllib.parse.quote(origin.rstrip("/"), safe="")
        _agent_browser(
            "open", f"chrome://settings/content/siteDetails?site={quoted}",
        )
        _agent_browser("wait", "200")
        _agent_browser(
            "click", _browser_ref(role="button", name="Reset permissions"),
        )
        _agent_browser("wait", "100")
        _agent_browser("click", _browser_ref(role="button", name="Reset"))


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
                    "self.addEventListener('message', (event) => {\n"
                    "  if (!event.data || !event.data.type.startsWith('test-')) return;\n"
                    "  const reply = event.ports && event.ports[0];\n"
                    "  const run = async () => {\n"
                    "    if (event.data.type === 'test-payload') {\n"
                    "      const data = event.data.payload === null ? null : {\n"
                    "        text: () => event.data.payload,\n"
                    "      };\n"
                    "      const message = pushNotification({data});\n"
                    "      return {message, options: notificationOptions(message)};\n"
                    "    }\n"
                    "    if (event.data.type === 'test-show-payload') {\n"
                    "      const data = event.data.payload === null ? null : {\n"
                    "        text: () => event.data.payload,\n"
                    "      };\n"
                    "      const message = pushNotification({data});\n"
                    "      await self.registration.showNotification(\n"
                    "        message.title, notificationOptions(message),\n"
                    "      );\n"
                    "      const shown = await self.registration.getNotifications({\n"
                    "        tag: message.tag,\n"
                    "      });\n"
                    "      const result = {\n"
                    "        count: shown.length,\n"
                    "        notifications: shown.map(item => ({\n"
                    "          title: item.title, body: item.body, tag: item.tag,\n"
                    "          data: item.data, actions: item.actions || [],\n"
                    "        })),\n"
                    "      };\n"
                    "      if (event.data.close_after) shown.forEach(item => item.close());\n"
                    "      return result;\n"
                    "    }\n"
                    "    if (event.data.type === 'test-read-device') {\n"
                    "      return {ok: true, record: await readDeviceRecord() || null};\n"
                    "    }\n"
                    "    if (event.data.type === 'test-refresh-device') {\n"
                    "      const raw = event.data.subscription;\n"
                    "      const subscription = raw === null ? null : {\n"
                    "        endpoint: raw.endpoint, toJSON: () => raw,\n"
                    "      };\n"
                    "      return refreshStoredDevice(\n"
                    "        await readDeviceRecord(), subscription,\n"
                    "        Boolean(event.data.preserve_on_refusal),\n"
                    "      );\n"
                    "    }\n"
                    "    return {ok: false, reason: 'unknown_test'};\n"
                    "  };\n"
                    "  event.waitUntil(run().then(\n"
                    "    value => { if (reply) reply.postMessage(value); },\n"
                    "    error => { if (reply) reply.postMessage({\n"
                    "      ok: false, reason: String(error && error.message || error),\n"
                    "    }); },\n"
                    "  ));\n"
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

        def do_POST(self):  # noqa: N802 - stdlib HTTP handler contract
            match = re.fullmatch(
                r"/api/web-push/devices/([A-Za-z0-9_-]{16,128})/refresh",
                self.path,
            )
            if match is None:
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                self.send_error(400)
                return
            self.server.refresh_requests.append({
                "device_id": match.group(1),
                "authorization": self.headers.get("Authorization"),
                "credentials_cookie": self.headers.get("Cookie"),
                "payload": payload,
            })
            status = self.server.refresh_status
            if status == 200 and payload.get("subscription") is None:
                response = {"ok": True, "retired": True}
            elif status == 200:
                response = {
                    "ok": True,
                    "device_id": match.group(1),
                    "status": "active",
                    "device_update_token": NEXT_UPDATE_TOKEN,
                    "token_version": payload["token_version"] + 1,
                }
            else:
                response = {
                    "ok": False,
                    "error": (
                        "device_not_found" if status == 404
                        else "temporarily_unavailable"
                    ),
                }
            body = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.worker_build = "first"
    server.refresh_status = 200
    server.refresh_requests = []
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

    def test_payload_validation_is_generic_deduped_and_route_bounded(
        self, worker_origin,
    ):
        origin, _server = worker_origin
        _agent_browser("open", origin)
        result = _evaluate(
            f"""(async () => {{
              {_WORKER_COMMAND}
              const base = {{
                v: 1,
                event_id: {json.dumps(EVENT_ID)},
                class: 'approval.commit_sign.requested',
                title: 'Secret title from sender',
                body: 'Secret body from sender',
                route: '/activity?focus=approval&id=opaque_1',
                tag: 'attention:' + {json.dumps(EVENT_ID)},
                issued_at: 1,
                expires_at: 2,
              }};
              const valid = await workerCommand({{
                type: 'test-payload', payload: JSON.stringify(base),
              }});
              const duplicate = await workerCommand({{
                type: 'test-payload', payload: JSON.stringify(base),
              }});
              const unsafe = await workerCommand({{
                type: 'test-payload', payload: JSON.stringify({{
                  ...base, route: 'https://evil.example/steal',
                }}),
              }});
              const malformed = await workerCommand({{
                type: 'test-payload', payload: '{{',
              }});
              const future = await workerCommand({{
                type: 'test-payload', payload: JSON.stringify({{...base, v: 2}}),
              }});
              const unknownClass = await workerCommand({{
                type: 'test-payload', payload: JSON.stringify({{
                  ...base, class: 'caller.chosen',
                }}),
              }});
              const oversized = await workerCommand({{
                type: 'test-payload', payload: 'x'.repeat(2049),
              }});
              return JSON.stringify({{
                valid, duplicate, unsafe, malformed, future, unknownClass, oversized,
              }});
            }})()"""
        )
        assert result["valid"]["message"] == {
            "title": "Autonomy needs your attention",
            "body": "Open the dashboard to review.",
            "route": "/activity?focus=approval&id=opaque_1",
            "tag": f"attention:{EVENT_ID}",
            "eventId": EVENT_ID,
        }
        assert result["valid"]["options"]["renotify"] is False
        assert result["valid"]["options"]["requireInteraction"] is False
        assert result["valid"]["options"].get("actions", []) in (
            [], [{"action": "review", "title": "Review"}],
        )
        assert result["duplicate"]["message"]["tag"] == result["valid"]["message"]["tag"]
        assert result["unsafe"]["message"]["route"] == "/activity"
        fallback = {
            "title": "Autonomy needs your attention",
            "body": "Open the dashboard to review.",
            "route": "/activity",
            "tag": "autonomy-attention",
            "eventId": None,
        }
        for key in ("malformed", "future", "unknownClass", "oversized"):
            assert result[key]["message"] == fallback

        with _notification_permission(origin):
            assert _evaluate(
                "JSON.stringify(Notification.permission)",
            ) == "granted"
            shown = _evaluate(
                f"""(async () => {{
                  {_WORKER_COMMAND}
                  const payload = JSON.stringify({{
                    v: 1,
                    event_id: {json.dumps(EVENT_ID)},
                    class: 'approval.commit_sign.requested',
                    route: '/activity?focus=approval&id=opaque_1',
                    tag: 'attention:' + {json.dumps(EVENT_ID)},
                    issued_at: 1,
                    expires_at: 2,
                  }});
                  const first = await workerCommand({{
                    type: 'test-show-payload', payload,
                  }});
                  const replacement = await workerCommand({{
                    type: 'test-show-payload', payload, close_after: true,
                  }});
                  return JSON.stringify({{permission: Notification.permission, first, replacement}});
                }})()""",
            )
        assert shown["permission"] == "granted"
        assert shown["first"]["count"] == 1
        assert shown["replacement"]["count"] == 1
        assert shown["replacement"]["notifications"][0]["tag"] == (
            f"attention:{EVENT_ID}"
        )
        assert shown["replacement"]["notifications"][0]["title"] == (
            "Autonomy needs your attention"
        )

    def test_update_token_rotation_offline_latch_and_forget_cleanup(
        self, worker_origin,
    ):
        origin, server = worker_origin
        _agent_browser("open", origin)
        server.refresh_status = 200
        server.refresh_requests.clear()
        device_id = "device_installation_1"
        application_key = "B" * 87
        subscription = {
            "endpoint": "https://push.example.test/rotated",
            "expirationTime": None,
            "keys": {"p256dh": "cHVia2V5", "auth": "YXV0aA"},
        }
        rotated = _evaluate(
            f"""(async () => {{
              {_WORKER_COMMAND}
              await workerCommand({{
                type: 'web-push-store-device',
                record: {{
                  v: 1,
                  device_id: {json.dumps(device_id)},
                  update_token: {json.dumps(UPDATE_TOKEN)},
                  token_version: 1,
                  endpoint_hash: {'"' + 'a' * 64 + '"'},
                  vapid_key_id: 'vapid-key-1',
                  application_server_key: {json.dumps(application_key)},
                  pending_refresh: false,
                }},
              }});
              const refresh = await workerCommand({{
                type: 'test-refresh-device',
                subscription: {json.dumps(subscription)},
              }});
              const after = await workerCommand({{type: 'test-read-device'}});
              return JSON.stringify({{refresh, after}});
            }})()"""
        )
        assert rotated["refresh"] == {"ok": True, "retired": False}
        record = rotated["after"]["record"]
        assert record["update_token"] == NEXT_UPDATE_TOKEN
        assert record["token_version"] == 2
        assert record["pending_refresh"] is False
        assert re.fullmatch(r"[a-f0-9]{64}", record["endpoint_hash"])
        request = server.refresh_requests[-1]
        assert request["authorization"] == f"WebPushDevice {UPDATE_TOKEN}"
        assert request["credentials_cookie"] is None
        assert request["payload"]["old_endpoint_hash"] == "a" * 64
        assert request["payload"]["token_version"] == 1

        server.refresh_status = 503
        deferred = _evaluate(
            f"""(async () => {{
              {_WORKER_COMMAND}
              const response = await workerCommand({{
                type: 'test-refresh-device',
                subscription: {json.dumps(subscription)},
              }});
              const after = await workerCommand({{type: 'test-read-device'}});
              return JSON.stringify({{response, after}});
            }})()"""
        )
        assert deferred["response"] == {
            "ok": False, "reason": "temporarily_unavailable",
        }
        assert deferred["after"]["record"]["pending_refresh"] is True
        assert deferred["after"]["record"]["token_version"] == 2

        server.refresh_status = 200
        reconciled = _evaluate(
            f"""(async () => {{
              {_WORKER_COMMAND}
              const response = await workerCommand({{
                type: 'test-refresh-device',
                subscription: {json.dumps(subscription)},
              }});
              const after = await workerCommand({{type: 'test-read-device'}});
              return JSON.stringify({{response, after}});
            }})()"""
        )
        assert reconciled["response"] == {"ok": True, "retired": False}
        assert reconciled["after"]["record"]["pending_refresh"] is False
        assert reconciled["after"]["record"]["token_version"] == 3

        server.refresh_status = 404
        refused = _evaluate(
            f"""(async () => {{
              {_WORKER_COMMAND}
              const before = (await workerCommand({{type: 'test-read-device'}})).record;
              const response = await workerCommand({{
                type: 'web-push-forget-local', require_token_retirement: true,
              }});
              const after = (await workerCommand({{type: 'test-read-device'}})).record;
              return JSON.stringify({{before, response, after}});
            }})()"""
        )
        assert refused["response"] == {"ok": False, "reason": "device_not_found"}
        assert refused["after"]["update_token"] == refused["before"]["update_token"]
        assert refused["after"]["pending_refresh"] is True

        server.refresh_status = 200
        forgotten = _evaluate(
            f"""(async () => {{
              {_WORKER_COMMAND}
              const response = await workerCommand({{
                type: 'web-push-forget-local', require_token_retirement: true,
              }});
              const after = await workerCommand({{type: 'test-read-device'}});
              return JSON.stringify({{response, after}});
            }})()"""
        )
        assert forgotten == {
            "response": {"ok": True},
            "after": {"ok": True, "record": None},
        }
        assert server.refresh_requests[-1]["payload"]["subscription"] is None


def test_controller_exports_only_the_normative_nine_states_and_current_api():
    controller = CONTROLLER.read_text()
    expected = {
        "unsupported", "requires_install", "permission_default",
        "permission_denied", "subscribing", "active", "stale", "revoked",
        "error_retryable",
    }
    assert set(re.findall(r"result\('([^']+)'", controller)) == expected
    assert controller.count("Notification.requestPermission()") == 1
    assert controller.index("Notification.requestPermission()") < controller.index(
        "pushManager.subscribe",
    )
    assert "/api/web-push/config" in controller
    assert "/api/web-push/devices/" in controller
    assert "/api/web-push/state" not in controller
    assert "/api/web-push/subscriptions" not in controller
    assert "subscription.unsubscribe()" in controller
    assert "reg.getNotifications()" in controller
    assert "deleteDeviceRecord()" in controller
    assert "navigator.clearAppBadge" in controller
    assert "require_token_retirement: !serverRetired" in controller
    assert "config lists only the current stable" in controller


def test_worker_source_is_closed_to_registered_classes_and_nonsemantic_close():
    worker = WORKER.read_text()
    expected_classes = {
        item.notification_class
        for item in PRODUCTION_APPROVAL_REGISTRY.kinds.values()
    }
    declared_classes = set(re.findall(r"'((?:approval\.)[^']+\.requested)'", worker))
    assert declared_classes == expected_classes
    assert "self.registration.showNotification" in worker
    assert "renotify: false" in worker
    assert "requireInteraction: false" in worker
    assert "action: 'review'" in worker
    assert "action: 'approve'" not in worker
    assert "action: 'decline'" not in worker
    assert "addEventListener('notificationclose'" not in worker
    assert "type: 'web-push:navigate'" in worker
    assert "await client.navigate(route)" in worker
    assert "self.clients.openWindow(route)" in worker
    assert "credentials: 'omit'" in worker
    assert "'Authorization': 'WebPushDevice '" in worker
    assert "pending_refresh: true" in worker
    assert "event.data.type !== 'web-push-store-device'" in worker
    assert "addEventListener('fetch'" not in worker
