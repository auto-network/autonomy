"""Convert extracted ChatGPT/Claude JSON (with HTML) to clean markdown."""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from markdownify import markdownify as md, MarkdownConverter


class ChatConverter(MarkdownConverter):
    """Custom converter that handles ChatGPT/Claude HTML quirks."""

    def convert_pre(self, el, text, **kwargs):
        """Preserve code blocks with language hints."""
        code = el.find("code")
        lang = ""
        if code:
            classes = code.get("class", [])
            if isinstance(classes, list):
                for c in classes:
                    if c.startswith("language-") or c.startswith("lang-"):
                        lang = c.split("-", 1)[1]
                        break
                    elif c.startswith("hljs"):
                        continue
            text = code.get_text()
        else:
            text = el.get_text()

        return f"\n```{lang}\n{text}\n```\n"

    def convert_a(self, el, text, **kwargs):
        """Convert links, skip empty citation links."""
        href = el.get("href", "")
        if not text.strip() or not href:
            return ""
        if re.match(r"^\d+$", text.strip()):
            return ""
        return f"[{text.strip()}]({href})"


def extract_thinking_summary(soup) -> str | None:
    """Extract Claude's thinking summary from sr-only span."""
    sr = soup.select_one("span.sr-only[role='status'][aria-live='polite']")
    if sr:
        text = sr.get_text(strip=True)
        sr.decompose()
        return text if text else None
    return None


def strip_thinking_grid(soup):
    """Remove the thinking summary grid wrapper, keep only the response content."""
    # Claude wraps responses in a grid: row-start-1 = thinking summary, row-start-2 = content
    grid = soup.select_one(".grid.grid-rows-\\[auto_auto\\]")
    if not grid:
        # Try looser match
        grid = soup.select_one("div[class*='grid-rows']")
    if grid:
        # Keep only the row-start-2 content (actual response)
        row2 = grid.select_one("[class*='row-start-2']")
        if row2:
            grid.replace_with(row2)


def html_to_markdown(html: str, source: str = "chatgpt") -> tuple[str, str | None]:
    """Convert HTML to clean markdown. Returns (markdown, thinking_summary)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    # Extract Claude thinking summary before it gets converted
    thinking = extract_thinking_summary(soup)

    # Strip Claude's grid wrapper
    if source == "claude":
        strip_thinking_grid(soup)

    # Remove residual citation/source elements
    for tag in soup.select(
        "[class*='citation'], [class*='source'], [class*='reference'], "
        "button, [data-testid*='citation'], sup"
    ):
        tag.decompose()

    result = ChatConverter(
        heading_style="atx",
        bullets="-",
        strong_em_symbol="*",
        code_language_callback=None,
    ).convert_soup(soup)

    # Clean up excessive whitespace
    result = re.sub(r"\n{3,}", "\n\n", result)
    # Remove trailing spaces on lines
    result = re.sub(r" +\n", "\n", result)
    return result.strip(), thinking


def render_turn(turn: dict, source: str = "chatgpt") -> list[str]:
    """Render a single turn to markdown lines."""
    lines = []
    num = turn["turn"]
    role = turn["role"]
    msg_id = turn.get("msgId", "")
    tag = {"user": "USER", "assistant": "ASSISTANT"}.get(role, role.upper())

    lines.append(f"## Turn {num} — {tag}")
    if msg_id:
        lines.append(f"<!-- message_id: {msg_id} -->")

    html = turn.get("html", "")
    thinking = None
    if html:
        content, thinking = html_to_markdown(html, source=source)
    else:
        content = turn.get("text", "").strip()

    if thinking:
        lines.append(f"> **Thinking:** {thinking}")
    lines.append("")
    lines.append(content)
    lines.append("")
    return lines


def convert_file(json_path: str, output_path: str = None):
    """Convert a JSON extraction file to markdown."""
    json_path = Path(json_path)
    raw = json_path.read_text()

    # Handle REPL output format (has "Result: " prefix)
    if "Result: " in raw:
        idx = raw.index("Result: ")
        raw = raw[idx + 8:]

    data = json.loads(raw)

    # Detect source from first entry or filename
    source = "chatgpt"  # default

    # Try to get metadata from the data
    title = "Untitled"
    url = ""
    conv_id = json_path.stem

    # Build markdown
    lines = []
    lines.append("---")
    lines.append(f'title: "{title}"')
    lines.append(f"source: {source}")
    if url:
        lines.append(f"url: {url}")
    lines.append(f"conversation_id: {conv_id}")
    lines.append(f"extracted_at: {datetime.now().isoformat()}")
    lines.append(f"total_turns: {len(data)}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title}")
    lines.append("")

    for turn in data:
        lines.extend(render_turn(turn, source))

    md_text = "\n".join(lines)

    if output_path is None:
        output_path = json_path.with_suffix(".md")
    else:
        output_path = Path(output_path)

    output_path.write_text(md_text)
    print(f"Wrote {len(data)} turns to {output_path} ({output_path.stat().st_size:,} bytes)")
    return output_path


def convert_raw(raw_path: str, title: str, url: str, output_dir: str):
    """Convert raw REPL HTML extraction to both clean JSON and markdown."""
    raw = Path(raw_path).read_text()

    if "Result: " in raw:
        idx = raw.index("Result: ")
        raw = raw[idx + 8:]

    data = json.loads(raw)
    conv_id = url.split("/c/")[-1].split("/chat/")[-1].split("?")[0] if url else datetime.now().strftime("%Y%m%d_%H%M%S")
    source = "chatgpt" if "chatgpt" in url else "claude" if "claude" in url else "unknown"

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Save clean JSON
    json_out = out_dir / f"{conv_id}.json"
    json_out.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    # Build markdown
    lines = []
    lines.append("---")
    lines.append(f'title: "{title}"')
    lines.append(f"source: {source}")
    lines.append(f"url: {url}")
    lines.append(f"conversation_id: {conv_id}")
    lines.append(f"extracted_at: {datetime.now().isoformat()}")
    lines.append(f"total_turns: {len(data)}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title}")
    lines.append("")

    for turn in data:
        lines.extend(render_turn(turn, source))

    md_out = out_dir / f"{conv_id}.md"
    md_out.write_text("\n".join(lines))

    print(f"Saved {len(data)} turns:")
    print(f"  JSON: {json_out} ({json_out.stat().st_size:,} bytes)")
    print(f"  MD:   {md_out} ({md_out.stat().st_size:,} bytes)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python convert.py <raw_file> [title] [url] [output_dir]")
        sys.exit(1)

    raw_file = sys.argv[1]
    title = sys.argv[2] if len(sys.argv) > 2 else "Untitled"
    url = sys.argv[3] if len(sys.argv) > 3 else ""
    output_dir = sys.argv[4] if len(sys.argv) > 4 else "data/chatgpt"

    convert_raw(raw_file, title, url, output_dir)
