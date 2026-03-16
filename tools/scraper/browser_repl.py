#!/usr/bin/env python3
"""Interactive browser REPL with HTTP interface."""

from playwright.sync_api import sync_playwright
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import json
import traceback

PORT = 8765

# Global state
output_buffer = []
buffer_lock = threading.Lock()
page = None
browser = None
context = None
network_logs = []

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
    global page, browser

    buffer_write(f">>> {cmd}")

    try:
        if cmd == "quit":
            buffer_write("Closing browser...")
            return "QUIT"

        elif cmd.startswith("goto "):
            url = cmd[5:].strip()
            buffer_write(f"Navigating to {url}...")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            buffer_write(f"Page loaded: {page.title()}")

        elif cmd.startswith("query "):
            selector = cmd[6:].strip()
            elements = page.query_selector_all(selector)
            buffer_write(f"Found {len(elements)} elements matching '{selector}'")
            for i, el in enumerate(elements[:20]):
                tag = el.evaluate("e => e.tagName")
                text = el.inner_text()[:50].replace('\n', ' ').strip()
                attrs = el.evaluate("e => { return {class: e.className?.substring(0,50), id: e.id, 'aria-label': e.getAttribute('aria-label'), disabled: e.disabled, 'aria-disabled': e.getAttribute('aria-disabled')} }")
                buffer_write(f"  [{i}] <{tag}> text='{text}' {attrs}")

        elif cmd.startswith("text "):
            selector = cmd[5:].strip()
            el = page.query_selector(selector)
            if el:
                buffer_write(el.inner_text()[:500])
            else:
                buffer_write("Element not found")

        elif cmd.startswith("click "):
            selector = cmd[6:].strip()
            el = page.query_selector(selector)
            if el:
                el.click()
                buffer_write("Clicked")
            else:
                buffer_write("Element not found")

        elif cmd.startswith("eval "):
            js = cmd[5:].strip()
            result = page.evaluate(js)
            buffer_write(f"Result: {json.dumps(result, indent=2)}")

        elif cmd == "netlog":
            # Show captured network requests
            buffer_write(f"Network logs ({len(network_logs)} requests):")
            for log in network_logs[-50:]:
                buffer_write(f"  {log['method']} {log['url'][:100]}")

        elif cmd == "netclear":
            network_logs.clear()
            buffer_write("Network logs cleared")

        elif cmd == "responses":
            responses = [l for l in network_logs if l.get("type") == "response"]
            buffer_write(f"JSON responses captured: {len(responses)}")
            for r in responses:
                buffer_write(f"  URL: {r['url'][:80]}")
                buffer_write(f"  Body: {r['body'][:500]}")
                buffer_write("")

        elif cmd.startswith("viewport "):
            # Set viewport size: viewport 800 600
            parts = cmd[9:].strip().split()
            if len(parts) >= 2:
                width = int(parts[0])
                height = int(parts[1])
                page.set_viewport_size({"width": width, "height": height})
                buffer_write(f"Viewport set to {width}x{height}")
            else:
                size = page.viewport_size
                buffer_write(f"Current viewport: {size['width']}x{size['height']}")
                buffer_write("Usage: viewport <width> <height>")

        elif cmd == "stock":
            # Get stock matrix from digitalData
            result = page.evaluate("""() => {
                const lp = window.digitalData?.product?.[0]?.linkedProduct || [];
                return lp.map(p => ({
                    color: p.productInfo?.color,
                    size: p.productInfo?.size,
                    stock: p.attributes?.stockStatus
                }));
            }""")
            buffer_write(f"Stock matrix ({len(result)} SKUs):")
            # Group by color
            by_color = {}
            for item in result:
                c = item['color']
                if c not in by_color:
                    by_color[c] = []
                by_color[c].append(item)
            for color, items in sorted(by_color.items()):
                buffer_write(f"  Color {color}:")
                for item in sorted(items, key=lambda x: x['size']):
                    status = "IN" if item['stock'] == 'in-stock' else "OUT"
                    buffer_write(f"    {item['size']}: {status}")

        else:
            buffer_write(f"Unknown command: {cmd}")

    except Exception as e:
        buffer_write(f"Error: {e}")
        traceback.print_exc()

    return "OK"

class ReplHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        """GET /read - return and clear buffer"""
        content = buffer_read()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(content.encode())

    def do_POST(self):
        """POST / with body = command"""
        length = int(self.headers.get('Content-Length', 0))
        cmd = self.rfile.read(length).decode().strip()

        result = execute_command(cmd)

        # Return buffered output immediately
        content = buffer_read()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.end_headers()
        self.wfile.write(content.encode())

        if result == "QUIT":
            threading.Thread(target=lambda: server.shutdown()).start()

def main():
    global page, browser, context, server

    from pathlib import Path
    user_data_dir = str(Path(__file__).parent.parent / ".browser_profile")

    print(f"Starting browser REPL on port {PORT}...")
    print(f"Profile: {user_data_dir}")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-infobars",
            ],
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
            viewport={"width": 1920, "height": 1080},
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            window.chrome = {runtime: {}};
        """)
        browser = None  # persistent context has no separate browser object
        page = context.pages[0] if context.pages else context.new_page()

        def on_request(request):
            network_logs.append({"method": request.method, "url": request.url})

        def on_response(response):
            url = response.url
            content_type = response.headers.get("content-type", "")
            if "json" in content_type:
                try:
                    body = response.text()
                    network_logs.append({"type": "response", "url": url, "body": body[:20000]})
                except:
                    pass

        page.on("request", on_request)
        page.on("response", on_response)

        print(f"Browser ready. HTTP server on http://localhost:{PORT}")
        print("  POST / with command body")
        print("  GET /read to get buffered output")

        server = HTTPServer(('localhost', PORT), ReplHandler)
        server.serve_forever()

        context.close()

    print("Browser REPL ended.")

if __name__ == "__main__":
    main()
