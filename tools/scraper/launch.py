"""Launch a visible stealth browser via Scrapling's StealthySession + HTTP REPL."""
import json
import shlex
import threading
import time
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from scrapling.fetchers import StealthySession

PORT = 8765
PROFILE_DIR = str(Path(__file__).parents[2] / ".browser_profile")

# Global state
output_buffer = []
buffer_lock = threading.Lock()
session = None
page = None

def buffer_write(msg):
    with buffer_lock:
        output_buffer.append(msg)
    print(msg)

def buffer_read():
    with buffer_lock:
        result = '\n'.join(output_buffer)
        output_buffer.clear()
        return result

def execute_command(cmd):
    global page, session

    buffer_write(f">>> {cmd}")

    try:
        if cmd == "quit":
            buffer_write("Closing browser...")
            return "QUIT"

        elif cmd.startswith("goto "):
            url = cmd[5:].strip()
            buffer_write(f"Navigating to {url}...")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            buffer_write(f"Page loaded: {page.url}")

        elif cmd.startswith("query "):
            selector = cmd[6:].strip()
            if page:
                elements = page.query_selector_all(selector)
                buffer_write(f"Found {len(elements)} elements matching '{selector}'")
                for i, el in enumerate(elements[:20]):
                    tag = el.evaluate("e => e.tagName")
                    text = el.inner_text()[:80].replace('\n', ' ').strip()
                    buffer_write(f"  [{i}] <{tag}> {text}")

        elif cmd.startswith("text "):
            selector = cmd[5:].strip()
            if page:
                el = page.query_selector(selector)
                if el:
                    buffer_write(el.inner_text()[:2000])
                else:
                    buffer_write("Element not found")

        elif cmd.startswith("click "):
            selector = cmd[6:].strip()
            if page:
                el = page.query_selector(selector)
                if el:
                    el.click()
                    buffer_write("Clicked")
                else:
                    buffer_write("Element not found")

        elif cmd.startswith("eval "):
            js = cmd[5:].strip()
            if page:
                result = page.evaluate(js)
                buffer_write(f"Result: {json.dumps(result, indent=2, default=str)}")

        elif cmd == "url":
            if page:
                buffer_write(page.url)

        elif cmd == "title":
            if page:
                buffer_write(page.title())

        elif cmd == "tabs":
            if session and session.page_pool:
                for i, p in enumerate(session.page_pool):
                    buffer_write(f"  [{i}] {p.url}")

        elif cmd == "html":
            if page:
                buffer_write(page.content()[:5000])

        elif cmd.startswith("viewport "):
            parts = cmd[9:].strip().split()
            if len(parts) >= 2 and page:
                page.set_viewport_size({"width": int(parts[0]), "height": int(parts[1])})
                buffer_write(f"Viewport set to {parts[0]}x{parts[1]}")

        elif cmd.startswith("screenshot"):
            # screenshot [path] [--full]
            rest = cmd[len("screenshot"):].strip()
            full = False
            if "--full" in rest.split():
                full = True
                rest = " ".join(t for t in rest.split() if t != "--full").strip()
            path = rest or f"/tmp/scrapling-{int(time.time())}.png"
            if page:
                page.screenshot(path=path, full_page=full)
                buffer_write(f"Screenshot: {path} (full_page={full})")

        elif cmd.startswith("attach "):
            # attach <selector> <path>  — Playwright set_input_files
            try:
                tokens = shlex.split(cmd[len("attach "):].strip())
            except ValueError as e:
                buffer_write(f"Parse error: {e}")
                return "OK"
            if len(tokens) != 2:
                buffer_write("Usage: attach <selector> <path>")
                return "OK"
            selector, path = tokens
            if not Path(path).is_file():
                buffer_write(f"File not found: {path}")
                return "OK"
            if page:
                el = page.query_selector(selector)
                if not el:
                    buffer_write(f"No element matches: {selector}")
                    return "OK"
                el.set_input_files(path)
                buffer_write(f"Attached {path} to {selector}")

        elif cmd.startswith("click_role "):
            # click_role <role> <name>
            try:
                tokens = shlex.split(cmd[len("click_role "):].strip())
            except ValueError as e:
                buffer_write(f"Parse error: {e}")
                return "OK"
            if len(tokens) < 2:
                buffer_write("Usage: click_role <role> <name>")
                return "OK"
            role, name = tokens[0], tokens[1]
            if page:
                page.get_by_role(role, name=name).first.click()
                buffer_write(f"Clicked role={role} name={name!r}")

        elif cmd.startswith("hover_role "):
            # hover_role <role> <name>
            try:
                tokens = shlex.split(cmd[len("hover_role "):].strip())
            except ValueError as e:
                buffer_write(f"Parse error: {e}")
                return "OK"
            if len(tokens) < 2:
                buffer_write("Usage: hover_role <role> <name>")
                return "OK"
            role, name = tokens[0], tokens[1]
            if page:
                page.get_by_role(role, name=name).first.hover()
                buffer_write(f"Hovered role={role} name={name!r}")

        elif cmd.startswith("paste_file "):
            # paste_file <selector> <path>  — fills via Playwright .fill (works for inputs and contenteditable)
            try:
                tokens = shlex.split(cmd[len("paste_file "):].strip())
            except ValueError as e:
                buffer_write(f"Parse error: {e}")
                return "OK"
            if len(tokens) != 2:
                buffer_write("Usage: paste_file <selector> <path>")
                return "OK"
            selector, path = tokens
            p = Path(path)
            if not p.is_file():
                buffer_write(f"File not found: {path}")
                return "OK"
            text = p.read_text()
            if page:
                loc = page.locator(selector).first
                loc.click()
                loc.fill(text)
                buffer_write(f"Filled {selector} with {len(text)} chars from {path}")

        else:
            buffer_write(f"Unknown command: {cmd}")
            buffer_write("Commands: goto, query, text, click, eval, url, title, tabs, html, viewport, screenshot, attach, click_role, hover_role, paste_file, quit")

    except Exception as e:
        buffer_write(f"Error: {e}")
        traceback.print_exc()

    return "OK"


class ReplHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        content = buffer_read()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(content.encode())

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        cmd = self.rfile.read(length).decode().strip()
        result = execute_command(cmd)
        content = buffer_read()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(content.encode())
        if result == "QUIT":
            threading.Thread(target=lambda: server.shutdown()).start()


def main():
    global page, session, server

    print(f"Starting stealth browser REPL on port {PORT}...")
    print(f"Profile: {PROFILE_DIR}")

    session = StealthySession(
        headless=False,
        solve_cloudflare=True,
        user_data_dir=PROFILE_DIR,
    )
    session.start()

    # Get a page from the context
    if session.context:
        pages = session.context.pages
        page = pages[0] if pages else session.context.new_page()

    print(f"Browser ready. HTTP REPL on http://localhost:{PORT}")
    print("  curl -X POST localhost:8765 -d 'goto https://chatgpt.com'")

    server = HTTPServer(('localhost', PORT), ReplHandler)
    try:
        server.serve_forever()
    finally:
        session.close()

    print("Browser REPL ended.")


if __name__ == "__main__":
    main()
