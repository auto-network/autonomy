#!/usr/bin/env python3
"""Thin OpenRouter client and smoke CLI.

OpenRouter exposes an OpenAI-compatible chat-completions API at
https://openrouter.ai/api/v1. This module is the smallest thing that lets a
session prove the credential path works and run one prompt against one model;
the inference provider registry (graph://5fbf3881-ec5) remains the design of
record for routing and usage accounting and should wrap this when built.

Credential: ``OPENROUTER_API_KEY`` in the environment, injected at launch from
the operator's vault via the workspace row's
``env: {"OPENROUTER_API_KEY": "credential:openrouter.api-key"}``. As a
fallback for host terminals, ``OPENROUTER_API_KEY_FILE`` names a file whose
first line is the key. The key is never printed or logged.

Usage:
  openrouter.py models [--filter SUBSTR] [--limit N]
  openrouter.py chat --model MODEL (--prompt TEXT | --file PATH) [--system TEXT]
                     [--max-tokens N] [--temperature T] [--json]
  openrouter.py smoke            # one cheap call, prints model, latency, usage
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = "https://openrouter.ai/api/v1"
APP_HEADERS = {
    "HTTP-Referer": "https://auto.network",
    "X-Title": "Autonomy",
}
SMOKE_MODEL = "openai/gpt-4o-mini"


def api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        path = os.environ.get("OPENROUTER_API_KEY_FILE", "").strip()
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                key = fh.readline().strip()
    if not key:
        sys.exit(
            "openrouter: no credential. Launch from a workspace whose row sets "
            "env OPENROUTER_API_KEY to credential:openrouter.api-key, or set "
            "OPENROUTER_API_KEY_FILE on a host terminal."
        )
    return key


def request(method: str, path: str, body: dict | None = None, timeout: float = 120.0) -> dict:
    headers = {"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json", **APP_HEADERS}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # never echo headers (they carry the key)
        detail = exc.read().decode("utf-8", "replace")[:800]
        sys.exit(f"openrouter: HTTP {exc.code} on {path}: {detail}")
    except urllib.error.URLError as exc:
        sys.exit(f"openrouter: network error on {path}: {exc.reason}")


def chat(model: str, prompt: str, system: str | None = None, max_tokens: int = 2048,
         temperature: float | None = None) -> dict:
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if temperature is not None:
        body["temperature"] = temperature
    t0 = time.monotonic()
    out = request("POST", "/chat/completions", body)
    out["_latency_s"] = round(time.monotonic() - t0, 2)
    return out


def text_of(completion: dict) -> str:
    choices = completion.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):  # some providers return content parts
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content or ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="openrouter")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("models"); m.add_argument("--filter", default=""); m.add_argument("--limit", type=int, default=40)
    c = sub.add_parser("chat"); c.add_argument("--model", required=True)
    g = c.add_mutually_exclusive_group(required=True); g.add_argument("--prompt"); g.add_argument("--file")
    c.add_argument("--system"); c.add_argument("--max-tokens", type=int, default=2048)
    c.add_argument("--temperature", type=float); c.add_argument("--json", action="store_true")
    sub.add_parser("smoke")
    a = ap.parse_args(argv)

    if a.cmd == "models":
        rows = request("GET", "/models").get("data", [])
        rows = [r for r in rows if a.filter.lower() in r.get("id", "").lower()]
        for r in sorted(rows, key=lambda r: r.get("id", ""))[: a.limit]:
            p = r.get("pricing") or {}
            print(f"{r.get('id'):50s} ctx={r.get('context_length')}  $/M in={float(p.get('prompt') or 0)*1e6:.2f} out={float(p.get('completion') or 0)*1e6:.2f}")
        print(f"{len(rows)} models matched")
        return 0
    if a.cmd == "chat":
        prompt = a.prompt if a.prompt is not None else open(a.file, encoding="utf-8").read()
        out = chat(a.model, prompt, a.system, a.max_tokens, a.temperature)
        if a.json:
            print(json.dumps(out, indent=1))
        else:
            print(text_of(out))
            u = out.get("usage") or {}
            print(f"\n[{out.get('model')} · {out['_latency_s']}s · tokens in={u.get('prompt_tokens')} out={u.get('completion_tokens')}]", file=sys.stderr)
        return 0
    if a.cmd == "smoke":
        out = chat(SMOKE_MODEL, "Reply with the single word: ready", max_tokens=8, temperature=0)
        u = out.get("usage") or {}
        print(f"openrouter ok · model={out.get('model')} · reply={text_of(out).strip()!r} · {out['_latency_s']}s · tokens in={u.get('prompt_tokens')} out={u.get('completion_tokens')}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
