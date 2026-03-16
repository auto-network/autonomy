# Conversation Scraping

## Purpose

Extract conversation history from ChatGPT and Claude.ai web sessions for the Autonomy Network knowledge base.

## Stack

- **Scrapling** (v0.4.2) — anti-detect browser via patchright (patched Playwright/Chromium). Bypasses Cloudflare Turnstile.
- **HTTP REPL** (`browser_repl.py`) — keeps browser alive, accepts commands via `curl -X POST localhost:8765 -d '<command>'`
- **markdownify** + BeautifulSoup — HTML-to-markdown conversion with citation stripping

## Architecture

```
launch.py          — Start stealth browser + HTTP REPL server on :8765
browser_repl.py    — HTTP command interface (from lululemon project, adapted)
convert.py         — JSON (with HTML) → clean markdown with YAML frontmatter
```

## Process

### 1. Launch browser
```bash
DISPLAY=:0 .venv/bin/python tools/scraper/launch.py
```
Browser opens visible via WSLg. REPL listens on localhost:8765.
Persistent profile at `.browser_profile/` (repo root) preserves login sessions across restarts.

### 2. Navigate & log in
```bash
curl -X POST localhost:8765 -d 'goto https://chatgpt.com'
```
User logs in manually in the visible browser window.

### 3. Extract conversation (HTML)
Navigate to the conversation, then:
```bash
curl -s -X POST localhost:8765 -d 'eval (() => {
  const turns = document.querySelectorAll("article[data-testid^=\"conversation-turn\"]");
  return Array.from(turns).map((article, i) => {
    const roleEl = article.querySelector("[data-message-author-role]");
    const role = roleEl ? roleEl.getAttribute("data-message-author-role") : "unknown";
    const msgId = article.querySelector("[data-message-id]")?.getAttribute("data-message-id") || "";
    const content = article.querySelector(".markdown, .whitespace-pre-wrap");
    if (!content) return { turn: i+1, role, msgId, html: "", text: article.innerText };
    const clone = content.cloneNode(true);
    clone.querySelectorAll("a[class*=\"citation\"], span[class*=\"citation\"], button, [data-testid*=\"citation\"], a > sup").forEach(el => el.remove());
    clone.querySelectorAll("[class*=\"source\"], [class*=\"reference\"]").forEach(el => el.remove());
    return { turn: i+1, role, msgId, html: clone.innerHTML };
  });
})()' > /tmp/raw_extract.json
```

### 4. Convert to markdown
```bash
.venv/bin/python tools/scraper/convert.py /tmp/raw_extract.json "Title" "https://chatgpt.com/c/ID" data/chatgpt
```

Produces:
- `data/chatgpt/<id>.json` — clean source JSON with HTML per turn
- `data/chatgpt/<id>.md` — markdown with YAML frontmatter, turn headers, message IDs

## Output format

```markdown
---
title: "Conversation Title"
source: chatgpt
url: https://chatgpt.com/c/...
conversation_id: ...
extracted_at: 2026-03-15T...
total_turns: 18
---

# Conversation Title

## Turn 1 — USER
<!-- message_id: ... -->

User's message text.

## Turn 2 — ASSISTANT
<!-- message_id: ... -->

**Bold**, *italic*, `code`, lists, code blocks all preserved from original HTML.
```

## REPL Commands

| Command | Description |
|---------|-------------|
| `goto <url>` | Navigate to URL |
| `eval <js>` | Execute JavaScript, return result |
| `query <selector>` | List matching DOM elements |
| `text <selector>` | Get innerText of first match |
| `click <selector>` | Click first matching element |
| `url` | Current page URL |
| `title` | Current page title |
| `tabs` | List open tabs |
| `html` | First 5000 chars of page HTML |
| `quit` | Shut down browser |

## Claude.ai extraction

Not yet implemented. Will need different DOM selectors — Claude uses `[data-testid="human-turn"]` / `[data-testid="assistant-turn"]` patterns. Same REPL + convert pipeline applies.

## Dependencies

```
scrapling[all]    # includes patchright, browserforge, curl_cffi, msgspec
markdownify
beautifulsoup4
```
