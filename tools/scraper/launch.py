"""Launch a visible stealth browser via Scrapling's StealthySession + HTTP REPL."""
import json
import threading
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

        else:
            buffer_write(f"Unknown command: {cmd}")
            buffer_write("Commands: goto, query, text, click, eval, url, title, tabs, html, viewport, quit")

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
