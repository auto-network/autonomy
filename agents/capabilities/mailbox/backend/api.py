"""Mailbox broker backend: read-only IMAP, and SMTP sending after approval.

Runs inside the dashboard process only. A session never holds the mailbox
password: the ``mail-*`` tools call the dashboard's ``/api/mailbox/*`` routes,
which call these functions host-side. Reads open the folder read-only
(IMAP EXAMINE) and fetch with BODY.PEEK, so reading never changes a flag, moves
or deletes anything. Sending exists only as the executor of an operator-
approved ``email_send`` request (tools/dashboard/mailbox_routes.py).

Any IMAP/SMTP mailbox works. Configuration is the org's install Setting
``autonomy.org.capability.install#1`` key ``mailbox`` (contract ``mailbox``),
``broker_config`` (strings only, never a secret):

    imap_host, imap_port (993, implicit TLS), username, folder (INBOX),
    smtp_host (imap_host), smtp_port (587, STARTTLS), from_addr (username),
    password_vault_key (the audited-vault name; default mailbox_password)

The password is read from the operator's audited vault at
``<org>:<password_vault_key>`` (seal it with ``graph vault seal <name> --org
<org> --tier audited --from-file <path>``); a locked vault fails closed.
``MAILBOX_*`` environment variables override each value (the test seam).
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import os
import re
import smtplib
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses, make_msgid, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

CONTRACT = "mailbox"
MAX_TEXT = 100_000
MAX_LIST = 200

# Seams for tests: replaced by fakes, never by a caller.
_IMAP = imaplib.IMAP4_SSL
_SMTP = smtplib.SMTP


class MailboxError(Exception):
    """A mailbox operation failed; the message never carries the password."""


@dataclass(frozen=True)
class MailboxConfig:
    imap_host: str
    imap_port: int
    username: str
    password: str
    folder: str
    smtp_host: str
    smtp_port: int
    from_addr: str

    def scrub(self, text: str) -> str:
        return text.replace(self.password, "<redacted>") if self.password else text

    @classmethod
    def resolve(cls, org: str | None) -> "MailboxConfig":
        installed: dict = {}
        if org:
            try:
                from tools.graph import ops as graph_ops
                rows = graph_ops.read_set("autonomy.org.capability.install", org=org, peers=[])
                for m in (getattr(rows, "members", []) or []):
                    payload = m.payload if isinstance(m.payload, dict) else {}
                    if payload.get("contract") == CONTRACT:
                        installed = payload.get("broker_config") or {}
                        break
            except Exception:
                pass

        def value(env: str, key: str, default: str = "") -> str:
            return (os.environ.get(env) or str(installed.get(key) or "") or default).strip()

        imap_host = value("MAILBOX_IMAP_HOST", "imap_host")
        username = value("MAILBOX_USERNAME", "username")
        vault_key = value("MAILBOX_PASSWORD_VAULT_KEY", "password_vault_key", "mailbox_password")
        password, vault_locked = "", False
        env_file = os.environ.get("MAILBOX_PASSWORD_FILE")
        if env_file:
            try:
                password = Path(env_file).expanduser().read_text().strip()
            except OSError:
                pass
        if not password and org:
            try:
                from tools.graph import settings_ops
                from tools.graph.schemas.vault_credential import VAULT_AUDITED_SET_ID
                row = settings_ops.read_set_key(
                    VAULT_AUDITED_SET_ID, f"{org}:{vault_key}", org=org, peers=[])
            except Exception:
                row = None
            if row is not None and row.get("vault_error") is None:
                password = ((row.get("payload") or {}).get("value", "") or "").strip()
            elif row is not None:
                vault_locked = True
        missing = [n for n, v in (("imap_host", imap_host), ("username", username),
                                  ("password", password)) if not v]
        if missing:
            if missing == ["password"] and vault_locked:
                hint = (f"the mailbox password is sealed at {org}:{vault_key} but the "
                        "vault is locked; unlock it once to release it")
            elif "password" in missing and org:
                hint = (f"seal it: graph vault seal {vault_key} --org {org} --tier "
                        "audited --from-file <path>")
            else:
                hint = (f"set imap_host and username on the mailbox install Setting"
                        f"{' in ' + repr(org) if org else ''}")
            raise MailboxError(f"mailbox is not configured: missing {', '.join(missing)} ({hint})")
        return cls(
            imap_host=imap_host,
            imap_port=int(value("MAILBOX_IMAP_PORT", "imap_port", "993")),
            username=username,
            password=password,
            folder=value("MAILBOX_FOLDER", "folder", "INBOX"),
            smtp_host=value("MAILBOX_SMTP_HOST", "smtp_host", imap_host),
            smtp_port=int(value("MAILBOX_SMTP_PORT", "smtp_port", "587")),
            from_addr=value("MAILBOX_FROM", "from_addr", username),
        )


# ── IMAP, read-only ─────────────────────────────────────────────────────────

class _Session:
    """One logged-in IMAP connection with the folder opened read-only."""

    def __init__(self, cfg: MailboxConfig):
        self.cfg = cfg
        self.m = None

    def __enter__(self):
        try:
            self.m = _IMAP(self.cfg.imap_host, self.cfg.imap_port,
                           ssl_context=ssl.create_default_context(), timeout=20)
            self.m.login(self.cfg.username, self.cfg.password)
            typ, data = self.m.select(_quote(self.cfg.folder), readonly=True)
        except (imaplib.IMAP4.error, OSError) as e:
            self._close()
            raise MailboxError(self.cfg.scrub(f"IMAP {self.cfg.imap_host}: {e}")) from None
        if typ != "OK":
            self._close()
            raise MailboxError(f"cannot open folder {self.cfg.folder!r}: {_text(data)}")
        self.count = int(_text(data) or 0)
        return self

    def __exit__(self, *exc):
        self._close()

    def _close(self):
        if self.m is not None:
            try:
                self.m.logout()
            except Exception:
                pass
            self.m = None

    def uid(self, command: str, *args):
        try:
            typ, data = self.m.uid(command, *args)
        except (imaplib.IMAP4.error, OSError) as e:
            raise MailboxError(self.cfg.scrub(f"IMAP {command}: {e}")) from None
        if typ != "OK":
            raise MailboxError(f"IMAP {command} refused: {_text(data)}")
        return data


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _text(data) -> str:
    if isinstance(data, (list, tuple)):
        data = data[0] if data else b""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return str(data or "")


def _clean(value: str | None, name: str) -> str | None:
    if value is None or value == "":
        return None
    if any(c in value for c in '\r\n"\\') or len(value) > 200:
        raise MailboxError(f"{name} may not contain quotes, backslashes or line breaks")
    return value


def _criteria(*, after_uid: int = 0, to=None, sender=None, subject=None, text=None) -> list[str]:
    crit: list[str] = []
    if after_uid:
        crit += ["UID", f"{int(after_uid) + 1}:*"]
    for key, val in (("TO", to), ("FROM", sender), ("SUBJECT", subject), ("TEXT", text)):
        val = _clean(val, key.lower())
        if val:
            crit += [key, f'"{val}"']
    return crit or ["ALL"]


def _header_summary(uid: int, raw: bytes, internaldate: str, size: int) -> dict:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    return {
        "uid": uid,
        "from": str(msg.get("From", "")),
        "to": str(msg.get("To", "")),
        "subject": str(msg.get("Subject", "")),
        "date": str(msg.get("Date", "")),
        "received": internaldate,
        "size": size,
    }


_FETCH_META = re.compile(rb"UID (\d+).*?RFC822\.SIZE (\d+).*?INTERNALDATE \"([^\"]+)\"", re.S)
_FETCH_META_ALT = re.compile(rb"UID (\d+).*?INTERNALDATE \"([^\"]+)\".*?RFC822\.SIZE (\d+)", re.S)


def _parse_fetch(data) -> list[tuple[int, int, str, bytes]]:
    out = []
    for item in data:
        if not isinstance(item, tuple):
            continue
        meta, body = item[0], item[1]
        m = _FETCH_META.search(meta)
        if m:
            uid, size, idate = int(m.group(1)), int(m.group(2)), m.group(3).decode()
        else:
            m = _FETCH_META_ALT.search(meta)
            if not m:
                continue
            uid, idate, size = int(m.group(1)), m.group(2).decode(), int(m.group(3))
        out.append((uid, size, idate, body))
    return out


def _received(idate: str) -> datetime | None:
    try:
        return datetime.strptime(idate, "%d-%b-%Y %H:%M:%S %z")
    except ValueError:
        return None


def list_messages(cfg: MailboxConfig, *, limit: int = 20, after_uid: int = 0, to=None,
                  sender=None, subject=None, text=None, newer_than: int = 0) -> dict:
    """Newest-first headers of messages matching every given filter."""
    limit = max(1, min(int(limit or 20), MAX_LIST))
    with _Session(cfg) as s:
        found = s.uid("SEARCH", None, *_criteria(after_uid=after_uid, to=to, sender=sender,
                                                 subject=subject, text=text))
        # "UID n:*" still matches the newest message when every UID is below n
        # (RFC 3501: * is the highest UID in use), so filter here as well.
        uids = sorted((u for u in (int(x) for x in _text(found).split()) if u > after_uid),
                      reverse=True)
        messages = []
        if uids:
            want = ",".join(str(u) for u in uids[: limit * 3 if newer_than else limit])
            data = s.uid("FETCH", want, "(UID RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER.FIELDS "
                                        "(FROM TO SUBJECT DATE MESSAGE-ID)])")
            cutoff = time.time() - newer_than if newer_than else None
            for uid, size, idate, raw in sorted(_parse_fetch(data), reverse=True):
                if cutoff is not None:
                    when = _received(idate)
                    if when is None or when.timestamp() < cutoff:
                        continue
                messages.append(_header_summary(uid, raw, idate, size))
                if len(messages) >= limit:
                    break
        return {"folder": cfg.folder, "total_in_folder": s.count, "matched": len(uids),
                "messages": messages}


class _HtmlText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.links: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href.startswith(("http://", "https://")):
                self.links.append(href)
        if tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


_URL = re.compile(r"https?://[^\s<>\"')\]]+")
_CODE = re.compile(r"(?<![\w-])(\d{4,8})(?![\w-])")


def read_message(cfg: MailboxConfig, uid: int) -> dict:
    """One message: headers, plain text, links, likely one-time codes, attachment names."""
    uid = int(uid)
    with _Session(cfg) as s:
        data = s.uid("FETCH", str(uid), "(UID RFC822.SIZE INTERNALDATE BODY.PEEK[])")
    parsed = _parse_fetch(data)
    if not parsed:
        raise MailboxError(f"no message with UID {uid} in {cfg.folder}")
    _uid, size, idate, raw = parsed[0]
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    text, html_links, attachments = "", [], []
    plain = msg.get_body(preferencelist=("plain",))
    html = msg.get_body(preferencelist=("html",))
    if plain is not None:
        text = plain.get_content()
    if html is not None:
        h = _HtmlText()
        h.feed(html.get_content())
        html_links = h.links
        if not text:
            text = re.sub(r"\n\s*\n+", "\n\n", "".join(h.parts)).strip()
    for part in msg.iter_attachments():
        payload = part.get_payload(decode=True) or b""
        attachments.append({"filename": part.get_filename() or "", "content_type":
                            part.get_content_type(), "size": len(payload)})
    links = list(dict.fromkeys(_URL.findall(text) + html_links))
    subject = str(msg.get("Subject", ""))
    codes = list(dict.fromkeys(_CODE.findall(subject + "\n" + text)))
    return {
        **_header_summary(uid, raw, idate, size),
        "cc": str(msg.get("Cc", "")),
        "text": text[:MAX_TEXT],
        "truncated": len(text) > MAX_TEXT,
        "links": links[:50],
        "codes": codes[:10],
        "attachments": attachments,
    }


def wait_for(cfg: MailboxConfig, *, timeout: int = 50, poll: float = 3.0, **filters) -> dict:
    """Wait up to *timeout* seconds for a message matching *filters*."""
    deadline = time.monotonic() + max(1, min(int(timeout), 55))
    while True:
        found = list_messages(cfg, limit=5, **filters)
        if found["messages"]:
            return {"found": True, **found}
        if time.monotonic() >= deadline:
            return {"found": False, **found}
        time.sleep(poll)


def probe(cfg: MailboxConfig) -> dict:
    with _Session(cfg) as s:
        return {"ok": True, "mailbox": cfg.username, "folder": cfg.folder,
                "messages": s.count, "imap": f"{cfg.imap_host}:{cfg.imap_port}"}


# ── SMTP, only after an operator approves ───────────────────────────────────

def validate_send(to: str, subject: str, body: str, cc: str = "") -> dict:
    """Check an outgoing message before it is staged for approval."""
    for name, val in (("subject", subject), ("to", to), ("cc", cc)):
        if "\r" in (val or "") or "\n" in (val or ""):
            raise MailboxError(f"{name} may not contain line breaks")
    rcpt = [a for _n, a in getaddresses([to or ""]) if a]
    ccs = [a for _n, a in getaddresses([cc or ""]) if a]
    if not rcpt:
        raise MailboxError("at least one recipient (to) is required")
    if len(rcpt) + len(ccs) > 10:
        raise MailboxError("at most 10 recipients")
    if not (subject or "").strip():
        raise MailboxError("subject is required")
    if len(body or "") > 200_000:
        raise MailboxError("body is over 200 KB")
    return {"to": ", ".join(rcpt), "cc": ", ".join(ccs), "subject": subject.strip(),
            "body": body or ""}


def send_message(cfg: MailboxConfig, *, to: str, subject: str, body: str, cc: str = "") -> dict:
    fields = validate_send(to, subject, body, cc)
    msg = EmailMessage()
    msg["From"] = cfg.from_addr
    msg["To"] = fields["to"]
    if fields["cc"]:
        msg["Cc"] = fields["cc"]
    msg["Subject"] = fields["subject"]
    msg["Date"] = email.utils.format_datetime(datetime.now(timezone.utc))
    msg["Message-ID"] = make_msgid(domain=cfg.from_addr.split("@")[-1] or None)
    msg.set_content(fields["body"])
    try:
        with _SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(cfg.username, cfg.password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        raise MailboxError(cfg.scrub(f"SMTP {cfg.smtp_host}: {e}")) from None
    return {"message_id": msg["Message-ID"], "from": cfg.from_addr, "to": fields["to"],
            "cc": fields["cc"], "subject": fields["subject"]}
