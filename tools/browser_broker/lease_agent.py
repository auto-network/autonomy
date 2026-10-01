"""Lease agent: the one program that drives a leased browser (auto-8q7oe.5).

Runs inside a lease container (image ``autonomy-browser:local``) and listens on
port 7300 on the ``autonomy-browser`` network, which only the dashboard and
lease containers join. Every request must carry ``X-Lease-Secret`` equal to
``BROWSER_LEASE_SECRET``; the dashboard generates it per lease and never hands
it to a caller. Design of record: graph://c330323d-986.

Endpoints:

- ``GET /health`` — probes the browser, its browsing context and the lease's
  page through CDP; a closed page is unhealthy.
- ``POST /command`` — ``{"op", "args"}`` for the operations in :data:`OPS`.
  None of them runs caller-supplied script.
- ``POST /abort`` — stops the running command and returns once it has stopped.
- ``POST /lock`` — ``{"locked": bool}``; while locked every command is refused
  (409), so none can start after the operator takes control; locking also
  stops the running command.
- ``POST /expiry`` — ``{"expires_at": <unix seconds>}``, passed to the watchdog.

Threads. Playwright's sync API belongs to the thread that started it, so the
main thread runs the browser and executes commands one at a time. HTTP
handlers run on their own threads and reach Chrome for ``/health`` and
``/abort`` over a CDP socket bound to 127.0.0.1 inside the container, on a
random port so a page probing the usual debugging ports finds nothing.

Stopping a command. ``Runtime.terminateExecution`` alone does not stop a
Playwright action that is still waiting for its element (measured,
auto-8q7oe.5): the action runs outside the page script it terminates. So every
command runs as attempts of at most :data:`ATTEMPT_MS` that check the abort
flag between them, ``/abort`` also sends ``Page.stopLoading``, and ``/abort``
returns only when the command thread has stopped.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import signal
import socket
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from tools.browser_broker.browser_controller import BrowserController, CommandError

PORT = 7300
SECRET_ENV = "BROWSER_LEASE_SECRET"
PROFILE_DIR = Path("/profile")
DOWNLOAD_DIR = Path("/tmp/downloads")
EXPIRY_FILE = Path("/tmp/lease/expiry")
AGENT_PID_FILE = Path("/tmp/lease/agent.pid")

#: The complete command surface. No operation evaluates caller-supplied
#: script, and none returns cookies or storage contents.
OPS = frozenset({
    "goto", "snapshot", "screenshot", "click", "fill", "type", "press",
    "wait", "url", "title", "text", "download",
})

ATTEMPT_MS = 500
DEFAULT_TIMEOUT_MS = 30_000
MAX_TIMEOUT_MS = 60_000
ABORT_WAIT_S = 1.0
MAX_TEXT_CHARS = 200_000
MAX_BODY_BYTES = 1 << 20

#: Chrome flags added to patchright's launch. --enable-unsafe-swiftshader:
#: without it Chrome in the container has no WebGL context at all, a louder
#: signal than a software renderer (auto-8q7oe.3).
CHROME_ARGS = ("--enable-unsafe-swiftshader",)
#: Playwright passes --disable-dev-shm-usage by default; the lease gets a sized
#: /dev/shm instead (design: "Resource limits").
CHROME_IGNORED_DEFAULTS = ("--disable-dev-shm-usage",)

_REF_RE = re.compile(r"^e[0-9]{1,6}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9]+(\+[A-Za-z0-9]+)*$")
_ROLE_RE = re.compile(r"^[a-z]{1,32}$")


class BadRequest(ValueError):
    """The request is malformed; answered with HTTP 400."""


class Aborted(RuntimeError):
    """The running command was stopped by ``POST /abort``."""


class Locked(RuntimeError):
    """The lease is locked (operator control or a privileged operation)."""


# ── request validation (pure; unit-tested) ─────────────────────────────


def secret_ok(presented: Optional[str], expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(
        (presented or "").encode(), expected.encode())


def _str(args: dict, key: str, *, required: bool = True, limit: int = 4096) -> Optional[str]:
    value = args.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > limit:
        raise BadRequest(f"{key} must be a non-empty string of at most {limit} characters")
    return value


def _timeout_ms(args: dict) -> int:
    value = args.get("timeout_ms", DEFAULT_TIMEOUT_MS)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_TIMEOUT_MS:
        raise BadRequest(f"timeout_ms must be an integer in 1..{MAX_TIMEOUT_MS}")
    return value


def parse_target(spec: Any) -> tuple[str, ...]:
    """A target names one element in exactly one way:

    ``{"ref": "e3"}`` (from ``snapshot``), ``{"role": "button", "name": "Sign in"}``,
    ``{"label": "Email"}``, ``{"text": "Continue"}`` or ``{"css": "#main"}``.
    CSS is always parsed by the CSS engine, never as another selector kind.
    """
    if not isinstance(spec, dict):
        raise BadRequest("target must be an object")
    keys = set(spec)
    if keys == {"ref"}:
        ref = _str(spec, "ref", limit=8)
        if not _REF_RE.fullmatch(ref):
            raise BadRequest("ref must look like e12, from snapshot")
        return ("ref", ref)
    if keys == {"role", "name"}:
        role = _str(spec, "role", limit=32)
        if not _ROLE_RE.fullmatch(role):
            raise BadRequest("role must be an ARIA role name")
        return ("role", role, _str(spec, "name", limit=256))
    for kind in ("label", "text", "css"):
        if keys == {kind}:
            return (kind, _str(spec, kind, limit=1024))
    raise BadRequest("target needs exactly one of ref, role+name, label, text or css")


def validate_command(body: Any) -> tuple[str, dict]:
    """``(op, args)`` with every argument checked, or BadRequest."""
    if not isinstance(body, dict) or set(body) - {"op", "args"}:
        raise BadRequest("command must be {\"op\", \"args\"}")
    op = body.get("op")
    if op not in OPS:
        raise BadRequest(f"unknown operation: {op!r}")
    args = body.get("args", {})
    if not isinstance(args, dict):
        raise BadRequest("args must be an object")
    allowed = {
        "goto": {"url", "timeout_ms"},
        "snapshot": set(),
        "screenshot": {"full_page"},
        "click": {"target", "timeout_ms"},
        "fill": {"target", "value", "timeout_ms"},
        "type": {"target", "text", "timeout_ms"},
        "press": {"target", "key", "timeout_ms"},
        "wait": {"text", "url", "target", "ms", "timeout_ms"},
        "url": set(),
        "title": set(),
        "text": {"target", "timeout_ms"},
        "download": {"target", "timeout_ms"},
    }[op]
    extra = set(args) - allowed
    if extra:
        raise BadRequest(f"{op} does not take {sorted(extra)}")
    out: dict = {"timeout_ms": _timeout_ms(args)}
    if op == "goto":
        url = _str(args, "url")
        if urlparse(url).scheme not in {"http", "https"}:
            raise BadRequest("goto only accepts http(s) URLs")
        out["url"] = url
    elif op == "screenshot":
        full = args.get("full_page", False)
        if not isinstance(full, bool):
            raise BadRequest("full_page must be a boolean")
        out["full_page"] = full
    elif op in {"click", "download"}:
        out["target"] = parse_target(args.get("target"))
    elif op == "fill":
        out["target"] = parse_target(args.get("target"))
        value = args.get("value")
        if not isinstance(value, str) or len(value) > 65_536:
            raise BadRequest("value must be a string of at most 65536 characters")
        out["value"] = value
    elif op == "type":
        if "target" in args:
            out["target"] = parse_target(args["target"])
        out["text"] = _str(args, "text", limit=65_536)
    elif op == "press":
        if "target" in args:
            out["target"] = parse_target(args["target"])
        key = _str(args, "key", limit=64)
        if not _KEY_RE.fullmatch(key):
            raise BadRequest("key must be a key name such as Enter or Control+A")
        out["key"] = key
    elif op == "wait":
        given = [k for k in ("text", "url", "target", "ms") if k in args]
        if len(given) != 1:
            raise BadRequest("wait needs exactly one of text, url, target or ms")
        kind = given[0]
        if kind == "target":
            out["target"] = parse_target(args["target"])
        elif kind == "ms":
            ms = args["ms"]
            if isinstance(ms, bool) or not isinstance(ms, int) or not 0 < ms <= MAX_TIMEOUT_MS:
                raise BadRequest(f"ms must be an integer in 1..{MAX_TIMEOUT_MS}")
            out["ms"] = ms
        else:
            out[kind] = _str(args, kind, limit=2048)
    elif op == "text" and "target" in args:
        out["target"] = parse_target(args["target"])
    return op, out


# ── sign-in helpers (pure; unit-tested) ────────────────────────────────


def exact_origin(value: str) -> Optional[tuple[str, str, int]]:
    """``(scheme, host, port)`` of an origin or URL, or None. A bare hostname
    (how a credential's origin is stored) means ``https://<host>:443``."""
    if not isinstance(value, str) or not value:
        return None
    parsed = urlparse(value if "://" in value else f"https://{value}")
    if not parsed.hostname or parsed.scheme not in ("http", "https"):
        return None
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None
    return parsed.scheme, parsed.hostname.lower(), port


def same_https_origin(url: str, credential_origin: tuple[str, str, int]) -> bool:
    """HTTPS, and scheme, host and port equal to the credential's origin."""
    return credential_origin[0] == "https" and exact_origin(url) == credential_origin


VERIFICATION_TERMS = ("verification code", "security code", "one-time code", "two-factor",
                      "multi-factor", "2-step", "enter the code")
REFUSAL_TERMS = ("incorrect", "invalid", "didn't match", "did not match", "try again",
                 "not recognized", "wrong password")


def parse_login_request(body: Any) -> dict:
    """The page-work part of a sign-in: origin, fields and submit as semantic
    locators (label or role+name), optional success_text."""
    if not isinstance(body, dict):
        raise BadRequest("body must be an object")
    origin = exact_origin(body.get("origin", ""))
    if origin is None or origin[0] != "https":
        raise BadRequest("origin must be an https origin")
    fields, submit = body.get("fields"), body.get("submit")
    if not isinstance(fields, dict) or not fields or len(fields) > 4:
        raise BadRequest("fields must map 1-4 credential keys to locators")
    for key, spec in list(fields.items()) + [("submit", submit)]:
        if not isinstance(key, str) or not isinstance(spec, dict):
            raise BadRequest("every locator must be an object")
        if spec.get("kind") not in ("label", "role") or not isinstance(spec.get("name"), str) \
                or not spec["name"] or (spec["kind"] == "role" and not isinstance(spec.get("role"), str)):
            raise BadRequest("locators are {kind: label|role, name, role?}; CSS is not accepted")
    success = body.get("success_text") or []
    if not isinstance(success, list) or not all(isinstance(t, str) and t for t in success):
        raise BadRequest("success_text must be a list of strings")
    return {"origin": origin, "fields": fields, "submit": submit, "success_text": success[:5]}


# ── raw CDP, for the HTTP threads ──────────────────────────────────────


class Cdp:
    """Minimal CDP over the loopback debugging port, usable from any thread."""

    def __init__(self, port: int):
        self.port = port

    def _json(self, path: str) -> Any:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=2) as resp:
            return json.load(resp)

    def version(self) -> dict:
        return self._json("/json/version")

    def targets(self) -> list:
        return self._json("/json/list")

    def call(self, target_id: str, method: str, params: Optional[dict] = None,
             timeout: float = 2.0) -> dict:
        from websockets.sync.client import connect

        url = f"ws://127.0.0.1:{self.port}/devtools/page/{target_id}"
        with connect(url, open_timeout=timeout, close_timeout=0.5, max_size=None) as ws:
            ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            deadline = time.monotonic() + timeout
            while True:
                reply = json.loads(ws.recv(timeout=max(0.01, deadline - time.monotonic())))
                if reply.get("id") == 1:
                    return reply


