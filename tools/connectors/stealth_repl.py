#!/usr/bin/env python3
"""Provider-isolated headed Scrapling browser with a compact HTTP REPL.

This runs on the host desktop, not in an agent container.  Each instance receives
its own profile directory and TCP port so trusted-device state is never shared
between providers.  The command surface intentionally resembles agent-browser:
agents can take a semantic snapshot, use temporary element refs, or locate by
role/label/text when a provider makes a small DOM change.

Reachability is not authorization: agent containers run with
``--network=host``, so every request — driving the browser as much as the
login action — must authenticate with the caller's per-session
``Authorization: Bearer $CROSSTALK_TOKEN``. Identity is resolved host-side
from that token and the launcher-stamped session row (``repl_auth.py``);
the caller never supplies a session or workspace. Unauthenticated callers
get a liveness-only ``/health`` with no browsing state.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# The host launches this file directly from the connector worktree (its own
# directory on sys.path); tests import it as a package module.
try:
    import repl_auth
except ImportError:  # pragma: no cover — package-import path
    from tools.connectors import repl_auth


INTERACTIVE_SNAPSHOT_JS = r"""
() => {
  const candidates = Array.from(document.querySelectorAll(
    'a[href],button,input,select,textarea,[role="button"],[role="link"],'+
    '[role="checkbox"],[role="radio"],[role="tab"],[role="menuitem"],'+
    '[contenteditable="true"],[tabindex]:not([tabindex="-1"])'
  ));
  const visible = element => {
    const style = getComputedStyle(element);
    const box = element.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' &&
      box.width > 0 && box.height > 0;
  };
  const roleFor = element => {
    if (element.getAttribute('role')) return element.getAttribute('role');
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute('type') || '').toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button' || type === 'button' || type === 'submit') return 'button';
    if (tag === 'select') return 'combobox';
    if (type === 'checkbox') return 'checkbox';
    if (type === 'radio') return 'radio';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') return type === 'range' ? 'slider' : type === 'number' ? 'spinbutton' : 'textbox';
    return tag;
  };
  const nameFor = element => {
    const labelledBy = element.getAttribute('aria-labelledby');
    const labelled = labelledBy && document.getElementById(labelledBy);
    const explicit = element.id && document.querySelector(`label[for="${CSS.escape(element.id)}"]`);
    return (element.getAttribute('aria-label') || labelled?.innerText || explicit?.innerText ||
      element.innerText || element.getAttribute('placeholder') ||
      element.getAttribute('name') || element.getAttribute('title') || '').trim()
      .replace(/\s+/g, ' ').slice(0, 180);
  };
  document.querySelectorAll('[data-finance-agent-ref]').forEach(
    element => element.removeAttribute('data-finance-agent-ref')
  );
  return candidates.filter(visible).map((element, index) => {
    const ref = `e${index + 1}`;
    element.setAttribute('data-finance-agent-ref', ref);
    return {
      ref,
      role: roleFor(element),
      name: nameFor(element),
      type: element.getAttribute('type'),
      checked: ['checkbox', 'radio'].includes((element.getAttribute('type') || '').toLowerCase()) ? element.checked : null,
      disabled: Boolean(element.disabled) || element.getAttribute('aria-disabled') === 'true',
      href: element.href || null,
    };
  });
}
"""


class CommandError(RuntimeError):
    pass


def _tokens(value: str) -> list[str]:
    try:
        return shlex.split(value)
    except ValueError as exc:
        raise CommandError(f"command parse error: {exc}") from exc


class BrowserController:
    def __init__(self, provider: str, profile_dir: Path, download_dir: Path,
                 start_url: str | None, autonomy_root: Path | None = None,
                 login_key_file: Path | None = None):
        self.provider = provider
        self.profile_dir = profile_dir.resolve()
        self.download_dir = download_dir.resolve()
        self.start_url = start_url
        self.autonomy_root = autonomy_root
        self.login_key_file = login_key_file
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.session = None
        self.page = None
        self.lock = threading.RLock()

    def start(self) -> None:
        from scrapling.fetchers import StealthySession

        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.download_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.session = StealthySession(
            headless=False,
            solve_cloudflare=True,
            user_data_dir=str(self.profile_dir),
        )
        self.session.start()
        if not self.session.context:
            raise RuntimeError("Scrapling did not create a browser context")
        pages = self.session.context.pages
        self.page = pages[0] if pages else self.session.context.new_page()
        if self.start_url:
            self.page.goto(self.start_url, wait_until="domcontentloaded", timeout=60_000)

    def close(self) -> None:
        if self.session:
            self.session.close()

    def status(self) -> dict[str, Any]:
        page = self.page
        return {
            "provider": self.provider,
            "started_at": self.started_at,
            "pid": os.getpid(),
            "profile_dir": str(self.profile_dir),
            "download_dir": str(self.download_dir),
            "page_ready": page is not None,
            "url": page.url if page else None,
            "title": page.title() if page else None,
        }

    def _need_page(self):
        if not self.page:
            raise CommandError("browser page is not ready")
        return self.page

    @staticmethod
    def _semantic_locator(page, kind: str, value: str, role: str | None = None):
        if kind == "role":
            locator = page.get_by_role(role, name=value, exact=True)
            if locator.count() == 0:
                locator = page.get_by_role(role, name=value, exact=False)
            return locator.first
        if kind == "label":
            locator = page.get_by_label(value, exact=True)
            if locator.count() == 0:
                locator = page.get_by_label(value, exact=False)
            return locator.first
        if kind == "text":
            locator = page.get_by_text(value, exact=True)
            if locator.count() == 0:
                locator = page.get_by_text(value, exact=False)
            return locator.first
        raise CommandError(f"unsupported locator kind: {kind}")

    def execute(self, command: str) -> dict[str, Any]:
        with self.lock:
            started = time.monotonic()
            try:
                result = self._execute(command.strip())
                return {
                    "ok": True,
                    "operation": command.split(maxsplit=1)[0] if command else "",
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "result": result,
                }
            except Exception as exc:
                traceback.print_exc()
                return {
                    "ok": False,
                    "operation": command.split(maxsplit=1)[0] if command else "",
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "error": str(exc),
                }

    def secure_login(self, request: dict[str, Any],
                     caller: "repl_auth.ReplCaller") -> dict[str, Any]:
        """Fill a bounded login form using a host-decrypted secure Setting.

        ``caller`` is the authenticated identity the handler derived — its
        workspace feeds the reconstructed HPKE label, so a credential not
        sealed for that workspace does not decrypt.
        """
        with self.lock:
            if not self.autonomy_root or not self.login_key_file:
                raise CommandError("secure login is not configured for this REPL")
            target_key = str(request.get("target_key") or "")
            org = str(request.get("org") or "personal")
            origin = str(request.get("origin") or "")
            fields = request.get("fields")
            submit = request.get("submit")
            if not target_key or not origin or not isinstance(fields, dict) or not isinstance(submit, dict):
                raise CommandError("login requires target_key, origin, fields, and submit")
            page = self._need_page()
            page_host = (urlparse(page.url).hostname or "").lower()
            if page_host != origin and not page_host.endswith("." + origin):
                raise CommandError("current page is outside the approved login origin")
            for label in request.get("success_text") or []:
                landmark = page.get_by_text(str(label), exact=False).first
                if landmark.count() and landmark.is_visible():
                    return {"authenticated": True, "already_authenticated": True,
                            "url": page.url, "title": page.title(),
                            "target_key": target_key}
            try:
                from repl_login import load_credentials
            except ImportError:  # pragma: no cover — package-import path
                from tools.connectors.repl_login import load_credentials
            credentials = load_credentials(
                autonomy_root=self.autonomy_root, key_file=self.login_key_file,
                org=org, target_key=target_key, expected_origin=origin,
                caller_workspace=caller.workspace_id)
            start_url = page.url
            password_locator = None
            try:
                for credential_key, locator_spec in fields.items():
                    if credential_key not in credentials or not isinstance(locator_spec, dict):
                        raise CommandError("login field does not match provisioned credential")
                    kind = str(locator_spec.get("kind") or "label")
                    name = str(locator_spec.get("name") or "")
                    role = str(locator_spec.get("role") or "") or None
                    if kind not in {"label", "role"} or not name:
                        raise CommandError("login fields require a semantic label or role")
                    locator = self._semantic_locator(page, kind, name, role)
                    if locator.count() != 1:
                        raise CommandError(f"login field is not unique: {credential_key}")
                    input_type = (locator.get_attribute("type") or "text").lower()
                    allowed_types = {"password"} if credential_key == "password" else {"text", "email"}
                    if input_type not in allowed_types:
                        raise CommandError(f"refusing to put {credential_key} into {input_type} field")
                    locator.fill(credentials[credential_key])
                    if credential_key == "password":
                        password_locator = locator
                submit_kind = str(submit.get("kind") or "role")
                submit_name = str(submit.get("name") or "")
                submit_role = str(submit.get("role") or "button")
                if submit_kind not in {"label", "role"} or not submit_name:
                    raise CommandError("login submit requires a semantic locator")
                button = self._semantic_locator(page, submit_kind, submit_name, submit_role)
                if button.count() != 1:
                    raise CommandError("login submit is not unique")
                button.click()
                deadline = time.monotonic() + min(float(request.get("timeout_s") or 30), 60)
                while time.monotonic() < deadline:
                    page.wait_for_timeout(500)
                    current_host = (urlparse(page.url).hostname or "").lower()
                    if current_host != origin and not current_host.endswith("." + origin):
                        raise CommandError("login redirected outside the approved origin")
                    password_gone = password_locator is None or password_locator.count() == 0 \
                        or not password_locator.is_visible()
                    if password_gone:
                        for label in request.get("dismiss_optional") or []:
                            optional = page.get_by_role("button", name=str(label), exact=False).first
                            if optional.count() and optional.is_visible():
                                optional.click()
                                page.wait_for_timeout(500)
                                break
                        for label in request.get("success_text") or []:
                            landmark = page.get_by_text(str(label), exact=False).first
                            if landmark.count() and landmark.is_visible():
                                return {"authenticated": True, "url": page.url,
                                        "title": page.title(), "target_key": target_key}
                    if page.url != start_url and password_gone:
                        return {"authenticated": True, "url": page.url,
                                "title": page.title(), "target_key": target_key}
                    body = page.locator("body").inner_text().lower()
                    if any(term in body for term in ("verification code", "security code", "two-factor", "multi-factor")):
                        return {"authenticated": False, "human_required": True,
                                "reason": "verification_required", "url": page.url}
                raise CommandError("login did not reach a verified authenticated page")
            finally:
                credentials.clear()

    def _execute(self, command: str) -> Any:
        page = self._need_page()
        if command == "status":
            return self.status()
        if command == "url":
            return page.url
        if command == "title":
            return page.title()
        if command == "snapshot":
            return page.evaluate(INTERACTIVE_SNAPSHOT_JS)
        if command == "tabs":
            return [{"index": i, "url": item.url, "title": item.title()} for i, item in enumerate(page.context.pages)]
        if command == "cookies_summary":
            return sorted({
                (cookie.get("domain"), cookie.get("name"), cookie.get("expires"))
                for cookie in page.context.cookies()
            })
        if command.startswith("goto "):
            url = command[5:].strip()
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                raise CommandError("goto only accepts http(s) URLs")
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            return {"url": page.url, "title": page.title()}
        if command.startswith("tab "):
            try:
                index = int(command[len("tab "):].strip())
                self.page = page.context.pages[index]
            except (ValueError, IndexError) as exc:
                raise CommandError("usage: tab <valid index from tabs>") from exc
            self.page.bring_to_front()
            return {"index": index, "url": self.page.url, "title": self.page.title()}
        if command.startswith("snapshot "):
            raise CommandError("snapshot takes no arguments")
        if command.startswith("click_ref "):
            ref = command[len("click_ref "):].strip().lstrip("@")
            locator = page.locator(f'[data-finance-agent-ref="{ref}"]').first
            if locator.count() == 0:
                raise CommandError(f"unknown or stale ref: {ref}; take a new snapshot")
            locator.click()
            return {"clicked": ref}
        if command.startswith("fill_ref "):
            tokens = _tokens(command[len("fill_ref "):])
            if len(tokens) != 2:
                raise CommandError('usage: fill_ref <ref> "value"')
            ref, value = tokens
            ref = ref.lstrip("@")
            locator = page.locator(f'[data-finance-agent-ref="{ref}"]').first
            if locator.count() == 0:
                raise CommandError(f"unknown or stale ref: {ref}; take a new snapshot")
            locator.fill(value)
            return {"filled": ref, "characters": len(value)}
        for prefix, kind in (("click_role ", "role"), ("fill_role ", "role")):
            if command.startswith(prefix):
                tokens = _tokens(command[len(prefix):])
                minimum = 2 if prefix.startswith("click") else 3
                if len(tokens) != minimum:
                    raise CommandError(f'usage: {prefix.strip()} <role> "name"' + (' "value"' if minimum == 3 else ''))
                role, name = tokens[:2]
                locator = self._semantic_locator(page, kind, name, role)
                if prefix.startswith("click"):
                    locator.click()
                    return {"clicked": {"role": role, "name": name}}
                locator.fill(tokens[2])
                return {"filled": {"role": role, "name": name}, "characters": len(tokens[2])}
        for prefix, kind in (("click_label ", "label"), ("fill_label ", "label"), ("check_label ", "label"), ("click_text ", "text")):
            if command.startswith(prefix):
                tokens = _tokens(command[len(prefix):])
                minimum = 2 if prefix.startswith("fill") else 1
                if len(tokens) != minimum:
                    raise CommandError(f'usage: {prefix.strip()} "name"' + (' "value"' if minimum == 2 else ''))
                locator = self._semantic_locator(page, kind, tokens[0])
                if prefix.startswith("fill"):
                    locator.fill(tokens[1])
                    return {"filled": {kind: tokens[0]}, "characters": len(tokens[1])}
                if prefix.startswith("check"):
                    locator.check()
                    return {"checked": {kind: tokens[0]}}
                locator.click()
                return {"clicked": {kind: tokens[0]}}
        if command.startswith("wait_text "):
            tokens = _tokens(command[len("wait_text "):])
            if not 1 <= len(tokens) <= 2:
                raise CommandError('usage: wait_text "text" [timeout_ms]')
            timeout = int(tokens[1]) if len(tokens) == 2 else 30_000
            page.get_by_text(tokens[0], exact=False).first.wait_for(state="visible", timeout=timeout)
            return {"visible": tokens[0], "url": page.url}
        if command.startswith("wait_url "):
            tokens = _tokens(command[len("wait_url "):])
            if not 1 <= len(tokens) <= 2:
                raise CommandError('usage: wait_url "glob" [timeout_ms]')
            timeout = int(tokens[1]) if len(tokens) == 2 else 30_000
            page.wait_for_url(tokens[0], timeout=timeout)
            return {"url": page.url}
        if command.startswith("download_ref "):
            tokens = _tokens(command[len("download_ref "):])
            if not 1 <= len(tokens) <= 2:
                raise CommandError('usage: download_ref <ref> [filename.pdf]')
            ref = tokens[0].lstrip("@")
            locator = page.locator(f'[data-finance-agent-ref="{ref}"]').first
            if locator.count() == 0:
                raise CommandError(f"unknown or stale ref: {ref}; take a new snapshot")
            with page.expect_download(timeout=60_000) as pending:
                locator.click()
            return self._save_download(pending.value, tokens[1] if len(tokens) == 2 else None)
        if command.startswith("download_role "):
            tokens = _tokens(command[len("download_role "):])
            if len(tokens) not in {2, 3}:
                raise CommandError('usage: download_role <role> "name" [filename.pdf]')
            locator = self._semantic_locator(page, "role", tokens[1], tokens[0])
            with page.expect_download(timeout=60_000) as pending:
                locator.click()
            return self._save_download(pending.value, tokens[2] if len(tokens) == 3 else None)
        if command.startswith("fetch_url "):
            tokens = _tokens(command[len("fetch_url "):])
            if len(tokens) != 2:
                raise CommandError('usage: fetch_url "url" "filename"')
            url, requested_name = tokens
            parsed = urlparse(url)
            current = urlparse(page.url)
            if parsed.scheme != "https" or parsed.hostname != current.hostname:
                raise CommandError("fetch_url is restricted to the current HTTPS origin")
            response = page.context.request.get(url, timeout=60_000)
            if not response.ok:
                raise CommandError(f"fetch_url returned HTTP {response.status}")
            return self._save_bytes(response.body(), requested_name,
                                    response.headers.get("content-type"))
        if command.startswith("screenshot"):
            tokens = _tokens(command[len("screenshot"):])
            requested = Path(tokens[0]).name if tokens else f"{self.provider}-{int(time.time())}.png"
            destination = self.download_dir / requested
            page.screenshot(path=str(destination), full_page="--full" in tokens)
            return {"path": str(destination), "byte_size": destination.stat().st_size}
        if command.startswith("query "):
            selector = command[len("query "):].strip()
            return page.locator(selector).count()
        if command.startswith("text "):
            selector = command[len("text "):].strip()
            locator = page.locator(selector).first
            return locator.inner_text() if locator.count() else None
        if command.startswith("eval "):
            return page.evaluate(command[len("eval "):])
        raise CommandError(
            "unknown command; use status, snapshot, goto, click/fill_ref, click/fill_role, "
            "click/fill/check_label, click_text, wait_text, wait_url, download_ref, "
            "download_role, screenshot, query, text, eval, url, title, tabs, tab, cookies_summary"
        )

    def _save_bytes(self, content: bytes, requested_name: str,
                    content_type: str | None = None) -> dict[str, Any]:
        filename = Path(requested_name).name
        if not filename:
            raise CommandError("download filename is empty")
        destination = self.download_dir / filename
        if destination.exists():
            destination = self.download_dir / f"{destination.stem}-{int(time.time())}{destination.suffix}"
        destination.write_bytes(content)
        return {"path": str(destination), "byte_size": len(content),
                "content_type": content_type}

    def _save_download(self, download, requested_name: str | None) -> dict[str, Any]:
        suggested = Path(download.suggested_filename).name
        filename = Path(requested_name).name if requested_name else suggested
        if not filename:
            filename = f"{self.provider}-{int(time.time())}.bin"
        destination = self.download_dir / filename
        if destination.exists():
            destination = self.download_dir / f"{destination.stem}-{int(time.time())}{destination.suffix}"
        download.save_as(str(destination))
        return {
            "path": str(destination),
            "suggested_filename": suggested,
            "byte_size": destination.stat().st_size,
        }


class ReplHandler(BaseHTTPRequestHandler):
    controller: BrowserController
    server_version = "FinanceStealthREPL/1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            # A caller timed out or a phone backgrounded while the bounded
            # browser operation was still completing. The browser state is
            # authoritative; a disconnected response is not a server fault.
            return

    def _authenticate(self) -> "repl_auth.ReplCaller | None":
        """Resolve the caller from the bearer token, or respond with the
        refusal and return None. Any resolution failure fails closed."""
        try:
            return repl_auth.authenticate(
                autonomy_root=self.controller.autonomy_root,
                authorization=self.headers.get("Authorization"))
        except repl_auth.ReplAuthError as exc:
            self._json(exc.status, {"ok": False, "error": str(exc)})
            return None
        except Exception as exc:
            traceback.print_exc()
            self._json(403, {"ok": False,
                             "error": f"caller authentication failed: {exc}"})
            return None

    def do_GET(self) -> None:
        if self.path in {"/", "/health"}:
            # Liveness needs no identity, but browsing state (URL, title,
            # profile paths) of an authenticated provider session must not
            # leak to anyone who can merely open the socket.
            try:
                repl_auth.authenticate(
                    autonomy_root=self.controller.autonomy_root,
                    authorization=self.headers.get("Authorization"))
            except Exception:
                # No provider label either: naming the profile is naming the
                # site whose cookie jar this process holds.
                self._json(200, {
                    "ok": True,
                    "started_at": self.controller.started_at,
                    "page_ready": self.controller.page is not None,
                    "authenticated": False,
                })
                return
            self._json(200, {"ok": True, "authenticated": True,
                             **self.controller.status()})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        caller = self._authenticate()
        if caller is None:
            return
        if self.path == "/api/login":
            try:
                payload = json.loads(raw or b"{}")
                result = self.controller.secure_login(payload, caller)
                self._json(200, {"ok": True, "result": result})
            except Exception as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if self.path == "/api/command":
            try:
                payload = json.loads(raw or b"{}")
                command = str(payload["command"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"ok": False, "error": f"invalid command payload: {exc}"})
                return
        elif self.path == "/":
            command = raw.decode().strip()
        else:
            self._json(404, {"ok": False, "error": "not found"})
            return
        result = self.controller.execute(command)
        self._json(200 if result["ok"] else 400, result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-dir", required=True, type=Path)
    parser.add_argument("--download-dir", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--start-url")
    parser.add_argument("--autonomy-root", type=Path)
    parser.add_argument("--login-key-file", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    controller = BrowserController(args.profile_dir.name, args.profile_dir, args.download_dir,
                                   args.start_url, args.autonomy_root,
                                   args.login_key_file)
    controller.start()
    handler = type("ProviderReplHandler", (ReplHandler,), {"controller": controller})
    # Scrapling's synchronous Patchright page must be driven on the same thread
    # that created it. A single-threaded server also serializes agent commands.
    server = HTTPServer((args.bind, args.port), handler)
    print(json.dumps({"event": "ready", "port": args.port, **controller.status()}, sort_keys=True), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        controller.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
