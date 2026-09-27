"""Shared plumbing for the mail-* tools. Imported, not executed.

These tools hold NO mailbox credential. Reads call the dashboard's
/api/mailbox/* broker routes (IMAP runs host-side, read-only); sending stages
an email_send approval and blocks until the operator decides. Every call
carries the session bearer ($CROSSTALK_TOKEN); the dashboard derives the
session, its workspace and its org from it.
"""
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DASH = os.environ.get("AUTONOMY_DASHBOARD") or os.environ.get("GRAPH_API") or "https://localhost:8080"
SESSION = os.environ.get("AUTONOMY_SESSION", "")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE   # the node's own self-signed dashboard, as curl -k


def fail(message, code=1):
    print(message, file=sys.stderr)
    sys.exit(code)


class Transient(Exception):
    """The dashboard failed or was unreachable; the request may still be live."""


def call(method, path, body=None, timeout=70, retry=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(DASH.rstrip("/") + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + os.environ.get("CROSSTALK_TOKEN", ""))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}").get("error") or e.reason
        except ValueError:
            detail = e.reason
        if retry and e.code >= 500:
            raise Transient(f"{detail} (HTTP {e.code})")
        fail(f"mail: {detail} (HTTP {e.code})")
    except (urllib.error.URLError, OSError) as e:
        if retry:
            raise Transient(f"dashboard unreachable: {e}")
        fail(f"mail: dashboard unreachable at {DASH}: {e}")


def seconds(text):
    """'90', '90s', '15m', '2h', '1d' -> seconds."""
    m = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", str(text or ""))
    if not m:
        fail(f"mail: not a duration: {text!r} (use 90, 15m, 2h, 1d)")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def query(**params):
    items = {k: v for k, v in params.items() if v not in (None, "", 0)}
    return "?" + urllib.parse.urlencode(items) if items else ""


def add_filters(parser):
    parser.add_argument("--to", help="recipient contains (plus-addresses work: agent+claude@...)")
    parser.add_argument("--from", dest="sender", help="sender contains")
    parser.add_argument("--subject", help="subject contains")
    parser.add_argument("--text", help="headers or body contain")
    parser.add_argument("--newer-than", default="", help="only messages received within, e.g. 15m")
    parser.add_argument("--after-uid", type=int, default=0, help="only messages with a higher UID")


def filter_params(a):
    return dict(to=a.to, **{"from": a.sender}, subject=a.subject, text=a.text,
                newer_than=seconds(a.newer_than) if a.newer_than else 0, after_uid=a.after_uid)


def print_list(result):
    msgs = result.get("messages") or []
    if not msgs:
        print(f"No matching messages ({result.get('total_in_folder', 0)} in {result.get('folder')}).")
        return
    for m in msgs:
        print(f"{m['uid']:>6}  {m['received']}  {m['from'][:40]:<40}  {m['subject']}")
        if m.get("to"):
            print(f"        to {m['to']}")


def print_message(m):
    print(f"UID      {m['uid']}")
    print(f"From     {m['from']}")
    print(f"To       {m['to']}")
    if m.get("cc"):
        print(f"Cc       {m['cc']}")
    print(f"Subject  {m['subject']}")
    print(f"Received {m['received']}")
    if m.get("codes"):
        print(f"Codes    {' '.join(m['codes'])}   (numbers of 4-8 digits; check which is the code)")
    for link in m.get("links") or []:
        print(f"Link     {link}")
    for a in m.get("attachments") or []:
        print(f"Attached {a['filename']} ({a['content_type']}, {a['size']} bytes)")
    print()
    print(m.get("text", ""))
    if m.get("truncated"):
        print("\n[text truncated]")