def window_args(screen: str) -> list[str]:
    """Fill the Xvfb screen: there is no window manager to maximize the window."""
    match = re.fullmatch(r"([0-9]{3,5})x([0-9]{3,5})(x[0-9]+)?", screen)
    if not match:
        return []
    return ["--window-position=0,0", f"--window-size={match[1]},{match[2]}"]


def launch_options(cdp_port: int, screen: str) -> dict:
    """Chrome's launch options. Certificate errors are never ignored: lease pages
    can reach the dashboard on the lease network, and only its TLS certificate,
    which no lease hostname matches, keeps them out (see TOOL.md)."""
    return dict(
        channel="chrome", headless=False, no_viewport=True, chromium_sandbox=True,
        accept_downloads=True, ignore_https_errors=False,
        ignore_default_args=list(CHROME_IGNORED_DEFAULTS),
        args=[*CHROME_ARGS, *window_args(screen), f"--remote-debugging-port={cdp_port}"],
    )


def _free_loopback_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# ── the agent ──────────────────────────────────────────────────────────


class LeaseAgent(BrowserController):
    def __init__(self, profile_dir: Path = PROFILE_DIR, download_dir: Path = DOWNLOAD_DIR):
        super().__init__("lease", profile_dir, download_dir, start_url=None)
        self.cdp: Optional[Cdp] = None
        self.page_target_id: Optional[str] = None
        self._playwright = None
        self._context = None
        self._jobs: "queue.Queue[tuple[str, dict, queue.Queue]]" = queue.Queue(maxsize=1)
        self._abort = threading.Event()
        #: Set while the operator (or a privileged operation) holds the lease:
        #: every command is refused, so none can start after take-control.
        self._locked = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._shutdown = threading.Event()
        self._downloads: list = []

    # browser lifecycle

    def start(self) -> None:
        from patchright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.download_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        port = _free_loopback_port()
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            str(self.profile_dir), **launch_options(port, os.environ.get("BROWSER_SCREEN", "")))
        pages = self._context.pages
        self.page = pages[0] if pages else self._context.new_page()
        self.page.on("download", lambda download: self._downloads.append(download))
        session = self._context.new_cdp_session(self.page)
        self.page_target_id = session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
        session.detach()
        self.cdp = Cdp(port)

    def close(self) -> None:
        try:
            if self._context:
                self._context.close()
        finally:
            if self._playwright:
                self._playwright.stop()

    # health and abort (HTTP threads)

    def health(self) -> dict:
        report = {"browser": False, "context": False, "page": False}
        try:
            self.cdp.version()
            report["browser"] = True
            pages = [t for t in self.cdp.targets() if t.get("type") == "page"]
            report["context"] = bool(pages)
            if any(t.get("id") == self.page_target_id for t in pages):
                reply = self.cdp.call(self.page_target_id, "Runtime.evaluate",
                                      {"expression": "1", "returnByValue": True})
                report["page"] = reply.get("result", {}).get("result", {}).get("value") == 1
        except Exception:
            pass
        report["healthy"] = all(report.values())
        return report

    def abort(self) -> dict:
        if self._idle.is_set():
            return {"stopped": True, "running": False}
        self._abort.set()
        for method in ("Runtime.terminateExecution", "Page.stopLoading"):
            try:
                self.cdp.call(self.page_target_id, method, timeout=0.3)
            except Exception:
                pass
        return {"stopped": self._idle.wait(ABORT_WAIT_S), "running": True}

    # command thread

    def set_locked(self, locked: bool) -> dict:
        """Lock refuses every new command; locking also stops the running one."""
        if locked:
            self._locked.set()
            stopped = self.abort()
        else:
            self._locked.clear()
            stopped = {"stopped": True, "running": not self._idle.is_set()}
        return {"locked": self._locked.is_set(), **stopped}

    def submit(self, op: str, args: dict, timeout_s: float) -> dict:
        if self._locked.is_set():
            raise Locked()
        reply: queue.Queue = queue.Queue(maxsize=1)
        try:
            self._jobs.put_nowait((op, args, reply))
        except queue.Full:
            raise CommandError("busy") from None
        return reply.get(timeout=timeout_s)

    def serve_commands(self) -> None:
        """Run on the thread that started the browser until shutdown."""
        while not self._shutdown.is_set():
            try:
                op, args, reply = self._jobs.get(timeout=0.5)
            except queue.Empty:
                continue
            self._abort.clear()
            self._idle.clear()
            try:
                if op == "__internal__":
                    reply.put({"ok": True, "result": self.run_internal(args["op"], args["payload"])})
                    continue
                if self._locked.is_set():  # locked while this command waited in the queue
                    raise Aborted()
                reply.put({"ok": True, "result": self.run(op, args)})
            except Aborted:
                reply.put({"ok": False, "error": "aborted"})
            except Exception as exc:
                reply.put({"ok": False, "error": _error_text(exc)})
            finally:
                self._idle.set()

    def _check_abort(self) -> None:
        if self._abort.is_set():
            raise Aborted()

    def _bounded(self, action: Callable[[int], Any], timeout_ms: int) -> Any:
        """Run ``action(attempt_ms)`` in attempts of at most ATTEMPT_MS until it
        succeeds, the command's timeout passes, or the command is aborted."""
        from patchright.sync_api import TimeoutError as PlaywrightTimeout

        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            self._check_abort()
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                raise CommandError(f"timed out after {timeout_ms} ms")
            try:
                return action(min(ATTEMPT_MS, remaining))
            except PlaywrightTimeout:
                continue

    def _locator(self, target: tuple):
        page = self._need_page()
        kind = target[0]
        if kind == "ref":
            return page.locator(f"aria-ref={target[1]}")
        if kind == "css":
            return page.locator(f"css={target[1]}").first
        if kind == "role":
            return self._semantic_locator(page, "role", target[2], target[1])
        return self._semantic_locator(page, kind, target[1])

    def run_internal(self, op: str, payload: dict) -> Any:
        """Broker-only jobs (never caller commands): sign-in page work."""
        if op == "login_check":
            return self.login_check(payload["req"])
        if op == "login_submit":
            return self.login_submit(payload["req"], payload["credentials"])
        if op == "login_cleanup":
            return self.login_cleanup()
        raise CommandError(f"unknown internal job: {op}")

    def submit_internal(self, op: str, payload: dict, timeout_s: float) -> dict:
        """Queue a broker job. Runs even while the lease is locked (the lock is
        against the caller, and sign-in runs under it)."""
        reply: queue.Queue = queue.Queue(maxsize=1)
        try:
            self._jobs.put_nowait(("__internal__", {"op": op, "payload": payload}, reply))
        except queue.Full:
            raise CommandError("busy") from None
        return reply.get(timeout=timeout_s)

    def run(self, op: str, args: dict) -> Any:
        page = self._need_page()
        timeout = args["timeout_ms"]
        if op == "url":
            return page.url
        if op == "title":
            return page.title()
        if op == "goto":
            page.goto(args["url"], wait_until="domcontentloaded", timeout=timeout)
            self._check_abort()
            return {"url": page.url, "title": page.title()}
        if op == "snapshot":
            return page.locator(":root").aria_snapshot(mode="ai")
        if op == "screenshot":
            png = page.screenshot(full_page=args["full_page"])
            return {"png_base64": base64.b64encode(png).decode(), "bytes": len(png)}
        if op == "text":
            if "target" in args:
                locator = self._locator(args["target"])
                text = self._bounded(lambda t: locator.inner_text(timeout=t), timeout)
            else:
                text = page.locator("body").inner_text()
            return text[:MAX_TEXT_CHARS]
        if op == "click":
            locator = self._locator(args["target"])
            self._bounded(lambda t: locator.click(timeout=t), timeout)
            return {"clicked": True}
        if op == "fill":
            locator = self._locator(args["target"])
            self._bounded(lambda t: locator.fill(args["value"], timeout=t), timeout)
            return {"filled": True, "characters": len(args["value"])}
        if op == "type":
            if "target" in args:
                locator = self._locator(args["target"])
                self._bounded(lambda t: locator.focus(timeout=t), timeout)
            text = args["text"]
            for start in range(0, len(text), 16):
                self._check_abort()
                page.keyboard.type(text[start:start + 16])
            return {"typed": True, "characters": len(text)}
        if op == "press":
            if "target" in args:
                locator = self._locator(args["target"])
                self._bounded(lambda t: locator.press(args["key"], timeout=t), timeout)
            else:
                page.keyboard.press(args["key"])
            return {"pressed": args["key"]}
        if op == "wait":
            return self._wait(page, args, timeout)
        if op == "download":
            return self._download(page, args, timeout)
        raise CommandError(f"unknown operation: {op}")  # unreachable after validation

    # sign-in (auto-8q7oe.9) — run as jobs on the Playwright thread

    def _login_locator(self, page, spec: dict):
        kind, name = spec["kind"], spec["name"]
        return self._semantic_locator(page, kind, name, spec.get("role"))

    def login_check(self, req: dict) -> dict:
        """Step 2: the page is on the credential's exact HTTPS origin and every
        locator resolves to exactly one element. No secret exists yet."""
        page = self._need_page()
        if not same_https_origin(page.url, req["origin"]):
            return {"ok": False, "reason": "origin"}
        for spec in list(req["fields"].values()) + [req["submit"]]:
            if self._login_locator(page, spec).count() != 1:
                return {"ok": False, "reason": "locator"}
        return {"ok": True}

    def login_submit(self, req: dict, credentials: dict) -> dict:
        """Steps 4-6: re-check, type with real input events, submit, wait, and
        clean up (unless the page asks for a verification code)."""
        page = self._need_page()
        origin = req["origin"]
        handover = False

        def guard(route):
            request = route.request
            if (request.is_navigation_request() or request.method != "GET") \
                    and not same_https_origin(request.url, origin):
                route.abort()  # a submission (or navigation) to another origin
            else:
                route.continue_()

        page.route("**/*", guard)
        tainted = False
        try:
            for key, spec in req["fields"].items():
                if key not in credentials:
                    return {"authenticated": False, "reason": "field-not-provisioned"}
                if not same_https_origin(page.url, origin):
                    return {"authenticated": False, "reason": "origin"}
                locator = self._login_locator(page, spec)
                if locator.count() != 1:
                    return {"authenticated": False, "reason": "locator"}
                action = locator.evaluate(
                    "el => el.form ? (el.form.getAttribute('action') === null ? location.href"
                    " : el.form.action) : location.href")
                if not same_https_origin(action, origin):
                    return {"authenticated": False, "reason": "form-action"}
                input_type = (locator.get_attribute("type") or "text").lower()
                allowed = {"password"} if key == "password" else {"text", "email", "tel"}
                if input_type not in allowed:
                    return {"authenticated": False, "reason": "field-type"}
                # Type into THIS element, not whatever has focus: a page script
                # that moves focus must not receive the secret.
                locator.fill("", timeout=5000)
                locator.focus(timeout=5000)
                if not locator.evaluate("el => el === document.activeElement"):
                    return {"authenticated": False, "reason": "focus"}
                locator.press_sequentially(credentials[key], timeout=15000)
                if not locator.evaluate("el => el === document.activeElement"):
                    # Part of the secret may have gone elsewhere: the reload in
                    # `finally` discards every typed value, wherever it landed.
                    tainted = True
                    return {"authenticated": False, "reason": "focus"}
            if not same_https_origin(page.url, origin):
                return {"authenticated": False, "reason": "origin"}
            self._login_locator(page, req["submit"]).click(timeout=5000)
            outcome = self._login_wait(page, req)
            handover = outcome.get("human_required", False)
            return outcome
        except Exception as exc:
            return {"authenticated": False, "reason": "error", "detail": _error_text(exc)[:120]}
        finally:
            credentials.clear()
            try:
                page.unroute("**/*", guard)
            except Exception:
                pass
            if not handover:
                self.login_cleanup()
            if tainted:
                try:
                    page.reload(wait_until="domcontentloaded", timeout=30_000)
                except Exception:
                    pass

    def _login_wait(self, page, req: dict) -> dict:
        origin, deadline = req["origin"], time.monotonic() + 30
        start_url = page.url
        while time.monotonic() < deadline:
            page.wait_for_timeout(500)
            if not same_https_origin(page.url, origin):
                return {"authenticated": False, "reason": "redirect-to-other-origin"}
            for text in req["success_text"]:
                landmark = page.get_by_text(text, exact=False).first
                if landmark.count() and landmark.is_visible():
                    return {"authenticated": True, "reason": "success-text"}
            body = page.locator("body").inner_text().lower()
            if any(term in body for term in VERIFICATION_TERMS):
                return {"authenticated": False, "human_required": True, "reason": "verification-required"}
            password_visible = page.locator("input[type=password]:visible").count() > 0
            if not req["success_text"] and page.url != start_url and not password_visible:
                return {"authenticated": True, "reason": "left-login-page"}
            if password_visible and any(term in body for term in REFUSAL_TERMS):
                return {"authenticated": False, "reason": "refused"}
        return {"authenticated": False, "reason": "timeout"}

    def login_cleanup(self) -> dict:
        """Step 6: clear every password input in every frame and confirm it;
        reload the page when that cannot be confirmed."""
        page = self._need_page()
        clear = ("() => { for (const el of document.querySelectorAll('input[type=password]'))"
                 " { el.value = ''; } return [...document.querySelectorAll('input[type=password]')]"
                 ".every(el => el.value === ''); }")
        confirmed = True
        for frame in page.frames:
            try:
                confirmed = frame.evaluate(clear) and confirmed
            except Exception:
                confirmed = False
        if not confirmed:
            page.reload(wait_until="domcontentloaded", timeout=30_000)
        return {"cleaned": True, "reloaded": not confirmed}

    def _wait(self, page, args: dict, timeout: int) -> dict:
        if "ms" in args:
            end = time.monotonic() + args["ms"] / 1000
            while (left := end - time.monotonic()) > 0:
                self._check_abort()
                page.wait_for_timeout(min(ATTEMPT_MS, left * 1000))
            return {"waited_ms": args["ms"]}
        if "url" in args:
            self._bounded(lambda t: page.wait_for_url(args["url"], timeout=t), timeout)
            return {"url": page.url}
        if "text" in args:
            locator = page.get_by_text(args["text"], exact=False).first
        else:
            locator = self._locator(args["target"])
        self._bounded(lambda t: locator.wait_for(state="visible", timeout=t), timeout)
        return {"visible": True}

    def _download(self, page, args: dict, timeout: int) -> dict:
        seen = len(self._downloads)
        locator = self._locator(args["target"])
        self._bounded(lambda t: locator.click(timeout=t), timeout)
        deadline = time.monotonic() + timeout / 1000
        while len(self._downloads) == seen:
            self._check_abort()
            if time.monotonic() >= deadline:
                raise CommandError("no download started")
            page.wait_for_timeout(250)
        download = self._downloads[seen]
        file_id = secrets.token_hex(8)
        destination = self.download_dir / file_id
        download.save_as(str(destination))
        digest = hashlib.sha256()
        with destination.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return {"file": file_id, "suggested_name": download.suggested_filename,
                "size": destination.stat().st_size, "sha256": digest.hexdigest()}


def _error_text(exc: BaseException) -> str:
    # Playwright errors carry a multi-line call log; the first line is the
    # error, the rest can quote page content.
    return (str(exc).splitlines() or [type(exc).__name__])[0][:500]


def write_expiry(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0 \
            or value != value or value == float("inf"):
        raise BadRequest("expires_at must be a positive Unix time")
    EXPIRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = EXPIRY_FILE.with_suffix(".tmp")
    temporary.write_text(repr(float(value)))
    temporary.replace(EXPIRY_FILE)
    return float(value)


# ── HTTP ───────────────────────────────────────────────────────────────


class LeaseHandler(BaseHTTPRequestHandler):
    agent: LeaseAgent
    secret: str
    server_version = "LeaseAgent/1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _authorized(self) -> bool:
        if secret_ok(self.headers.get("X-Lease-Secret"), self.secret):
            return True
        self._json(401, {"error": "unauthorized"})
        return False

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 <= length <= MAX_BODY_BYTES:
            raise BadRequest("request body too large")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise BadRequest("request body is not JSON") from None

    def do_GET(self) -> None:
        if not self._authorized():
            return
        if self.path != "/health":
            return self._json(404, {"error": "not found"})
        report = self.agent.health()
        self._json(200 if report["healthy"] else 503, report)

    def do_POST(self) -> None:
        if not self._authorized():
            return
        try:
            body = self._body()
            if self.path == "/command":
                op, args = validate_command(body)
                try:
                    result = self.agent.submit(op, args, args["timeout_ms"] / 1000 + 30)
                except Locked:
                    return self._json(409, {"error": "locked"})
                except CommandError:
                    return self._json(409, {"error": "busy"})
                return self._json(200, result)
            if self.path in ("/login/check", "/login/submit"):
                req = parse_login_request(body)
                if self.path == "/login/check":
                    return self._json(200, self.agent.submit_internal(
                        "login_check", {"req": req}, 30)["result"])
                credentials = body.get("credentials")
                if not isinstance(credentials, dict) or not all(
                        isinstance(k, str) and isinstance(v, str) for k, v in credentials.items()):
                    raise BadRequest("credentials must map keys to strings")
                try:
                    reply = self.agent.submit_internal(
                        "login_submit", {"req": req, "credentials": credentials}, 90)
                finally:
                    credentials.clear()
                    body.clear()
                return self._json(200, reply["result"] if reply.get("ok") else
                                  {"authenticated": False, "reason": "error"})
            if self.path == "/login/cleanup":
                return self._json(200, self.agent.submit_internal("login_cleanup", {}, 60)["result"])
            if self.path == "/lock":
                if not isinstance(body, dict) or not isinstance(body.get("locked"), bool):
                    raise BadRequest('body must be {"locked": true|false}')
                return self._json(200, self.agent.set_locked(body["locked"]))
            if self.path == "/abort":
                return self._json(200, self.agent.abort())
            if self.path == "/expiry":
                if not isinstance(body, dict):
                    raise BadRequest("body must be an object")
                return self._json(200, {"expires_at": write_expiry(body.get("expires_at"))})
            self._json(404, {"error": "not found"})
        except BadRequest as exc:
            self._json(400, {"error": str(exc)})
        except queue.Empty:
            self._json(504, {"error": "command did not finish"})


def _watch_browser(agent: LeaseAgent) -> None:
    """Exit the process (and so the container) when the browser is gone."""
    failures = 0
    while True:
        time.sleep(1)
        try:
            agent.cdp.version()
            failures = 0
        except Exception:
            failures += 1
            if failures >= 2:
                print("lease agent: browser exited", file=sys.stderr)
                os._exit(0)


def main() -> int:
    secret = os.environ.pop(SECRET_ENV, "")
    if len(secret) < 32:
        print(f"lease agent: {SECRET_ENV} is missing or too short", file=sys.stderr)
        return 2
    AGENT_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    AGENT_PID_FILE.write_text(str(os.getpid()))

    agent = LeaseAgent()
    agent.start()

    def stop(signum, frame):
        agent._shutdown.set()
        agent._abort.set()
        # A clean close flushes the profile; never let it hold the container.
        threading.Timer(6.0, lambda: os._exit(0)).start()

    signal.signal(signal.SIGTERM, stop)
    handler = type("Handler", (LeaseHandler,), {"agent": agent, "secret": secret})
    server = ThreadingHTTPServer(("0.0.0.0", PORT), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=_watch_browser, args=(agent,), daemon=True).start()
    agent.serve_commands()
    agent.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
